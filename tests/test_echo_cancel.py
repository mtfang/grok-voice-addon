"""Offline check of the residual echo canceller: Grok output (48 kHz, from a box run) is 'played' on a timeline,
the mic gets user speech (different voice) + delayed, attenuated echo + noise; report ERLE and user preservation."""
import sys, math, numpy as np, soundfile as sf
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0] + "/grok_voice_live/bridge")
from echo_cancel import EchoCanceller

def db(x): return 10 * math.log10(np.mean(np.asarray(x, float) ** 2) + 1e-12)

out48, sr = sf.read(sys.argv[1], dtype="float64"); out48 = out48 if out48.ndim == 1 else out48[:, 0]
user, sru = sf.read(sys.argv[2], dtype="float64"); user = user if user.ndim == 1 else user[:, 0]
user = np.interp(np.linspace(0, len(user) - 1, int(len(user) * 16000 / sru)), np.arange(len(user)), user)
echo_db, delay_ms = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0, float(sys.argv[4]) if len(sys.argv) > 4 else 120
ec = EchoCanceller(out_rate=sr)
T = 12.0; t0 = 100.0
from scipy.signal import resample_poly
ref16 = resample_poly(out48[: int(T * sr)], 1, sr // 16000)[: int(T * 16000)]
ref16 = np.pad(ref16, (0, int(T * 16000) - len(ref16)))
D = int(delay_ms * 16)
echo = np.concatenate([np.zeros(D), ref16])[: len(ref16)] * 10 ** (echo_db / 20)
u = np.zeros_like(echo); us = 6 * 16000; u[us: us + len(user)] = user[: len(u) - us]
rng = np.random.default_rng(1); noise = rng.standard_normal(len(echo)) * 10 ** (-28 / 20)
mic = u + echo + noise
# feed the reference 40 ms chunks, 300 ms ahead of their play time (like the bridge)
chunk = int(0.04 * sr); res = []
ri = 0
for k in range(len(mic) // 512):
    t = t0 + (k + 1) * 0.032
    while ri * chunk / sr < (k + 1) * 0.032 + 0.3 and (ri + 1) * chunk <= int(T * sr):
        ec.add_ref(t0 + ri * chunk / sr, (np.clip(out48[ri * chunk:(ri + 1) * chunk], -1, 1) * 32767).astype("<i2").tobytes()); ri += 1
    fr = (np.clip(mic[k * 512:(k + 1) * 512], -1, 1) * 32767).astype("<i2").tobytes()
    res.append(np.frombuffer(ec.process(t, fr), "<i2") / 32768.0)
res = np.concatenate(res); n = len(res)
seg = slice(2 * 16000, us)              # echo only, after convergence
useg = slice(us, us + len(user))
print(f"delay est {ec.delay} samples (true {D}), gcc {ec.gcc_peak:.1f}")
print(f"echo-only: mic {db(mic[seg]):.1f} dBFS -> residual {db(res[seg]):.1f} dBFS (ERLE {db(mic[seg]) - db(res[seg]):.1f} dB); noise {db(noise):.1f}")
print(f"user segment: user {db(u[useg]):.1f}, mic {db(mic[useg]):.1f}, residual {db(res[useg]):.1f}, residual-minus-user error {db(res[useg] - u[useg][:len(res[useg])]):.1f} dBFS")
sf.write("/tmp/ec_residual.wav", res, 16000)
