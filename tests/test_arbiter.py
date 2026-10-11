"""Offline test of the bridge's wake arbitration (no network, no xAI): staggered hellos 50-250 ms apart."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "grok_voice_live", "bridge"))
os.environ.setdefault("GROK_BRIDGE_OPTIONS", "/nonexistent")
import logging; logging.disable(logging.WARNING)
from server import WakeArbiter


class C:
    def __init__(self, d): self.device = d


async def case(arrivals, window=0.35):
    """arrivals: [(device, delay_s, score)] -> {device: won}"""
    arb = WakeArbiter(); res = {}
    async def one(d, delay, sc):
        await asyncio.sleep(delay); won, _ = await arb.claim(C(d), sc, window); res[d] = won
    await asyncio.gather(*(one(*a) for a in arrivals))
    return res


async def main():
    fails = 0
    for stagger in (0.05, 0.10, 0.15, 0.20, 0.25):
        for louder_second in (False, True):
            r = await case([("living", 0.0, -30.0 if louder_second else -20.0), ("kitchen", stagger, -18.0 if louder_second else -32.0)])
            exp = {"living": not louder_second, "kitchen": louder_second}
            ok = r == exp; fails += not ok
            print(f"stagger {stagger*1000:.0f} ms, louder={'kitchen' if louder_second else 'living'}: {r} {'OK' if ok else 'FAIL'}")
        r = await case([("living", 0.0, None), ("kitchen", stagger, -10.0)])   # old firmware: no score -> first
        ok = r == {"living": True, "kitchen": False}; fails += not ok
        print(f"stagger {stagger*1000:.0f} ms, no score on one: {r} {'OK' if ok else 'FAIL'}")
    r = await case([("living", 0.0, -20.0), ("kitchen", 0.6, -10.0)])           # outside the window: both answer
    ok = r == {"living": True, "kitchen": True}; fails += not ok
    print(f"600 ms apart (separate utterances): {r} {'OK' if ok else 'FAIL'}")
    r = await case([("living", 0.0, -20.0)])
    ok = r == {"living": True}; fails += not ok
    print(f"single device: {r} {'OK' if ok else 'FAIL'}")
    print("FAILS", fails)

asyncio.run(main())
