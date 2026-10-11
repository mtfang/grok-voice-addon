"""Wyoming-style framing: one JSON header line, then `data_length` bytes of JSON data,
then `payload_length` bytes of binary payload. Compatible with Wyoming 1.x peers."""
from __future__ import annotations
import asyncio, json

MAX_HEADER = 4096
MAX_DATA = 16 * 1024
MAX_PAYLOAD = 64 * 1024


class ProtocolError(Exception):
    pass


async def read_event(reader: asyncio.StreamReader) -> tuple[str, dict, bytes] | None:
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError as e:
        raise ProtocolError("header too long") from e
    if len(line) > MAX_HEADER:
        raise ProtocolError("header too long")
    hdr = json.loads(line)
    if not isinstance(hdr, dict) or not isinstance(hdr.get("type"), str):
        raise ProtocolError("bad header")
    data = hdr.get("data") or {}
    dlen, plen = int(hdr.get("data_length") or 0), int(hdr.get("payload_length") or 0)
    if not (0 <= dlen <= MAX_DATA and 0 <= plen <= MAX_PAYLOAD):
        raise ProtocolError("length out of range")
    if dlen:
        data = {**data, **json.loads(await reader.readexactly(dlen))}
    payload = await reader.readexactly(plen) if plen else b""
    return hdr["type"], data, payload


def encode_event(etype: str, data: dict | None = None, payload: bytes = b"") -> bytes:
    hdr: dict = {"type": etype, "version": "1.5.0"}
    body = b""
    if data:
        body = json.dumps(data, separators=(",", ":")).encode()
        hdr["data_length"] = len(body)
    if payload:
        hdr["payload_length"] = len(payload)
    return json.dumps(hdr, separators=(",", ":")).encode() + b"\n" + body + payload
