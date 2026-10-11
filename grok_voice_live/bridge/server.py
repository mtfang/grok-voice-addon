"""Grok live voice bridge: a Wyoming-framed TCP server for Satellite1 devices.

Per connection (opened by the device after its local wake word):
  device -> bridge : [grok-hello {token,device}] audio-start, audio-chunk (PCM16 mono 16 kHz)..., audio-stop
  bridge -> device : grok-state {state}, audio-start {rate:48000}, audio-chunk (PCM16 mono 48 kHz, resampled
                     from xAI's 24 kHz with soxr HIGH and run through the Satellite1 EQ + limiter),
                     audio-stop, grok-clear (barge-in: flush speaker), transcript, grok-end {reason}
The bridge owns one LiveKit AgentSession (xAI realtime + HA MCP tools) per connection. The conversation stays
open until the user dismisses Grok (hang_up tool: "thanks", "that's all", "goodbye"...), says the wake word again,
or `idle_timeout_s` passes with nobody talking (never while a tool call or its spoken result is pending), capped
at `max_session_s`.
"""
from __future__ import annotations
import asyncio, collections, hmac, logging, math, os, time, wave
import numpy as np
from livekit import rtc
from livekit.agents import Agent, AgentSession, function_tool, mcp
from livekit.agents.voice import io
from livekit.plugins import xai

from protocol import read_event, encode_event, ProtocolError
from output_eq import OutputEQ
from echo_cancel import EchoCanceller
import settings

log = logging.getLogger("grok-bridge")
EQ_PRESET = ""  # loaded in main()
OUT_RATE = 24000  # rate LiveKit hands us (xAI realtime output); we resample to the device rate ourselves


