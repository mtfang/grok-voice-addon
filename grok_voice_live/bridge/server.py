"""Grok live voice bridge: a Wyoming-framed TCP server for Satellite1 devices.

Per connection (opened by the device after its local wake word):
  device -> bridge : [grok-hello {token,device}] audio-start, audio-chunk (PCM16 mono 16 kHz)..., audio-stop
  bridge -> device : grok-state {state}, audio-start {rate:48000}, audio-chunk (PCM16 mono 48 kHz, resampled
                     from xAI's 24 kHz with soxr HIGH and run through the Satellite1 EQ + limiter),
                     audio-stop, grok-clear (barge-in: flush speaker), transcript, grok-end {reason}
The bridge owns one LiveKit AgentSession (xAI realtime + HA MCP tools) per connection and hangs up after
`idle_timeout_s` of silence or `max_session_s` total, so xAI is only billed while someone is talking.
"""
from __future__ import annotations
import asyncio, collections, hmac, logging, math, os, time, wave
import numpy as np
from livekit import rtc
from livekit.agents import Agent, AgentSession, mcp
from livekit.agents.voice import io
from livekit.plugins import xai

from protocol import read_event, encode_event, ProtocolError
from output_eq import OutputEQ
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
                 eq: OutputEQ | None = None) -> None:
        super().__init__(label="device-speaker", capabilities=io.AudioOutputCapabilities(pause=False),
                         sample_rate=OUT_RATE)
        self._send, self._lead = send, lead_s
        # Satellite1 speaker pipeline (mixer -> I2S/TAS2780) runs at 48 kHz: send 48 kHz so the on-device
        # resampler is a pass-through, and resample here with soxr HIGH instead.
        self.rate = device_rate
        self._chunk = device_rate * 2 * 40 // 1000          # 40 ms of PCM16 mono
        self._fmt = {"rate": device_rate, "width": 2, "channels": 1}
        self._eq = eq                                      # per-session state (one DeviceAudioOutput per session)
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
        self._task = asyncio.ensure_future(self._pump())

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
        self._sent_s += len(pcm) / 2 / self.rate
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
        _VAD = silero.VAD.load(min_speech_duration=0.15, activation_threshold=0.6)
    return _VAD


def build_session(opts: dict) -> tuple[AgentSession, Agent]:
    llm = xai.realtime.RealtimeModel(model=opts["model"], voice=opts["voice"], api_key=opts["xai_api_key"])
    ha_mcp = mcp.MCPServerHTTP(
        url=opts["ha_url"].rstrip("/") + "/api/mcp", transport_type="streamable_http",
        headers={"Authorization": "Bearer " + opts["ha_token"]},
        timeout=10, client_session_timeout_seconds=20,
    )
    # XMOS does hardware AEC on the device, so LiveKit's software AEC warm-up (which blocks barge-in for
    # the first 3 s of each reply) is shortened.
    session = AgentSession(llm=llm, user_away_timeout=float(opts["idle_timeout_s"]),
                           aec_warmup_duration=float(opts["aec_warmup_s"]) or None)
    agent = Agent(instructions=opts["instructions"],
                  tools=[mcp.MCPToolset(id="home_assistant", mcp_server=ha_mcp)])
    return session, agent


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
        session, agent = build_session(o)
        inp = QueueAudioInput()
        in_stats = AudioStats("in<-device")
        in_fmt = {"rate": 16000, "width": 2, "channels": 1}
        gain = 10 ** (float(o["input_gain_db"]) / 20.0)
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
        out = DeviceAudioOutput(self.send, lead_s=o["out_lead_ms"] / 1000.0, device_rate=rate, eq=eq)
        session.input.audio, session.output.audio = inp, out

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
            if ev.new_state == "away":
                self.end("idle")

        @session.on("agent_state_changed")
        def _a(ev):
            self.send("grok-state", {"state": ev.new_state})

        @session.on("conversation_item_added")
        def _c(ev):
            it = ev.item
            if getattr(it, "type", None) == "message" and it.text_content:
                log.info("%s: %s", it.role, it.text_content)
                self.send("transcript", {"role": it.role, "text": it.text_content[:500]})

        @session.on("function_tools_executed")
        def _f(ev):
            for call, _ in ev.zipped():
                log.info("tool %s %s", call.name, call.arguments)

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
                n = len(payload) // (2 * ch)
                fr = rtc.AudioFrame(payload, rate, ch, n)
                inp.push(fr)
                if vad_stream is not None:
                    vad_stream.push_frame(fr)
            elif etype == "audio-stop":
                return False
            return True

        # Barge-in: xAI's server VAD does interrupt on its own (~0.85 s in tests), but LiveKit ignores a
        # local VAD while the realtime model does turn detection. For a faster cut-off (~0.3 s) the
        # bridge runs its own Silero VAD on the (XMOS echo-cancelled) mic audio and interrupts itself.
        vad_stream = _vad().stream() if o["local_vad_barge_in"] else None

        async def barge_loop():
            from livekit.agents import vad as _v
            async for vev in vad_stream:
                if vev.type != _v.VADEventType.INFERENCE_DONE:
                    log.debug("local VAD: %s (agent_state=%s)", vev.type, session.agent_state)
                if (vev.type == _v.VADEventType.START_OF_SPEECH and session.agent_state == "speaking"):
                    log.info("barge-in detected -> interrupting agent")
                    self.send("grok-state", {"state": "interrupted"})
                    try:
                        await session.interrupt(force=True)
                    except Exception as e:
                        log.debug("interrupt: %s", e)

        btask = asyncio.ensure_future(barge_loop()) if vad_stream else None
        if btask:
            btask.add_done_callback(lambda t: t.cancelled() or not t.exception() or
                                    log.error("barge loop failed: %r", t.exception()))
        rtask = asyncio.ensure_future(reader_loop())
        try:
            await session.start(agent)
            log.info("xAI session ready in %.2fs", time.monotonic() - t0)
            rt = getattr(getattr(session, "_activity", None), "realtime_llm_session", None)
            if rt is not None:
                rt.on("input_speech_started", lambda ev: log.info("xAI: input_audio_buffer.speech_started"))
                rt.on("input_speech_stopped", lambda ev: log.info("xAI: input_audio_buffer.speech_stopped"))
                rt.on("input_audio_transcription_completed",
                      lambda ev: log.info("xAI: input transcription: %s", getattr(ev, "transcript", "")))
                rt.on("generation_created", lambda ev: log.debug("xAI: response generation created"))
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
            rtask.cancel()
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
            log.info("%s; format %s; input gain %+.1f dB", in_stats.summary(), in_fmt, float(o["input_gain_db"]))
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
