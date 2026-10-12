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
    "idle_timeout_s": 10,        # hang up after this long of true silence after Grok finishes (never during tool calls)
    "max_session_s": 600,
    "client_token": "",
    "allowed_clients": [],
    "out_lead_ms": 300,
    "aec_warmup_s": 0.3,
    "local_vad_barge_in": True,
    "eq_enabled": True,          # FutureProofHomes Satellite1 speaker EQ on Grok's output audio
    "eq_preset_file": "",        # default: bridge/eq/satellite1_eq_preset.txt
    "output_rate": 48000,        # Satellite1 speaker pipeline native rate
    "startup_checks": True,
    "vad_gate": True,            # send xAI silence while local Silero VAD hears no speech (noisy AGC'd mic)
    "vad_gate_preroll_ms": 600,
    "tool_reply_protect_s": 2.5, # no barge-in during the first seconds of a spoken tool result
    "barge_onset_s": 1.2,
    "barge_frac_floor": 0.5,     # min fraction of frames above echo+margin while Grok's audio plays
    "barge_strict_until_s": 0.0, # strict at the start of each reply until the echo model re-fits (cap)
    "barge_hv_from_db": -6.0,    # echo coupling where the loud-playback strictness starts ...
    "barge_hv_full_db": -3.0,    # ... and where it is fully on
    "barge_hv_frac": 0.6,        # at full strictness: fraction of frames above echo+margin
    "barge_hv_margin_db": 6.0,   # at full strictness: margin and median excess over the predicted echo
    "eq_preamp_db": 2.0,         # EQ preamp (FPH preset says +5 dB; less drive = less speaker distortion)
    "eq_ceiling_db": -3.0,       # output limiter ceiling        # stricter barge-in margin for the first part of each reply (echo path re-locking)
    "playback_volume_cap": 0.7,  # Grok plays at most this fraction of full volume (speaker stays linear; 0 = off)
    "arbitration_window_ms": 350,  # several Satellites woken by one "Hey Grok": only the loudest answers (0 = off)
    "web_search": True,          # xAI built-in web search tool in the realtime session
    "x_search": True,            # xAI built-in X (Twitter) search
    # Architecture rule: non-home capabilities come from xAI's own tools; HA MCP tools are only for the home.
    "hidden_tools": ["script__assist_search_google", "llm__GetDateTime"],   # HA MCP tools not offered to Grok
    "hidden_tool_pattern": r"search|google|wiki|web|news|weather|ask_|llm__",  # regex on HA tool names
    "memory_enabled": True,      # conversation memory: markdown files + recall_memory / remember tools
    "memory_dir": "/share/grok_voice_live/memory",
    "memory_retention_days": 180,
    "memory_model": "grok-4.20-non-reasoning",   # cheap text model for the end-of-session summary
    "timezone": "America/Los_Angeles",   # local time goes into the instructions at session start
    "echo_cancel": True,         # remove Grok's own voice from the mic using the exact output as reference
    # echo-aware barge-in while Grok's audio plays (see server.py)
    "barge_threshold": 0.8,      # Silero probability required during agent audio (normal gate uses 0.6)
    "barge_min_speech_ms": 224,  # sustained speech needed to interrupt (288 before 0.4.3, tuned on AGC'd ch0)
    "barge_margin_db": 3.0,      # a frame counts as "user" when the mic is this far above the predicted echo
    "barge_onset_extra_db": 8.0, # extra margin during the first 0.8 s of each reply
    "barge_frac": 0.4,           # share of frames in the barge window that must count as "user"
    "barge_echo_init_db": -10.0, # starting echo coupling (mic dBFS minus output dBFS); learned per device and
                                 # kept across restarts. XMOS ch1 (no AGC) measures -13..-17 dB (+2..+3 on AGC'd ch0)
    "barge_noise_init_db": -45.0, # starting mic noise floor (ch1 + 24 dB input gain: -46 dBFS); learned per device
    "echo_tail_ms": 350,         # treat this long after playback ends as still echo-prone
    "input_gain_db": 0.0,        # software gain on device mic audio before it goes to xAI
    "debug_dump_s": 0,           # >0: write the first N s of inbound (device mic) audio to a WAV
    "debug_dump_dir": "/share/grok_voice_live",
    "log_level": "info",
    "instructions": (
        "You are Grok, a friendly voice assistant in Michael's home, speaking English. "
        "This is a continuing spoken conversation: after you answer, the user may keep talking to you without "
        "saying the wake word again, so don't say goodbye or wrap up after each answer. "
        "You can chat, answer general questions, tell stories and jokes, and control the home. "
        "Keep replies short and conversational, one or two sentences, unless the user asks for more "
        "detail or for something long like a story; then give them the length they asked for. "
        "For home control, call the Home Assistant tools right away without asking for confirmation, "
        "then confirm in a few words. After every tool call, always tell the user the result out loud. "
        "Home Assistant tools are only for the home (devices, states, timers, lists, broadcasts). "
        "Use the area argument for rooms. For questions about device state, "
        "use GetLiveContext and answer only what was asked. Answer general questions from your own "
        "knowledge. You HAVE live web search and X search tools: use them for news, current events, weather, "
        "scores, prices, recent facts, or whenever asked to look something up (X search for posts and trends on X). "
        "Never say you can't search, browse or get current information; search instead. "
        "When the user is done or dismisses you (for example: thanks, that's all, that's it, goodbye, bye, "
        "stop listening, never mind), call the hang_up tool and say a very short goodbye. "
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
