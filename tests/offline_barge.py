"""Offline barge-in tuning: no xAI session, no cost.
Replays recorded bridge output (Grok's stories) as device echo (delay, coupling, optional soft clipping) + pink
noise + a different voice cutting in, through the real EchoCanceller, Silero and EchoModel, then evaluates the
barge-in decision rule for parameter sets. usage: offline_barge.py [mode ...]   (modes: norm loud)"""
import sys, os, math, glob, collections, itertools, json
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "grok_voice_live", "bridge"))
os.environ.setdefault("GROK_BRIDGE_OPTIONS", "/nonexistent")
import logging; logging.disable(logging.WARNING)
from echo_cancel import EchoCanceller
from server import EchoModel
from settings import DEFAULTS
from livekit.plugins.silero import onnx_model

FR = 512; SR = 16000; DT = FR / SR
STORIES = sorted(glob.glob(os.path.join(HERE, "results", "story04*_?.wav")) + glob.glob(os.path.join(HERE, "results", "c2story_?.wav")))
BARGE = "/workspace/grok-voice-agent/test_audio/barge_leo.wav"


class FakeOut:
    def __init__(self):
        self.timeline = collections.deque(maxlen=400); self.seg_t0 = 0.0
    def env_db(self, t0, t1):
        return max((db for t, db in self.timeline if t0 <= t <= t1), default=-120.0)


def pink(n, dbfs, rng):
    w = np.fft.rfft(rng.standard_normal(n)); f = np.arange(len(w)); f[0] = 1
    x = np.fft.irfft(w / np.sqrt(f), n); x /= np.sqrt(np.mean(x * x)) + 1e-12
    return x * 10 ** (dbfs / 20)


def load16(path):
    x, sr = sf.read(path, dtype="float64")
    if x.ndim > 1: x = x.mean(1)
    return resample_poly(x, SR, sr) if sr != SR else x


_VADS = {}
def features(out48, mode, barge_t=None, barge_db=0.0, seed=0, echo_db=2.0, clip_dbfs=-14.0, delay_ms=120):
    """Per-frame (t, p, excess, hv-ish inputs) for one simulated session."""
    rng = np.random.default_rng(seed)
    n48 = len(out48) // 3 * 3
    ref16 = out48[:n48].reshape(-1, 3).mean(1)
    d = int(delay_ms * SR / 1000)
    echo = np.concatenate([np.zeros(d), ref16])[: len(ref16)] * 10 ** (echo_db / 20)
    if mode == "loud":
        lim = 10 ** (clip_dbfs / 20); echo = lim * np.tanh(echo / lim)
    mic = echo + pink(len(echo), -28, rng)
    if barge_t is not None:
        b = load16(BARGE) * 10 ** (barge_db / 20); i0 = int(barge_t * SR)
        seg = b[: max(0, len(mic) - i0)]; mic[i0:i0 + len(seg)] += seg
    mic = np.clip(mic, -1, 1)
    ec = EchoCanceller(out_rate=48000)
    out = FakeOut(); em = EchoModel(out, 0.15, float(DEFAULTS["barge_echo_init_db"]), -30.0)
    sess = onnx_model.new_inference_session(False)
    vad = onnx_model.OnnxModel(onnx_session=sess, sample_rate=SR)
    T0 = 100.0; chunk = 1920; ri = 0; feats = []
    ec.new_segment(T0)
    for k in range(len(mic) // FR):
        t = T0 + (k + 1) * DT + rng.uniform(0, 0.004)
        while ri * chunk / 48000 < (k + 1) * DT + 0.3 and (ri + 1) * chunk <= n48:   # bridge sends ahead
            c = out48[ri * chunk:(ri + 1) * chunk]; tp = T0 + ri * chunk / 48000
            ec.add_ref(tp, (np.clip(c, -1, 1) * 32767).astype("<i2").tobytes())
            out.timeline.append((tp, 20 * math.log10(np.sqrt(np.mean(c * c)) + 1e-6)))
            ri += 1
        fr = (mic[k * FR:(k + 1) * FR] * 32767).astype("<i2").tobytes()
        res = np.frombuffer(ec.process(t, fr), "<i2").astype(np.float32) / 32768
        p = float(vad(res))
        mdb = 20 * math.log10(float(np.sqrt(np.mean(res * res))) + 1e-6)
        exp = em.expected(t); em.add(t, mdb)
        feats.append((t - T0, p, mdb - exp, em.fits, max(em.coupling, em.peak)))
        if barge_t is not None and t - T0 > barge_t + 1.0: break
    return feats


def decide(feats, P):
    """Mirror of the bridge's barge-in rule. Returns time of the first detection (s from reply start) or None."""
    thr, margin = P["barge_threshold"], P["barge_margin_db"]
    minf = max(1, int(P["barge_min_speech_ms"]) // 32)
    cand, low, fits0 = [], 0, None
    for t, p, ex, fits, coup in feats:
        if fits0 is None: fits0 = fits
        m_eff = margin + (P["barge_onset_extra_db"] if t < P["barge_onset_s"] else 0.0)
        converged = fits > fits0 or t > P["barge_strict_until_s"]
        lo, hi = P["barge_hv_from_db"], P["barge_hv_full_db"]
        hv = 1.0 if not converged else min(1.0, max(0.0, (coup - lo) / max(0.1, hi - lo)))
        m_eff += hv * max(0.0, P["barge_hv_margin_db"] - margin)
        if p >= thr:
            cand.append((p, ex, m_eff)); low = 0
        else:
            low += 1
            if low >= 2: cand = []
        if len(cand) < minf: continue
        win = cand[-minf:]
        med = float(np.median([x[1] for x in win])); frac = sum(1 for x in win if x[1] >= x[2]) / len(win)
        f0 = max(P["barge_frac"], P["barge_frac_floor"]); freq = f0 + hv * max(0.0, P["barge_hv_frac"] - f0)
        if frac < freq or med < hv * P["barge_hv_margin_db"]: continue
        return t
    return None


def build(modes):
    cache = os.path.join(HERE, "local", "offline_feats.npz")
    data = {}
    for mode in modes:
        for si, s in enumerate(STORIES):
            out48, sr = sf.read(s, dtype="float64"); assert sr == 48000
            data[(mode, si, None, None)] = features(out48, mode, seed=si)
            for bt in (3.0, 5.0, 7.0):
                for g in (0.0, -6.0):
                    data[(mode, si, bt, g)] = features(out48, mode, bt, g, seed=si * 7 + int(bt))
            print("features", mode, os.path.basename(s), flush=True)
    return data


def evaluate(data, P):
    r = collections.defaultdict(lambda: [0, 0, 0])   # (mode, level) -> [hits, self, n]
    for (mode, si, bt, g), f in data.items():
        t = decide(f, P)
        if bt is None:
            r[(mode, "story")][1] += t is not None; r[(mode, "story")][2] += 1
        else:
            lv = "norm" if g == 0 else "quiet"
            k = r[(mode, lv)]; k[2] += 1
            if t is not None and t < bt: k[1] += 1
            elif t is not None and t - bt <= 0.55: k[0] += 1
    return dict(r)


if __name__ == "__main__":
    import pickle
    modes = sys.argv[1:] or ["norm", "loud"]
    pk = os.path.join(HERE, "local", "offline_feats.pkl")
    if os.path.exists(pk):
        data = pickle.load(open(pk, "rb"))
    else:
        data = build(modes); pickle.dump(data, open(pk, "wb"))
    P = {k: float(v) for k, v in DEFAULTS.items() if k.startswith("barge_")}
    print("current defaults:", evaluate(data, P))
