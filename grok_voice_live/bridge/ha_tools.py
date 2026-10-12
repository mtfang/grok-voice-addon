"""Bridge-local Home Assistant tools for the live agent (home features only, via HA REST + WebSocket API with
the add-on's non-admin token). HA's MCP server only sees Assist-exposed entities and can't list automations,
read last_triggered or run automations/scripts; these tools cover that. Read-only, except run_automation,
run_script and toggle_automation, which only accept real automation/script entity_ids and need confirmed=true
(the agent confirms aloud first). No HA data is persisted."""
from __future__ import annotations
import asyncio, datetime, difflib, logging, math, re, time
import aiohttp

log = logging.getLogger("grok-bridge")

MAX_OUT = 1800
# words people use for groups of areas -> area/name words that should match them
SYNONYMS = {
    "outdoor": ["outdoor", "outside", "exterior", "backyard", "back yard", "front yard", "yard", "porch", "patio",
                "carport", "driveway", "garden", "deck", "balcony", "garage", "front door", "back door", "pool"],
    "temperature": ["temperature", "temp"], "humidity": ["humidity"], "door": ["door", "contact", "opening"],
    "motion": ["motion", "occupancy", "presence"], "light": ["light", "lamp"],
}
SYNONYMS["outside"] = SYNONYMS["exterior"] = SYNONYMS["outdoor"]
SYNONYMS["temp"] = SYNONYMS["temperature"]
_STOP = {"the", "a", "an", "my", "of", "in", "on", "at", "sensor", "sensors", "please", "all", "any", "is", "what"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("_", " ")).strip()


