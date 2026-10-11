"""Device emulator: speaks the exact grok_stream firmware protocol to the bridge.
Sends grok-hello, audio-start, then real-time PCM16 mono 16 kHz in 1024-byte audio-chunks (32 ms, like the
firmware), keeps streaming silence like a live mic until the bridge sends grok-end / closes.
Saves received 24 kHz audio to out_<name>.wav and reports latency (end of input speech -> first audio chunk).
Usage: python test_client.py <host> <port> <name> <input.wav> [token]
Env: BARGE_WAV/BARGE_AFTER_S/BARGE_GAIN_DB (barge-in), NOISE_DBFS=-26 [NOISE_COLOR=white|pink] (constant mic noise floor),
     ECHO_DB=+2 [ECHO_DELAY_MS=120] (device echo: the bridge's output, played out in real time like the speaker,
     decimated to 16 kHz, delayed and mixed into the mic at ECHO_DB relative to the output level; grok-clear
     flushes it like the device does), MAX_S (stop after N s), NEXT_WAV/NEXT_AFTER_IDLE_S (say a second
     utterance N s after the bridge's audio stops, e.g. "thanks, that's all")."""
from __future__ import annotations
import asyncio, json, sys, time, wave
import numpy as np, soundfile as sf
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0] + "/grok_voice_live/bridge")
from protocol import read_event, encode_event

FMT = {"rate": 16000, "width": 2, "channels": 1}


def load16(path):
    x, sr = sf.read(path, dtype="int16")
    if x.ndim > 1: x = x[:, 0]
    n = int(len(x) * 16000 / sr)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.int16)


