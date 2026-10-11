# grok-voice-addon

Live speech-to-speech Grok (xAI realtime) for FutureProofHomes Satellite1, controlling Home Assistant via the
MCP Server integration. Two parts:

- `grok_voice_live/`: Home Assistant add-on (local build, no `image:`). It's a TCP server on port 10700 that wraps a
  LiveKit Agents `AgentSession` (xAI realtime `grok-voice-latest`, voice `sirius`) plus HA MCP tools (`/api/mcp`, non-admin
  `grok-voice` user token). It hangs up after 8 s of silence or 120 s total, and does barge-in via Silero VAD.
- `firmware/`: ESPHome external component `grok_stream` (full duplex, Wyoming-style framing, derived from RealtimeClaw's
  MIT `wyoming_tcp_client`), plus `satellite1-live.yaml` for satellite1-d98894 (FPH v0.2.1).

## Protocol (one TCP connection per wake)
Each frame is a JSON header line `{"type","data_length","payload_length"}`, then the JSON data, then the binary payload.
- device to bridge: `grok-hello {token,device,version}`, `audio-start {rate:16000,width:2,channels:1}`,
  `audio-chunk` (1024 B = 32 ms PCM16 mono 16 kHz, XMOS ch0 = AEC+IC+NS+AGC), `audio-stop`
- bridge to device: `grok-state {state}`, `audio-start {rate:48000}`, `audio-chunk` (3840 B = 40 ms PCM16 mono 48 kHz,
  paced at most 300 ms ahead of playout), `audio-stop`, `grok-clear` (barge-in: flush), `transcript`, `grok-end {reason}`
- Limits: header 4 KB / data 16 KB / payload 64 KB on the bridge; 512 B / 1 KB / 8 KB on the device.

## Test on a dev box
    GROK_BRIDGE_OPTIONS=tests/local/options.box.json python grok_voice_live/bridge/server.py
    python tests/test_client.py 127.0.0.1 10700 out_x input.wav <client_token>
    BARGE_WAV=interrupt.wav BARGE_AFTER_S=1.0 python tests/test_client.py ...   # barge-in test

## Output audio (EQ and sample rate)

- `eq_enabled` (default `true`): applies FutureProofHomes' recommended Satellite1 parametric EQ
  (`bridge/eq/satellite1_eq_preset.txt`, Equalizer APO format: preamp +5 dB, HP 65.8 Hz, PK 850 Hz -4.6 dB Q0.7,
  2 kHz -4.9 dB Q1, 2.8 kHz -5 dB Q3, 5 kHz +4 dB Q1, 8 kHz -2 dB Q1) as RBJ biquads at the output rate, with
  per-session filter state, then a peak limiter (-1 dBFS ceiling, 80 ms release) and a hard clip guard.
- `output_rate` (default `48000`): the Satellite1 speaker pipeline (mixer, I2S, TAS2780) runs at 48 kHz, so the
  add-on resamples xAI's 24 kHz audio to 48 kHz with soxr (HIGH quality) and the device plays it without resampling.
- On start the add-on logs read-only checks: xAI key valid, and HA MCP reachable (tool list).
