"""Conversation memory for the live Grok bridge: plain markdown files on the HA server, nothing else.

Layout (memory_dir, default /share/grok_voice_live/memory, editable via Samba / File editor):
  YYYY-MM-DD_HHMM_<device>.md   one file per conversation: front matter, summary bullets, condensed transcript
  facts.md                      durable facts ("- YYYY-MM-DD: fact"), appended by the `remember` tool
Tools for the realtime agent: recall_memory(query, days) (BM25 over summaries + transcripts + facts) and
remember(fact). The summary is one cheap xAI chat-completions call after the session closed (never blocks it)."""
from __future__ import annotations
import asyncio, datetime, logging, math, os, re, collections

log = logging.getLogger("grok-bridge")

FACTS = "facts.md"
_FNAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})_(.+)\.md$")
_WORD = re.compile(r"[a-z0-9']+")
_STOP = set("""a an the and or but if of to in on at for with from by is are was were be been am i you me my your we
our it its this that these those do did does what which who whom how when where why can could would should will
just so about up out as into than then there here they them he she his her him not no yes ok okay hey grok
tell said say talk talked last time remember told anything something conversation conversations chat
chatted discuss discussed earlier previous recent recently ago yesterday today week""".split())
RECALL_CAP = 1500


def _tokens(text: str) -> list[str]:
    return [w.strip("'") for w in _WORD.findall(text.lower()) if w.strip("'") and w.strip("'") not in _STOP]


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s or "device").strip("-")[:40] or "device"


