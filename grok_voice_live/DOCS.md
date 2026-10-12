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

## Conversation length and dismissal

The live session is a continuing conversation: after a reply you can keep talking without the wake word.
It ends when you dismiss Grok ("thanks", "that's all", "goodbye", "stop listening", "never mind": the model
calls its `hang_up` tool and says a short goodbye), when you say the wake word again or press the action
button, or after `idle_timeout_s` (default 75 s) with nobody talking. The idle timer never runs while a tool
call or its spoken result is pending. `max_session_s` (default 600 s) caps every session.

Every tool call ends with a spoken result: if the turn that issued the call was interrupted (LiveKit then
drops the tool reply), the add-on asks Grok to report the result.

## Echo cancellation and barge-in

`echo_cancel` (default `true`): the add-on knows exactly what it sent to the speaker. It keeps that output as an
echo reference (decimated to 16 kHz, on a timeline of estimated play times), tracks the echo delay with GCC-PHAT
(the delay shifts by tens of ms between replies) and removes Grok's voice from the mic with a frequency-domain
NLMS filter before anything else (Silero, the gate, xAI) hears it. The learned echo path is kept per device.

The Satellite1's XMOS AEC leaves residual echo of Grok's own voice in the mic signal, and its AGC brings it up
to about the level of the user's voice. While Grok's audio plays (and `echo_tail_ms`, default 350 ms, after),
the gate toward xAI stays shut, so xAI never hears the echo, and the add-on interrupts Grok only when:

- Silero VAD probability is at least `barge_threshold` (default 0.8) for `barge_min_speech_ms` (288 ms), and
- at least `barge_frac` (40%) of those frames are `barge_margin_db` (4 dB) above the predicted residual echo
  plus noise floor (3 dB stricter in the first 0.6 s of a reply, while the echo path settles).

The echo prediction is the add-on's own output level, delayed by a fitted echo lag, plus a fitted echo
coupling. Both are learned from the session audio (cross-correlation of the level envelopes while Grok talks)
and remembered per device. Each session close logs the lag, coupling and how close rejected echo came.

## Diagnostics

- `log_level: debug`: per-second inbound (device mic) and outbound (to device) audio stats (bytes/s, frames/s,
  RMS and peak dBFS), xAI server-VAD and transcription events, and realtime metrics. At `info` the add-on still logs
  a per-session summary of inbound/outbound audio, the device's audio format and xAI transcripts.
- `debug_dump_s` (0-60, default 0): write the first N seconds of raw inbound audio of each session to
  `/share/grok_voice_live/inbound_<time>.wav` (reachable via the Samba / File editor add-ons).
- `input_gain_db` (default 0): software gain on device mic audio before it is sent to xAI.

## Tools: home vs. everything else (0.4.1)

Home Assistant MCP tools are only used for the home (devices, states, timers, lists, broadcasts). Everything
else uses xAI's own server-side tools: `web_search` and `x_search` (both on by default). HA tools whose names
match `hidden_tool_pattern` (default: search/google/wiki/web/news/weather/`llm__`) are hidden from the live
agent, e.g. `script.assist_search_google` and `llm__GetDateTime`; the local date and time (`timezone`, default
America/Los_Angeles) is written into the instructions at session start instead.

## Barge-in context (0.4.1)

When a barge-in fires, the bridge forwards the echo-cancelled mic audio from the local VAD onset minus 300 ms
(not a fixed pre-roll), so the start of the interruption is not cut off. While a confirmed barge-in overlaps
Grok's audio, the echo canceller stops adapting (it would otherwise learn the user's voice and distort it).
The interrupted reply is truncated server-side to what was actually played (`conversation.item.truncate`),
and the same realtime session is kept across barge-ins and tool calls, so earlier turns stay in context.

## Several satellites

One bridge serves any number of devices at once: one session per device (keyed by the device name from its
hello; a new wake from the same device replaces its old session), separate echo-canceller state per device,
and every log line is tagged `[device-name]`.

## 0.4.2

- **Several Satellites, one "Hey Grok"**: hellos arriving within `arbitration_window_ms` (350 ms) are arbitrated;
  the device that heard the wake word loudest (`wake_rms_db` in its hello) answers, the others get
  `grok-stand-down` and go quietly back to wake listening. A wake outside the window is an independent session.
- **Idle**: `idle_timeout_s` default 10 s, counted only in true silence (never while Grok speaks, a tool call or
  its spoken result is pending, or the user talks).
