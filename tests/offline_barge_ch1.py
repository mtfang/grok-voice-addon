"""Offline (no xAI, no cost) barge-in replay for the ch1 (no-AGC) device input: Grok story audio as echo (coupling, delay) + noise +
a barge voice at a given level, through the real EchoCanceller / Silero / EchoModel; then the bridge's decision rule.
usage: offline_barge_ch1.py build <tag> <init_coupling_db> <init_noise_dbfs>; offline_barge_ch1.py eval <tag> ..."""
import sys, os, math, collections, pickle, numpy as np, soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import offline_barge as ob
from offline_barge import EchoCanceller, EchoModel, FakeOut, pink, load16, onnx_model, FR, SR, DT

def features(out48, barge_t, barge_rms, echo_db, noise_db, delay_ms, init_coup, init_noise, seed):
    rng = np.random.default_rng(seed)
    n48 = len(out48) // 3 * 3; ref16 = out48[:n48].reshape(-1, 3).mean(1)
    d = int(delay_ms * SR / 1000)
    echo = np.concatenate([np.zeros(d), ref16])[: len(ref16)] * 10 ** (echo_db / 20)
    mic = echo + pink(len(echo), noise_db, rng)
    if barge_t is not None:
        b = load16(ob.BARGE); act = b[np.abs(b) > 0.02]; b = b / (np.sqrt(np.mean(act ** 2)) + 1e-9) * 10 ** (barge_rms / 20)
        i0 = int(barge_t * SR); seg = b[: max(0, len(mic) - i0)]; mic[i0:i0 + len(seg)] += seg
    mic = np.clip(mic, -1, 1)
    ec = EchoCanceller(out_rate=48000); out = FakeOut(); em = EchoModel(out, 0.15, init_coup, init_noise)
    vad = onnx_model.OnnxModel(onnx_session=onnx_model.new_inference_session(False), sample_rate=SR)
    T0 = 100.0; chunk = 1920; ri = 0; feats = []; ec.new_segment(T0)
    for k in range(len(mic) // FR):
        t = T0 + (k + 1) * DT + rng.uniform(0, 0.004)
        while ri * chunk / 48000 < (k + 1) * DT + 0.3 and (ri + 1) * chunk <= n48:
            c = out48[ri * chunk:(ri + 1) * chunk]; tp = T0 + ri * chunk / 48000
            ec.add_ref(tp, (np.clip(c, -1, 1) * 32767).astype("<i2").tobytes())
            out.timeline.append((tp, 20 * math.log10(np.sqrt(np.mean(c * c)) + 1e-6))); ri += 1
        fr = (mic[k * FR:(k + 1) * FR] * 32767).astype("<i2").tobytes()
        res = np.frombuffer(ec.process(t, fr), "<i2").astype(np.float32) / 32768
        p = float(vad(res)); mdb = 20 * math.log10(float(np.sqrt(np.mean(res * res))) + 1e-6)
        exp = em.expected(t); em.add(t, mdb)
        feats.append((t - T0, p, mdb - exp, em.fits, max(em.coupling, em.peak)))
        if barge_t is not None and t - T0 > barge_t + 1.2: break
        if barge_t is None and t - T0 > 12: break
    return feats

def decide(feats, P, why=None):
    thr, margin = P["barge_threshold"], P["barge_margin_db"]; minf = max(1, int(P["barge_min_speech_ms"]) // 32)
    cand, low, fits0 = [], 0, None
    for t, p, ex, fits, coup in feats:
        if fits0 is None: fits0 = fits
        m_eff = margin + (P["barge_onset_extra_db"] if t < P["barge_onset_s"] else 0.0)
        conv = fits > fits0 or t > P["barge_strict_until_s"]
        lo, hi = P["barge_hv_from_db"], P["barge_hv_full_db"]
        hv = 1.0 if not conv else min(1.0, max(0.0, (coup - lo) / max(0.1, hi - lo)))
        m_eff += hv * max(0.0, P["barge_hv_margin_db"] - margin)
        if p >= thr: cand.append((p, ex, m_eff)); low = 0
        else:
            low += 1
            if low >= 2:
                if len(cand) >= 3 and why is not None:
                    w = cand[-minf:]; fr = sum(1 for x in w if x[1] >= x[2]) / len(w)
                    why["short" if len(cand) < minf else "below_echo"] += 1
                cand = []
        if len(cand) < minf: continue
        w = cand[-minf:]; med = float(np.median([x[1] for x in w])); fr = sum(1 for x in w if x[1] >= x[2]) / len(w)
        f0 = max(P["barge_frac"], P["barge_frac_floor"]); freq = f0 + hv * max(0.0, P["barge_hv_frac"] - f0)
        if fr < freq or med < hv * P["barge_hv_margin_db"]: continue
        return t
    return None
def evaluate(data, P):
    r = collections.defaultdict(lambda: [0, 0, 0, 0.0]); why = collections.Counter()
    for (si, e, bt, lv), f in data.items():
        t = decide(f, P, why if bt is not None else None)
        if bt is None:
            k = r[(e, "story")]; k[1] += t is not None; k[2] += 1
        else:
            k = r[(e, lv)]; k[2] += 1
            if t is not None and t < bt: k[1] += 1
            elif t is not None and t - bt <= 0.8: k[0] += 1; k[3] += t - bt
    return {f"{k[0]:+.0f}dB/{k[1]}": (v[0], v[1], v[2], round(v[3] / max(1, v[0]), 2)) for k, v in sorted(r.items(), key=str)}, dict(why)
if __name__ == "__main__":
    import itertools
    from settings import DEFAULTS
    LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local")
    if sys.argv[1] == "build":
        tag, init_coup, init_noise = sys.argv[2], float(sys.argv[3]), float(sys.argv[4]); data = {}
        for si, s in enumerate(ob.STORIES):
            out48, sr = sf.read(s, dtype="float64")
            for echo_db in (-13.0, -7.0):          # measured ch1 coupling (living room 0.4.2); -7 = worse case
                data[(si, echo_db, None, None)] = features(out48, None, 0, echo_db, -46, 60, init_coup, init_noise, si)
                for bt in (1.0, 3.0, 6.0):
                    for lv in (-30.0, -36.0):      # barge voice active RMS (normal / quiet)
                        data[(si, echo_db, bt, lv)] = features(out48, bt, lv, echo_db, -46, 60, init_coup, init_noise,
                                                               si * 7 + int(bt))
            print("done", os.path.basename(s), flush=True)
        pickle.dump(data, open(os.path.join(LOCAL, f"ch1_feats_{tag}.pkl"), "wb"))
    else:
        base = {k: float(v) for k, v in DEFAULTS.items() if k.startswith("barge_")}
        for tag in sys.argv[2:]:
            data = pickle.load(open(os.path.join(LOCAL, f"ch1_feats_{tag}.pkl"), "rb"))
            print(tag, "defaults:", evaluate(data, base))
            for thr, ms, mg in itertools.product((0.8, 0.7), (288, 224, 192), (4.0, 3.0)):
                print(f"  thr {thr} min {ms} margin {mg}:",
                      *evaluate(data, dict(base, barge_threshold=thr, barge_min_speech_ms=ms, barge_margin_db=mg)))
