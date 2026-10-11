"""Unit test for the bridge output EQ (grok_voice_live/bridge/output_eq.py).
Usage: python tests/test_eq.py [saved_output.wav]   (writes <wav>.eq48k.wav next to it)"""
import os, sys, wave
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "grok_voice_live", "bridge"))
from output_eq import OutputEQ, parse_apo
from livekit import rtc

PRESET = open(os.path.join(os.path.dirname(__file__), "..", "grok_voice_live", "bridge", "eq",
                           "satellite1_eq_preset.txt")).read()
FS = 48000


def tone_gain_db(f, amp=0.05, fs=FS):
    eq = OutputEQ(PRESET, fs)
    t = np.arange(int(fs * 0.5)) / fs
    x = (amp * np.sin(2 * np.pi * f * t) * 32767).astype("<i2")
    y = np.frombuffer(eq.process(x.tobytes()), "<i2").astype(float)
    xs, ys = x[fs // 4:].astype(float), y[fs // 4:]          # skip filter settling
    return 20 * np.log10(np.sqrt((ys ** 2).mean()) / np.sqrt((xs ** 2).mean()))


def main():
    pre, flt = parse_apo(PRESET)
    assert pre == 5.0 and len(flt) == 6, (pre, flt)
    assert ("HP", 65.8, 0.0, 1.0) in flt and ("PK", 2800.0, -5.0, 3.0) in flt
    eq = OutputEQ(PRESET, FS)
    # 1) measured tone gains match the designed RBJ response (low level, so the limiter stays out)
    worst = 0.0
    for f in (40, 65.8, 100, 300, 850, 1000, 2000, 2800, 4000, 5000, 8000, 12000):
        meas, des = tone_gain_db(f), float(eq.response_db([f])[0])
        worst = max(worst, abs(meas - des))
        print(f"  {f:7.1f} Hz  designed {des:+6.2f} dB  measured {meas:+6.2f} dB")
    assert worst < 0.3, worst
    # 2) chunked processing == one-shot (state carried across chunks)
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(FS) * 2000).astype("<i2").tobytes()
    a = OutputEQ(PRESET, FS).process(x)
    e2 = OutputEQ(PRESET, FS); b = b"".join(e2.process(x[i:i + 3840]) for i in range(0, len(x), 3840))
    assert np.max(np.abs(np.frombuffer(a, "<i2").astype(int) - np.frombuffer(b, "<i2").astype(int))) <= 1
    # 3) limiter: full-scale 5 kHz (+9 dB after preamp+PK) never exceeds the ceiling, no wraparound
    t = np.arange(FS) / FS
    loud = (0.99 * np.sin(2 * np.pi * 5000 * t) * 32767).astype("<i2").tobytes()
    el = OutputEQ(PRESET, FS); y = np.frombuffer(el.process(loud), "<i2").astype(int)
    print(f"  full-scale 5 kHz: out peak {20*np.log10(np.abs(y).max()/32767):+.2f} dBFS, "
          f"limited blocks {el.limited_blocks}, hard-clipped samples {el.clipped_samples}")
    assert np.abs(y).max() <= 32767 * 10 ** (-0.9 / 20) + 2 and el.clipped_samples < 50
    # 4) real Grok output: resample 24k->48k (soxr HIGH, as the bridge does) + EQ, in 40 ms chunks
    wav = sys.argv[1] if len(sys.argv) > 1 else None
    if wav and os.path.exists(wav):
        with wave.open(wav) as w:
            sr, raw = w.getframerate(), w.readframes(w.getnframes())
        rs = rtc.AudioResampler(sr, FS, num_channels=1, quality=rtc.AudioResamplerQuality.HIGH)
        er = OutputEQ(PRESET, FS); out = bytearray(); step = sr * 2 * 20 // 1000
        for i in range(0, len(raw), step):
            c = raw[i:i + step]; c += b"\0" * (len(c) % 2)
            for f in rs.push(rtc.AudioFrame(c, sr, 1, len(c) // 2)):
                out += er.process(bytes(f.data))
        for f in rs.flush():
            out += er.process(bytes(f.data))
        xi = np.frombuffer(raw, "<i2").astype(float); yo = np.frombuffer(bytes(out), "<i2").astype(float)
        dur_in, dur_out = len(xi) / sr, len(yo) / FS
        rms = lambda v: 20 * np.log10(np.sqrt((v ** 2).mean()) / 32768 + 1e-12)
        print(f"  {os.path.basename(wav)}: {dur_in:.2f}s @ {sr} -> {dur_out:.2f}s @ {FS}; peak in "
              f"{20*np.log10(np.abs(xi).max()/32768):+.2f} dBFS / out {20*np.log10(np.abs(yo).max()/32768):+.2f} dBFS; "
              f"RMS in {rms(xi):+.1f} / out {rms(yo):+.1f} dBFS; limiter {er.limited_blocks} ms, clipped {er.clipped_samples}")
        assert abs(dur_in - dur_out) < 0.05 and np.isfinite(yo).all()
        assert np.abs(yo).max() <= 32767 * 10 ** (-0.9 / 20) + 2
        dst = os.path.splitext(wav)[0] + ".eq48k.wav"
        with wave.open(dst, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(FS); w.writeframes(bytes(out))
        print("  wrote", dst)
    print("EQ tests passed")


if __name__ == "__main__":
    main()
