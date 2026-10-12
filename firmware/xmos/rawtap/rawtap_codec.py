"""Reference encoder/decoder for the Satellite1 'rawtap' I2S packing (FPH_RAW_TAP=ON).

48 kHz stereo 32-bit I2S words, interleaved L,R. Per 16 kHz sample n, frames 3n..3n+2:
  L[3n+k] = ch0 (AEC+IC+NS+AGC), repeated 3x (stock-compatible)
  R[3n+0] = ch1[31:16] | ref[31:17]<<1 | 1   (marker)
  R[3n+1] = E[31:16]   | W[31:17]<<1
  R[3n+2] = N[31:16]   | S[31:17]<<1
"""
import numpy as np

def _hi(x): return (x.astype(np.int64) & 0xFFFF0000)
def _lo(x): return ((x.astype(np.int64) >> 16) & 0xFFFE)

def encode(ch0, ch1, ref, E, W, N, S):
    n = len(ch0)
    L = np.repeat(ch0.astype(np.int64), 3)
    R = np.empty(3 * n, np.int64)
    R[0::3] = _hi(ch1) | _lo(ref) | 1
    R[1::3] = _hi(E) | _lo(W)
    R[2::3] = _hi(N) | _lo(S)
    words = np.empty(6 * n, np.int64); words[0::2] = L; words[1::2] = R
    return (words & 0xFFFFFFFF).astype(np.uint32).view(np.int32)

def find_phase(words, min_triplets=32):
    """Return index of the first R word that starts a triplet (LSB pattern 1,0,0)."""
    R = words[1::2].view(np.uint32)
    for p in range(3):
        bits = R[p:p + 3 * min_triplets] & 1
        if len(bits) >= 3 * min_triplets and np.all(bits[0::3] == 1) and not np.any(bits[1::3]) and not np.any(bits[2::3]):
            return p
    return None

def decode(words):
    p = find_phase(words)
    if p is None: raise ValueError("no rawtap sync")
    L = words[0::2]; R = words[1::2].view(np.uint32)
    m = (len(R) - p) // 3
    R = R[p:p + 3 * m].reshape(m, 3); L = L[p:p + 3 * m].reshape(m, 3)
    hi = lambda w: (w & 0xFFFF0000).astype(np.uint32).view(np.int32) >> 16      # int16 range
    lo = lambda w: ((w & 0xFFFE).astype(np.uint16)).view(np.int16).astype(np.int32)
    return dict(ch0=L[:, 0], ch1=hi(R[:, 0]), ref=lo(R[:, 0]), E=hi(R[:, 1]), W=lo(R[:, 1]), N=hi(R[:, 2]), S=lo(R[:, 2]))

if __name__ == "__main__":
    rng = np.random.default_rng(0); n = 4000
    sig = {k: rng.integers(-2**31, 2**31 - 1, n, dtype=np.int64).astype(np.int32) for k in "ch0 ch1 ref E W N S".split()}
    w = encode(sig["ch0"], sig["ch1"], sig["ref"], sig["E"], sig["W"], sig["N"], sig["S"])
    for drop in range(0, 6):                       # start reading mid-stream at every word offset (DMA phase)
        ww = w[drop:]; ww = ww[: len(ww) // 2 * 2]
        if drop % 2: continue                      # ESP reads whole L/R frames, so offset is a multiple of 2 words
        d = decode(ww); k = len(d["ch0"]); s = (drop // 2 + 2) // 3
        assert np.array_equal(d["ch0"], sig["ch0"][s:s + k])
        assert np.array_equal(d["E"], sig["E"][s:s + k] >> 16)
        assert np.array_equal(d["W"] >> 1, sig["W"][s:s + k] >> 17)
        assert np.array_equal(d["ref"] >> 1, sig["ref"][s:s + k] >> 17)
    # stock ESPHome sat1_microphone subsampling (samples_32[i/3] = samples_32[i]) -> channel 0 still valid
    for drop in (0, 2, 4):
        st = w[drop:][::3]; ch0_stock = st[0::2]
        assert np.all(np.isin(ch0_stock, sig["ch0"]))
    print("rawtap codec self-test OK")
