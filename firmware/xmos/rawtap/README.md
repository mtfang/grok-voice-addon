# Satellite1 "rawtap" (raw mics + AEC reference) — test build

Branch `rawtap` only. Not part of the released add-on.

## XMOS image `v1.0.3-dev.1`
FutureProofHomes Satellite1-XMOS **v1.0.3** (`satellite1_firmware_fixed_delay`) + `satellite1-xmos-rawtap.patch`,
built with XTC 15.3.0 and `-DFPH_RAW_TAP=ON`. With the option OFF the patched tree rebuilds the official v1.0.3
factory image bit-identically (md5 5f5788ecb240082f61acd36f247ea3b2).

* Mic array captures all 4 PDM mics (mapping E, W, N, S); the AEC/IC/NS/AGC pipeline is unchanged (E/W).
* I2S to the ESP32 stays 48 kHz stereo 32-bit. Per 16 kHz sample n (frames 3n..3n+2):
  * `L[3n+0..2]` = ch0 (AEC+IC+NS+AGC), repeated 3x (stock-compatible)
  * `R[3n+0]` = ch1[31:16] | ref[31:17]<<1 | 1  (marker LSB = 1; ref = (refL+refR)/2, pre-AEC)
  * `R[3n+1]` = mic E[31:16] | mic W[31:17]<<1
  * `R[3n+2]` = mic N[31:16] | mic S[31:17]<<1
* `satellite1_rawtap.factory.bin.b64` is the factory image as base64 text ("B64:" header) so it can be pushed as
  text; the forked `components/memory_flasher` decodes it at compile time. md5 is of the binary image.
* Reference codec + self-test: `rawtap_codec.py`.

## XMOS image `v1.0.3-dev.2` (current)
Same as dev.1 plus `-DFPH_RAW_TAP_CH1_GAIN=6 -DFPH_RAW_TAP_MIC_SHIFT=2`: ch1 gets a saturating x6 gain before the
16-bit truncation (identical level to the stock path's ESPHome `gain_factor: 6`, so the YAML now uses
`gain_factor: 1` for micro_wake_word and grok_stream), raw mics get a saturating <<2 (+12 dB).
md5 748f9823276cd389ffd0344712a15e75.

## ESPHome side
* `components/sat1_rawtap` — microphone platform (fork of FPH v0.2.1 `satellite1` microphone): locks onto the
  marker (32 triplets), outputs the usual 2-channel 16 kHz stream (ch0, ch1 = decoded 16-bit AEC+IC+NS) so
  voice_assistant / micro_wake_word / grok_stream are unchanged; falls back to stock decoding if no marker for ~1 s;
  ch1 is muted while re-locking after a sync loss. Sync losses are logged (`rawtap` tag) with read gap, RX DMA
  overflow count and the relock phase delta.
  Capture: `start_capture(ms, sweep, dbfs)` -> PSRAM, then base64 `RTH/RTD/RTE` log lines (tag `rawtap`).
* `components/memory_flasher` — FPH v0.2.1 memory_flasher + base64 image support.
* `firmware/satellite1-rawtap.yaml` — test config (placeholders for key/token/IP).

## Rollback
OTA the normal config (embeds stock XMOS v1.0.3): the ESP32 sees the version mismatch at boot and rewrites the
stock XMOS image over SPI.
