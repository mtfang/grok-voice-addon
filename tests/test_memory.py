"""Offline tests for bridge/memory.py (no xAI, no HA). usage: python tests/test_memory.py"""
import os, sys, tempfile, datetime, asyncio
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "grok_voice_live", "bridge"))
import memory

fails = 0
def check(name, cond):
    global fails
    print(("OK   " if cond else "FAIL ") + name); fails += not cond

d = tempfile.mkdtemp()
st = memory.MemoryStore(d, "America/Los_Angeles", retention_days=180)
now = st.now()
p1 = st.save_conversation("satellite1-d98894", now - datetime.timedelta(days=2), 95.0,
                          [("user", "My sister Anna is visiting next Friday"), ("assistant", "Nice, I'll keep that in mind."),
                           ("user", "Also set the thermostat to 70 when she arrives"), ("assistant", "Will do.")],
                          ["Sister Anna visits next Friday", "User wants thermostat at 70 when she arrives"])
p2 = st.save_conversation("satellite1-d93640", now - datetime.timedelta(days=1), 40.0,
                          [("user", "Tell me a story about a dragon"), ("assistant", "Once upon a time a dragon named Ember...")], None)
p3 = st.save_conversation("satellite1-d93640", now - datetime.timedelta(days=60), 30.0,
                          [("user", "What's a good pizza recipe"), ("assistant", "Try a Neapolitan dough.")], ["Pizza recipe"])
old = st.save_conversation("satellite1-d93640", now - datetime.timedelta(days=400), 10.0, [("user", "ancient")], None)
check("file name format", os.path.basename(p1).startswith((now - datetime.timedelta(days=2)).strftime("%Y-%m-%d_")) and p1.endswith("_satellite1-d98894.md"))
txt = open(p1).read()
check("front matter + sections", txt.startswith("---\ndate: ") and "device: satellite1-d98894" and "duration_s: 95" in txt and "## Summary" in txt and "**User:** My sister Anna" in txt)
check("no-summary placeholder", "(no summary: short conversation)" in open(p2).read())
dup = st.save_conversation("satellite1-d93640", now - datetime.timedelta(days=1), 1.0, [("user", "x")], None)
check("same-minute collision gets suffix", dup.endswith("_2.md") and os.path.exists(p2))
os.remove(dup)

r = st.search("Anna visiting")
check("search finds the Anna conversation first", r.splitlines()[0].endswith(")") is False and "Anna" in r.splitlines()[0])
r = st.search("dragon story")
check("search dragon", "dragon" in r.lower().split("\n")[0])
check("days filter excludes 60-day-old", "Pizza" not in st.search("pizza recipe", 30) and st.search("pizza recipe", 30).startswith("No match"))
check("days=90 includes it", "Pizza" in st.search("pizza recipe", 90))
check("empty query -> most recent first", "d93640" in st.search("what did we talk about").splitlines()[0])
check("no match -> recent conversations", st.search("quantum zebra").startswith("No match"))
check("last conversation -> recent", "d93640" in st.search("our last conversation").splitlines()[0])
line = st.remember("The garage code is in the blue notebook")
check("remember appends dated line", line.startswith(f"- {now:%Y-%m-%d}: The garage") and "blue notebook" in open(os.path.join(d, "facts.md")).read())
check("facts searchable", "blue notebook" in st.search("garage code"))
for i in range(30):
    st.remember(f"fact number {i}")
ctx = st.prompt_context()
check("prompt context: last 20 facts only + last conversation pointer", "fact number 29" in ctx and "fact number 9 " not in ctx and "Your last conversation was" in ctx and len(ctx) < 1200)
big = st.save_conversation("satellite1-d98894", now, 600.0, [("user", "lamp " * 400), ("assistant", "lamp " * 400)] * 5, None)
check("recall capped at 1500 chars", len(st.search("lamp")) <= 1500)
check("prune removes >180 days only", st.prune() == 1 and not os.path.exists(old) and os.path.exists(p3))

# SessionMemory.finish: <2 user turns -> no summary call, still saved
sm = memory.SessionMemory(st, {"xai_api_key": "x", "memory_model": "m"}, "satellite1-test")
sm.add("user", "hello there"); sm.add("assistant", "Hi!")
called = []
async def fake(*a, **k): called.append(1); return ["x"]
memory.summarize = fake
asyncio.run(sm.finish())
check("short session: no summary call, transcript saved", not called and any("satellite1-test" in n for _, _, n in st.conversations()))
sm2 = memory.SessionMemory(st, {"xai_api_key": "x", "memory_model": "m"}, "satellite1-two")
for t in ("one", "two"): sm2.add("user", t); sm2.add("assistant", "ok")
asyncio.run(sm2.finish())
check("2 user turns: summary requested", called == [1])
print("FAILS", fails)
sys.exit(1 if fails else 0)
