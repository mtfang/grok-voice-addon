# Firmware: satellite1-live.yaml

1. In ESPHome Device Builder, open the existing satellite1-d98894 config. Copy its `api: encryption: key:` and
   `friendly_name`, then replace the file with `satellite1-live.yaml`. Put the key in place of
   `__API_ENCRYPTION_KEY__`, set `grok_bridge_host` to the HA host's LAN IP (`__HA_LAN_IP__`) and
   `grok_client_token` to the add-on's `client_token`.
2. `external_components` pulls `grok_stream` from `github://mtfang/grok-voice-addon@main` (the component lives in
   `components/grok_stream` at the repo root, where ESPHome looks by default).
3. If the FPH S3 assets return 403 during compile (they do from our dev box), add these substitution overrides
   and swap the wake-word model URLs. The FPH build uses the same hey_jarvis v2 / okay_nabu models as ESPHome:
     hey_jarvis: https://github.com/esphome/micro-wake-word-models/raw/main/models/v2/hey_jarvis.json
     okay_nabu:  https://github.com/esphome/micro-wake-word-models/raw/main/models/v2/okay_nabu.json
     stop:       https://github.com/kahrendt/microWakeWord/releases/download/stop/stop.json
   Sounds: `*_sound_file` substitutions (see common/media_player.yaml) can point at any FLAC/MP3 URL.
4. The YAML already carries `esp32: framework: components: [espressif/cjson ^1.7.18]` and `compile_process_limit: 1`
   (needed for FPH v0.2.1 on ESPHome 2026.9.1).
5. Install. Use OTA ("Wirelessly"). The XMOS image is embedded and only reflashed if the version differs (v1.0.3, unchanged).

New HA entity: `switch.<device>_grok_live_voice` (ON: "Hey Grok" starts a live session; OFF: "Hey Grok" uses the stock Assist pipeline).
Only "Hey Grok" is active: hey_jarvis/okay_nabu are compiled but disabled at boot and ignored by the wake-word handler.
All existing entities, timers, media player and the Assist pipeline are kept.

Audio: the add-on sends Grok's reply as 48 kHz PCM16 mono (resampled from xAI's 24 kHz with soxr HIGH in the add-on
and run through FutureProofHomes' Satellite1 speaker EQ + a -1 dBFS limiter), so the on-device
`grok_resampling_speaker` is a pass-through into the 48 kHz mixer/I2S pipeline.
