"""Hermes-independent server core: listener, discovery, auth, per-device message state and outbox.

The Hermes adapter plugs in a `Backend`; tests plug in a stub.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

from .emoji import downgrade_message, draws_sprites
from .blobs import UPLOAD_MAX, BlobError, BlobStore
from .framing import (KIND_JSON, PROTOCOL_VERSION, Channel, ProtocolError, encode_blob_chunk, fit_reply,
                      server_handshake)
from .noise import KeyPair, NoiseError
from .pairing import Device, DeviceStore, TokenStore, b64u, fingerprint, unb64u

log = logging.getLogger("hermes3ds")

HANDSHAKE_TIMEOUT = 15.0
AWAY_HANDSHAKE_TIMEOUT = 5.0  # internet-facing: idle sockets mustn't hold the few slots for long
IDLE_TIMEOUT = 45.0
MESSAGE_HISTORY = 50
DISCOVERY_MAGIC = b"H3DS?"
CALLBACK_MAGIC = b"H3DS<"  # "call me back": the client can reach us by UDP but not by TCP (§2.3)
CALLBACK_TIMEOUT = 5.0
CALLBACK_MIN_INTERVAL = 1.0  # per source address
CALLBACK_MAX_PENDING = 4
AWAY_MAX_HANDSHAKES = 16  # unauthenticated handshakes in flight on the internet-facing away listener
_UNKNOWN = object()  # away address not known yet (Tailscale setup still running)


class RequestError(Exception):
    """Raised by a backend to answer a request with {"t":"error","code":...}."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(msg or code)
        self.code = code
        self.msg = msg or code


class Backend(Protocol):
    async def welcome_state(self, device: Device) -> dict[str, Any]: ...
    async def handle(self, device: Device, msg: dict[str, Any]) -> dict[str, Any] | None: ...
    async def on_connect(self, device: Device) -> None: ...
    async def on_disconnect(self, device: Device) -> None: ...


@dataclass
class _Msg:
    mid: str
    role: str
    kind: str
    text: str
    spans: list | None
    final: bool
    ts: float
    attach: list | None = None  # attachments (protocol.md §4.7)
    dirty: bool = False  # changed while the device was offline
    changed: float = 0.0  # time.monotonic() of the last change sent (or meant to be)


@dataclass
class _DeviceState:
    channel: Channel | None = None
    messages: "OrderedDict[str, _Msg]" = field(default_factory=OrderedDict)
    pending: "OrderedDict[str, dict]" = field(default_factory=OrderedDict)  # approvals / clarifies
    deleted: set = field(default_factory=set)  # mids deleted while the device was offline
    recent_deletes: "OrderedDict[str, float]" = field(default_factory=OrderedDict)  # mid -> when (monotonic)
    typing: bool = False
    caps: frozenset = frozenset()  # what the app's last hello said, kept while it is offline
    # When the device last sent an authenticated frame. A connection that died without closing (lid shut, Wi-Fi
    # gone) still accepts writes until IDLE_TIMEOUT, so whatever changed after this may never have arrived.
    last_rx: float = 0.0


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 7333
    discovery_port: int = 7334  # 0 disables discovery
    secure: bool = True
    name: str = "hermes"
    # Away-from-home listener (§2.4): the Tailscale Funnel forwards the internet to it. Loopback only, never
    # accepts pairing. 0 = off.
    away_port: int = 0
    away_host: str = "127.0.0.1"


def is_lan_peer(peer: Any) -> bool:
    """A source address pairing may come from: private (RFC 1918 / ULA), loopback or link-local."""
    try:
        addr = ipaddress.ip_address(peer[0])
    except (TypeError, ValueError, IndexError):
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (addr.is_private or addr.is_loopback or addr.is_link_local) and not addr.is_multicast


