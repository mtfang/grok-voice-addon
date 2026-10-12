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
import asyncio, collections, datetime, hmac, json, logging, math, os, time, wave
import numpy as np
from livekit import rtc
from livekit.agents import Agent, AgentSession, function_tool, mcp
from livekit.agents.voice import io
from livekit.plugins import xai

from protocol import read_event, encode_event, ProtocolError
from output_eq import OutputEQ
from echo_cancel import EchoCanceller
import settings
import memory
import ha_tools

HA: "ha_tools.HAClient | None" = None          # bridge-local HA read/automation tools, see ha_tools.py
HTTP: "object | None" = None                  # one shared aiohttp session for the xAI realtime plugin (no leaks)

MEM_STORE: "memory.MemoryStore | None" = None   # conversation memory (markdown files), see memory.py

log = logging.getLogger("grok-bridge")
import contextvars
# Which device a log line belongs to: set per connection; asyncio tasks (incl. LiveKit's) inherit it.
DEVICE: contextvars.ContextVar[str] = contextvars.ContextVar("grok_device", default="-")


class _ServerToolLog(logging.Filter):
    """The xAI plugin warns 'unknown function tool: web_search' when xAI runs one of its own server-side tools;
    that is expected: log it as the tool call it is."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg.startswith("unknown function tool: ") and msg.split(": ", 1)[1].split(",")[0] in ("web_search", "x_search"):
            record.levelno, record.levelname = logging.INFO, "INFO"
            record.msg, record.args = "xAI server tool call: %s (run by xAI)", (msg.split(": ", 1)[1].split(",")[0],)
        return True


class _DeviceTag(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.device = DEVICE.get()
        return True
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
        self.seg_n = 0                                     # audio-start events sent (matches the device's count)
        self.seg_pos = 0                                   # samples of the current reply sent so far
        self.seg_chunks: list[tuple[int, float]] = []      # (reply sample pos, t_play) of this reply's chunks
        self.ref_shift = 0.0                               # correction from the device's grok-played markers
        self._mk_seg, self._mk = -1, []                    # marker smoothing (see device_played)
        self._task = asyncio.ensure_future(self._pump())

    def env_db(self, t0: float, t1: float) -> float:
        """Loudest output chunk whose estimated play time falls in [t0, t1] (-120 if none)."""
        return max((db for t, db in self.timeline if t0 <= t <= t1), default=-120.0)

    def device_played(self, seg: int, pos: int, t_true: float) -> float | None:
        """Device marker: reply sample `pos` of reply `seg` entered the speaker at bridge time t_true.
        Shift the echo reference of this reply (already sent and future chunks) to match. Returns the shift."""
        if seg != self.seg_n or not self.seg_chunks:
            return None
        p0, t0 = max(((p, t) for p, t in self.seg_chunks if p <= pos), default=self.seg_chunks[0])
        t_est = t0 + (pos - p0) / self.rate
        delta = t_true - t_est
        if abs(delta) > 1.0:
            return delta
        # The real device sends a marker after every short speaker idle (every ~40 ms during a reply), each
        # with +-60 ms of timing jitter. Applying each one would shake the echo reference around: use the
        # first marker of a reply as is, then only move on a consistent offset (median of the last 7).
        if self._mk_seg != seg:
            self._mk_seg, self._mk = seg, []
        else:
            self._mk.append(delta)
            if len(self._mk) < 7:
                return None
            delta = float(np.median(self._mk)); self._mk = []
            if abs(delta) < 0.010:
                return delta
        if abs(delta) < 0.002:
            return delta
        self.ref_shift += delta
        self.seg_chunks = [(p, t + delta) if p >= p0 else (p, t) for p, t in self.seg_chunks]
        self.timeline = collections.deque(((t + delta) if t >= t_est - 0.001 else t, db) for t, db in self.timeline)
        self.timeline = collections.deque(self.timeline, maxlen=400)
        if self._ec is not None:
            self._ec.move(t_est, delta)
        return delta

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
                if self._eq is None and getattr(self, "gain", 1.0) < 1.0:
                    pcm = (np.frombuffer(pcm, "<i2").astype(np.float32) * self.gain).astype("<i2").tobytes()
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
                self.seg_n += 1; self.seg_pos = 0; self.seg_chunks = []; self.ref_shift = 0.0
                if self._ec is not None:
                    self._ec.new_segment(self._seg_start)
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
            t_play = self._seg_start + self._sent_s + self.ref_shift
            self.seg_chunks.append((self.seg_pos, t_play))
            self.seg_pos += len(pcm) // 2
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


class FilteredMCPToolset(mcp.MCPToolset):
    """HA MCP tools minus the ones listed in `hidden_tools` (e.g. a broken web-search script that xAI's own
    web search replaces)."""

    def __init__(self, *a, hidden: set[str] | None = None, pattern: str = "", **kw) -> None:
        super().__init__(*a, **kw)
        self._hidden = hidden or set()
        import re
        self._pat = re.compile(pattern, re.I) if pattern else None

    async def setup(self, *, reload: bool = False):
        await super().setup(reload=reload)
        from livekit.agents.llm.tool_context import get_raw_function_info
        keep, hid = [], []
        for t in self._tools:
            n = get_raw_function_info(t).name
            (hid if (n in self._hidden or (self._pat and self._pat.search(n))) else keep).append(t)
        if hid:
            log.info("HA tools hidden from the live agent (non-home): %s",
                     ", ".join(get_raw_function_info(t).name for t in hid))
        self._tools = keep
        return self


def build_session(opts: dict, extra_tools: list | None = None, extra_instructions: str = "") -> tuple[AgentSession, Agent]:
    llm = xai.realtime.RealtimeModel(model=opts["model"], voice=opts["voice"], api_key=opts["xai_api_key"],
                                     **({"http_session": HTTP} if HTTP is not None else {}))
    ha_mcp = ha_tools.clean_mcp_server_class(mcp.MCPServerHTTP)(
        url=opts["ha_url"].rstrip("/") + "/api/mcp", transport_type="streamable_http",
        headers={"Authorization": "Bearer " + opts["ha_token"]},
        timeout=10, client_session_timeout_seconds=20,
    )
    # XMOS does hardware AEC on the device, so LiveKit's software AEC warm-up (which blocks barge-in for
    # the first 3 s of each reply) is shortened.
    session = AgentSession(llm=llm, user_away_timeout=None, max_tool_steps=int(opts["max_tool_steps"]),
                           aec_warmup_duration=float(opts["aec_warmup_s"]) or None)
    tools: list = [FilteredMCPToolset(id="home_assistant", mcp_server=ha_mcp, hidden=set(opts["hidden_tools"]),
                                      pattern=opts["hidden_tool_pattern"])]
    if opts["web_search"]:
        tools.append(xai.realtime.WebSearch())        # xAI server-side search (no client round trip)
    if opts["x_search"]:
        tools.append(xai.realtime.XSearch())
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo(opts["timezone"]))
        when = now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z") + f" ({opts['timezone']})"
    except Exception:
        when = datetime.datetime.now().astimezone().strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
    instructions = opts["instructions"] + f" The current local date and time at the start of this conversation is {when}."
    instructions += extra_instructions
    log.info("xAI server tools: %s", ", ".join(n for n, on in (("web_search", opts["web_search"]),
                                                                  ("x_search", opts["x_search"])) if on) or "none")
    agent = Agent(instructions=instructions, tools=[*tools, *(extra_tools or [])])
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
        self.peak = -40.0                # loudest fitted coupling (how loud the speaker has been), see barge-in

    def out_at(self, t: float) -> float:
        # chunk with play time tp covers [tp, tp + 40 ms]; widen by one frame for timing slop
        return self.out.env_db(t - 0.040 - self.FR, t + self.FR)

    def onset_expected(self, t: float, t0: float) -> float:
        """Reply onset: the device starts playing up to a few hundred ms off the estimated time, and the XMOS AEC
        has to re-converge (measured on ch1: echo at about 0 dB coupling vs -10..-13 dB steady state for ~1 s).
        Predict from the loudest output anywhere in the reply so far (plus what was already sent), ignoring lag."""
        o = self.out.env_db(t0 - 0.05, t + 0.4)
        return 10 * math.log10(10 ** (self.noise / 10) + 10 ** ((o + max(self.coupling, self.peak)) / 10))

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
        self.peak = max(self.peak, self.coupling)
        log.debug("echo model fit: lag %.0f ms, coupling %+.1f dB (corr %.2f, %d frames)",
                  self.lag * 1000, self.coupling, best, int(m.sum()))


ECHO_STATE: dict[str, tuple[float, float, float]] = {}   # device -> (lag s, coupling dB, noise floor dBFS)
ECHO_CANCELLERS: dict[str, EchoCanceller] = {}            # device -> canceller (learned echo path survives sessions)
ACTIVE: dict[str, asyncio.Task] = {}
ECHO_PEAK: dict[str, float] = {}                          # device -> loudest echo coupling seen (dB)
_LIM_KNEE = 0.5                                           # input soft limiter knee (-6 dBFS)


def _write_ring(d: str, device: str, blocks: list) -> None:
    """Debug ring -> 4-channel 16 kHz WAV: ch1 raw device input, ch2 after gain + limiter, ch3 echo-cancelled
    (what Silero and xAI get), ch4 echo-aligned reference (Grok's output as the canceller sees it)."""
    try:
        os.makedirs(d, exist_ok=True)
        chans = [np.concatenate([np.frombuffer(b[i], "<i2") for b in blocks]) for i in range(4)]
        n = min(len(c) for c in chans)
        inter = np.stack([c[:n] for c in chans], axis=1).astype("<i2")
        path = os.path.join(d, time.strftime("ring_%Y%m%d_%H%M%S") + f"_{device}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(4); w.setsampwidth(2); w.setframerate(16000); w.writeframes(inter.tobytes())
        log.info("debug ring written: %s (%.1f s; raw, gained, cleaned, reference)", path, n / 16000)
    except Exception as e:
        log.warning("debug ring not written: %r", e)


ECHO_STATE_FILE = os.environ.get("GROK_ECHO_STATE_FILE", "/data/echo_state.json")


def _load_echo_state() -> None:
    """Learned echo lag/coupling/noise per device survive add-on restarts (the first reply after a restart
    otherwise starts from the defaults, which made barge-in impossible until the echo model re-fitted)."""
    try:
        with open(ECHO_STATE_FILE) as f:
            d = json.load(f)
        for dev, v in d.items():
            ECHO_STATE[dev] = (float(v["lag"]), float(v["coupling"]), float(v["noise"]))
            ECHO_PEAK[dev] = float(v.get("peak", -40.0))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("echo state %s not loaded: %r", ECHO_STATE_FILE, e)


def _save_echo_state() -> None:
    try:
        d = {dev: {"lag": s[0], "coupling": s[1], "noise": s[2], "peak": ECHO_PEAK.get(dev, -40.0)}
             for dev, s in ECHO_STATE.items()}
        tmp = ECHO_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, ECHO_STATE_FILE)
    except Exception as e:
        log.debug("echo state not saved: %r", e)


class WakeArbiter:
    """Several Satellites hearing the same "Hey Grok" (open floor plan) must not all answer. The first hello
    opens a short window; every device that wakes within it joins the group, and when the window closes the
    best one (loudest wake word at its mic, i.e. probably the closest; ties/missing -> first arrival) keeps its
    session and the others get a clean `grok-stand-down`. A wake after the window is an independent session."""

    def __init__(self) -> None:
        self.group: dict | None = None

    async def claim(self, conn: "Connection", score: float | None, window: float) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        g = self.group
        if g is None or now - g["t0"] > window:
            g = self.group = {"t0": now, "members": [], "done": loop.create_future(), "winner": None}
            loop.call_later(window, self._decide, g)
        if g["done"].done():
            return False, "late"
        g["members"].append((conn, score, len(g["members"]), now))
        await asyncio.shield(g["done"])
        if len(g["members"]) > 1:
            desc = ", ".join(f"{c.device} {'%.1f dBFS' % s if s is not None else 'n/a'} +{(t - g['t0']) * 1000:.0f} ms"
                             for c, s, _, t in g["members"])
        else:
            desc = ""
        return g["winner"] is conn, desc

    def _decide(self, g: dict) -> None:
        ms = g["members"]
        scored = [m for m in ms if m[1] is not None]
        if len(scored) == len(ms) and ms:
            best = max(ms, key=lambda m: (m[1], -m[2]))       # loudest wake, earliest on a tie
        else:
            best = min(ms, key=lambda m: m[2]) if ms else None # fall back to first arrival
        g["winner"] = best[0] if best else None
        if not g["done"].done():
            g["done"].set_result(True)


ARBITER = WakeArbiter()


class Connection:
    def __init__(self, opts, reader, writer) -> None:
        self.opts, self.reader, self.writer = opts, reader, writer
        self.peer = writer.get_extra_info("peername")[0]
        self.device = self.peer
        self.device_stop_reason = ""
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
        import re as _re
        self.device = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(first[1].get("device") or self.peer))[:64]
        DEVICE.set(self.device)
        win = float(o["arbitration_window_ms"]) / 1000.0
        if win > 0:
            sc = first[1].get("wake_rms_db")
            try:
                sc = float(sc) if sc is not None else None
            except (TypeError, ValueError):
                sc = None
            won, desc = await ARBITER.claim(self, sc, win)
            if desc:
                log.info("wake arbitration (%s): %s", desc, "this device answers" if won else "standing down")
            if not won:
                self.send("grok-stand-down", {"reason": "other_device"})
                try:
                    await asyncio.wait_for(self.writer.drain(), 2)
                except Exception:
                    pass
                self.end_reason = "stand_down"
                return
        # one session per device: a new wake from the same device replaces its old session; other devices'
        # sessions are untouched (state below is keyed by device name, not by a single global)
        old = ACTIVE.get(self.device)
        if old is not None and old is not asyncio.current_task() and not old.done():
            log.info("replacing the previous session of %s", self.device)
            old.cancel()
        ACTIVE[self.device] = asyncio.current_task()
        log.info("session open for %s (%s); %d active session(s)", self.peer, self.device,
                 sum(1 for t in ACTIVE.values() if not t.done()))
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

        mem = memory.SessionMemory(MEM_STORE, o, self.device) if MEM_STORE is not None else None
        mem_text = ""
        if mem is not None:
            try:
                ctx = MEM_STORE.prompt_context()
            except Exception:
                log.exception("memory context"); ctx = ""
            mem_text = memory.MEMORY_GUIDE + (" " + ctx if ctx else "")
        ha_extra = ha_tools.make_tools(HA) if HA is not None else []
        session, agent = build_session(o, [hang_up, *(mem.tools() if mem else []), *ha_extra],
                                       mem_text + (ha_tools.HA_GUIDE if ha_extra else ""))
        inp = QueueAudioInput()
        in_stats = AudioStats("in<-device")
        in_fmt = {"rate": 16000, "width": 2, "channels": 1}
        gain = 10 ** (float(o["input_gain_db"]) / 20.0)
        use_lim = bool(o["input_limiter"])
        ring_n = int(float(o["debug_ring_s"]) * 16000 / 512)
        # debug ring: (raw in, after gain+limiter, echo-cancelled, echo-aligned reference) per mic block, int16
        ring: collections.deque | None = collections.deque(maxlen=ring_n) if ring_n > 0 else None
        sil = {"raw": None, "t": 0.0, "raw_max": 0.0, "clean_max": 0.0, "raw_db": -120.0, "clean_db": -120.0,
               "clipped": 0, "limited": 0}
        # VAD gate: the Satellite1's XMOS ASR channel is auto-gained, so between utterances the room noise floor
        # sits around -25 dBFS RMS. xAI's server VAD can take that for ongoing speech and never end the turn (seen
        # on the device: speech_started, transcripts, but no speech_stopped / no reply). The bridge runs Silero
        # locally and sends xAI digital silence while Silero hears no speech, with a pre-roll so onsets aren't cut.
        use_gate = bool(o["vad_gate"])
        gate = {"open": not use_gate, "opened": 0}
        # Echo-aware barge-in. While Grok's audio is playing (and `echo_tail_ms` after), residual speaker echo
        # in the XMOS-processed mic signal (on AGC'd ch0 about -17..-22 dBFS RMS, as loud as the user's own speech;
        # on ch1 without AGC the coupling is about -13 dB, 0.4.3 retune) is loud enough for Silero to call speech. So
        # in that window the gate toward xAI stays shut and the bridge only interrupts when Silero is confident
        # for `barge_min_speech_ms` AND the mic is `barge_margin_db` above the expected echo (output envelope at
        # the estimated play time + a learned echo coupling) and above the noise floor.
        echo = {"cand": [], "low": 0, "rejected": 0,
                "fired": 0, "speaking": False, "mic_db": -120.0, "barging": False, "barge_t": 0.0,
                "start_in_echo": False, "max_rej": -99.0, "rej_short": 0, "rej_echo": 0}
        tail_s = float(o["echo_tail_ms"]) / 1000.0
        busy = {"tools": set(), "reply_wait_since": None, "last_gen": 0.0, "last_act": time.monotonic()}
        preroll: collections.deque = collections.deque(maxlen=max(1, int(o["vad_gate_preroll_ms"]) // 32))
        onset_buf: collections.deque = collections.deque(maxlen=63)   # (t, frame), ~2 s of cleaned mic
        dump = None
        if int(o["debug_dump_s"]) > 0:
            try:
                os.makedirs(o["debug_dump_dir"], exist_ok=True)
                path = os.path.join(o["debug_dump_dir"], time.strftime("inbound_%Y%m%d_%H%M%S") + f"_{self.device}.wav")
                dump = {"path": path, "w": wave.open(path, "wb"), "left": int(o["debug_dump_s"]) * 16000 * 2}
                dump["w"].setnchannels(1); dump["w"].setsampwidth(2); dump["w"].setframerate(16000)
                log.info("dumping first %ss of inbound audio (raw, before gain) to %s", o["debug_dump_s"], path)
            except Exception as e:
                log.warning("cannot open audio dump: %s", e); dump = None
        rate = int(o["output_rate"])
        eq = (OutputEQ(EQ_PRESET, rate, ceiling_db=float(o["eq_ceiling_db"]), preamp_db=o["eq_preamp_db"])
              if o["eq_enabled"] else None)
        ec = None
        if o["echo_cancel"]:
            ec = ECHO_CANCELLERS.get(self.device)
            if ec is None or ec.dec != max(1, rate // 16000):
                ec = ECHO_CANCELLERS[self.device] = EchoCanceller(out_rate=rate)
            ec.new_session()
            if os.environ.get("GROK_EC_DEBUG"):
                ec.debug = []
        # keep the speaker linear at high volume: Grok plays at most `playback_volume_cap` of full volume
        # (the device reports its media player volume in the hello; the cap is applied digitally)
        vol = first[1].get("volume")
        try:
            vol = float(vol) if vol is not None else -1.0
        except (TypeError, ValueError):
            vol = -1.0
        cap = float(o["playback_volume_cap"])
        out_gain = min(1.0, cap / vol) if (vol > 0 and cap > 0) else 1.0
        if out_gain < 1.0:
            log.info("device volume %.2f > cap %.2f: Grok's audio %.1f dB", vol, cap, 20 * math.log10(out_gain))
        if eq is not None:
            eq.pre *= out_gain
        out = DeviceAudioOutput(self.send, lead_s=o["out_lead_ms"] / 1000.0, device_rate=rate, eq=eq, ec=ec)
        out.gain = out_gain
        session.input.audio, session.output.audio = inp, out
        lag0, coup0, noise0 = ECHO_STATE.get(self.device, (0.15, float(o["barge_echo_init_db"]),
                                                            float(o["barge_noise_init_db"])))
        em = EchoModel(out, lag0, coup0, noise0)
        em.peak = ECHO_PEAK.get(self.device, -40.0) - 2.0   # last session's loudness, decayed a little

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
                busy["tool_reply_t"] = time.monotonic()  # protect its start from echo barge-in
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
                if mem is not None:
                    mem.add(it.role, it.text_content)
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
                    self.end(f"client_stop ({self.device_stop_reason or 'no reason given'})"); return

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
                raw_pl = payload
                if gain != 1.0 or use_lim:
                    x = np.frombuffer(payload, dtype="<i2").astype(np.float32) * (gain / 32768.0)
                    a = np.abs(x)
                    if use_lim and float(a.max()) > _LIM_KNEE:
                        # soft knee at -6 dBFS: smooth compression toward full scale instead of hard clipping
                        over = a > _LIM_KNEE
                        x[over] = np.sign(x[over]) * (_LIM_KNEE + (1 - _LIM_KNEE) *
                                                      np.tanh((a[over] - _LIM_KNEE) / (1 - _LIM_KNEE)))
                        sil["limited"] += int(over.sum())
                    sil["clipped"] += int((a >= 1.0).sum()) if not use_lim else 0
                    payload = (np.clip(x, -1.0, 32767 / 32768) * 32768).astype("<i2").tobytes()
                gained_pl = payload
                if ec is not None and rate == 16000 and ch == 1:
                    payload = ec.process(time.monotonic(), payload)   # remove Grok's own voice
                if ring is not None and rate == 16000 and ch == 1:
                    r = getattr(ec, "last_ref", None) if ec is not None else None
                    nn = len(payload) // 2
                    ref16 = (np.zeros(nn, "<i2") if r is None or len(r) != nn
                             else (np.clip(r, -1, 32767 / 32768) * 32768).astype("<i2"))
                    ring.append((raw_pl, gained_pl, payload, ref16.tobytes()))
                    now_s = time.monotonic()
                    if out.playing(now_s, tail_s):
                        g16 = np.frombuffer(gained_pl, "<i2").astype(np.float32) / 32768
                        c16 = np.frombuffer(payload, "<i2").astype(np.float32) / 32768
                        sil["raw_db"] = max(sil["raw_db"], 20 * math.log10(float(np.sqrt(np.mean(g16 ** 2))) + 1e-6))
                        sil["clean_db"] = max(sil["clean_db"], 20 * math.log10(float(np.sqrt(np.mean(c16 ** 2))) + 1e-6))
                        if now_s - sil["t"] >= 1.0:
                            log.debug("while playing (1 s): Silero max p %.2f; loudest frame gained %.1f / cleaned "
                                      "%.1f dBFS; limited %d samples", sil["clean_max"], sil["raw_db"], sil["clean_db"],
                                      sil["limited"])
                            sil.update(t=now_s, raw_max=0.0, clean_max=0.0, raw_db=-120.0, clean_db=-120.0, limited=0)
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
                onset_buf.append((time.monotonic(), fr))
                if gate["open"]:
                    inp.push(fr)
                else:
                    preroll.append(fr)
                    inp.push(rtc.AudioFrame(bytes(len(payload)), rate, ch, n))
            elif etype == "grok-played":
                if ec is not None and ec.Tm0 is not None:
                    t_true = ec.Tm0 + int(data.get("mic", 0)) / 16000.0
                    d = out.device_played(int(data.get("seg", -1)), int(data.get("pos", 0)), t_true)
                    if d is not None and abs(d) >= 0.002:
                        log.debug("device playback marker seg %s pos %s: reference shifted %+.0f ms", data.get("seg"),
                                  data.get("pos"), d * 1000)
            elif etype == "audio-stop":
                self.device_stop_reason = str((data or {}).get("reason") or "unspecified")[:40]
                return False
            return True

        # Barge-in: xAI's server VAD does interrupt on its own (~0.85 s in tests), but LiveKit ignores a
        # local VAD while the realtime model does turn detection. For a faster cut-off (~0.3 s) the
        # bridge runs its own Silero VAD on the (XMOS echo-cancelled) mic audio and interrupts itself.
        vad_stream = _vad().stream()

        def open_gate(why: str, keep: int | None = None, since: float | None = None) -> None:
            if gate["open"]:
                return
            gate["open"] = True; gate["opened"] += 1
            if since is not None:                # everything from the speech onset (minus a margin) onward
                frames = [f for t, f in onset_buf if t >= since]
            else:
                frames = list(preroll)[-keep:] if keep else list(preroll)
            for f in frames:                     # replay the onset Silero needed to decide
                inp.push(f)
            log.debug("VAD gate open (%s, pre-roll %d ms)", why, len(frames) * 32)
            preroll.clear()

        def tool_busy() -> bool:
            return (bool(busy["tools"]) or busy["reply_wait_since"] is not None or hang["at"] is not None
                    # the first seconds of a tool-result reply can't be cut by barge-in (the result must be heard)
                    or time.monotonic() - busy.get("tool_reply_t", -1e9) < float(o["tool_reply_protect_s"]))

        thr = float(o["barge_threshold"]); margin = float(o["barge_margin_db"])
        min_frames = max(1, int(o["barge_min_speech_ms"]) // 32)

        async def barge_loop():
            from livekit.agents import vad as _v
            async for vev in vad_stream:
                now = time.monotonic()
                if vev.type == _v.VADEventType.START_OF_SPEECH:
                    echo["speaking"] = True
                    # Silero reports START after min_speech_duration of speech: back-date to the onset
                    echo["onset_t"] = now - float(getattr(vev, "speech_duration", 0.15) or 0.15)
                    echo["start_in_echo"] = out.playing(now, tail_s)
                elif vev.type == _v.VADEventType.END_OF_SPEECH:
                    echo["speaking"] = False; echo["start_in_echo"] = False
                    echo["dt"] = False                   # double-talk over: let the echo filter adapt again
                    if ec is not None:
                        ec.freeze = False
                if vev.type != _v.VADEventType.INFERENCE_DONE:
                    log.debug("local VAD: %s (agent_state=%s, playing=%s)", vev.type, session.agent_state,
                              out.playing(now, tail_s))
                    if vev.type == _v.VADEventType.END_OF_SPEECH and gate["open"] and use_gate:
                        gate["open"] = False
                        log.debug("VAD gate closed")
                    continue
                p, mic = float(vev.probability), echo["mic_db"]
                in_echo = out.playing(now, tail_s)
                if in_echo:
                    sil["clean_max"] = max(sil["clean_max"], p)
                expected = em.expected(now)
                if now - out.seg_t0 < float(o["barge_onset_s"]):
                    expected = max(expected, em.onset_expected(now, out.seg_t0))
                excess = mic - expected
                if not in_echo and not echo["start_in_echo"]:
                    if ec is not None:
                        ec.freeze = False
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
                m_eff = margin + (float(o["barge_onset_extra_db"]) if now - out.seg_t0 < float(o["barge_onset_s"]) else 0.0)
                # loud playback (high volume: speaker/amp nonlinear, residual echo the linear canceller can't
                # remove): the closer the measured echo coupling gets to 0 dB, the stricter the barge-in.
                # Real interruptions on the device: 78-100% of frames, 8-15 dB over echo; self ones 44-67%, 4-5 dB.
                hv_lo, hv_hi = float(o["barge_hv_from_db"]), float(o["barge_hv_full_db"])
                # The coupling estimate decays toward -15 dB whenever the canceller removes the linear echo,
                # so use the loudest coupling fitted so far (this session or the device's last one), and be
                # fully strict at the start of each reply until the echo model has re-fitted on it.
                if echo.get("seg_seen") != out.seg_t0:
                    echo["seg_seen"] = out.seg_t0; echo["seg_fits0"] = em.fits
                converged = em.fits > echo["seg_fits0"] or now - out.seg_t0 > float(o["barge_strict_until_s"])
                hv = 1.0 if not converged else min(1.0, max(0.0, (max(em.coupling, em.peak) - hv_lo)
                                                            / max(0.1, hv_hi - hv_lo)))
                echo["hv"] = hv
                m_eff += hv * max(0.0, float(o["barge_hv_margin_db"]) - margin)
                if ec is not None:
                    # double-talk (barge-in confirmed while Grok's audio is still in the air): don't let the echo
                    # filter adapt on the user's voice, it would distort it. Not before the barge-in is confirmed:
                    # freezing on residual echo at a reply onset would stop the filter from re-converging.
                    ec.freeze = bool(echo.get("dt"))
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
                            short = len(echo["cand"]) < min_frames
                            echo["rej_short" if short else "rej_echo"] += 1
                            log.debug("echo candidate rejected (%s): %d frames, %.0f%% above echo+margin, median %.1f dB",
                                      "too short" if short else "not above echo", len(echo["cand"]), 100 * frac,
                                      float(np.median([x[1] for x in w])))
                        echo["cand"].clear()
                if len(echo["cand"]) < min_frames:
                    continue
                win = echo["cand"][-min_frames:]
                mean_p = sum(x[0] for x in win) / len(win)
                med_ex = float(np.median([x[1] for x in win]))
                frac = sum(1 for x in win if x[1] >= x[2]) / len(win)
                # user speech fills the syllable gaps of Grok's echo: require enough frames well above the
                # predicted echo, not just a louder average
                hv = echo.get("hv", 0.0)
                frac0 = max(float(o["barge_frac"]), float(o["barge_frac_floor"]))   # floor while Grok plays
                frac_req = frac0 + hv * max(0.0, float(o["barge_hv_frac"]) - frac0)
                if frac < frac_req or med_ex < hv * float(o["barge_hv_margin_db"]):
                    continue
                if tool_busy():
                    log.debug("barge-in suppressed: tool call / tool reply / hang-up pending")
                    echo["cand"].clear(); continue
                echo["cand"].clear(); echo["barging"] = True; echo["barge_t"] = now
                if not in_echo:                          # speech right after Grok stopped: just let it through
                    log.debug("speech confirmed after agent audio (%.1f dB over expected)", med_ex)
                    if use_gate:
                        open_gate("speech after agent audio", since=echo.get("onset_t", now - 0.6) - 0.3)
                    continue
                echo["fired"] += 1
                echo["dt"] = True                        # user talking over Grok's (still audible) audio
                log.info("barge-in detected (p=%.2f, %.0f%% of frames above echo+margin, median %.1f dB over "
                         "expected echo %.1f dBFS) -> interrupting agent", mean_p, 100 * frac, med_ex, expected)
                if use_gate:
                    open_gate("barge-in", since=echo.get("onset_t", now - 0.6) - 0.3)
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
            ECHO_STATE[self.device] = (em.lag, em.coupling, em.noise)
            ECHO_PEAK[self.device] = em.peak
            _save_echo_state()
            if ring:
                _write_ring(o["debug_dump_dir"], self.device, list(ring))
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
                     "echo candidates rejected %d (%d too short, %d not above echo; worst: %.0f%% of frames above echo+%.1f dB; "
                     "needs %.0f%%)",
                     em.lag * 1000, em.coupling, em.fits, em.corr, em.noise, echo["fired"], echo["rejected"],
                     echo["rej_short"], echo["rej_echo"],
                     100 * max(0.0, echo["max_rej"]), margin, 100 * max(float(o["barge_frac"]), float(o["barge_frac_floor"])))
            log.info("%s; audio from xAI %d B (24 kHz)", out.stats.summary(), out.from_xai_bytes)
            if eq is not None and (eq.limited_blocks or eq.clipped_samples):
                log.info("EQ limiter engaged on %d ms of audio, %d samples hard-clipped",
                         eq.limited_blocks * 1000 * eq.block // rate, eq.clipped_samples)
            try:
                await session.aclose()
            except Exception:
                log.exception("session close")
            log.info("session closed for %s: %s after %.1fs", self.peer, self.end_reason, time.monotonic() - t0)
            if mem is not None:
                memory.spawn(mem.finish())        # summary + markdown file, in the background


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
                import re as _re
                pat = _re.compile(opts["hidden_tool_pattern"], _re.I) if opts["hidden_tool_pattern"] else None
                hid = [t.name for t in tools.tools if t.name in set(opts["hidden_tools"]) or (pat and pat.search(t.name))]
                log.info("live agent tools: HA (home only) %d, hidden from the agent: %s; xAI server tools: %s",
                         len(tools.tools) - len(hid), ", ".join(hid) or "none",
                         ", ".join(n for n in ("web_search", "x_search") if opts[n]) or "none")
                log.info("live agent local tools: hang_up%s%s; max tool steps %d", ", recall_memory, remember" if opts["memory_enabled"] else "",
                         ", find_entities, get_entity, get_history, list_automations, run_automation, run_script, "
                         "toggle_automation" if opts["ha_local_tools"] else "", int(opts["max_tool_steps"]))
    except BaseException as e:  # noqa: BLE001 (anyio ExceptionGroup)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        log.error("startup check HA MCP: FAIL (%r)", e)


async def main() -> None:
    opts = settings.load()
    logging.basicConfig(level=getattr(logging, opts["log_level"].upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s [%(device)s] %(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(_DeviceTag())
    logging.getLogger("livekit.plugins.xai").addFilter(_ServerToolLog())
    logging.getLogger("livekit.agents").setLevel(os.environ.get("LIVEKIT_LOG_LEVEL", "WARNING"))
    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "mcp", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("livekit.plugins.openai").setLevel(os.environ.get("LIVEKIT_LOG_LEVEL", "WARNING"))
    log.info("options: %s", settings.redacted(opts))
    _load_echo_state()
    if ECHO_STATE:
        log.info("echo state restored for %s", ", ".join(f"{d} (coupling {s[1]:+.1f} dB, noise {s[2]:.1f} dBFS)"
                                                       for d, s in ECHO_STATE.items()))
    global EQ_PRESET
    preset = opts["eq_preset_file"] or os.path.join(os.path.dirname(os.path.abspath(__file__)), "eq", "satellite1_eq_preset.txt")
    with open(preset) as f:
        EQ_PRESET = f.read()
    if opts["eq_enabled"]:
        e = OutputEQ(EQ_PRESET, int(opts["output_rate"]), ceiling_db=float(opts["eq_ceiling_db"]),
                     preamp_db=opts["eq_preamp_db"])
        log.info("output EQ on @ %d Hz: preamp %+.1f dB, %s; limiter ceiling %.1f dBFS", e.fs, e.preamp_db,
                 ", ".join(f"{k} {fc:g}Hz {g:+g}dB Q{q:g}" for k, fc, g, q in e.filters), e.ceiling_db)
    else:
        log.info("output EQ off; output %d Hz", int(opts["output_rate"]))
    global HA, HTTP
    import aiohttp
    HTTP = aiohttp.ClientSession()
    if opts["ha_local_tools"]:
        HA = ha_tools.HAClient(opts["ha_url"], opts["ha_token"], opts["timezone"])
        try:
            idx = await HA.index()
            log.info("HA local tools: %d entities indexed (%d automations, %d scripts, %d with an area)", len(idx),
                     sum(e["domain"] == "automation" for e in idx), sum(e["domain"] == "script" for e in idx),
                     sum(bool(e["area"]) for e in idx))
        except Exception as e:
            log.warning("HA local tools: index check failed (%r); tools stay on and retry per call", e)
    if opts["memory_enabled"]:
        global MEM_STORE
        try:
            MEM_STORE = memory.MemoryStore(opts["memory_dir"], opts["timezone"], int(opts["memory_retention_days"]))
            n = len(MEM_STORE.conversations())
            log.info("memory on: %s (%d conversations, %d facts, keep %d days, summaries by %s)", opts["memory_dir"],
                     n, len(MEM_STORE.facts_tail(10_000)), MEM_STORE.retention_days, opts["memory_model"])
            asyncio.get_running_loop().create_task(memory.prune_loop(MEM_STORE))
        except Exception:
            log.exception("memory disabled: cannot use %s", opts["memory_dir"])
            MEM_STORE = None
    else:
        log.info("memory off")
    if opts["startup_checks"]:
        await startup_checks(opts)

    async def on_client(reader, writer):
        peer = writer.get_extra_info("peername")[0]
        DEVICE.set(peer)                   # refined to the device name once its hello arrives
        sock = writer.get_extra_info("socket")
        try:
            import socket as _s; sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
        except Exception:
            pass
        conn = Connection(opts, reader, writer)
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