class AudioStats:
    """Per-direction counters for diagnostics: bytes/frames per second, RMS and peak (dBFS) of PCM16."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.total_bytes = self.total_frames = 0
        self.total_sq = 0.0; self.total_n = 0; self.total_peak = 0
        self._reset_window(); self.t_last = time.monotonic()

    def _reset_window(self) -> None:
        self.w_bytes = self.w_frames = 0; self.w_sq = 0.0; self.w_n = 0; self.w_peak = 0

    def add(self, pcm: bytes) -> None:
        x = np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype(np.int64)
        sq = float((x * x).sum()); pk = int(np.abs(x).max()) if x.size else 0
        self.w_bytes += len(pcm); self.w_frames += 1; self.w_sq += sq; self.w_n += x.size; self.w_peak = max(self.w_peak, pk)
        self.total_bytes += len(pcm); self.total_frames += 1; self.total_sq += sq; self.total_n += x.size
        self.total_peak = max(self.total_peak, pk)

    @staticmethod
    def db(v: float) -> float:
        return 20 * math.log10(v / 32768.0) if v > 0 else -120.0

    def window_line(self) -> str | None:
        """Returns a per-second summary line (and resets the window) once a second has elapsed."""
        now = time.monotonic()
        if now - self.t_last < 1.0:
            return None
        dt = now - self.t_last; self.t_last = now
        rms = math.sqrt(self.w_sq / self.w_n) if self.w_n else 0.0
        line = (f"{self.name}: {self.w_bytes / dt:.0f} B/s, {self.w_frames / dt:.1f} frames/s, "
                f"rms {self.db(rms):.1f} dBFS, peak {self.db(self.w_peak):.1f} dBFS")
        self._reset_window()
        return line

    def summary(self) -> str:
        rms = math.sqrt(self.total_sq / self.total_n) if self.total_n else 0.0
        return (f"{self.name}: {self.total_bytes} B in {self.total_frames} frames, rms {self.db(rms):.1f} dBFS, "
                f"peak {self.db(self.total_peak):.1f} dBFS")


class QueueAudioInput(io.AudioInput):
    def __init__(self) -> None:
        super().__init__(label="device-mic")
        self.q: asyncio.Queue[rtc.AudioFrame | None] = asyncio.Queue(maxsize=500)  # ~10 s of 20 ms frames

    def push(self, frame: rtc.AudioFrame | None) -> None:
        if self.q.full():
            self.q.get_nowait()          # drop oldest rather than grow without bound
        self.q.put_nowait(frame)

    async def __anext__(self) -> rtc.AudioFrame:
        f = await self.q.get()
        if f is None:
            raise StopAsyncIteration
        return f


class DeviceAudioOutput(io.AudioOutput):
    """Streams agent audio to the device, at most `lead` seconds ahead of real-time playout, so a
    barge-in only has to discard a little audio (grok-clear tells the device to flush)."""

    def __init__(self, send, lead_s: float, on_first_chunk=None, device_rate: int = 48000,
                 eq: OutputEQ | None = None, ec: EchoCanceller | None = None) -> None:
        super().__init__(label="device-speaker", capabilities=io.AudioOutputCapabilities(pause=False),
                         sample_rate=OUT_RATE)
        self._send, self._lead = send, lead_s
        # Satellite1 speaker pipeline (mixer -> I2S/TAS2780) runs at 48 kHz: send 48 kHz so the on-device
        # resampler is a pass-through, and resample here with soxr HIGH instead.
        self.rate = device_rate
        self._chunk = device_rate * 2 * 40 // 1000          # 40 ms of PCM16 mono
        self._fmt = {"rate": device_rate, "width": 2, "channels": 1}
        self._eq = eq                                      # per-session state (one DeviceAudioOutput per session)
        self._ec = ec                                      # gets every chunk as the echo reference
        self._rs: rtc.AudioResampler | None = None
        self._rs_in = 0
        self.stats = AudioStats("out->device")
        self.from_xai_bytes = 0
        self._q: collections.deque[bytes | None] = collections.deque()
        self._wake = asyncio.Event()
        self._seg_start: float | None = None
        self._sent_s = 0.0
        self._in_segment = False
        self._buf = bytearray()
        self._on_first_chunk = on_first_chunk
        # playback timeline for the echo-aware barge-in: (estimated play time, chunk RMS dBFS)
        self.timeline: collections.deque[tuple[float, float]] = collections.deque(maxlen=400)
        self.play_end = 0.0                                # estimated time the device runs out of audio
        self.seg_t0 = 0.0                                  # when the current/last reply segment started
        self._task = asyncio.ensure_future(self._pump())

    def env_db(self, t0: float, t1: float) -> float:
        """Loudest output chunk whose estimated play time falls in [t0, t1] (-120 if none)."""
        return max((db for t, db in self.timeline if t0 <= t <= t1), default=-120.0)

    def playing(self, now: float, tail: float = 0.0) -> bool:
        return now < self.play_end + tail

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        if not self._in_segment:
            self._in_segment = True
            self._q.append(b"START")
        self.from_xai_bytes += len(frame.data) * 2
        for pcm in self._convert(frame):
            self._q.append(pcm)
        self._wake.set()

    def _convert(self, frame: rtc.AudioFrame | None) -> list[bytes]:
        """Resample to the device rate (frame=None flushes the resampler) and apply the EQ."""
        if frame is not None and frame.sample_rate != self.rate and (self._rs is None or self._rs_in != frame.sample_rate):
            self._rs = rtc.AudioResampler(frame.sample_rate, self.rate, num_channels=1,
                                          quality=rtc.AudioResamplerQuality.HIGH)
            self._rs_in = frame.sample_rate
        if frame is None:
            frames = self._rs.flush() if self._rs else []
            self._rs = None
        elif self._rs is not None and frame.sample_rate != self.rate:
            frames = self._rs.push(frame)
        else:
            frames = [frame]
        out = []
        for f in frames:
            pcm = bytes(f.data)
            if pcm:
                out.append(self._eq.process(pcm) if self._eq else pcm)
        return out

    def flush(self) -> None:
        super().flush()
        if self._in_segment:
            self._in_segment = False
            for pcm in self._convert(None):  # resampler tail
                self._q.append(pcm)
            self._q.append(None)            # end-of-segment marker
            self._wake.set()

    def clear_buffer(self) -> None:
        dropped = self._seg_start is not None or self._q or self._buf
        self._q.clear()
        self._buf.clear()
        self._rs = None                     # drop resampler history; EQ state just decays
        if self._in_segment or self._seg_start is not None:
            pos = (time.monotonic() - self._seg_start) if self._seg_start else 0.0
            self._seg_start, self._in_segment = None, False
            self.play_end = min(self.play_end, time.monotonic())
            if self._ec is not None:
                self._ec.truncate(time.monotonic())
            self._send("grok-clear", None)
            self.on_playback_finished(playback_position=min(pos, self._sent_s), interrupted=True)
        elif dropped:
            self._send("grok-clear", None)

    async def _pump(self) -> None:
        buf = self._buf
        while True:
            if not self._q:
                self._wake.clear()
                await self._wake.wait()
                continue
            item = self._q[0]
            if item == b"START":
                self._q.popleft()
                self._seg_start, self._sent_s = time.monotonic(), 0.0
                self.seg_t0 = self._seg_start
                self._send("audio-start", self._fmt)
                self.on_playback_started(created_at=time.time())
                continue
            if item is None:
                self._q.popleft()
                while buf:
                    ahead = self._sent_s - (time.monotonic() - self._seg_start)
                    if ahead > self._lead:
                        await asyncio.sleep(min(0.05, ahead - self._lead)); continue
                    self._emit(bytes(buf[:self._chunk])); del buf[:self._chunk]
                start, dur = self._seg_start, self._sent_s
                self._send("audio-stop", None)
                if start is not None:
                    await asyncio.sleep(max(0.0, start + dur - time.monotonic()))
                    if self._seg_start == start:
                        self._seg_start = None
                        self.on_playback_finished(playback_position=dur, interrupted=False)
                continue
            # pace: don't get more than `lead` ahead of real-time playout
            ahead = self._sent_s - (time.monotonic() - self._seg_start)
            if ahead > self._lead:
                await asyncio.sleep(min(0.05, ahead - self._lead))
                continue
            if len(buf) < self._chunk:
                buf += self._q.popleft()
            if len(buf) >= self._chunk:      # send in 40 ms chunks, pacing each one
                self._emit(bytes(buf[:self._chunk])); del buf[:self._chunk]

    def _emit(self, pcm: bytes) -> None:
        if self._sent_s == 0.0 and self._on_first_chunk:
            self._on_first_chunk()
        if self._seg_start is not None:
            now = time.monotonic()
            if self._seg_start + self._sent_s < now - 0.005:
                # underrun (xAI audio arrived slower than real time): the device's buffer ran dry and it will
                # play this chunk on arrival, so re-anchor the playback timeline (keeps the echo reference aligned)
                self._seg_start = now - self._sent_s
            t_play = self._seg_start + self._sent_s
            x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            self.timeline.append((t_play, 20 * math.log10(float(np.sqrt(np.mean(x * x))) / 32768.0 + 1e-6)))
            if self._ec is not None:
                self._ec.add_ref(t_play, pcm)
        self._sent_s += len(pcm) / 2 / self.rate
        if self._seg_start is not None:
            self.play_end = max(self.play_end, self._seg_start + self._sent_s)
        self.stats.add(pcm)
        line = self.stats.window_line()
        if line:
            log.debug(line)
        self._send("audio-chunk", self._fmt, pcm)

    async def aclose(self) -> None:
        self._task.cancel()


_VAD = None


def _vad():
    global _VAD
    if _VAD is None:
        from livekit.plugins import silero
        _VAD = silero.VAD.load(min_speech_duration=0.15, min_silence_duration=0.4, activation_threshold=0.6)
    return _VAD


def build_session(opts: dict, extra_tools: list | None = None) -> tuple[AgentSession, Agent]:
    llm = xai.realtime.RealtimeModel(model=opts["model"], voice=opts["voice"], api_key=opts["xai_api_key"])
    ha_mcp = mcp.MCPServerHTTP(
        url=opts["ha_url"].rstrip("/") + "/api/mcp", transport_type="streamable_http",
        headers={"Authorization": "Bearer " + opts["ha_token"]},
        timeout=10, client_session_timeout_seconds=20,
    )
    # XMOS does hardware AEC on the device, so LiveKit's software AEC warm-up (which blocks barge-in for
    # the first 3 s of each reply) is shortened.
    session = AgentSession(llm=llm, user_away_timeout=None,
                           aec_warmup_duration=float(opts["aec_warmup_s"]) or None)
    agent = Agent(instructions=opts["instructions"],
                  tools=[mcp.MCPToolset(id="home_assistant", mcp_server=ha_mcp), *(extra_tools or [])])
    return session, agent


class EchoModel:
    """Predicts the residual speaker echo level in the device mic signal from what the bridge sent:
    echo_dB(t) ~= output_dB(t - lag) + coupling. lag and coupling are fitted from the session's own audio
    (cross-correlating the mic and output level envelopes while Grok talks; median for the coupling, so a
    user talking over Grok doesn't skew it) and remembered per device."""
    FR = 0.032                       # one Silero/mic frame
    GRID = 0.008

    def __init__(self, out: "DeviceAudioOutput", lag: float, coupling: float, noise: float) -> None:
        self.out, self.lag, self.coupling, self.noise = out, lag, coupling, noise
        self.hist: collections.deque[tuple[float, float]] = collections.deque(maxlen=190)  # (t, mic_db) in echo
        self.frames = 0
        self.fits = 0
        self.corr = 0.0

    def out_at(self, t: float) -> float:
        # chunk with play time tp covers [tp, tp + 40 ms]; widen by one frame for timing slop
        return self.out.env_db(t - 0.040 - self.FR, t + self.FR)

    def expected(self, t: float) -> float:
        return 10 * math.log10(10 ** (self.noise / 10) + 10 ** ((self.out_at(t - self.lag) + self.coupling) / 10))

    def add(self, t: float, mic_db: float) -> None:
        self.hist.append((t, mic_db)); self.frames += 1
        if self.frames % 31 == 0 and len(self.hist) >= 60:
            self.fit()

    def fit(self) -> None:
        tl = list(self.out.timeline)
        if len(tl) < 20:
            return
        ts = np.array([x[0] for x in self.hist]); ys = np.array([x[1] for x in self.hist])
        t0 = ts[0] - 0.6
        n = int((ts[-1] - t0) / self.GRID) + 8
        g = np.full(n, -120.0)
        for tp, db in tl:                                # rasterize the output envelope on an 8 ms grid
            a = int((tp - t0) / self.GRID); b = int((tp + 0.040 - t0) / self.GRID)
            if b < 0 or a >= n:
                continue
            a, b = max(a, 0), min(b, n)
            g[a:b] = np.maximum(g[a:b], db)
        from scipy.ndimage import maximum_filter1d
        g = maximum_filter1d(g, size=int(2 * self.FR / self.GRID) + 1)
        best, best_lag = -1.0, self.lag
        for lag in np.arange(0.0, 0.48, 0.016):
            idx = ((ts - lag - t0) / self.GRID).astype(int)
            ok = (idx >= 0) & (idx < n)
            x = g[idx[ok]]; y = ys[ok]
            m = x > -60
            if m.sum() < 40:
                continue
            x, y = x[m], y[m]
            if x.std() < 1e-3 or y.std() < 1e-3:
                continue
            c = float(np.corrcoef(x, y)[0, 1])
            if c > best:
                best, best_lag = c, float(lag)
        self.corr = best
        if best < 0.3:                                   # nothing echo-like left in the mic (or canceller works)
            self.coupling = max(-15.0, self.coupling - 2.0)
            return
        self.lag = best_lag if self.fits == 0 else 0.5 * self.lag + 0.5 * best_lag
        idx = ((ts - self.lag - t0) / self.GRID).astype(int)
        ok = (idx >= 0) & (idx < n)
        x = g[idx[ok]]; y = ys[ok]
        pn = 10 ** (self.noise / 10)
        m = (x > -60) & (10 ** (y / 10) > 2 * pn)        # echo clearly above the noise floor
        if m.sum() < 30:                                 # echo is below the noise floor
            self.coupling = max(-15.0, self.coupling - 2.0)
            return
        e = 10 * np.log10(np.maximum(10 ** (y[m] / 10) - pn, 1e-12)) - x[m]
        c = float(np.median(e))
        self.coupling = min(15.0, max(-40.0, c if self.fits == 0 else 0.5 * self.coupling + 0.5 * c))
        self.fits += 1
        log.debug("echo model fit: lag %.0f ms, coupling %+.1f dB (corr %.2f, %d frames)",
                  self.lag * 1000, self.coupling, best, int(m.sum()))


ECHO_STATE: dict[str, tuple[float, float, float]] = {}   # peer -> (lag s, coupling dB, noise floor dBFS)
ECHO_CANCELLERS: dict[str, EchoCanceller] = {}            # peer -> canceller (learned echo path survives sessions)


class Connection:
    def __init__(self, opts, reader, writer) -> None:
        self.opts, self.reader, self.writer = opts, reader, writer
        self.peer = writer.get_extra_info("peername")[0]
        self.ended = asyncio.Event()
        self.end_reason = "client"
        self.t_first_out: float | None = None

    def send(self, etype: str, data: dict | None, payload: bytes = b"") -> None:
        if not self.writer.is_closing():
            self.writer.write(encode_event(etype, data, payload))

    def end(self, reason: str) -> None:
        if not self.ended.is_set():
            self.end_reason = reason
            self.ended.set()

    async def run(self) -> None:
        o = self.opts
        if o["allowed_clients"] and self.peer not in o["allowed_clients"]:
            log.warning("rejecting client %s (not in allowed_clients)", self.peer)
            return
        first = await asyncio.wait_for(read_event(self.reader), timeout=5)
        if first is None:
            return
        if o["client_token"]:
            tok = first[1].get("token", "") if first[0] == "grok-hello" else ""
            if not hmac.compare_digest(str(tok), o["client_token"]):
                log.warning("rejecting client %s (bad token)", self.peer)
                self.send("grok-end", {"reason": "unauthorized"}); await self.writer.drain()
                return
        log.info("session open for %s (%s)", self.peer, first[1].get("device", "?"))
        t0 = time.monotonic()
        hang = {"at": None}

        @function_tool
        async def hang_up() -> str:
            """End the conversation and stop listening. Call this when the user is done or dismisses you,
            for example: thanks, thank you that's all, that's it, goodbye, bye, stop listening, never mind,
            go away, I'm done. Do not call it after a normal question or command."""
            if hang["at"] is None:
                hang["at"] = time.monotonic()
                log.info("hang_up requested by the model")
            return "The session will end now. Say a very short goodbye (two to four words) and nothing else."

        session, agent = build_session(o, [hang_up])
        inp = QueueAudioInput()
        in_stats = AudioStats("in<-device")
        in_fmt = {"rate": 16000, "width": 2, "channels": 1}
        gain = 10 ** (float(o["input_gain_db"]) / 20.0)
        # VAD gate: the Satellite1's XMOS ASR channel is auto-gained, so between utterances the room noise floor
        # sits around -25 dBFS RMS. xAI's server VAD can take that for ongoing speech and never end the turn (seen
        # on the device: speech_started, transcripts, but no speech_stopped / no reply). The bridge runs Silero
        # locally and sends xAI digital silence while Silero hears no speech, with a pre-roll so onsets aren't cut.
        use_gate = bool(o["vad_gate"])
        gate = {"open": not use_gate, "opened": 0}
        # Echo-aware barge-in. While Grok's audio is playing (and `echo_tail_ms` after), residual speaker echo
        # in the XMOS-processed mic signal (measured on satellite1-d98894 at about -17..-22 dBFS RMS, as loud as
        # the user's own speech, because the XMOS AGC boosts it) is loud enough for Silero to call it speech. So
        # in that window the gate toward xAI stays shut and the bridge only interrupts when Silero is confident
        # for `barge_min_speech_ms` AND the mic is `barge_margin_db` above the expected echo (output envelope at
        # the estimated play time + a learned echo coupling) and above the noise floor.
        echo = {"cand": [], "low": 0, "rejected": 0,
                "fired": 0, "speaking": False, "mic_db": -120.0, "barging": False, "barge_t": 0.0,
                "start_in_echo": False, "max_rej": -99.0}
        tail_s = float(o["echo_tail_ms"]) / 1000.0
        busy = {"tools": set(), "reply_wait_since": None, "last_gen": 0.0, "last_act": time.monotonic()}
        preroll: collections.deque = collections.deque(maxlen=max(1, int(o["vad_gate_preroll_ms"]) // 32))
        dump = None
        if int(o["debug_dump_s"]) > 0:
            try:
                os.makedirs(o["debug_dump_dir"], exist_ok=True)
                path = os.path.join(o["debug_dump_dir"], time.strftime("inbound_%Y%m%d_%H%M%S.wav"))
                dump = {"path": path, "w": wave.open(path, "wb"), "left": int(o["debug_dump_s"]) * 16000 * 2}
                dump["w"].setnchannels(1); dump["w"].setsampwidth(2); dump["w"].setframerate(16000)
                log.info("dumping first %ss of inbound audio (raw, before gain) to %s", o["debug_dump_s"], path)
            except Exception as e:
                log.warning("cannot open audio dump: %s", e); dump = None
        rate = int(o["output_rate"])
        eq = OutputEQ(EQ_PRESET, rate) if o["eq_enabled"] else None
        ec = None
        if o["echo_cancel"]:
            ec = ECHO_CANCELLERS.get(self.peer)
            if ec is None or ec.dec != max(1, rate // 16000):
                ec = ECHO_CANCELLERS[self.peer] = EchoCanceller(out_rate=rate)
            ec.new_session()
            if os.environ.get("GROK_EC_DEBUG"):
                ec.debug = []
        out = DeviceAudioOutput(self.send, lead_s=o["out_lead_ms"] / 1000.0, device_rate=rate, eq=eq, ec=ec)
        session.input.audio, session.output.audio = inp, out
        lag0, coup0, noise0 = ECHO_STATE.get(self.peer, (0.15, float(o["barge_echo_init_db"]), -30.0))
        em = EchoModel(out, lag0, coup0, noise0)

        @session.on("user_input_transcribed")
        def _ut(ev):
            log.info("xAI user transcript (final=%s): %s", getattr(ev, "is_final", "?"), getattr(ev, "transcript", ""))

        @session.on("metrics_collected")
        def _m(ev):
            log.debug("metrics: %s", getattr(ev, "metrics", ev))

        @session.on("error")
        def _e(ev):
            log.warning("session error: %r", getattr(ev, "error", ev))

        @session.on("user_state_changed")
        def _u(ev):
            log.info("user_state %s -> %s (xAI server VAD)", ev.old_state, ev.new_state)
            busy["last_act"] = time.monotonic()

        @session.on("agent_state_changed")
        def _a(ev):
            busy["last_act"] = time.monotonic()
            if (ev.new_state == "speaking" and busy["reply_wait_since"] is not None and not busy["tools"]
                    and busy["last_gen"] > busy["reply_wait_since"]):
                busy["reply_wait_since"] = None          # the tool result is being spoken
            if hang["at"] is not None and ev.old_state == "speaking" and ev.new_state != "speaking":
                asyncio.get_event_loop().call_later(0.4, self.end, "dismissed")
            # keep the ring in "listening" for the whole open conversation instead of idle
            self.send("grok-state", {"state": "listening" if ev.new_state in ("listening", "idle") else ev.new_state})

        @session.on("tool_execution_updated")
        def _tu(ev):
            u = ev.update
            busy["last_act"] = time.monotonic()
            if u.type == "tool_call_started":
                busy["tools"].add(u.function_call.call_id)
                log.debug("tool started: %s", u.function_call.name)
            elif u.type == "tool_call_ended":
                busy["tools"].discard(u.call_id)

        @session.on("conversation_item_added")
        def _c(ev):
            it = ev.item
            if getattr(it, "type", None) == "message" and it.text_content:
                log.info("%s: %s", it.role, it.text_content)
                self.send("transcript", {"role": it.role, "text": it.text_content[:500]})

        @session.on("function_tools_executed")
        def _f(ev):
            names = []
            for call, res in ev.zipped():
                busy["tools"].discard(call.call_id)
                names.append(call.name)
                log.info("tool %s %s -> %s", call.name, call.arguments,
                         (str(getattr(res, "output", ""))[:160] if res is not None else "?"))
            if any(n != "hang_up" for n in names) and hang["at"] is None:
                busy["reply_wait_since"] = time.monotonic()
                asyncio.ensure_future(ensure_tool_reply(busy["reply_wait_since"]))

        async def ensure_tool_reply(since: float) -> None:
            """Every tool call must end with a spoken result. LiveKit drops the tool reply when the turn that
            issued the call was interrupted, so if no reply generation starts in time, request one."""
            grace, quiet_since = 3.0, time.monotonic()
            while True:
                await asyncio.sleep(0.25)
                if busy["reply_wait_since"] != since or self.ended.is_set():
                    return
                now = time.monotonic()
                if busy["last_gen"] > since:              # the tool-result reply has been generated
                    busy["reply_wait_since"] = None; return
                if busy["tools"] or out.playing(now) or session.agent_state == "thinking":
                    quiet_since = now                    # tool still running / preamble still playing
                    continue
                if now - quiet_since >= grace:
                    log.warning("no spoken reply after tool call -> requesting one")
                    try:
                        session.generate_reply(instructions="Briefly tell the user the result of the tool call "
                                                            "you just made, in one short sentence.")
                    except Exception as e:
                        log.warning("generate_reply failed: %r", e)
                    busy["reply_wait_since"] = None
                    return

        @session.on("close")
        def _cl(ev):
            self.end("session_closed")

        async def reader_loop():
            if first[0] != "grok-hello":
                handle(first)
            while True:
                ev = await read_event(self.reader)
                if ev is None:
                    self.end("client_closed"); return
                if not handle(ev):
                    self.end("client_stop"); return

        def handle(ev) -> bool:
            nonlocal dump
            etype, data, payload = ev
            if etype == "audio-start":
                in_fmt.update({k: data[k] for k in ("rate", "width", "channels") if k in data})
                log.info("device audio format: %s", in_fmt)
            elif etype == "audio-chunk" and payload:
                rate = int(data.get("rate", 16000)); ch = int(data.get("channels", 1))
                if int(data.get("width", 2)) != 2 or len(payload) % (2 * ch):
                    log.warning("dropping audio-chunk with unsupported format %s (%d B)", data, len(payload))
                    return True
                in_stats.add(payload)
                line = in_stats.window_line()
                if line:
                    log.debug(line)
                if dump is not None:
                    part = payload[: dump["left"]]
                    dump["w"].writeframes(part); dump["left"] -= len(part)
                    if dump["left"] <= 0:
                        dump["w"].close(); log.info("audio dump written: %s", dump["path"]); dump = None
                if gain != 1.0:
                    x = np.frombuffer(payload, dtype="<i2").astype(np.float32) * gain
                    payload = np.clip(x, -32768, 32767).astype("<i2").tobytes()
                if ec is not None and rate == 16000 and ch == 1:
                    payload = ec.process(time.monotonic(), payload)   # remove Grok's own voice
                n = len(payload) // (2 * ch)
                fr = rtc.AudioFrame(payload, rate, ch, n)
                xx = np.frombuffer(payload, dtype="<i2").astype(np.float32)
                echo["mic_db"] = 20 * math.log10(float(np.sqrt(np.mean(xx * xx))) / 32768.0 + 1e-6)
                if (gate["open"] and use_gate and out.playing(time.monotonic(), tail_s)
                        and (not echo["barging"] or out.seg_t0 > echo["barge_t"] + 0.2)):
                    gate["open"] = False; echo["barging"] = False   # Grok is talking: shut the gate until a barge-in
                    log.debug("VAD gate closed (agent audio playing)")
                if vad_stream is not None:
                    vad_stream.push_frame(fr)
                if gate["open"]:
                    inp.push(fr)
                else:
                    preroll.append(fr)
                    inp.push(rtc.AudioFrame(bytes(len(payload)), rate, ch, n))
            elif etype == "audio-stop":
                return False
            return True

        # Barge-in: xAI's server VAD does interrupt on its own (~0.85 s in tests), but LiveKit ignores a
        # local VAD while the realtime model does turn detection. For a faster cut-off (~0.3 s) the
        # bridge runs its own Silero VAD on the (XMOS echo-cancelled) mic audio and interrupts itself.
        vad_stream = _vad().stream()

        def open_gate(why: str, keep: int | None = None) -> None:
            if gate["open"]:
                return
            gate["open"] = True; gate["opened"] += 1
            frames = list(preroll)[-keep:] if keep else list(preroll)
            for f in frames:                     # replay the onset Silero needed to decide
                inp.push(f)
            log.debug("VAD gate open (%s, pre-roll %d ms)", why, len(preroll) * 32)
            preroll.clear()

        def tool_busy() -> bool:
            return bool(busy["tools"]) or busy["reply_wait_since"] is not None or hang["at"] is not None

        thr = float(o["barge_threshold"]); margin = float(o["barge_margin_db"])
        min_frames = max(1, int(o["barge_min_speech_ms"]) // 32)

        async def barge_loop():
            from livekit.agents import vad as _v
            async for vev in vad_stream:
                now = time.monotonic()
                if vev.type == _v.VADEventType.START_OF_SPEECH:
                    echo["speaking"] = True
                    echo["start_in_echo"] = out.playing(now, tail_s)
                elif vev.type == _v.VADEventType.END_OF_SPEECH:
                    echo["speaking"] = False; echo["start_in_echo"] = False
                if vev.type != _v.VADEventType.INFERENCE_DONE:
                    log.debug("local VAD: %s (agent_state=%s, playing=%s)", vev.type, session.agent_state,
                              out.playing(now, tail_s))
                    if vev.type == _v.VADEventType.END_OF_SPEECH and gate["open"] and use_gate:
                        gate["open"] = False
                        log.debug("VAD gate closed")
                    continue
                p, mic = float(vev.probability), echo["mic_db"]
                in_echo = out.playing(now, tail_s)
                expected = em.expected(now)
                excess = mic - expected
                if not in_echo and not echo["start_in_echo"]:
                    if p < 0.3:                          # track the noise floor between utterances
                        em.noise += (mic - em.noise) * (0.05 if mic < em.noise else 0.01)
                    echo["cand"].clear()
                    if use_gate and echo["speaking"] and not gate["open"]:
                        open_gate("speech")
                    continue
                if in_echo:
                    em.add(now, mic)
                # the first ~0.8 s of each reply: the echo delay jumps by tens of ms between replies, so until the
                # canceller has re-locked (and the XMOS AEC re-converged) residual echo is higher: be stricter
                m_eff = margin + (float(o["barge_onset_extra_db"]) if now - out.seg_t0 < 0.8 else 0.0)
                if p >= thr:
                    echo["cand"].append((p, excess, m_eff)); echo["low"] = 0
                else:
                    echo["low"] += 1
                    if echo["low"] >= 2:                 # tolerate one-frame dips
                        if len(echo["cand"]) >= 3:
                            echo["rejected"] += 1
                            w = echo["cand"][-min_frames:]
                            frac = sum(1 for x in w if x[1] >= x[2]) / len(w)
                            echo["max_rej"] = max(echo["max_rej"], frac)
                            log.debug("echo candidate rejected: %d frames, %.0f%% above echo+margin, median %.1f dB",
                                      len(echo["cand"]), 100 * frac, float(np.median([x[1] for x in w])))
                        echo["cand"].clear()
                if len(echo["cand"]) < min_frames:
                    continue
                win = echo["cand"][-min_frames:]
                mean_p = sum(x[0] for x in win) / len(win)
                med_ex = float(np.median([x[1] for x in win]))
                frac = sum(1 for x in win if x[1] >= x[2]) / len(win)
                # user speech fills the syllable gaps of Grok's echo: require enough frames well above the
                # predicted echo, not just a louder average
                if frac < float(o["barge_frac"]):
                    continue
                if tool_busy():
                    log.debug("barge-in suppressed: tool call / tool reply / hang-up pending")
                    echo["cand"].clear(); continue
                echo["cand"].clear(); echo["barging"] = True; echo["barge_t"] = now
                if not in_echo:                          # speech right after Grok stopped: just let it through
                    log.debug("speech confirmed after agent audio (%.1f dB over expected)", med_ex)
                    if use_gate:
                        open_gate("speech after agent audio", keep=min_frames + 4)
                    continue
                echo["fired"] += 1
                log.info("barge-in detected (p=%.2f, %.0f%% of frames above echo+margin, median %.1f dB over "
                         "expected echo %.1f dBFS) -> interrupting agent", mean_p, 100 * frac, med_ex, expected)
                if use_gate:
                    open_gate("barge-in", keep=min_frames + 4)
                if o["local_vad_barge_in"] and session.agent_state == "speaking":
                    self.send("grok-state", {"state": "interrupted"})
                    try:
                        await session.interrupt(force=True)
                    except Exception as e:
                        log.debug("interrupt: %s", e)

        btask = asyncio.ensure_future(barge_loop())
        if btask:
            btask.add_done_callback(lambda t: t.cancelled() or not t.exception() or
                                    log.error("barge loop failed: %r", t.exception()))
        rtask = asyncio.ensure_future(reader_loop())

        async def keepalive():
            # the device ends a session after `idle_timeout` (15 s) without bytes from the bridge
            while True:
                await asyncio.sleep(5)
                self.send("grok-ping", None)

        ktask = asyncio.ensure_future(keepalive())

        async def watchdog():
            idle = float(o["idle_timeout_s"])
            while True:
                await asyncio.sleep(0.5)
                now = time.monotonic()
                if hang["at"] is not None and now - hang["at"] > 8.0:
                    self.end("dismissed"); return
                active = (gate["open"] or echo["speaking"] or session.agent_state in ("speaking", "thinking")
                          or out.playing(now) or busy["tools"] or busy["reply_wait_since"] is not None
                          or session.user_state == "speaking")
                if active:
                    busy["last_act"] = now
                elif now - busy["last_act"] > idle:
                    self.end("idle"); return

        wtask = asyncio.ensure_future(watchdog())
        try:
            await session.start(agent)
            log.info("xAI session ready in %.2fs", time.monotonic() - t0)
            rt = getattr(getattr(session, "_activity", None), "realtime_llm_session", None)
            if rt is not None:
                rt.on("input_speech_started", lambda ev: log.info("xAI: input_audio_buffer.speech_started"))
                rt.on("input_speech_stopped", lambda ev: log.info("xAI: input_audio_buffer.speech_stopped"))
                rt.on("input_audio_transcription_completed",
                      lambda ev: log.info("xAI: input transcription: %s", getattr(ev, "transcript", "")))
                def _gen(ev):
                    busy["last_gen"] = time.monotonic(); busy["last_act"] = busy["last_gen"]
                    log.debug("xAI: response generation created")
                rt.on("generation_created", _gen)
                rt.on("error", lambda ev: log.warning("xAI error: %r", getattr(ev, "error", ev)))
            else:
                log.debug("realtime session handle not available for event hooks")
            try:
                await asyncio.wait_for(self.ended.wait(), timeout=float(o["max_session_s"]))
            except asyncio.TimeoutError:
                self.end("max_duration")
        except (ProtocolError, ValueError) as e:
            log.warning("protocol error from %s: %s", self.peer, e); self.end("protocol_error")
        finally:
            rtask.cancel(); ktask.cancel(); wtask.cancel()
            ECHO_STATE[self.peer] = (em.lag, em.coupling, em.noise)
            if btask:
                btask.cancel()
                await vad_stream.aclose()
            inp.push(None)
            try:
                self.send("grok-end", {"reason": self.end_reason})
                await self.writer.drain()
            except Exception:
                pass
            await out.aclose()
            if dump is not None:
                try:
                    dump["w"].close(); log.info("audio dump written (session ended early): %s", dump["path"])
                except Exception:
                    pass
            log.info("%s; format %s; input gain %+.1f dB; VAD gate %s (opened %d times)", in_stats.summary(), in_fmt,
                     float(o["input_gain_db"]), "on" if use_gate else "off", gate["opened"])
            if ec is not None and ec.debug is not None:
                np.save(os.environ["GROK_EC_DEBUG"] + f"_{int(time.time())}.npy",
                        np.array([np.concatenate([m, r]) for m, r in ec.debug if len(r) == 512 + 3200]))
                np.save(os.environ["GROK_EC_DEBUG"] + f"_res_{int(time.time())}.npy",
                        np.array([np.concatenate([[f], e, y]) for f, e, y in getattr(ec, "res_dbg", [])]))
                ec.res_dbg = []
            if ec is not None:
                log.info("echo canceller: bulk delay %s ms, ERLE %.1f dB (gcc peak %.0f)",
                         f"{ec.delay / 16:.0f}" if ec.delay is not None else "?", ec.erle_db, ec.gcc_peak)
            log.info("echo: lag %.0f ms, coupling %+.1f dB (fits %d, corr %.2f), noise floor %.1f dBFS; barge-ins %d, "
                     "echo candidates rejected %d (worst: %.0f%% of frames above echo+%.1f dB; needs %.0f%%)",
                     em.lag * 1000, em.coupling, em.fits, em.corr, em.noise, echo["fired"], echo["rejected"],
                     100 * max(0.0, echo["max_rej"]), margin, 100 * float(o["barge_frac"]))
            log.info("%s; audio from xAI %d B (24 kHz)", out.stats.summary(), out.from_xai_bytes)
            if eq is not None and (eq.limited_blocks or eq.clipped_samples):
                log.info("EQ limiter engaged on %d ms of audio, %d samples hard-clipped",
                         eq.limited_blocks * 1000 * eq.block // rate, eq.clipped_samples)
            try:
                await session.aclose()
            except Exception:
                log.exception("session close")
            log.info("session closed for %s: %s after %.1fs", self.peer, self.end_reason, time.monotonic() - t0)


async def startup_checks(opts: dict) -> None:
    """Read-only checks: xAI key valid (GET /v1/api-key) and HA MCP reachable (initialize + list tools)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.get("https://api.x.ai/v1/api-key",
                                headers={"Authorization": "Bearer " + opts["xai_api_key"]}) as r:
                if r.status == 200:
                    j = await r.json()
                    bad = j.get("api_key_blocked") or j.get("api_key_disabled") or j.get("team_blocked")
                    log.info("startup check xAI: %s", "FAIL (key blocked/disabled)" if bad else "OK")
                else:
                    log.error("startup check xAI: FAIL (HTTP %s)", r.status)
    except Exception as e:
        log.error("startup check xAI: FAIL (%r)", e)
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        url = opts["ha_url"].rstrip("/") + "/api/mcp"
        async with streamablehttp_client(url, headers={"Authorization": "Bearer " + opts["ha_token"]},
                                         timeout=10) as (rd, wr, _):
            async with ClientSession(rd, wr) as cs:
                await asyncio.wait_for(cs.initialize(), 15)
                tools = await asyncio.wait_for(cs.list_tools(), 15)
                log.info("startup check HA MCP (%s): OK, %d tools: %s", url, len(tools.tools),
                         ", ".join(t.name for t in tools.tools)[:400])
    except BaseException as e:  # noqa: BLE001 (anyio ExceptionGroup)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        log.error("startup check HA MCP: FAIL (%r)", e)


async def main() -> None:
    opts = settings.load()
    logging.basicConfig(level=getattr(logging, opts["log_level"].upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("livekit.agents").setLevel(os.environ.get("LIVEKIT_LOG_LEVEL", "WARNING"))
    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "mcp", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("livekit.plugins.openai").setLevel(os.environ.get("LIVEKIT_LOG_LEVEL", "WARNING"))
    log.info("options: %s", settings.redacted(opts))
    global EQ_PRESET
    preset = opts["eq_preset_file"] or os.path.join(os.path.dirname(os.path.abspath(__file__)), "eq", "satellite1_eq_preset.txt")
    with open(preset) as f:
        EQ_PRESET = f.read()
    if opts["eq_enabled"]:
        e = OutputEQ(EQ_PRESET, int(opts["output_rate"]))
        log.info("output EQ on @ %d Hz: preamp %+.1f dB, %s; limiter ceiling -1 dBFS", e.fs, e.preamp_db,
                 ", ".join(f"{k} {fc:g}Hz {g:+g}dB Q{q:g}" for k, fc, g, q in e.filters))
    else:
        log.info("output EQ off; output %d Hz", int(opts["output_rate"]))
    if opts["startup_checks"]:
        await startup_checks(opts)
    active: dict[str, asyncio.Task] = {}

    async def on_client(reader, writer):
        peer = writer.get_extra_info("peername")[0]
        sock = writer.get_extra_info("socket")
        try:
            import socket as _s; sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
        except Exception:
            pass
        if peer in active and not active[peer].done():
            active[peer].cancel()          # a new wake from the same device replaces the old session
        conn = Connection(opts, reader, writer)
        active[peer] = asyncio.current_task()
        try:
            await conn.run()
        except (asyncio.TimeoutError, ProtocolError, ConnectionError) as e:
            log.warning("client %s: %s", peer, e)
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(on_client, "0.0.0.0", int(opts["port"]), limit=8192)
    log.info("listening on :%s", opts["port"])
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
