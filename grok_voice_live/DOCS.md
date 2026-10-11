# Grok Voice Live

Bridges Satellite1 devices (firmware with the `grok_stream` component) to the xAI realtime voice API,
with Home Assistant device control via the MCP Server integration (`/api/mcp`).

Options:
- `xai_api_key`: xAI API key.
- `ha_token`: long-lived token of the non-admin `grok-voice` HA user (MCP tools see only Assist-exposed entities).
- `ha_url`: default `http://homeassistant:8123` (Core on the internal Supervisor network).
- `client_token`: shared secret; must match `grok_client_token` in the device YAML. Strongly recommended.
- `allowed_clients`: optional list of device IPs allowed to connect (e.g. `192.168.200.157`).
- `idle_timeout_s` / `max_session_s`: hang up after this much silence / this long in total (billing control).
- `local_vad_barge_in`: Silero VAD on the bridge for fast barge-in (~0.3 s instead of ~0.85 s).

Port 10700/tcp is exposed on the host. The device opens a connection only after its local wake word.

## Output audio (EQ and sample rate)

- `eq_enabled` (default `true`): applies FutureProofHomes' recommended Satellite1 parametric EQ
  (`bridge/eq/satellite1_eq_preset.txt`, Equalizer APO format: preamp +5 dB, HP 65.8 Hz, PK 850 Hz -4.6 dB Q0.7,
  2 kHz -4.9 dB Q1, 2.8 kHz -5 dB Q3, 5 kHz +4 dB Q1, 8 kHz -2 dB Q1) as RBJ biquads at the output rate, with
  per-session filter state, then a peak limiter (-1 dBFS ceiling, 80 ms release) and a hard clip guard.
- `output_rate` (default `48000`): the Satellite1 speaker pipeline (mixer, I2S, TAS2780) runs at 48 kHz, so the
  add-on resamples xAI's 24 kHz audio to 48 kHz with soxr (HIGH quality) and the device plays it without resampling.
- On start the add-on logs read-only checks: xAI key valid, and HA MCP reachable (tool list).
