"""Reference client — what the 3DS app does, in Python. Used by tests and tools/fake3ds.py."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

from .framing import KIND_JSON, KIND_BLOB, PROTOCOL_VERSION, Channel, ProtocolError, client_handshake, \
    encode_blob_chunk
from .noise import KeyPair
from .pairing import b64u

CALLBACK_MAGIC = b"H3DS<"


class ServerError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class _BlobRx:
    """Collects the BLOB frames of one blob.get."""

    def __init__(self, offset: int):
        self.next = offset
        self.parts: list[bytes] = []
        self.size: int | None = None
        self.done = asyncio.Event()
        self.error: str | None = None

    def check(self) -> None:
        if self.size is not None and self.next >= self.size:
            self.done.set()


class Client:
    def __init__(self, channel: Channel, welcome: dict[str, Any]):
        self.channel = channel
        self.welcome = welcome
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self._next_id = 1
        self._waiters: dict[int, asyncio.Future] = {}
        self._blob_rx: dict[str, _BlobRx] = {}
        self._reader = asyncio.create_task(self._read_loop())

    @staticmethod
    def _hello(device: str, pair_token: bytes | None, caps: list[str] | None = None) -> dict[str, Any]:
        hello: dict[str, Any] = {"t": "hello", "v": PROTOCOL_VERSION, "app": "fake3ds", "device": device,
                                 "model": "old3ds", "screen": {"top": [400, 240], "bottom": [320, 240]},
                                 "caps": ["jpeg", "png", "pcm16"] if caps is None else caps}
        if pair_token is not None:
            hello["pair"] = b64u(pair_token)
        return hello

    @classmethod
    async def connect(cls, host: str, port: int, *, device: str = "fake3ds", static: KeyPair | None = None,
                      server_key: bytes | None = None, pair_token: bytes | None = None,
                      caps: list[str] | None = None, timeout: float = 15.0) -> "Client":
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        return await cls._handshake(reader, writer, cls._hello(device, pair_token, caps), static, server_key,
                                    timeout)

    @classmethod
    async def connect_callback(cls, host: str, discovery_port: int = 7334, *, listen_port: int = 0,
                               device: str = "fake3ds", static: KeyPair | None = None,
                               server_key: bytes | None = None, pair_token: bytes | None = None,
                               timeout: float = 15.0) -> "Client":
        """Ask the server to dial us (protocol.md §2.3), then run the normal client handshake on that stream."""
        accepted: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if accepted.done():
                writer.close()
            else:
                accepted.set_result((reader, writer))

        listener = await asyncio.start_server(on_accept, "0.0.0.0", listen_port)
        try:
            port = listener.sockets[0].getsockname()[1]
            request = CALLBACK_MAGIC + bytes([PROTOCOL_VERSION]) + port.to_bytes(2, "big")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                for _ in range(4):
                    sock.sendto(request, (host, discovery_port))
                    done, _ = await asyncio.wait({accepted}, timeout=0.7)
                    if done:
                        break
                reader, writer = await asyncio.wait_for(asyncio.shield(accepted), timeout)
            finally:
                sock.close()
        finally:
            listener.close()
        return await cls._handshake(reader, writer, cls._hello(device, pair_token), static, server_key, timeout)

    @classmethod
    async def _handshake(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hello: dict,
                         static: KeyPair | None, server_key: bytes | None, timeout: float) -> "Client":
        try:
            channel, reply = await asyncio.wait_for(
                client_handshake(reader, writer, hello, static=static, server_key=server_key), timeout)
        except BaseException:
            writer.close()
            raise
        if reply.get("t") != "welcome":
            await channel.close()
            raise ServerError(reply.get("code", "error"), reply.get("msg", ""))
        return cls(channel, reply)

    async def _read_loop(self) -> None:
        try:
            while True:
                kind, body = await self.channel.recv()
                if kind == KIND_BLOB:
                    bid, off, data = body
                    rx = self._blob_rx.get(bid)
                    if rx is None:
                        await self.events.put({"t": "_blob", "chunk": body})
                    elif off != rx.next:
                        rx.error = f"chunk at {off}, expected {rx.next}"
                        rx.done.set()
                    else:
                        rx.parts.append(data)
                        rx.next += len(data)
                        rx.check()
                    continue
                rid = body.get("id")
                if rid is not None and rid in self._waiters:
                    self._waiters.pop(rid).set_result(body)
                elif body["t"] == "ping":
                    await self.channel.send({"t": "pong", "n": body.get("n")})
                else:
                    await self.events.put(body)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ProtocolError) as exc:
            for fut in self._waiters.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            await self.events.put({"t": "_closed", "reason": str(exc)})

    async def request(self, t: str, timeout: float = 30.0, **fields: Any) -> dict:
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        await self.channel.send({"t": t, "id": rid, **fields})
        reply = await asyncio.wait_for(fut, timeout)
        if reply["t"] == "error":
            raise ServerError(reply.get("code", "error"), reply.get("msg", ""))
        return reply

    async def get_blob(self, blob: str, offset: int = 0, timeout: float = 60.0) -> bytes:
        """Fetch a blob (protocol.md §5) from `offset`; returns the bytes from there to the end."""
        rx = self._blob_rx[blob] = _BlobRx(offset)
        try:
            reply = await self.request("blob.get", timeout=timeout, blob=blob, offset=offset)
            rx.size = reply["size"]
            rx.check()
            await asyncio.wait_for(rx.done.wait(), timeout)
            if rx.error:
                raise ProtocolError(rx.error)
            return b"".join(rx.parts)
        finally:
            self._blob_rx.pop(blob, None)

    async def put_blob(self, blob: str, data: bytes, mime: str, name: str = "", *, stop_at: int | None = None,
                       chunk: int = 32768, timeout: float = 60.0) -> int:
        """Upload `data` as `blob` (protocol.md §5.1), resuming from what the PC already has. With `stop_at`,
        stop sending there (an interrupted upload, for tests) and return the offset reached; else wait for
        `blob.put_done` and return the size."""
        reply = await self.request("blob.put", timeout=timeout, blob=blob, size=len(data), mime=mime, name=name)
        off = reply["offset"]
        end = len(data) if stop_at is None else min(stop_at, len(data))
        while off < end:
            piece = data[off:min(off + chunk, end)]
            await self.channel.send_body(encode_blob_chunk(blob, off, piece))
            off += len(piece)
        if stop_at is not None and end < len(data):
            return off
        while True:
            ev = await self.next_event("blob.put_done", "error", timeout=timeout)
            if ev["t"] == "_closed":
                raise ConnectionError("connection closed")
            if ev.get("blob") != blob:
                continue
            if ev["t"] == "error":
                raise ServerError(ev.get("code", "error"), ev.get("msg", ""))
            return ev["size"]

    async def next_event(self, *types: str, timeout: float = 30.0) -> dict:
        """Next event, skipping ones whose type is not in `types` (if given)."""
        while True:
            ev = await asyncio.wait_for(self.events.get(), timeout)
            if not types or ev["t"] in types or ev["t"] == "_closed":
                return ev

    async def close(self) -> None:
        self._reader.cancel()
        await self.channel.close()


def discover(timeout: float = 1.5, port: int = 7334, target: str = "255.255.255.255") -> list[dict]:
    """UDP discovery (protocol.md §2.1). Returns the `here` replies with an `addr` field added."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    found: dict[str, dict] = {}
    try:
        sock.sendto(b"H3DS?" + bytes([PROTOCOL_VERSION]), (target, port))
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except (socket.timeout, ConnectionResetError):
                break
            try:
                info = json.loads(data)
            except ValueError:
                continue
            info["addr"] = addr[0]
            found[addr[0]] = info
    finally:
        sock.close()
    return list(found.values())


def load_device_key(path: Path) -> KeyPair:
    if path.exists():
        return KeyPair(path.read_bytes())
    kp = KeyPair()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(kp.private)
    return kp
