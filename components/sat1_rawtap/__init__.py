"""Satellite1 'rawtap' microphone platform (fork of FutureProofHomes satellite1 microphone, v0.2.1).

Decodes the rawtap XMOS firmware's packed I2S stream (see firmware/xmos/rawtap/README.md):
outputs the usual 2-channel 16 kHz stream (ch0 = AEC+IC+NS+AGC, ch1 = AEC+IC+NS) so all existing
consumers keep working, and exposes the raw channels (ref, mic E/W/N/S) for capture.
Falls back to the stock decoding automatically when the XMOS runs stock firmware.
"""
CODEOWNERS = ["@mtfang"]