- **Loud playback**: Grok plays at most `playback_volume_cap` (0.7) of full volume (the device reports its media
  player volume); EQ preamp +2 dB (`eq_preamp_db`) and limiter ceiling -3 dBFS (`eq_ceiling_db`) keep the speaker
  linear. Firmware streams XMOS ch1 (AEC+IC+NS, no AGC, fixed x6 gain) instead of the AGC channel.
- **Barge-in**: at least 50% of the barge window must be above the predicted echo while Grok plays
  (`barge_frac_floor`); stricter when the measured echo coupling is high (`barge_hv_*`). Device playback markers
  are smoothed (median of 7) instead of shifting the echo reference on every marker.
- **Logs**: startup shows the live agent's tools (HA home tools, hidden ones, xAI web/X search); a device-ended
  session logs `client_stop (<reason>)` (action_button, ha_switch_off, device_idle_timeout, stand_down, ...).

## 0.4.3

- **Barge-in retuned for XMOS ch1** (no AGC: echo coupling about -13 dB instead of +2..+3 dB on the AGC channel).
  The echo model now starts from `barge_echo_init_db` -10 dB and a -45 dBFS noise floor (`barge_noise_init_db`)
  instead of +3 dB / -30 dBFS, which predicted an echo 16 dB louder than the real one and rejected every real
  interruption until the model re-fitted. `barge_min_speech_ms` 288 -> 224, `barge_margin_db` 4 -> 3.
  Offline replay on ch1-like audio (tests/offline_barge_ch1.py, no xAI): interruptions caught 0/72 before,
  59/72 after (median ~0.35 s), no self-interruptions on Grok-only audio.
- The learned echo lag/coupling/noise per device is kept in `/data/echo_state.json` across add-on restarts.
- Session summary splits rejected echo candidates into "too short" and "not above echo".

## 0.4.4: conversation memory

- Each conversation is saved on the HA server as markdown in `memory_dir` (default
  `/share/grok_voice_live/memory`, reachable via Samba / File editor): `YYYY-MM-DD_HHMM_<device>.md` with
  front matter (date/time, device, duration), a 3-6 bullet summary (one cheap xAI text call after the session
  closed, `memory_model`, skipped below 2 user turns) and the condensed transcript. Durable facts go to `facts.md`.
- Tools for the live agent: `recall_memory(query, days=30)` (BM25 keyword search over summaries, transcripts and
  facts; top 3 with dates, at most 1500 chars) and `remember(fact)`. The last 20 facts plus a one-line pointer
  to the latest conversation are added to the instructions at session start.
- Options: `memory_enabled` (true), `memory_retention_days` (180, pruned daily), `memory_dir`, `memory_model`.
- Memory never leaves the HA server (it is not part of this repository; edit or delete the files freely).

## 0.4.5

- **Input soft limiter** (`input_limiter`, on): after `input_gain_db`, samples above -6 dBFS are compressed
  smoothly toward full scale instead of hard-clipping.
- **Debug ring** (`debug_ring_s`, 0 = off): keeps the last N s of the device input and writes a 4-channel 16 kHz
  WAV (`ring_<time>_<device>.wav` in `debug_dump_dir`) at session close: ch1 raw device input, ch2 after gain and
  limiter, ch3 echo-cancelled (what Silero and xAI get), ch4 the echo-aligned reference. While Grok plays it logs,
  once per second, Silero's max speech probability on the gained raw input and on the cleaned signal.
- **Debug downloads** (`debug_http`, off): read-only listing of the dump WAVs on the add-on's ingress page
  (HA login required; only the Supervisor ingress proxy may connect). Turn it off after debugging.

## 0.4.6

- **No more self-interruption at reply starts.** On XMOS ch1 the first ~1 s of every reply carries echo about
  10 dB above the steady state (AEC re-converging; device playback starts a few hundred ms off the estimate), which
  the 0.4.3 thresholds took for the user. During the first `barge_onset_s` (now 2.0 s) of a reply the predicted
  echo is the loudest output of the reply so far (lag ignored), and the onset margin is 12 dB (`barge_onset_extra_db`).
  Interruptions in the first 2 s of a reply need to be clearly louder than Grok; later ones are unchanged.
- `debug_ring_s` up to 180 s; the per-second "while playing" debug line shows the cleaned-signal Silero maximum.