class HAClient:
    def __init__(self, base_url: str, token: str, tz: str = "America/Los_Angeles"):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token}
        self.token = token
        self._http: aiohttp.ClientSession | None = None
        self._reg: dict | None = None
        self._reg_t = 0.0
        self._lock = asyncio.Lock()
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(tz)
        except Exception:
            self.tz = None

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()

    # ---------- HA access ----------
    async def get(self, path: str, params: dict | None = None):
        async with self.http.get(self.base + path, headers=self.headers, params=params) as r:
            if r.status != 200:
                raise PermissionError(f"HTTP {r.status}") if r.status in (401, 403) else RuntimeError(f"HTTP {r.status}")
            return await r.json()

    async def call_service(self, domain: str, service: str, data: dict) -> tuple[int, str]:
        async with self.http.post(f"{self.base}/api/services/{domain}/{service}", headers=self.headers, json=data) as r:
            return r.status, (await r.text())[:200]

    async def registries(self) -> dict:
        """Area/device/entity registries over the WebSocket API (non-admin users may read them); cached 5 min."""
        async with self._lock:
            if self._reg is not None and time.monotonic() - self._reg_t < 300:
                return self._reg
            url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
            out = {}
            async with self.http.ws_connect(url, max_msg_size=0) as ws:
                await ws.receive_json()
                await ws.send_json({"type": "auth", "access_token": self.token})
                if (await ws.receive_json()).get("type") != "auth_ok":
                    raise PermissionError("websocket auth failed")
                for i, (k, t) in enumerate((("areas", "config/area_registry/list"), ("devices", "config/device_registry/list"),
                                            ("entities", "config/entity_registry/list")), 1):
                    await ws.send_json({"id": i, "type": t})
                    while True:
                        m = await ws.receive_json()
                        if m.get("id") == i:
                            break
                    out[k] = m.get("result") or [] if m.get("success") else []
            self._reg, self._reg_t = out, time.monotonic()
            return out

    # ---------- formatting ----------
    def pt(self, iso: str | None) -> str:
        if not iso:
            return "never"
        try:
            t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            t = t.astimezone(self.tz) if self.tz else t.astimezone()
            now = datetime.datetime.now(t.tzinfo)
            day = "today" if t.date() == now.date() else ("yesterday" if t.date() == (now - datetime.timedelta(days=1)).date()
                                                         else t.strftime("%a %b %-d"))
            return f"{day} {t:%-I:%M %p} {t.tzname() or ''}".strip()
        except Exception:
            return iso

    async def index(self) -> list[dict]:
        states = await self.get("/api/states")
        try:
            reg = await self.registries()
        except Exception as e:
            log.warning("HA registries unavailable (%r): areas/aliases not searchable", e)
            reg = {"areas": [], "devices": [], "entities": []}
        areas = {a["area_id"]: a for a in reg["areas"]}
        devs = {d["id"]: d for d in reg["devices"]}
        ents = {e["entity_id"]: e for e in reg["entities"]}
        out = []
        for s in states:
            eid = s["entity_id"]; a = s.get("attributes", {})
            e = ents.get(eid, {}); d = devs.get(e.get("device_id") or "", {})
            area_id = e.get("area_id") or d.get("area_id")
            area = areas.get(area_id, {})
            out.append({
                "entity_id": eid, "domain": eid.split(".", 1)[0], "name": a.get("friendly_name") or eid,
                "aliases": list(e.get("aliases") or []), "area": area.get("name") or "",
                "area_aliases": list(area.get("aliases") or []),
                "device": d.get("name_by_user") or d.get("name") or "", "state": s.get("state"),
                "unit": a.get("unit_of_measurement") or "", "device_class": a.get("device_class") or "",
                "hidden": bool(e.get("hidden_by")) or bool(e.get("disabled_by")),
            })
        return out

    # ---------- tools ----------
    async def find_entities(self, query: str, domain: str | None = None, area: str | None = None, limit: int = 15) -> str:
        idx = await self.index()
        domain = (domain or "").strip().lower().rstrip(".") or None
        if domain in ("none", "null", "any"):
            domain = None
        area = (area or "").strip() or None
        if area and area.lower() in ("none", "null", "any"):
            area = None
        words = [w for w in _norm(query).split() if w not in _STOP]
        groups = [SYNONYMS.get(w, [w]) for w in words]
        area_words = SYNONYMS.get(_norm(area), [_norm(area)]) if area else []
        res = []
        for e in idx:
            if domain and e["domain"] != domain:
                continue
            area_text = _norm(" ".join([e["area"], *e["area_aliases"]]))
            if area_words and not any(w in area_text or w in _norm(e["name"]) for w in area_words):
                continue
            hay = _norm(" ".join([e["entity_id"], e["name"], *e["aliases"], e["area"], *e["area_aliases"], e["device"],
                                  e["device_class"]]))
            score = 0.0
            for g in groups:
                best = 0.0
                for w in g:
                    if re.search(r"\b" + re.escape(w), hay):
                        best = max(best, 2.0 if re.search(r"\b" + re.escape(w) + r"\b", hay) else 1.5)
                    elif len(w) >= 4:
                        r = max((difflib.SequenceMatcher(None, w, t).ratio() for t in hay.split()), default=0)
                        if r >= 0.8:
                            best = max(best, r)
                score += best
            if words and score == 0:
                continue
            if words and score < 0.6 * 2 * len(groups) and len(groups) > 1:
                continue    # most query words must match
            score += 0.3 * difflib.SequenceMatcher(None, _norm(query), _norm(e["name"])).ratio()
            if e["hidden"]:
                score -= 1.0
            if e["state"] in ("unavailable", "unknown"):
                score -= 0.5
            res.append((score, e))
        res.sort(key=lambda x: (-x[0], x[1]["entity_id"]))
        if not res:
            return f"No entities match '{query}'" + (f" in area '{area}'" if area else "") + (f" (domain {domain})" if domain else "") + "."
        lines = [f"{len(res)} match(es); top {min(limit, len(res))} (entity_id | name | area | state):"]
        for _, e in res[:limit]:
            st = f"{e['state']} {e['unit']}".strip()
            lines.append(f"{e['entity_id']} | {e['name']} | {e['area'] or '-'} | {st}")
        return "\n".join(lines)[:MAX_OUT]

    async def _state(self, entity_id: str) -> dict | None:
        eid = (entity_id or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", eid):
            return None
        try:
            return await self.get(f"/api/states/{eid}")
        except RuntimeError:
            return None

    async def get_entity(self, entity_id: str) -> str:
        s = await self._state(entity_id)
        if s is None:
            return f"Unknown entity_id '{entity_id}'. Use find_entities to get a real id."
        a = dict(s.get("attributes", {}))
        name = a.pop("friendly_name", s["entity_id"]); unit = a.pop("unit_of_measurement", "")
        lt = a.pop("last_triggered", None) if s["entity_id"].split(".")[0] in ("automation", "script") else None
        for k in ("icon", "entity_picture", "supported_features", "supported_color_modes", "attribution", "id",
                  "effect_list", "options", "hvac_modes", "fan_modes", "preset_modes", "swing_modes", "source_list",
                  "sound_mode_list", "available_tones", "editable"):
            a.pop(k, None)
        attrs = ", ".join(f"{k}={v}" for k, v in a.items() if not isinstance(v, (list, dict)) or len(str(v)) < 80)
        out = (f"{s['entity_id']} ({name}): {s['state']} {unit}".rstrip() +
               f"; last changed {self.pt(s.get('last_changed'))}, last updated {self.pt(s.get('last_updated'))}")
        if s["entity_id"].split(".")[0] in ("automation", "script"):
            out += f"; last triggered {self.pt(lt)}"
        if attrs:
            out += f". Attributes: {attrs}"
        return out[:MAX_OUT]

    async def get_history(self, entity_id: str, hours: float = 24) -> str:
        s = await self._state(entity_id)
        if s is None:
            return f"Unknown entity_id '{entity_id}'. Use find_entities to get a real id."
        try:
            hours = float(hours or 24)
        except (TypeError, ValueError):
            hours = 24.0
        hours = min(max(hours, 0.1), 24 * 14)
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(hours=hours)
        data = await self.get(f"/api/history/period/{start.isoformat()}",
                              {"filter_entity_id": s["entity_id"], "end_time": end.isoformat(),
                               "minimal_response": "", "no_attributes": ""})
        rows = data[0] if data else []
        pts = [(r.get("last_changed") or r.get("last_updated"), r.get("state")) for r in rows]
        unit = s.get("attributes", {}).get("unit_of_measurement", "")
        name = s.get("attributes", {}).get("friendly_name", s["entity_id"])
        nums = []
        for t, v in pts:
            try:
                f = float(v)
                if math.isfinite(f):
                    nums.append((t, f))
            except (TypeError, ValueError):
                pass
        if nums and len(nums) >= 0.8 * len(pts):
            lo = min(nums, key=lambda x: x[1]); hi = max(nums, key=lambda x: x[1])
            avg = sum(v for _, v in nums) / len(nums)
            return (f"{name}, last {hours:g} h ({len(nums)} readings): now {nums[-1][1]:g} {unit}, min {lo[1]:g} "
                    f"at {self.pt(lo[0])}, max {hi[1]:g} at {self.pt(hi[0])}, average {avg:.1f} {unit}").strip()[:MAX_OUT]
        ch = []
        for t, v in pts:
            if not ch or ch[-1][1] != v:
                ch.append((t, v))
        if not ch:
            return f"{name}: no history in the last {hours:g} h."
        head = f"{name}, last {hours:g} h: {len(ch) - 1} change(s); "
        items = [f"{self.pt(t)} {v}" for t, v in ch]
        body = "; ".join(items)
        while len(head) + len(body) > MAX_OUT and len(items) > 3:
            items = items[1:]; body = "(earlier omitted); " + "; ".join(items)
        return (head + body)[:MAX_OUT]

    async def list_automations(self) -> str:
        states = await self.get("/api/states")
        lines = []
        for dom, label in (("automation", "Automations"), ("script", "Scripts")):
            xs = sorted((s for s in states if s["entity_id"].startswith(dom + ".")),
                        key=lambda s: s["attributes"].get("friendly_name", s["entity_id"]).lower())
            lines.append(f"{label} ({len(xs)}):")
            for s in xs:
                st = s["state"] if dom == "automation" else ("running" if s["state"] == "on" else "idle")
                lines.append(f"{s['entity_id']} | {s['attributes'].get('friendly_name', '')} | {st} | "
                             f"last triggered {self.pt(s['attributes'].get('last_triggered'))}")
        out = "\n".join(lines)
        return out if len(out) <= 6000 else out[:6000] + "\n(truncated; use find_entities with domain automation or script)"

    async def _action(self, entity_id: str, domain: str, service: str, confirmed: bool, verb: str) -> str:
        s = await self._state(entity_id)
        if s is None or s["entity_id"].split(".")[0] != domain:
            return (f"Refused: '{entity_id}' is not an existing {domain} entity. Only use real ids from "
                    "list_automations or find_entities.")
        name = s["attributes"].get("friendly_name", s["entity_id"])
        if not confirmed:
            return f"Not done yet: ask the user to confirm that you should {verb} '{name}', then call again with confirmed=true."
        status, text = await self.call_service(domain, service, {"entity_id": s["entity_id"]})
        log.info("HA action %s.%s %s -> HTTP %s", domain, service, s["entity_id"], status)
        if status == 200:
            return f"Done: {verb} '{name}'."
        if status in (401, 403):
            return f"Home Assistant refused ({status}): the voice assistant's user isn't allowed to {verb} '{name}'."
        return f"Home Assistant error {status} for {verb} '{name}': {text}"

    async def run_automation(self, entity_id: str, confirmed: bool = False) -> str:
        return await self._action(entity_id, "automation", "trigger", confirmed, "run the automation")

    async def run_script(self, entity_id: str, confirmed: bool = False) -> str:
        return await self._action(entity_id, "script", "turn_on", confirmed, "run the script")

    async def toggle_automation(self, entity_id: str, enabled: bool, confirmed: bool = False) -> str:
        return await self._action(entity_id, "automation", "turn_on" if enabled else "turn_off", confirmed,
                                  "enable the automation" if enabled else "disable the automation")


HA_GUIDE = (" Besides the Home Assistant control tools you have read tools over ALL of the home's entities: "
            "find_entities (fuzzy search by name, room/area or kind, e.g. 'outdoor temperature', 'carport motion'), "
            "get_entity, get_history, and list_automations (automations and scripts with last triggered times). "
            "When you're unsure of a name or entity id, call find_entities first; never invent or guess entity ids, "
            "use only ids those tools returned. Don't pass empty or 'None' arguments. run_automation, run_script and "
            "toggle_automation can affect many devices: say what you're about to run and get the user's yes before "
            "calling them with confirmed=true.")


def make_tools(ha: HAClient) -> list:
    from livekit.agents import function_tool

    async def _safe(name, coro):
        try:
            return await coro
        except PermissionError as e:
            return f"Home Assistant denied {name} for the voice assistant's user ({e})."
        except Exception as e:
            log.warning("HA tool %s failed: %r", name, e)
            return f"{name} failed: {e!r}"

    @function_tool
    async def find_entities(query: str, domain: str | None = None, area: str | None = None) -> str:
        """Fuzzy search over ALL Home Assistant entities (names, aliases, areas/rooms, device names). Returns up to
        15 real entity_ids with name, area, state and unit. Use before any other tool when unsure of an id.
        Args:
            query: what to look for, e.g. "outdoor temperature", "carport motion", "porch light", "garage door"
            domain: optional entity domain filter, e.g. sensor, binary_sensor, light, switch, automation, script, cover
            area: optional area/room filter, e.g. "Backyard", "Kitchen", or "outdoor"
        """
        return await _safe("find_entities", ha.find_entities(query, domain, area))

    @function_tool
    async def get_entity(entity_id: str) -> str:
        """Current state, key attributes, last changed/updated (and last triggered for automations and scripts)
        of one entity. entity_id must be a real id from find_entities or list_automations.
        Args:
            entity_id: e.g. sensor.backyard_temperature
        """
        return await _safe("get_entity", ha.get_entity(entity_id))

    @function_tool
    async def get_history(entity_id: str, hours: float = 24) -> str:
        """Compact history of one entity: min/max/average for numeric sensors, otherwise the state changes with
        times. entity_id must be a real id from find_entities.
        Args:
            entity_id: e.g. binary_sensor.front_door
            hours: how far back, in hours (default 24, max 336)
        """
        return await _safe("get_history", ha.get_history(entity_id, hours))

    @function_tool
    async def list_automations() -> str:
        """All automations (on/off, last triggered) and scripts (running/idle, last triggered)."""
        return await _safe("list_automations", ha.list_automations())

    @function_tool
    async def run_automation(entity_id: str, confirmed: bool = False) -> str:
        """Run (trigger) an automation now. Ask the user to confirm first, then call with confirmed=true.
        Args:
            entity_id: a real automation.* id from list_automations or find_entities
            confirmed: true only after the user said yes
        """
        return await _safe("run_automation", ha.run_automation(entity_id, confirmed))

    @function_tool
    async def run_script(entity_id: str, confirmed: bool = False) -> str:
        """Run a script now. Ask the user to confirm first, then call with confirmed=true.
        Args:
            entity_id: a real script.* id from list_automations or find_entities
            confirmed: true only after the user said yes
        """
        return await _safe("run_script", ha.run_script(entity_id, confirmed))

    @function_tool
    async def toggle_automation(entity_id: str, enabled: bool, confirmed: bool = False) -> str:
        """Enable or disable an automation. Ask the user to confirm first, then call with confirmed=true.
        Args:
            entity_id: a real automation.* id
            enabled: true to enable, false to disable
            confirmed: true only after the user said yes
        """
        return await _safe("toggle_automation", ha.toggle_automation(entity_id, enabled, confirmed))

    return [find_entities, get_entity, get_history, list_automations, run_automation, run_script, toggle_automation]


def clean_args(args: dict) -> tuple[dict, list[str]]:
    """Drop null-ish arguments the model sometimes sends (area None / 'None' / '')."""
    out, dropped = {}, []
    for k, v in (args or {}).items():
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none", "null", "undefined")) or v == []:
            dropped.append(k)
        else:
            out[k] = v
    return out, dropped


def clean_mcp_server_class(base):
    """MCP server subclass that strips null-ish arguments before calling an HA MCP tool."""
    class CleanArgsMCPServer(base):
        async def _call_tool(self, name, arguments, **kw):
            args, dropped = clean_args(arguments)
            if dropped:
                log.info("MCP %s: dropped empty argument(s) %s", name, ", ".join(dropped))
            return await super()._call_tool(name, args, **kw)
    return CleanArgsMCPServer
