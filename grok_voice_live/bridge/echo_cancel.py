"""Reference-based residual echo canceller for the device mic.

The bridge knows exactly what it sent to the speaker. EchoCanceller keeps that output, decimated to 16 kHz, on
a timeline of estimated play times, finds the bulk echo delay with GCC-PHAT (mic vs reference) and removes the
echo with a frequency-domain NLMS filter (overlap-save, 512 taps = 32 ms around the bulk delay). The XMOS AEC on
the Satellite1 already removes most of the echo; this takes out what it leaves (which the XMOS AGC then boosts
to about the level of a voice), so Silero and xAI hear the user, not Grok."""
from __future__ import annotations
import math
import numpy as np
from scipy.signal import firwin, lfilter

FS = 16000
N = 512                      # block = one mic frame (32 ms) = filter length
RING = FS * 30               # 30 s of reference


class EchoCanceller:
    def __init__(self, out_rate: int = 48000, mu: float = 0.3, max_delay_s: float = 0.5) -> None:
        self.ref = np.zeros(RING, np.float32)
        self.ref_hi = -1                       # highest absolute ref index written
        self.T0: float | None = None           # time of ref index 0
        self.Tm0: float | None = None          # time of mic sample 0
        self.m = 0                             # mic samples seen
        self.dec = max(1, out_rate // FS)
        self.lp = firwin(63, 7000, fs=out_rate) if self.dec > 1 else None
        self.lp_zi = np.zeros(62) if self.lp is not None else None
        self.mu, self.max_delay = mu, int(max_delay_s * FS)
        self.reg = 0.3
        self.W = np.zeros(2 * N, np.complex128)
        self.P = np.full(N + 1, 1e-6)
        self.delay: int | None = None          # bulk delay in samples (mic index - ref index)
        self.mic_hist = np.zeros(FS * 2, np.float32)
        self.frames = 0
        self.gcc_peak = 0.0
        self.erle_db = 0.0
        self.debug: list | None = None           # tests: per-frame (mic, ref window) capture
        self.seg_r0: int | None = None           # ref index where the current reply starts
        self.seg_locked = True
        self.freeze = False

    def new_session(self) -> None:
        """New connection: new timelines, but keep the learned echo path (delay + filter) for this device."""
        self.freeze = False
        self.ref[:] = 0; self.ref_hi = -1; self.T0 = None; self.Tm0 = None; self.m = 0
        self.mic_hist[:] = 0; self.frames = 0
        if self.lp_zi is not None:
            self.lp_zi[:] = 0

    # ---- reference side
    def add_ref(self, t_play: float, pcm: bytes) -> None:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        if self.lp is not None:
            x, self.lp_zi = lfilter(self.lp, 1.0, x, zi=self.lp_zi)
            x = x[:: self.dec]
        if self.T0 is None:
            self.T0 = t_play - 1.0
        i0 = int(round((t_play - self.T0) * FS))
        if i0 > self.ref_hi + 1:               # clear the gap (stale data from 30 s ago)
            self._zero(self.ref_hi + 1, i0)
        idx = np.arange(i0, i0 + len(x)) % RING
        self.ref[idx] = x
        self.ref_hi = max(self.ref_hi, i0 + len(x) - 1)

    def new_segment(self, t_play: float) -> None:
        """A new reply starts: re-lock the echo delay on its first ~200 ms (it can jump between replies)."""
        self.freeze = False
        if self.T0 is not None:
            self.seg_r0 = int(round((t_play - self.T0) * FS)); self.seg_locked = False

    def move(self, t_from: float, delta: float) -> None:
        """Shift the reference from t_from on by delta seconds (device reported the true playback start)."""
        if self.T0 is None:
            return
        a = int(round((t_from - self.T0) * FS)); d = int(round(delta * FS))
        if a > self.ref_hi or d == 0:
            return
        seg = self._ref_block(a, self.ref_hi + 1 - a).astype(np.float32)
        self._zero(a, self.ref_hi + 1)
        b = a + d
        idx = np.arange(b, b + len(seg))
        keep = idx >= 0
        self.ref[idx[keep] % RING] = seg[keep]
        if d > 0:
            pass                                   # gap [a, b) stays zero
        self.ref_hi = b + len(seg) - 1
        if getattr(self, "seg_r0", None) is not None and self.seg_r0 >= a - FS // 10:
            self.seg_r0 += d

    def truncate(self, t: float, keep_s: float = 0.03) -> None:
        """Speaker was flushed at time t: nothing after t (+ a little in flight) will be played."""
        if self.T0 is None:
            return
        i = int((t - self.T0 + keep_s) * FS)
        if i <= self.ref_hi:
            self._zero(i, self.ref_hi + 1)
            self.ref_hi = i - 1

    def _zero(self, a: int, b: int) -> None:
        if b - a >= RING:
            self.ref[:] = 0; return
        if b > a:
            self.ref[np.arange(a, b) % RING] = 0

    def _ref_block(self, start: int, n: int) -> np.ndarray:
        idx = np.arange(start, start + n)
        out = self.ref[idx % RING].astype(np.float64)
        out[(idx > self.ref_hi) | (idx < 0) | (idx < self.ref_hi - RING + 1)] = 0.0
        return out

    # ---- mic side
    def process(self, t_arrival: float, pcm: bytes) -> bytes:
        d = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        n = len(d)
        if self.Tm0 is None:
            self.Tm0 = t_arrival - n / FS
        self.mic_hist = np.roll(self.mic_hist, -n); self.mic_hist[-n:] = d
        m0 = self.m; self.m += n; self.frames += 1
        self.last_ref = None                      # debug: reference block aligned with this mic block (echo delay)
        if self.T0 is None or n != N:
            return pcm
        # mic sample m0 <-> ref index (aligned by wall clock), minus the bulk echo delay
        r_now = int(round((self.Tm0 + m0 / FS - self.T0) * FS))
        if self.debug is not None:
            self.debug.append((d.copy(), self._ref_block(r_now - self.max_delay, N + self.max_delay)[-N - 3200:]))
        # the echo delay shifts by tens of ms between replies (device buffer prefill), so track it continuously
        sr0 = getattr(self, "seg_r0", None)
        if (sr0 is not None and not self.seg_locked and self.delay is not None
                and r_now + N - self.delay - sr0 >= int(0.2 * FS)):
            # first ~200 ms of a new reply: correlate only over this reply's echo
            span = (r_now + N - self.delay - sr0) / FS + 0.05
            self._estimate_delay(r_now + N, win_s=min(0.5, span), force=True)
            self.seg_locked = True
        elif self.frames % 2 == 0:
            self._estimate_delay(r_now + N)
        if self.delay is None:
            return pcm
        r0 = r_now - self.delay + N // 4          # taps cover echo delays [delay - 8 ms, delay + 24 ms]
        x2 = self._ref_block(r0 - N, 2 * N)       # previous + current block
        self.last_ref = self._ref_block(r_now - self.delay, N)   # debug: echo-aligned reference
        if not x2.any():
            return pcm
        X = np.fft.fft(x2)
        y = np.real(np.fft.ifft(X * self.W))[N:]
        e = d - y
        E = np.fft.fft(np.concatenate([np.zeros(N), e]))
        px = np.abs(X[: N + 1]) ** 2
        self.P = 0.9 * self.P + 0.1 * px
        Pf = np.concatenate([self.P, self.P[1:N][::-1]])
        pd, pe, py = float(np.mean(d * d)), float(np.mean(e * e)), float(np.mean(y * y))
        # double-talk guard: if the residual is much bigger than the echo estimate, someone is talking: adapt slowly
        # (only once the filter has converged; before that the residual is all echo)
        mu = self.mu * (0.05 if (self.erle_db > 6 and pe > 4 * py and pe > 1e-6) else 1.0)
        if self.freeze:                           # bridge detected double-talk (user speaking over Grok)
            mu = 0.0
        G = mu * np.conj(X) * E / (Pf + 1e-8 + self.reg * float(np.mean(self.P)))
        g = np.real(np.fft.ifft(G))[:N]
        self.W += np.fft.fft(np.concatenate([g, np.zeros(N)]))
        if pe > pd * 4:                           # diverged: reset
            self.W[:] = 0; e = d
        elif pe > pd:
            # the "echo" we'd subtract isn't in the mic (e.g. reply not started yet on the device, or flushed):
            # never make the signal louder
            e = d
        if py > 1e-7:
            self.erle_db = 0.95 * self.erle_db + 0.05 * 10 * math.log10((pd + 1e-12) / (pe + 1e-12))
        if self.debug is not None:
            self.res_dbg = getattr(self, "res_dbg", []); self.res_dbg.append((self.frames, e.copy(), d.copy()))
        return (np.clip(e, -1, 1) * 32767).astype("<i2").tobytes()

    def _estimate_delay(self, r_end: int, win_s: float = 0.5, force: bool = False) -> None:
        L = int(win_s * FS)
        mic = self.mic_hist[-L:]
        ref = self._ref_block(r_end - L - self.max_delay, L + self.max_delay)
        if float(np.mean(ref[-L:] ** 2)) < 1e-6 or float(np.mean(mic ** 2)) < 1e-7:
            return
        nfft = 1 << int(math.ceil(math.log2(L + len(ref))))
        M = np.fft.rfft(mic, nfft); R = np.fft.rfft(ref, nfft)
        # partial PHAT whitening (beta 0.7): sharp peak, but silent stretches of the window don't dominate
        C = M * np.conj(R); C /= (np.abs(C) + 1e-9) ** 0.7
        cc = np.fft.irfft(C, nfft)
        # mic[n] ~ ref_block[n + max_delay - delay]  ->  peak of cc at index (delay - max_delay)
        lags = np.arange(0, self.max_delay + 1)
        vals = cc[(lags - self.max_delay) % nfft]
        k = int(np.argmax(vals)); peak = float(vals[k]); floor = float(np.median(np.abs(vals))) + 1e-9
        self.gcc_peak = peak / floor
        if self.gcc_peak < 8:
            return
        new = int(lags[k])
        self.delay_hist = (getattr(self, "delay_hist", []) + [new])[-8:]
        # re-centre the filter window on the new delay; the learned taps stay valid relative to it
        if self.delay is None or (abs(new - self.delay) > 3 and
                                  (force or sum(abs(d - new) <= 3 for d in self.delay_hist[-2:]) >= 2)):
            self.delay = new
