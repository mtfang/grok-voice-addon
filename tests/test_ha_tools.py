"""Read-only checks of bridge/ha_tools.py against a live HA (no service calls except none: action tools are
only exercised for argument validation / confirmation). usage: HA_URL=... HA_TOKEN_FILE=... python tests/test_ha_tools.py"""
import asyncio, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "grok_voice_live", "bridge"))
import ha_tools

fails = 0
def check(name, cond, out=""):
    global fails
    print(("OK   " if cond else "FAIL ") + name); fails += not cond
    if out: print("     " + out.replace("\n", "\n     ")[:900])

async def main():
    ha = ha_tools.HAClient(os.environ["HA_URL"], open(os.path.expanduser(os.environ["HA_TOKEN_FILE"])).read().strip())
    calls = []
    async def no_call(*a, **k):
        calls.append(a); raise AssertionError("service call attempted in a read-only test")
    ha.call_service = no_call
    idx = await ha.index()
    check(f"index: {len(idx)} entities, {sum(bool(e['area']) for e in idx)} with area", len(idx) > 100)
    for q, kw in [("outdoor temperature", {}), ("carport", {}), ("porch", {}), ("backyard", {}), ("motion", {"area": "outdoor"}),
                  ("temperature", {"domain": "sensor", "area": "None"})]:
        out = await ha.find_entities(q, **kw)
        check(f"find_entities({q!r}, {kw})", not out.startswith("No entities") or q in ("carport",), out)
        ids = [l.split(" | ")[0] for l in out.splitlines()[1:]]
        check("  ids are real", all(any(e["entity_id"] == i for e in idx) for i in ids))
    out = await ha.list_automations()
    check("list_automations", out.startswith("Automations (") and "Scripts (" in out, out[:600])
    auto = next((l.split(" | ")[0] for l in out.splitlines() if l.startswith("automation.")), None)
    if auto:
        check("get_entity automation has last triggered", "last triggered" in (o := await ha.get_entity(auto)), o)
    sensor = next((e["entity_id"] for e in idx if e["unit"] in ("°F", "°C") and e["state"] not in ("unknown", "unavailable")), None)
    check("get_history numeric", "min" in (o := await ha.get_history(sensor, 24)), o)
    bs = next((e["entity_id"] for e in idx if e["domain"] == "binary_sensor" and e["device_class"] in ("door", "motion", "occupancy")), None)
    check("get_history binary", "change" in (o := await ha.get_history(bs, 24)), o)
    check("get_entity unknown refused", (await ha.get_entity("sensor.made_up_thing")).startswith("Unknown entity_id"))
    check("get_entity bad syntax refused", (await ha.get_entity("'; DROP")).startswith("Unknown entity_id"))
    check("run_automation fake id refused", (await ha.run_automation("automation.does_not_exist", True)).startswith("Refused"))
    check("run_automation wrong domain refused", (await ha.run_automation(sensor, True)).startswith("Refused"))
    if auto:
        check("run_automation unconfirmed -> asks confirmation", (await ha.run_automation(auto, False)).startswith("Not done yet"))
        check("toggle_automation unconfirmed -> asks", (await ha.toggle_automation(auto, False)).startswith("Not done yet"))
    scr = next((e["entity_id"] for e in idx if e["domain"] == "script"), None)
    if scr:
        check("run_script unconfirmed -> asks", (await ha.run_script(scr)).startswith("Not done yet"))
    check("run_script fake refused", (await ha.run_script("script.nope", True)).startswith("Refused"))
    check("no service call was made", not calls)
    a, d = ha_tools.clean_args({"name": "lamp", "area": "None", "floor": None, "domain": ["light"], "x": ""})
    check("clean_args strips None/'None'/''", a == {"name": "lamp", "domain": ["light"]} and set(d) == {"area", "floor", "x"})
    await ha.aclose()

asyncio.run(main())
print("FAILS", fails); sys.exit(1 if fails else 0)
