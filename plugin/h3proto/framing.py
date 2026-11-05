"""Frames, bodies and the secure/plaintext channel (protocol.md §1, §3, §5) over asyncio streams."""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, Awaitable, Callable

from .noise import TAGLEN, Initiator, KeyPair, NoiseError, Responder, Transport

KIND_JSON = 0x01
KIND_BLOB = 0x02
MAX_FRAME = 65535
MAX_BODY_DATA = MAX_FRAME - TAGLEN - 1  # 65518
PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    """Malformed frame or message; the connection must be closed."""


# --- frames -----------------------------------------------------------------

async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one frame payload. Raises asyncio.IncompleteReadError on EOF."""
    (length,) = struct.unpack(">H", await reader.readexactly(2))
    return await reader.readexactly(length)


def frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"frame too large ({len(payload)} bytes)")
    return struct.pack(">H", len(payload)) + payload


# --- bodies -----------------------------------------------------------------

def encode_json(msg: dict[str, Any]) -> bytes:
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BODY_DATA:
        raise ProtocolError(f"json message too large ({len(data)} bytes)")
    return bytes([KIND_JSON]) + data


def _json_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def fit_reply(msg: dict[str, Any]) -> dict[str, Any]:
    """A list reply (`items`) cut down to fit one frame: oldest messages dropped from a history page (the app
    pages back with `before`), trailing items from other lists. Anything else is returned unchanged."""
    items = msg.get("items")
    total = _json_len(msg)
    if total <= MAX_BODY_DATA or not isinstance(items, list):
        return msg
    keep = list(items)
    from_front = msg.get("t") == "sessions.messages_r"
    while keep and total > MAX_BODY_DATA:
        total -= _json_len(keep.pop(0) if from_front else keep.pop()) + 1  # the item and its comma
    return {**msg, "items": keep, "cut": len(items) - len(keep)}


def encode_blob_chunk(blob_id: str, offset: int, data: bytes) -> bytes:
    bid = blob_id.encode("ascii")
    if not 0 < len(bid) < 256:
        raise ProtocolError("bad blob id")
    body = bytes([KIND_BLOB, len(bid)]) + bid + struct.pack(">I", offset) + data
    if len(body) - 1 > MAX_BODY_DATA:
        raise ProtocolError("blob chunk too large")
    return body


def max_blob_chunk(blob_id: str) -> int:
    return MAX_BODY_DATA - 1 - len(blob_id.encode("ascii")) - 4


def decode_body(body: bytes) -> tuple[int, Any]:
    """Returns (KIND_JSON, dict) or (KIND_BLOB, (blob_id, offset, data))."""
    if not body:
        raise ProtocolError("empty body")
    kind = body[0]
    if kind == KIND_JSON:
        try:
            msg = json.loads(body[1:].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("bad json") from exc
        if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
            raise ProtocolError("json message must be an object with a string 't'")
        return kind, msg
    if kind == KIND_BLOB:
        if len(body) < 2:
            raise ProtocolError("short blob chunk")
        n = body[1]
        if n == 0 or len(body) < 2 + n + 4:
            raise ProtocolError("short blob chunk")
        blob_id = body[2:2 + n].decode("ascii", errors="replace")
        (offset,) = struct.unpack(">I", body[2 + n:6 + n])
        return kind, (blob_id, offset, body[6 + n:])
    raise ProtocolError(f"unknown body kind {kind:#x}")


# --- channel ----------------------------------------------------------------

class Channel:
    """One established connection: sends and receives bodies, encrypting when secure."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 transport: Transport | None):
        self.reader = reader
        self.writer = writer
        self.transport = transport
        self.remote_static = transport.remote_static if transport else None
        self.out_filter: Callable[[dict[str, Any]], dict[str, Any]] | None = None  # rewrites JSON bodies sent
        self.caps: frozenset[str] = frozenset()  # what the app's hello said it can do (protocol.md §4.3)
        self._send_lock = asyncio.Lock()

    @property
    def secure(self) -> bool:
        return self.transport is not None

    async def send_body(self, body: bytes) -> None:
        async with self._send_lock:
            payload = self.transport.encrypt(body) if self.transport else body
            self.writer.write(frame(payload))
            await self.writer.drain()

    async def send(self, msg: dict[str, Any]) -> None:
        await self.send_body(encode_json(self.out_filter(msg) if self.out_filter else msg))

    async def recv(self) -> tuple[int, Any]:
        payload = await read_frame(self.reader)
        body = self.transport.decrypt(payload) if self.transport else payload
        return decode_body(body)

    async def close(self) -> None:
        try:
            self.writer.close()
            # A peer that vanished mid-download leaves unsent data buffered and wait_closed() would wait for
            # it (minutes): give it a moment, then drop the socket.
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except asyncio.TimeoutError:
            self.writer.transport.abort()
        except (ConnectionError, OSError):
            pass


# --- handshakes -------------------------------------------------------------

async def client_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           hello: dict[str, Any], *, static: KeyPair | None,
                           server_key: bytes | None) -> tuple[Channel, dict[str, Any]]:
    """Client side. Secure when `static` and `server_key` are given, else plaintext.
    Returns the channel and the server's first message (`welcome` or `error`)."""
    if static is not None and server_key is not None:
        init = Initiator(static, server_key)
        writer.write(frame(init.write_message_1(encode_json(hello))))
        await writer.drain()
        payload, transport = init.read_message_2(await read_frame(reader))
        _, reply = decode_body(payload)
        return Channel(reader, writer, transport), reply
    channel = Channel(reader, writer, None)
    await channel.send(hello)
    kind, reply = await channel.recv()
    if kind != KIND_JSON:
        raise ProtocolError("expected welcome")
    return channel, reply


HelloHandler = Callable[[dict[str, Any], bytes | None], Awaitable[dict[str, Any]]]


async def server_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                           static: KeyPair | None, on_hello: HelloHandler) -> tuple[Channel, dict, dict]:
    """Server side. `on_hello(hello, client_static_or_None)` returns the reply (`welcome` or `error`).
    Returns (channel, hello, reply); the caller closes the channel when reply is an error."""
    if static is not None:
        resp = Responder(static)
        payload = resp.read_message_1(await read_frame(reader))
        kind, hello = decode_body(payload)
        if kind != KIND_JSON:
            raise ProtocolError("expected hello")
        reply = await on_hello(hello, resp.remote_static)
        msg2, transport = resp.write_message_2(encode_json(reply))
        writer.write(frame(msg2))
        await writer.drain()
        return Channel(reader, writer, transport), hello, reply
    channel = Channel(reader, writer, None)
    kind, hello = await channel.recv()
    if kind != KIND_JSON:
        raise ProtocolError("expected hello")
    reply = await on_hello(hello, None)
    await channel.send(reply)
    return channel, hello, reply


__all__ = [
    "KIND_JSON", "KIND_BLOB", "MAX_FRAME", "MAX_BODY_DATA", "PROTOCOL_VERSION", "ProtocolError", "NoiseError",
    "Channel", "client_handshake", "server_handshake", "encode_json", "encode_blob_chunk",
    "decode_body", "max_blob_chunk", "frame", "read_frame",
]
