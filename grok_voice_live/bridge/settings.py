"""Settings: add-on options (/data/options.json) or a JSON file given in GROK_BRIDGE_OPTIONS.
Secrets can be given inline (add-on options) or as *_file paths (box testing). Never logged."""
from __future__ import annotations
import json, os

DEFAULTS = {
    "port": 10700,
    "ha_url": "http://homeassistant:8123",
    "ha_token": "", "ha_token_file": "",
    "xai_api_key": "", "xai_api_key_file": "",
    "model": "grok-voice-latest",
    "voice": "sirius",
    "idle_timeout_s": 8,
    "max_session_s": 120,
    "client_token": "",
    "allowed_clients": [],
    "out_lead_ms": 300,
    "aec_warmup_s": 0.3,
    "local_vad_barge_in": True,
    "eq_enabled": True,          # FutureProofHomes Satellite1 speaker EQ on Grok's output audio
    "eq_preset_file": "",        # default: bridge/eq/satellite1_eq_preset.txt
    "output_rate": 48000,        # Satellite1 speaker pipeline native rate
    "startup_checks": True,
    "input_gain_db": 0.0,        # software gain on device mic audio before it goes to xAI
    "debug_dump_s": 0,           # >0: write the first N s of inbound (device mic) audio to a WAV
    "debug_dump_dir": "/share/grok_voice_live",
    "log_level": "info",
    "instructions": (
        "You are Grok, a friendly voice assistant in Michael's home, speaking English. "
        "You can chat, answer general questions, tell stories and jokes, and control the home. "
        "Keep replies short and conversational, one or two sentences, unless the user asks for more "
        "detail or for something long like a story; then give them the length they asked for. "
        "For home control, call the Home Assistant tools right away without asking for confirmation, "
        "then confirm in a few words. Use the area argument for rooms. For questions about device state, "
        "use GetLiveContext and answer only what was asked. Answer general questions from your own "
        "knowledge; only use the web search script when asked to look something up online. "
        "This is spoken audio: no markdown, lists or emoji."
    ),
}


def load() -> dict:
    path = os.environ.get("GROK_BRIDGE_OPTIONS", "/data/options.json")
    opts = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            opts.update({k: v for k, v in json.load(f).items() if v not in (None, "")})
    for key in ("ha_token", "xai_api_key"):
        if not opts[key] and opts.get(key + "_file"):
            with open(os.path.expanduser(opts[key + "_file"])) as f:
                opts[key] = f.read().strip()
        if not opts[key]:
            raise SystemExit(f"missing required option: {key}")
    return opts


def redacted(opts: dict) -> dict:
    hide = {"ha_token", "xai_api_key", "client_token"}
    return {k: ("***" if k in hide and v else v) for k, v in opts.items() if k != "instructions"}
