# Grok Voice Live

Bridges Satellite1 devices (firmware with the `grok_stream` component) to the xAI realtime voice API,
with Home Assistant device control via the MCP Server integration (`/api/mcp`).

Options:
- `xai_api_key`: xAI API key.
- `ha_token`: long-lived token of the non-admin `grok-voice` HA user (MCP tools see only Assist-exposed entities).
- `ha_url`: default `http://homeassistant:8123` (Core on the internal Supervisor network).
- `client_token`: shared secret; must match `grok_client_token` in the device YAML. Strongly recommended.
- `allowed_clients`: optional list of device IPs allowed to connect (e.g. `<satellite-ip>`).
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

## Turn detection (vad_gate)

- `vad_gate` (default `true`): the Satellite1's XMOS ASR channel is auto-gained, so between utterances the room
  noise floor is around -25 dBFS RMS, and xAI's server VAD may never see the end of a turn (speech_started and
  transcripts, but no speech_stopped and no reply). With the gate on, the add-on runs Silero VAD locally and sends
  xAI digital silence whenever Silero hears no speech, replaying a 600 ms pre-roll when speech starts.

## Diagnostics

- `log_level: debug`: per-second inbound (device mic) and outbound (to device) audio stats (bytes/s, frames/s,
  RMS and peak dBFS), xAI server-VAD and transcription events, and realtime metrics. At `info` the add-on still logs
  a per-session summary of inbound/outbound audio, the device's audio format and xAI transcripts.
- `debug_dump_s` (0-60, default 0): write the first N seconds of raw inbound audio of each session to
  `/share/grok_voice_live/inbound_<time>.wav` (reachable via the Samba / File editor add-ons).
- `input_gain_db` (default 0): software gain on device mic audio before it is sent to xAI.