async def main(host, port, name, wav_in, token=""):
    speech = load16(wav_in)
    stream = np.concatenate([np.zeros(4800, np.int16), speech]).tobytes()   # 0.3 s lead-in after wake
    speech_end = len(stream)
    t_connect = time.monotonic()
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(encode_event("grok-hello", {"token": token, "device": "test-emulator", "version": 1}))
    writer.write(encode_event("audio-start", FMT)); await writer.drain()
    out, events, t = bytearray(), [], {}

    async def rx():
        while True:
            ev = await read_event(reader)
            if ev is None:
                t.setdefault("closed", time.monotonic()); return
            et, data, payload = ev
            now = time.monotonic()
            if et == "audio-chunk":
                t.setdefault("first_audio", now); out.extend(payload); t["last_audio"] = now
                if echo_g:
                    x = np.frombuffer(payload, np.int16).astype(np.float64)
                    r = int((data or {}).get("rate", t.get("out_rate", 48000))) // 16000 or 1
                    play.extend(x[: len(x) // r * r].reshape(-1, r).mean(axis=1))
                if "barge_at" in t and "speech_end" in t and t["speech_end"] > t["barge_at"]:
                    t.setdefault("first_audio_after_barge", now)
            else:
                if et == "grok-clear":
                    play.clear()
                if et == "audio-start" and data:
                    t["out_rate"] = int(data.get("rate", 24000))
                events.append({"t": round(now - t_connect, 3), "type": et, "data": data})
                print(f"  +{now - t_connect:6.2f}s {et} {json.dumps(data) if data else ''}", flush=True)
                if et == "grok-end":
                    t.setdefault("closed", now); return

    import os, collections
    echo_db = os.environ.get("ECHO_DB"); echo_g = 10 ** (float(echo_db) / 20) if echo_db else 0.0
    delay = np.zeros(int(16 * float(os.environ.get("ECHO_DELAY_MS", "120"))))
    play: collections.deque = collections.deque()
    max_s = float(os.environ.get("MAX_S", "150"))
    nxt = os.environ.get("NEXT_WAV"); nxt_idle = float(os.environ.get("NEXT_AFTER_IDLE_S", "1.5"))
    rtask = asyncio.ensure_future(rx())
    barge = os.environ.get("BARGE_WAV"); barge_after = float(os.environ.get("BARGE_AFTER_S", "1.5"))
    t0 = time.monotonic(); i = 0; CH = 1024
    noise_db = os.environ.get("NOISE_DBFS"); noise_color = os.environ.get("NOISE_COLOR", "white")
    noise_rms = 32768 * 10 ** (float(noise_db) / 20) if noise_db else 0.0   # emulate an AGC'd room noise floor
    rng = np.random.default_rng(7)
    while not rtask.done() and time.monotonic() - t0 < max_s:
        chunk = stream[i:i + CH] if i < len(stream) else b"\0" * CH
        chunk = chunk + b"\0" * (CH - len(chunk))
        if noise_rms:
            nz = rng.standard_normal(CH // 2)
            if noise_color == "pink":
                nz = np.cumsum(nz); nz -= np.convolve(nz, np.ones(64) / 64, mode="same"); nz /= nz.std() + 1e-9
            mixed = np.frombuffer(chunk, np.int16).astype(np.float64) + nz * noise_rms
            chunk = np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()
        if echo_g:
            spk = np.array([play.popleft() if play else 0.0 for _ in range(CH // 2)])
            dl = np.concatenate([delay, spk]); delay, e = dl[len(spk):], dl[:len(spk)]
            mixed = np.frombuffer(chunk, np.int16).astype(np.float64) + e * echo_g
            chunk = np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()
        writer.write(encode_event("audio-chunk", FMT, chunk))
        i += CH
        if barge and "first_audio" in t and "barge_at" not in t and time.monotonic() - t["first_audio"] > barge_after:
            t["barge_at"] = time.monotonic()
            b = np.clip(load16(barge).astype(np.float64) * 10 ** (float(os.environ.get("BARGE_GAIN_DB", "0")) / 20),
                        -32768, 32767).astype(np.int16).tobytes()
            stream = stream[:i] + b"\0" * max(0, i - len(stream)) + b; speech_end = len(stream); t.pop("speech_end", None); t.pop("first_audio_after_barge", None)
            print(f"  +{t['barge_at'] - t_connect:6.2f}s >>> injecting barge-in audio", flush=True)
        if (nxt and "next_at" not in t and "last_audio" in t and i >= speech_end
                and time.monotonic() - t["last_audio"] > nxt_idle):
            t["next_at"] = time.monotonic(); b = load16(nxt).tobytes()
            stream = stream[:i] + b"\0" * max(0, i - len(stream)) + b; speech_end = len(stream)
            print(f"  +{t['next_at'] - t_connect:6.2f}s >>> saying NEXT_WAV", flush=True)
        if "speech_end" not in t and i >= speech_end:
            t["speech_end"] = time.monotonic(); t.setdefault("speech_end_first", t["speech_end"])
            if "barge_at" in t: t["barge_end"] = t["speech_end"]
        await asyncio.sleep(max(0, t0 + i / 32000 - time.monotonic()))
    try:
        writer.write(encode_event("audio-stop")); await writer.drain(); writer.close()
    except Exception:
        pass
    path = f"{name}.wav"
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(t.get("out_rate", 24000)); w.writeframes(bytes(out))
    res = {"name": name, "out_wav": path, "out_audio_s": round(len(out) / 2 / t.get("out_rate", 24000), 2), "out_rate": t.get("out_rate"),
           "latency_end_of_input_to_first_audio_s": round(t["first_audio"] - t["speech_end_first"], 3) if "first_audio" in t and "speech_end_first" in t else None,
           "barge_clear_after_s": next((round(e["t"] - (t["barge_at"] - t_connect), 3) for e in events if e["type"] == "grok-clear"), None) if "barge_at" in t else None,
           "barge_reply_latency_s": next((round(e["t"] - (t["barge_end"] - t_connect), 3) for e in events if e["type"] == "audio-start" and "barge_end" in t and e["t"] > t["barge_end"] - t_connect), None),
           "session_s": round(t.get("closed", time.monotonic()) - t_connect, 1),
           "self_interrupts": sum(1 for e in events if e["type"] == "grok-clear" and ("barge_at" not in t or e["t"] < t["barge_at"] - t_connect)),
           "end_after_next_s": round(t["closed"] - t["next_at"], 2) if "next_at" in t and "closed" in t else None,
           "end": next((e["data"] for e in events if e["type"] == "grok-end"), None),
           "transcript": [e["data"] for e in events if e["type"] == "transcript"]}
    json.dump({**res, "events": events}, open(f"{name}.json", "w"), indent=1)
    print(json.dumps(res, indent=1))

if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