class MemoryStore:
    def __init__(self, root: str, tz: str = "America/Los_Angeles", retention_days: int = 180):
        self.root, self.retention_days = root, int(retention_days)
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(tz)
        except Exception:
            self.tz = None
        os.makedirs(root, exist_ok=True)

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(self.tz) if self.tz else datetime.datetime.now().astimezone()

    # ---------- writing ----------
    def save_conversation(self, device: str, start: datetime.datetime, duration_s: float,
                          turns: list[tuple[str, str]], summary: list[str] | None, room: str | None = None) -> str:
        base = f"{start:%Y-%m-%d_%H%M}_{_slug(device)}"
        path = os.path.join(self.root, base + ".md")
        n = 2
        while os.path.exists(path):
            path = os.path.join(self.root, f"{base}_{n}.md"); n += 1
        users = sum(1 for r, _ in turns if r == "user")
        lines = ["---", f"date: {start:%Y-%m-%d}", f"time: {start:%H:%M} {start.tzname() or ''}".rstrip(),
                 f"device: {device}"]
        if room:
            lines.append(f"room: {room}")
        lines += [f"duration_s: {duration_s:.0f}", f"user_turns: {users}", "---", "", "## Summary", ""]
        lines += [f"- {b}" for b in summary] if summary else ["- (no summary: short conversation)"]
        lines += ["", "## Transcript", ""]
        for role, text in turns:
            t = " ".join(text.split())
            if t:
                lines.append(f"**{'User' if role == 'user' else 'Grok'}:** {t}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
        return path

    def remember(self, fact: str) -> str:
        fact = " ".join((fact or "").split())
        if not fact:
            return ""
        p = os.path.join(self.root, FACTS)
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write("# Facts Grok remembers\n\nEdit freely: one fact per line.\n\n")
            line = f"- {self.now():%Y-%m-%d}: {fact}"
            f.write(line + "\n")
        return line

    def prune(self) -> int:
        cut = (self.now() - datetime.timedelta(days=self.retention_days)).date()
        n = 0
        for name in os.listdir(self.root):
            m = _FNAME.match(name)
            if m and datetime.date.fromisoformat(m.group(1)) < cut:
                try:
                    os.remove(os.path.join(self.root, name)); n += 1
                except OSError:
                    pass
        return n

    # ---------- reading ----------
    def conversations(self) -> list[tuple[datetime.date, str, str]]:
        out = []
        for name in os.listdir(self.root):
            m = _FNAME.match(name)
            if m:
                out.append((datetime.date.fromisoformat(m.group(1)), m.group(2), name))
        return sorted(out, reverse=True)

    def _read(self, name: str) -> str:
        try:
            with open(os.path.join(self.root, name), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    @staticmethod
    def _summary(text: str) -> list[str]:
        m = re.search(r"## Summary\s*\n(.*?)(\n## |\Z)", text, re.S)
        return [l[2:].strip() for l in (m.group(1) if m else "").splitlines() if l.startswith("- ")]

    def facts_tail(self, n: int = 20) -> list[str]:
        return [l for l in self._read(FACTS).splitlines() if l.startswith("- ")][-n:]

    def search(self, query: str, days: int = 30, k: int = 3, cap: int = RECALL_CAP) -> str:
        """BM25 over conversation files (summary weighted x2) and facts.md lines; top k with date + snippet."""
        q = [_stem(w) for w in _tokens(query)]
        cut = self.now().date() - datetime.timedelta(days=max(1, int(days or 30)))
        docs = []   # (label, lines, tokens)
        for d, hm, name in self.conversations():
            if d < cut:
                continue
            text = self._read(name)
            body = text.split("---", 2)[-1]
            summ = self._summary(text)
            dev = _FNAME.match(name).group(3)
            label = f"{d:%a %b %-d %Y} {hm[:2]}:{hm[2:]} ({dev})"
            lines = [l for l in body.splitlines() if l.strip() and not l.startswith("#")]
            toks = [_stem(w) for w in _tokens((" ".join(summ) + " ") * 2 + body)]
            docs.append((label, summ, lines, toks))
        for l in self.facts_tail(10_000):
            docs.append(("remembered fact", [], [l], [_stem(w) for w in _tokens(l)]))
        if not docs:
            return "No saved conversations or facts yet."
        if not q:   # no keywords ("what did we talk about"): most recent conversations
            hits = [(0.0, i) for i, d in enumerate(docs) if d[0] != "remembered fact"][:k]
        else:
            N = len(docs); avg = sum(len(d[3]) for d in docs) / N or 1.0
            df = collections.Counter(w for d in docs for w in set(d[3]))
            hits = []
            for i, (_, _, _, toks) in enumerate(docs):
                tf = collections.Counter(toks); s = 0.0
                for w in set(q):
                    if tf[w]:
                        idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                        s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(toks) / avg))
                if s > 0:
                    hits.append((s, i))
            hits.sort(key=lambda h: (-h[0], h[1]))
            hits = hits[:k]
            if not hits:
                recent = [i for i, d in enumerate(docs) if d[0] != "remembered fact"][:2]
                if not recent:
                    return f"Nothing in memory matches '{query}' in the last {days} days."
                return (f"No match for '{query}'. Most recent conversations:\n"
                        + "\n".join(f"{docs[i][0]}: " + "; ".join(docs[i][1][:3]) for i in recent))[:cap]
        parts, per = [], max(200, cap // max(1, len(hits)))
        for _, i in hits:
            label, summ, lines, _ = docs[i]
            qs = set(q)
            best = sorted(lines, key=lambda l: -len(qs & {_stem(w) for w in _tokens(l)}))[:3] if q else []
            snip = "; ".join(summ[:3]) if summ and summ[0] != "(no summary: short conversation)" else ""
            extra = " | ".join(b for b in best if b.lstrip("- ") not in snip)
            txt = f"{label}: " + " | ".join(x for x in (snip, extra) if x)
            parts.append(txt[:per])
        return "\n".join(parts)[:cap]

    def prompt_context(self) -> str:
        """Small block for the session instructions: recent facts + one-line pointer to the last conversation."""
        out = []
        facts = self.facts_tail(20)
        if facts:
            out.append("Things the user asked you to remember: " + " ".join(f.lstrip("- ") for f in facts))
        convs = self.conversations()
        if convs:
            d, hm, name = convs[0]
            s = self._summary(self._read(name))
            first = s[0] if s and not s[0].startswith("(no summary") else "short conversation"
            out.append(f"Your last conversation was {d:%A %b %-d} at {hm[:2]}:{hm[2:]}: {first[:160]}")
        return " ".join(out)


MEMORY_GUIDE = (" You have a memory of past conversations. When the user refers to a past conversation "
                "(what did we talk about, last time, remember when, what did I tell you), call recall_memory with "
                "a few keywords (or an empty query for the latest conversations) and answer from the result. When the "
                "user asks you to remember something, call remember with the fact as a short sentence. Don't "
                "narrate memory lookups; just answer.")

SUMMARY_PROMPT = ("Summarize this voice conversation between a user and Grok, a home voice assistant, as 3 to 6 short "
                  "markdown bullets (each starting with '- '): topics discussed, decisions or actions taken, facts the "
                  "user stated about themselves or their home, and open follow-ups. No preamble, bullets only.")


async def summarize(api_key: str, model: str, turns: list[tuple[str, str]], timeout: float = 30.0) -> list[str] | None:
    import aiohttp
    convo = "\n".join(f"{'User' if r == 'user' else 'Grok'}: {t}" for r, t in turns)[-12000:]
    body = {"model": model, "temperature": 0.2, "max_tokens": 300,
            "messages": [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": convo}]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
        async with http.post("https://api.x.ai/v1/chat/completions", json=body,
                             headers={"Authorization": "Bearer " + api_key}) as r:
            if r.status != 200:
                log.warning("memory summary: xAI HTTP %s", r.status)
                return None
            j = await r.json()
    text = j["choices"][0]["message"]["content"] or ""
    bullets = [l.strip()[2:].strip() for l in text.splitlines() if l.strip().startswith(("- ", "* "))]
    return bullets[:6] or None


class SessionMemory:
    """Per-session recorder + tools. Created only when memory is enabled."""
    def __init__(self, store: MemoryStore, opts: dict, device: str):
        self.store, self.opts, self.device = store, opts, device
        self.start = store.now()
        self.turns: list[tuple[str, str]] = []

    def add(self, role: str, text: str) -> None:
        if role in ("user", "assistant") and text and text.strip():
            self.turns.append((role, text.strip()))

    def tools(self) -> list:
        from livekit.agents import function_tool
        store = self.store

        @function_tool
        async def recall_memory(query: str, days: int = 30) -> str:
            """Search memory of past conversations and remembered facts. Use when the user refers to an earlier
            conversation (what did we talk about, last time, remember when, what did I tell you).
            Args:
                query: a few keywords about what to recall; empty string for the most recent conversations
                days: how far back to look, in days (default 30)
            """
            res = await asyncio.to_thread(store.search, query, days)
            log.info("recall_memory(%r, %s): %d chars", query, days, len(res))
            return res

        @function_tool
        async def remember(fact: str) -> str:
            """Save a durable fact the user asked you to remember, as one short third-person sentence with
            absolute dates (e.g. "The user's favorite color is teal", "Sam visits for dinner on Saturday Oct 17").
            Args:
                fact: the fact to remember
            """
            line = await asyncio.to_thread(store.remember, fact)
            log.info("remember: saved a fact (%d chars)", len(line))
            return "Saved." if line else "Nothing to save."

        return [recall_memory, remember]

    async def finish(self) -> None:
        """Called after the session closed (as a background task): summary (if >= 2 user turns) + file."""
        try:
            dur = (self.store.now() - self.start).total_seconds()
            if not self.turns:
                return
            users = sum(1 for r, _ in self.turns if r == "user")
            summary = None
            if users >= 2:
                try:
                    summary = await summarize(self.opts["xai_api_key"], self.opts["memory_model"], self.turns)
                except Exception as e:
                    log.warning("memory summary failed: %r", e)
            path = await asyncio.to_thread(self.store.save_conversation, self.device, self.start, dur,
                                           self.turns, summary)
            log.info("memory: saved %s (%d turns, summary %s)", os.path.basename(path), len(self.turns),
                     "yes" if summary else "no")
        except Exception:
            log.exception("memory save failed")


_TASKS: set = set()


def spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _TASKS.add(t); t.add_done_callback(_TASKS.discard)


async def prune_loop(store: MemoryStore) -> None:
    while True:
        try:
            n = await asyncio.to_thread(store.prune)
            if n:
                log.info("memory: pruned %d conversation(s) older than %d days", n, store.retention_days)
        except Exception:
            log.exception("memory prune")
        await asyncio.sleep(86400)
