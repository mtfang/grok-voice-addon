"""Output EQ for Satellite1: FutureProofHomes' recommended parametric EQ (Equalizer APO preset), as RBJ
biquads at the output sample rate, followed by a peak limiter and a hard clip guard (the preset has a
+5 dB preamp). One OutputEQ per session; filter and limiter state persist across chunks/replies."""
from __future__ import annotations
import math, re
import numpy as np
from scipy.signal import sosfilt, sosfilt_zi

_LINE = re.compile(r"Filter\s*\d*:\s*ON\s+(PK|HP|LP|LS|HS|LSC|HSC)\s+Fc\s+([\d.]+)\s*Hz"
                   r"(?:\s+Gain\s+([-+\d.]+)\s*dB)?(?:\s+Q\s+([\d.]+))?", re.I)


def parse_apo(text: str) -> tuple[float, list[tuple[str, float, float, float]]]:
    """Returns (preamp_db, [(type, fc, gain_db, q), ...]) from an Equalizer APO preset."""
    preamp, filters = 0.0, []
    for line in text.splitlines():
        m = re.match(r"\s*Preamp:\s*([-+\d.]+)\s*dB", line, re.I)
        if m:
            preamp = float(m.group(1)); continue
        m = _LINE.search(line)
        if m:
            filters.append((m.group(1).upper(), float(m.group(2)), float(m.group(3) or 0.0),
                            float(m.group(4) or 0.7071)))
    return preamp, filters


def rbj_biquad(kind: str, fc: float, gain_db: float, q: float, fs: float) -> np.ndarray:
    """RBJ Audio EQ Cookbook biquad, returned as a normalized SOS row [b0 b1 b2 1 a1 a2]."""
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * fc / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "PK":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]; a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind == "HP":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "LP":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind in ("LS", "LSC"):
        s = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) - (A - 1) * cw + s), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - s)]
        a = [(A + 1) + (A - 1) * cw + s, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - s]
    elif kind in ("HS", "HSC"):
        s = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) + (A - 1) * cw + s), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - s)]
        a = [(A + 1) - (A - 1) * cw + s, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - s]
    else:
        raise ValueError(f"unsupported filter type {kind}")
    return np.array([b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]])


class OutputEQ:
    """Stateful EQ + limiter for PCM16 mono. process() takes and returns int16 PCM bytes."""

    def __init__(self, preset_text: str, fs: int, ceiling_db: float = -1.0,
                 release_ms: float = 80.0, block_ms: float = 1.0, preamp_db: float | None = None) -> None:
        self.fs = fs
        self.preamp_db, self.filters = parse_apo(preset_text)
        if preamp_db is not None:          # override the preset's preamp (flat gain: same curve shape)
            self.preamp_db = float(preamp_db)
        self.ceiling_db = ceiling_db
        self.sos = np.vstack([rbj_biquad(k, f, g, q, fs) for k, f, g, q in self.filters])
        self.pre = 10 ** (self.preamp_db / 20.0)
        self.zi = sosfilt_zi(self.sos) * 0.0          # start from silence
        self.ceiling = 10 ** (ceiling_db / 20.0)
        self.block = max(1, int(fs * block_ms / 1000))
        # per-block release step so gain recovers ~fully over release_ms
        self.rel = 1.0 - math.exp(-self.block / (fs * release_ms / 1000.0))
        self.gain = 1.0
        self.limited_blocks = 0
        self.clipped_samples = 0

    def reset(self) -> None:
        self.zi[:] = 0.0; self.gain = 1.0

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        if not x.size:
            return pcm
        y, self.zi = sosfilt(self.sos, x * self.pre, zi=self.zi)
        # block peak limiter: instant attack per block, exponential release, gain ramps within a block
        n = y.size
        nb = (n + self.block - 1) // self.block
        pad = nb * self.block - n
        peaks = np.abs(np.pad(y, (0, pad))).reshape(nb, self.block).max(axis=1)
        need = np.minimum(1.0, self.ceiling / np.maximum(peaks, 1e-9))
        g = np.empty(nb)
        cur = self.gain
        for i in range(nb):
            cur = need[i] if need[i] < cur else cur + (min(1.0, need[i]) - cur) * self.rel
            g[i] = cur
        self.limited_blocks += int((g < 0.999).sum())
        self.gain = cur
        gains = np.repeat(g, self.block)[:n]
        y *= gains
        over = np.abs(y) > 0.999
        if over.any():                               # clip guard (should be rare after the limiter)
            self.clipped_samples += int(over.sum())
            np.clip(y, -0.999, 0.999, out=y)
        return (y * 32767.0).astype("<i2").tobytes()

    def response_db(self, freqs) -> np.ndarray:
        """Designed magnitude response (incl. preamp, excl. limiter) at the given frequencies."""
        from scipy.signal import sosfreqz
        _, h = sosfreqz(self.sos, worN=np.asarray(freqs, dtype=float), fs=self.fs)
        return 20 * np.log10(np.abs(h) * self.pre + 1e-12)