class Hermes3DSServer:
    def __init__(self, cfg: ServerConfig, backend: Backend, static: KeyPair,
                 devices: DeviceStore, tokens: TokenStore, server_info: dict[str, Any] | None = None,
                 blobs: BlobStore | None = None):
        self.cfg = cfg
        self.blobs = blobs
        self.backend = backend
        self.static = static
        self.devices = devices
        self.tokens = tokens
        self.server_info = server_info or {}
        self._states: dict[bytes, _DeviceState] = {}
        self._tcp: asyncio.base_events.Server | None = None
        self._away_tcp: asyncio.base_events.Server | None = None
        # Away handshakes in flight, oldest first. When full, the oldest is dropped rather than the newcomer: idle
        # sockets (every funnel peer looks like 127.0.0.1) can then delay the real 3DS but not lock it out.
        self._away_pending: "OrderedDict[asyncio.Task, None]" = OrderedDict()
        self._away: Any = _UNKNOWN  # {"host","port"}, None (no away route) or _UNKNOWN
        self._udp: asyncio.DatagramTransport | None = None
        self._conn_tasks: set[asyncio.Task] = set()
        self._dialing: set[tuple[str, int]] = set()
        self._last_dial: dict[str, float] = {}
        self._transfers: dict[Channel, dict[str, asyncio.Task]] = {}  # running blob.get streams per connection

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Bind listeners. Raises OSError (e.g. port in use) for the caller to report."""
        self._tcp = await asyncio.start_server(self._on_connection, self.cfg.host, self.cfg.port)
        if self.cfg.away_port:
            try:
                self._away_tcp = await asyncio.start_server(self._on_away_connection, self.cfg.away_host,
                                                            self.cfg.away_port)
            except OSError:
                self._tcp.close()
                raise
        if self.cfg.discovery_port:
            loop = asyncio.get_running_loop()
            try:
                self._udp, _ = await loop.create_datagram_endpoint(
                    lambda: _Discovery(self), local_addr=(self.cfg.host, self.cfg.discovery_port),
                    allow_broadcast=True)
            except OSError as exc:  # discovery is optional; manual IP still works
                log.warning("3ds discovery disabled: %s", exc)

    @property
    def port(self) -> int:
        return self._tcp.sockets[0].getsockname()[1] if self._tcp else self.cfg.port

    @property
    def away_port(self) -> int:
        return self._away_tcp.sockets[0].getsockname()[1] if self._away_tcp else 0

    async def stop(self) -> None:
        if self._udp:
            self._udp.close()
        if self._tcp:
            self._tcp.close()
        if self._away_tcp:
            self._away_tcp.close()
        for st in self._states.values():
            if st.channel:
                await st.channel.close()
        for task in list(self._conn_tasks):
            task.cancel()
        if self._tcp:
            try:
                await asyncio.wait_for(self._tcp.wait_closed(), 5)
            except asyncio.TimeoutError:
                pass

    def discovery_reply(self) -> bytes:
        return json.dumps({"t": "here", "v": PROTOCOL_VERSION, "name": self.cfg.name, "port": self.port,
                           "key": b64u(self.static.public),
                           "sec": "secure" if self.cfg.secure else "plaintext"}).encode("utf-8")

    # --- away address (§2.4) -------------------------------------------------

    @property
    def away(self) -> dict | None:
        return None if self._away is _UNKNOWN else self._away

    async def set_away(self, address: dict | None) -> None:
        """The public away address ({"host","port"}), or None when there is none. Online devices hear it now,
        the rest in their next welcome."""
        if address is not None:
            address = {"host": str(address["host"]), "port": int(address["port"])}
        if self._away is not _UNKNOWN and self._away == address:
            return
        self._away = address
        for st in list(self._states.values()):
            await self._send(st, {"t": "away", "away": address})

    def _away_fields(self) -> dict:
        return {} if self._away is _UNKNOWN else {"away": self._away}

    # --- call-back (§2.3) ----------------------------------------------------

    def request_callback(self, ip: str, port: int) -> bool:
        """Dial a client that asked to be called back. False if the request is refused or a duplicate.

        Only the datagram's source address is ever dialed, only private/link-local ones, at most once a
        second per address. The dialed stream then runs the normal server handshake, so the client still
        has to authenticate exactly as on an inbound connection.
        """
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if not (addr.is_private or addr.is_link_local) or addr.is_multicast or not 1024 <= port <= 65535:
            return False
        if (ip, port) in self._dialing:
            return True  # a retry of a request we are already serving
        now = time.monotonic()
        if len(self._dialing) >= CALLBACK_MAX_PENDING or now - self._last_dial.get(ip, -1e9) < CALLBACK_MIN_INTERVAL:
            return False
        self._last_dial[ip] = now
        if len(self._last_dial) > 256:
            self._last_dial = {k: v for k, v in self._last_dial.items() if now - v < 60}
        self._dialing.add((ip, port))
        task = asyncio.get_running_loop().create_task(self._dial(ip, port))
        self._conn_tasks.add(task)  # so stop() cancels a dial in progress
        task.add_done_callback(self._conn_tasks.discard)
        return True

    async def _dial(self, ip: str, port: int) -> None:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), CALLBACK_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            log.info("3ds call-back to %s:%d failed: %s", ip, port, exc)
            return
        finally:
            self._dialing.discard((ip, port))
        await self._on_connection(reader, writer)

    # --- connections --------------------------------------------------------

    async def _on_away_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while len(self._away_pending) >= AWAY_MAX_HANDSHAKES:  # the internet can reach this one: bound the work
            oldest, _ = self._away_pending.popitem(last=False)
            oldest.cancel()
        await self._on_connection(reader, writer, away=True)

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                             away: bool = False) -> None:
        task = asyncio.current_task()
        self._conn_tasks.add(task)
        peer = writer.get_extra_info("peername")
        device: Device | None = None
        channel: Channel | None = None
        try:
            authed: dict[str, Any] = {}

            async def on_hello(hello: dict, remote_static: bytes | None) -> dict:
                reply = await self._authorize(hello, remote_static, peer, away=away)
                if reply["t"] == "welcome":
                    authed["device"] = self.devices.get(remote_static) if remote_static else \
                        self._plaintext_device(hello)
                    reply.update(await self._welcome_extra(authed["device"]))
                    if not draws_sprites(hello.get("caps")):  # app without this emoji set: `:name:` text
                        reply = downgrade_message(reply)
                return reply

            if away:
                self._away_pending[task] = None
            try:
                channel, hello, reply = await asyncio.wait_for(
                    server_handshake(reader, writer, static=self.static if self.cfg.secure else None,
                                     on_hello=on_hello),
                    AWAY_HANDSHAKE_TIMEOUT if away else HANDSHAKE_TIMEOUT)
            finally:
                if away:
                    self._away_pending.pop(task, None)
            if reply["t"] != "welcome":
                return
            device = authed["device"]
            caps = hello.get("caps")
            channel.caps = frozenset(c for c in caps if isinstance(c, str)) if isinstance(caps, list) else frozenset()
            if not draws_sprites(hello.get("caps")):
                channel.out_filter = downgrade_message
            await self._attach(device, channel)
            await self._read_loop(device, channel)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except (NoiseError, ProtocolError) as exc:
            log.info("3ds connection from %s rejected: %s", peer, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("3ds connection error (%s)", peer)
        finally:
            self._conn_tasks.discard(task)
            if channel is not None:
                self._cancel_transfers(channel)
            if device is not None:
                await self._detach(device, channel)
            elif channel is not None:
                await channel.close()
            else:
                writer.close()

    def _plaintext_device(self, hello: dict) -> Device:
        """Plaintext dev mode: a stable fake key per device name."""
        name = str(hello.get("device") or "dev-3ds")
        key = ("plaintext:" + name).encode("utf-8").ljust(32, b"\x00")[:32]
        return self.devices.get(key) or Device(key, name, time.time())

    async def _authorize(self, hello: dict, remote_static: bytes | None, peer: Any, *, away: bool = False) -> dict:
        if hello.get("t") != "hello":
            return {"t": "error", "code": "bad_request", "msg": "expected hello"}
        if hello.get("v") != PROTOCOL_VERSION:
            return {"t": "error", "code": "version",
                    "msg": f"server speaks protocol v{PROTOCOL_VERSION}, client sent v{hello.get('v')}"}
        if remote_static is None:  # plaintext mode: no authentication by design, so never from the internet
            if away:
                return {"t": "error", "code": "not_paired", "msg": "plaintext dev mode works on the LAN only"}
            return {"t": "welcome"}
        name = str(hello.get("device") or "3DS")[:40]
        if self.devices.get(remote_static):
            self.devices.touch(remote_static, name)
            return {"t": "welcome"}
        token = hello.get("pair")
        if isinstance(token, str) and (away or not is_lan_peer(peer)):
            # A pairing code shown on the PC is only good at home: a leaked code is useless from the internet
            # (the Funnel arrives on the away listener).
            log.info("3ds pairing refused from %s (%s)", peer, "away listener" if away else "not a LAN address")
            return {"t": "error", "code": "pair_lan_only",
                    "msg": "Pairing works only at home, on the same network as the PC."}
        if isinstance(token, str):
            try:
                ok = self.tokens.consume(unb64u(token))
            except ValueError:
                ok = False
            if ok:
                dev = self.devices.add(remote_static, name)
                log.info("3ds paired new device %r (%s) from %s", name, dev.fp, peer)
                return {"t": "welcome", "paired": True}
            return {"t": "error", "code": "not_paired", "msg": "pairing code invalid or expired"}
        return {"t": "error", "code": "not_paired", "msg": "this 3DS is not paired with this PC"}

    async def _welcome_extra(self, device: Device) -> dict:
        info = {"v": PROTOCOL_VERSION, "server": self.cfg.name, "device_id": device.fp}
        info.update(self.server_info)
        info.update(self._away_fields())
        if self.blobs is not None:
            info["upload"] = {"max": UPLOAD_MAX}
        info["chat"] = await self.backend.welcome_state(device)
        return info

    def has_cap(self, device_key: bytes, cap: str, *, offline: bool = False) -> bool:
        """Whether the app connected for this device said `cap` in its hello (protocol.md §4.3). `offline`: also
        when no app is connected now but the last one said it."""
        st = self._states.get(device_key)
        if st and st.channel:
            return cap in st.channel.caps
        return bool(offline and st and cap in st.caps)

    async def _attach(self, device: Device, channel: Channel) -> None:
        st = self._states.setdefault(device.key, _DeviceState())
        old, st.channel = st.channel, channel
        st.caps = channel.caps
        if old is not None:  # newest connection wins (e.g. reconnect after sleep)
            await old.close()
        # Replay what the device missed: changed messages, pending prompts, typing state. Iterate over copies
        # (agent output can arrive while we await), and skip an item that can't be framed rather than
        # dropping the connection on every reconnect.
        # Also replay what changed since the device was last heard from: it may have gone into a dead socket.
        # The app matches messages by mid, so a second copy of one that did arrive only updates it in place.
        cutoff = st.last_rx
        deleted, st.deleted = list(st.deleted), set()
        deleted += [mid for mid, at in st.recent_deletes.items() if at > cutoff and mid not in deleted]
        for mid in deleted:
            await self._send_replay(channel, {"t": "delete", "mid": mid})
        for m in list(st.messages.values()):
            if m.dirty or m.changed > cutoff:
                m.dirty = False
                await self._send_replay(channel, self._msg_event(m))
        for prompt in list(st.pending.values()):
            await self._send_replay(channel, prompt)
        if st.typing:
            await channel.send({"t": "typing", "on": True})
        await self.backend.on_connect(device)

    @staticmethod
    async def _send_replay(channel: Channel, msg: dict) -> None:
        try:
            await channel.send(msg)
        except ProtocolError as exc:
            log.warning("3ds replay: skipped %s (%s)", msg.get("t"), exc)

    async def _detach(self, device: Device, channel: Channel | None) -> None:
        st = self._states.get(device.key)
        if st and st.channel is channel:
            st.channel = None
            await self.backend.on_disconnect(device)
        if channel is not None:
            await channel.close()

    async def _read_loop(self, device: Device, channel: Channel) -> None:
        while True:
            kind, body = await asyncio.wait_for(channel.recv(), IDLE_TIMEOUT)
            st = self._states.get(device.key)
            if st is not None and st.channel is channel:
                st.last_rx = time.monotonic()
            if self.cfg.secure and self.devices.get(device.key) is None:  # unpaired while connected
                log.info("3ds device %s was unpaired: closing its connection", device.fp)
                return
            if kind != KIND_JSON:
                await self._upload_chunk(device, channel, *body)
                continue
            t = body["t"]
            if t == "ping":
                await channel.send({"t": "pong", "n": body.get("n")})
                continue
            if t == "pong":
                continue
            rid = body.get("id")
            after = None
            try:
                if t == "blob.get":
                    reply, after = self._blob_get(channel, body)
                elif t == "blob.put":
                    reply, after = self._blob_put(device, channel, body)
                elif t == "blob.cancel":
                    self._cancel_transfer(channel, str(body.get("blob")))
                    continue
                else:
                    reply = await self.backend.handle(device, body)
            except RequestError as exc:
                reply = {"t": "error", "code": exc.code, "msg": exc.msg}
            except Exception:
                log.exception("3ds request %r failed", t)
                reply = {"t": "error", "code": "internal", "msg": f"{t} failed on the PC"}
            if reply is not None and reply.get("t") == "status_r":
                for k, v in self._away_fields().items():
                    reply.setdefault(k, v)
            if reply is not None and rid is not None:
                reply.setdefault("id", rid)
                try:
                    await channel.send(fit_reply(reply))
                except ProtocolError as exc:  # still too large: answer, don't drop the connection
                    log.warning("3ds request %r: reply not sent (%s)", t, exc)
                    await channel.send({"t": "error", "id": rid, "code": "too_large",
                                        "msg": f"{t}: the answer is too large to send"})
            if after is not None:
                after()  # blob.get: the stream starts only once its reply is on the wire

    # --- blobs (protocol.md §5) -----------------------------------------------------

    def _blob_get(self, channel: Channel, req: dict) -> tuple[dict, Any]:
        """Validate a blob.get; returns the reply and a callable that starts streaming the bytes."""
        bid = str(req.get("blob") or "")
        try:
            offset = int(req.get("offset") or 0)
        except (TypeError, ValueError):
            raise RequestError("bad_request", "bad offset")
        if self.blobs is None:
            raise RequestError("not_found", "no such blob")
        meta = self.blobs.meta(bid)
        if meta is None:
            raise RequestError("not_found", "no such blob")
        try:
            chunks = self.blobs.open_chunks(bid, offset)
        except BlobError as exc:
            if str(exc) == "not_found":
                raise RequestError("not_found", "no such blob")
            raise RequestError("bad_request", str(exc))
        reply = {"t": "blob.get_r", "blob": bid, "size": meta["size"]}

        def start() -> None:
            self._cancel_transfer(channel, bid)
            task = asyncio.get_running_loop().create_task(self._stream_blob(channel, bid, chunks))
            mine = self._transfers.setdefault(channel, {})
            mine[bid] = task
            task.add_done_callback(lambda t: mine.get(bid) is t and mine.pop(bid, None))

        return reply, start

    def _blob_put(self, device: Device, channel: Channel, req: dict) -> tuple[dict, Any]:
        """Start or resume an upload (§5.1). The reply says where to continue; an upload that is already
        complete is confirmed with `blob.put_done` right after it."""
        bid = str(req.get("blob") or "")
        size = req.get("size")
        if self.blobs is None:
            raise RequestError("denied", "this PC can't take uploads")
        try:
            have = self.blobs.begin_upload(bid, size if isinstance(size, int) else -1, str(req.get("mime") or ""),
                                           str(req.get("name") or ""), device.fp)
        except BlobError as exc:
            code = str(exc)
            raise RequestError(code, {"too_large": f"uploads are limited to {UPLOAD_MAX} bytes",
                                      "denied": "that upload id is taken"}.get(code, "bad upload request"))
        reply = {"t": "blob.put_r", "blob": bid, "offset": have}
        if have < size:
            return reply, None

        def done() -> None:
            asyncio.get_running_loop().create_task(self._send_quiet(channel, self._put_done(bid, size)))

        return reply, done

    @staticmethod
    def _put_done(bid: str, size: int) -> dict:
        return {"t": "blob.put_done", "blob": bid, "size": size}

    async def _send_quiet(self, channel: Channel, msg: dict) -> None:
        try:
            await channel.send(msg)
        except (ConnectionError, OSError, ProtocolError):
            pass

    async def _upload_chunk(self, device: Device, channel: Channel, bid: str, offset: int, data: bytes) -> None:
        """One BLOB frame of an upload. A piece that doesn't fit (wrong offset, unknown id) gets an `error`
        event naming the blob; the client then asks `blob.put` again to learn where to go on."""
        if self.blobs is None:
            return
        try:
            have, complete = await asyncio.to_thread(self.blobs.write_upload, bid, offset, data, device.fp)
        except BlobError as exc:
            log.info("3ds upload %s: piece at %d refused (%s)", bid, offset, exc)
            await channel.send({"t": "error", "code": str(exc), "blob": bid, "msg": "upload piece refused"})
            return
        except OSError as exc:
            log.warning("3ds upload %s: cannot write: %s", bid, exc)
            await channel.send({"t": "error", "code": "internal", "blob": bid, "msg": "the PC couldn't store it"})
            return
        if complete:
            log.info("3ds upload %s complete (%d bytes)", bid, have)
            await channel.send(self._put_done(bid, have))

    def upload_path(self, device: Device, bid: str):
        """A complete upload from this device (for the backend's `send`), or None."""
        return self.blobs.upload_path(bid, device.fp) if self.blobs is not None else None

    async def _stream_blob(self, channel: Channel, bid: str, chunks: Any) -> None:
        """Send BLOB frames until done; `send_body` drains, so a slow 3DS slows only this transfer."""
        try:
            while True:
                piece = await asyncio.to_thread(next, chunks, None)
                if piece is None:
                    return
                await channel.send_body(encode_blob_chunk(bid, piece[0], piece[1]))
        except (ConnectionError, OSError, ProtocolError) as exc:
            log.debug("3ds blob %s stream ended: %s", bid, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("3ds blob %s stream failed", bid)
        finally:
            try:
                chunks.close()
            except ValueError:  # still inside a worker thread's next(); the generator closes its file when done
                pass

    def _cancel_transfer(self, channel: Channel, bid: str) -> None:
        task = self._transfers.get(channel, {}).pop(bid, None)
        if task is not None:
            task.cancel()

    def _cancel_transfers(self, channel: Channel) -> None:
        for task in self._transfers.pop(channel, {}).values():
            task.cancel()

    # --- outbound API for the backend -------------------------------------------

    def is_online(self, device_key: bytes) -> bool:
        st = self._states.get(device_key)
        return bool(st and st.channel)

    async def _send(self, st: _DeviceState, msg: dict) -> bool:
        if st.channel is None:
            return False
        try:
            await st.channel.send(msg)
            return True
        except (ConnectionError, OSError, ProtocolError) as exc:
            log.debug("3ds send failed: %s", exc)
            return False

    @staticmethod
    def _msg_event(m: _Msg) -> dict:
        ev = {"t": "msg", "mid": m.mid, "role": m.role, "kind": m.kind, "text": m.text,
              "final": m.final, "ts": m.ts}
        if m.spans:
            ev["spans"] = m.spans
        if m.attach:
            ev["attach"] = m.attach
        return ev

    async def post_message(self, device_key: bytes, mid: str, text: str, *, role: str = "assistant",
                           kind: str = "reply", final: bool = True, spans: list | None = None,
                           attach: list | None = None) -> None:
        st = self._states.setdefault(device_key, _DeviceState())
        m = _Msg(mid, role, kind, text, spans, final, time.time(), attach or None, changed=time.monotonic())
        st.messages[mid] = m
        while len(st.messages) > MESSAGE_HISTORY:
            st.messages.popitem(last=False)
        if not await self._send(st, self._msg_event(m)):
            m.dirty = True

    async def update_message(self, device_key: bytes, mid: str, text: str, *, final: bool,
                             spans: list | None = None) -> bool:
        """Streaming edit. Sends `append` when text extends the previous text, else `edit`.

        `spans` (when given) replace the message's spans; [] clears them. An `append` carries the whole
        span list only when it changed, so formatting follows the text while it streams."""
        st = self._states.get(device_key)
        m = st.messages.get(mid) if st else None
        if m is None:
            return False
        old, old_spans = m.text, m.spans
        if spans is not None:
            m.spans = spans or None
        if text == old and final and m.final and m.spans == old_spans:
            return True  # repeated finalize (REQUIRES_EDIT_FINALIZE): nothing new to send
        m.text, m.final = text, final
        m.changed = time.monotonic()
        if st.channel is None or m.dirty:
            m.dirty = True
            return True
        spans_changed = m.spans != old_spans
        # A formatted final restates everything (apps that ignore `append` spans still end up formatted). So
        # does any span change on a channel whose out_filter rewrites text (emoji downgrade): it can only remap
        # span offsets against the whole text, not an appended tail.
        whole = (final and (spans_changed or m.spans is not None)) or (
            spans_changed and st.channel.out_filter is not None)
        if text.startswith(old) and not whole:
            if len(text) == len(old) and not final and not spans_changed:
                return True
            ev = {"t": "append", "mid": mid, "text": text[len(old):], "final": final}
            if spans_changed:
                ev["spans"] = m.spans or []
        else:
            ev = {"t": "edit", "mid": mid, "text": text, "final": final}
            if m.spans:
                ev["spans"] = m.spans
        if not await self._send(st, ev):
            m.dirty = True
        return True

    async def delete_message(self, device_key: bytes, mid: str) -> bool:
        """Remove a message from the device's transcript (e.g. a streamed preview Hermes replaced)."""
        st = self._states.get(device_key)
        if st is None or st.messages.pop(mid, None) is None:
            return False
        st.recent_deletes[mid] = time.monotonic()
        while len(st.recent_deletes) > MESSAGE_HISTORY:
            st.recent_deletes.popitem(last=False)
        if not await self._send(st, {"t": "delete", "mid": mid}):
            st.deleted.add(mid)
        return True

    def open_blank_messages(self, device_key: bytes) -> list[str]:
        """Streamed messages still open whose text is empty (all Hermes sent was a hidden MEDIA tag)."""
        st = self._states.get(device_key)
        if st is None:
            return []
        return [m.mid for m in st.messages.values() if not m.final and not m.text.strip() and not m.attach]

    async def set_typing(self, device_key: bytes, on: bool) -> None:
        st = self._states.setdefault(device_key, _DeviceState())
        if st.typing == on:
            return
        st.typing = on
        await self._send(st, {"t": "typing", "on": on})

    async def event(self, device_key: bytes, msg: dict) -> None:
        """Transient event (e.g. `tool`); dropped when the device is offline."""
        st = self._states.get(device_key)
        if st:
            await self._send(st, msg)

    async def set_pending(self, device_key: bytes, key: str, prompt: dict) -> None:
        st = self._states.setdefault(device_key, _DeviceState())
        st.pending[key] = prompt
        await self._send(st, prompt)

    async def clear_pending(self, device_key: bytes, key: str, done: dict | None) -> None:
        st = self._states.get(device_key)
        if st and st.pending.pop(key, None) is not None and done is not None:
            await self._send(st, done)

    def pending(self, device_key: bytes, key: str) -> dict | None:
        st = self._states.get(device_key)
        return st.pending.get(key) if st else None


class _Discovery(asyncio.DatagramProtocol):
    def __init__(self, server: Hermes3DSServer):
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if self.transport is None:
            return
        if not is_lan_peer(addr):  # discovery is for the home network: no replies (or name) to the internet
            return
        if data[:5] == DISCOVERY_MAGIC:
            self.transport.sendto(self.server.discovery_reply(), addr)
        elif data[:5] == CALLBACK_MAGIC and len(data) >= 8 and data[5] == PROTOCOL_VERSION:
            port = int.from_bytes(data[6:8], "big")
            if self.server.request_callback(addr[0], port):
                self.transport.sendto(json.dumps({"t": "dialing", "port": port}).encode("utf-8"), addr)


__all__ = ["Hermes3DSServer", "ServerConfig", "Backend", "RequestError", "fingerprint", "is_lan_peer"]
