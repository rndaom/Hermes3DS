"""Hermes gateway platform adapter for the Nintendo 3DS client (docs/protocol.md)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import itertools
import json
import logging
import re
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from . import compat
from .h3proto.away import AwayManager, parse_address, parse_mode
from .h3proto.audio import voice_to_wav
from .h3proto.blobs import BlobStore
from .h3proto import fetch
from .h3proto.media import build_attachment, build_attachment_bytes, clean_name, is_image_name
from .h3proto.pairing import Device, DeviceStore, TokenStore, load_or_create_key
from .h3proto.emoji import emoji_for_3ds
from .h3proto.richtext import markdown_to_rich, stream_to_rich
from .h3proto.server import Hermes3DSServer, RequestError, ServerConfig

logger = logging.getLogger("hermes3ds")

PLATFORM = "3ds"
DEFAULT_PORT = 7333
DEFAULT_DISCOVERY_PORT = 7334
DEFAULT_AWAY_PORT = 7339  # loopback listener the Tailscale Funnel forwards to (protocol.md §2.4)
# Hermes' streaming cursor (StreamingConfig.cursor default); the 3DS draws its own caret.
_CURSORS = (" ▉", "▉")
_MAX_CHARS = 8000  # keeps any UTF-8 message comfortably under the 64 KiB frame limit
DOWNLOAD_TIMEOUT = fetch.TIMEOUT  # seconds, per network operation of an image download
DOWNLOAD_TOTAL = fetch.TOTAL      # seconds, whole download
DOWNLOAD_MAX = fetch.MAX_BYTES
ATTACH_PER_MESSAGE = 8    # keeps the msg JSON far below the 64 KiB frame limit
PHOTOS_PER_SEND = 4       # pictures one 3DS `send` may carry (protocol.md §4.4)
HEARD_WAIT = 600.0        # seconds a voice message waits for Hermes' transcript echo
# "Open on PC" hands the file to its default app; never do that for things that run code.
_NO_OPEN_EXTS = {".exe", ".com", ".scr", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                 ".wsh", ".hta", ".msi", ".msp", ".lnk", ".url", ".reg", ".jar", ".dll", ".cpl", ".sh", ".command",
                 ".app", ".appimage", ".desktop", ".py", ".pyw", ".pyz", ".pl", ".rb", ".scpt", ".workflow",
                 ".bash", ".zsh", ".fish", ".csh", ".ksh", ".run", ".bin", ".out", ".elf", ".so", ".deb", ".rpm",
                 ".flatpak", ".flatpakref", ".snap", ".plasmoid", ".kwinscript", ".service", ".php", ".lua",
                 ".tcl", ".ps", ".msix", ".appx", ".ahk", ".jnlp", ".pkg", ".dmg", ".mpkg"}


def _data_dir():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLATFORM)


def chat_id_for(device: Device) -> str:
    return f"dev:{device.fp}"


def _strip_cursor(text: str) -> tuple[str, bool]:
    """Remove Hermes' streaming cursor. It is not always last: Hermes closes open markdown after it
    (e.g. "printed ` ▉`"), so the last occurrence is removed wherever it is."""
    for c in _CURSORS:
        i = text.rfind(c)
        if i >= 0:
            return text[:i] + text[i + len(c):], True
    return text, False


# The start of a MEDIA:<path> directive still streaming in (its path isn't complete, so Hermes' cleanup
# regex can't match it yet). Only at the end of the text, as its own word.
_PARTIAL_MEDIA = re.compile(r"""(?:^|(?<=\s))[`"'*_]{0,3}MEDIA(?::[^\n]*)?$""")


def _hide_media(text: str, final: bool) -> str:
    """Hide MEDIA:<path> directives (Hermes delivers those files as attachments after the stream)."""
    text = BasePlatformAdapter.strip_media_directives_for_display(text)
    if not final:
        text = _PARTIAL_MEDIA.sub("", text.rstrip())
    return text


def _echo_template() -> tuple[str, str]:
    """Hermes' transcript echo (`gateway.voice.transcript_echo_short`, e.g. '🎙️ "{text}"') split around
    the text, in the gateway's language."""
    with contextlib.suppress(Exception):
        from agent.i18n import t
        marker = "\x00"
        head, sep, tail = t("gateway.voice.transcript_echo_short", text=marker).partition(marker)
        if sep and head:
            return head, tail
    return '\U0001f399\ufe0f "', '"'


def _heard_text(content: str) -> Optional[str]:
    """The transcript in a Hermes STT echo, or None when `content` is something else."""
    head, tail = _echo_template()
    content = content.strip()
    if len(content) >= len(head) + len(tail) and content.startswith(head) and content.endswith(tail):
        return content[len(head):len(content) - len(tail)]
    return None


def _cfg(extra: dict, key: str, default: Any) -> Any:
    try:
        from gateway.platforms._shared import extra_or_secret
        return extra_or_secret(extra, key, f"THREEDS_{key.upper()}", default)
    except Exception:
        return extra.get(key, default)


def _cut(text, limit: int):
    """Keep a prompt inside one frame (Hermes doesn't bound the command it asks about)."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit] + f"… ({len(text) - limit} more characters)"


class ThreeDSAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    supports_status_text = True
    REQUIRES_EDIT_FINALIZE = True
    MAX_MESSAGE_LENGTH = _MAX_CHARS

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = getattr(config, "extra", {}) or {}
        self.host = str(_cfg(extra, "host", "0.0.0.0"))
        self.port = int(_cfg(extra, "port", DEFAULT_PORT))
        self.discovery_port = int(_cfg(extra, "discovery_port", DEFAULT_DISCOVERY_PORT))
        self.secure = str(_cfg(extra, "security", "secure")).lower() != "plaintext"
        self.server_name = str(_cfg(extra, "name", "") or socket.gethostname())
        self.away_mode = parse_mode(_cfg(extra, "away", "auto"))
        self.away_port = int(_cfg(extra, "away_port", DEFAULT_AWAY_PORT))
        self.away_public_port = int(_cfg(extra, "away_public_port", 0) or 0)
        self.away_address = parse_address(_cfg(extra, "away_address", ""))
        self._away_task: Optional[asyncio.Task] = None
        self._server: Optional[Hermes3DSServer] = None
        self._chat_keys: dict[str, bytes] = {}   # chat_id -> device key
        self._chat_names: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._trace_path = None
        self._approval_cards: dict[str, tuple[str, str]] = {}  # card mid -> (chat_id, session_key)
        self._blobs: Optional[BlobStore] = None
        self._awaiting_heard: dict[str, list[tuple[float, int]]] = {}  # chat_id -> [(sent at, send request id)]

    @property
    def name(self) -> str:
        return "Nintendo 3DS"

    # --- lifecycle --------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._acquire_platform_lock(PLATFORM, f"port:{self.port}", f"3DS listener on port {self.port}"):
            return False
        self._loop = asyncio.get_running_loop()
        data = _data_dir()
        self._trace_path = data / "trace.log" if (data / "trace").exists() else None
        try:
            static = load_or_create_key(data / "server_key")
        except Exception as exc:
            self._set_fatal_error("key_error", f"cannot load 3DS server key: {exc}", retryable=False)
            return False
        try:
            self._blobs = BlobStore(data / "blobs")
        except OSError as exc:
            logger.warning("3DS: blob store unavailable, media attachments disabled: %s", exc)
        server = Hermes3DSServer(
            ServerConfig(host=self.host, port=self.port, discovery_port=self.discovery_port,
                         secure=self.secure, name=self.server_name,
                         away_port=0 if self.away_mode == "off" or not self.secure else self.away_port),
            _Backend(self), static, DeviceStore(data / "devices.json"), TokenStore(data / "pair_tokens.json"),
            server_info={"hermes": _hermes_version()}, blobs=self._blobs)
        try:
            await server.start()
        except OSError as exc:
            self._release_platform_lock()
            self._set_fatal_error("port_in_use", f"3DS listener cannot bind {self.host}:{self.port} or the away "
                                  f"port 127.0.0.1:{self.away_port}: {exc}", retryable=False)
            return False
        self._server = server
        for dev in server.devices.all():
            self._chat_keys[chat_id_for(dev)] = dev.key
            self._chat_names[chat_id_for(dev)] = dev.name
        if not self.secure:
            logger.warning("3DS: PLAINTEXT dev mode — traffic is unencrypted and unauthenticated")
        self._mark_connected()
        self._wire_plugin_handlers(None)
        logger.info("3DS: listening on %s:%s (%s)", self.host, server.port, "secure" if self.secure else "plaintext")
        if server.away_port:
            away = AwayManager(server, data, mode=self.away_mode, address=self.away_address,
                               public_port=self.away_public_port)
            self._away_task = asyncio.get_running_loop().create_task(away.run())
        else:
            await server.set_away(None)  # paired 3DSes forget their away address
        return True

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        self._mark_disconnected()
        if self._away_task:
            self._away_task.cancel()
            self._away_task = None
        if self._server:
            await self._server.stop()
            self._server = None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": self._chat_names.get(chat_id, chat_id), "type": "dm"}

    # --- helpers ----------------------------------------------------------------

    def _key(self, chat_id: str) -> Optional[bytes]:
        return self._chat_keys.get(chat_id)

    def _mid(self, prefix: str = "m") -> str:
        return f"{prefix}{next(self._ids)}"

    def _trace(self, what: str, **fields: Any) -> None:
        """Opt-in debug log: create <plugin-data>/3ds/trace to record every outbound call."""
        if self._trace_path is None:
            return
        with contextlib.suppress(Exception):
            line = json.dumps({"ts": round(datetime.datetime.now().timestamp(), 3), "what": what, **fields},
                              ensure_ascii=False, default=str)
            with open(self._trace_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def source_for(self, device: Device):
        cid = chat_id_for(device)
        # The device proved its identity with its paired static key (Noise IK), so the adapter
        # vouches for it; no Hermes allowlist entry is needed.
        return self.build_source(chat_id=cid, chat_name=device.name, chat_type="dm", user_id=device.fp,
                                 user_name=device.name, role_authorized=True)

    def register_device(self, device: Device) -> str:
        cid = chat_id_for(device)
        self._chat_keys[cid] = device.key
        self._chat_names[cid] = device.name
        return cid

    async def inject_text(self, device: Device, text: str) -> str:
        mid = self._mid("u")
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=self.source_for(device),
                             message_id=mid, timestamp=datetime.datetime.now())
        await self.handle_message(event)
        return mid

    def _upload(self, device: Device, ref: Any) -> Path:
        bid = str(ref.get("blob") if isinstance(ref, dict) else "")
        path = self._server.upload_path(device, bid) if self._server else None
        if path is None:
            raise RequestError("not_found", f"upload {bid!r} isn't complete on the PC")
        return path

    async def inject_voice(self, device: Device, voice: dict, text: str, req: Any) -> str:
        """A voice message (protocol.md §4.4): IMA-ADPCM upload → WAV → Hermes' audio cache → a VOICE event, so
        the gateway's speech-to-text transcribes it. Its transcript echo becomes a `heard` event."""
        from gateway.platforms.base import cache_audio_from_bytes_async
        path = self._upload(device, voice)
        try:
            rate, block, ms = int(voice.get("rate") or 0), int(voice.get("block") or 0), int(voice.get("ms") or 0)
            wav = await asyncio.to_thread(lambda: voice_to_wav(path.read_bytes(), rate, block, ms))
        except (TypeError, ValueError) as exc:
            raise RequestError("bad_request", f"bad voice message: {exc}")
        cached = await cache_audio_from_bytes_async(wav, ".wav")
        mid = self._mid("u")
        cid = chat_id_for(device)
        if isinstance(req, int):
            now = time.monotonic()
            waiting = [w for w in self._awaiting_heard.get(cid, []) if now - w[0] < HEARD_WAIT][-7:]
            self._awaiting_heard[cid] = waiting + [(now, req)]
        self._trace("voice", chat_id=cid, mid=mid, path=cached, ms=ms, bytes=len(wav))
        event = MessageEvent(text=text, message_type=MessageType.VOICE, source=self.source_for(device),
                             message_id=mid, timestamp=datetime.datetime.now(), media_urls=[cached],
                             media_types=["audio/wav"])
        await self.handle_message(event)
        return mid

    async def inject_photos(self, device: Device, refs: list, text: str) -> str:
        """Photos from the 3DS camera (protocol.md §4.4 `attach`) → Hermes' image cache → a PHOTO event (vision)."""
        from gateway.platforms.base import cache_image_from_bytes_async
        urls, types = [], []
        for ref in refs:
            path = self._upload(device, ref)
            png = (self._blobs.meta(path.name) or {}).get("mime") == "image/png"
            try:
                data = await asyncio.to_thread(path.read_bytes)
                urls.append(await cache_image_from_bytes_async(data, ".png" if png else ".jpg"))
            except ValueError as exc:
                raise RequestError("bad_request", f"not a picture: {exc}")
            types.append("image/png" if png else "image/jpeg")
        mid = self._mid("u")
        self._trace("photos", chat_id=chat_id_for(device), mid=mid, paths=urls, text=text)
        event = MessageEvent(text=text, message_type=MessageType.PHOTO, source=self.source_for(device),
                             message_id=mid, timestamp=datetime.datetime.now(), media_urls=urls, media_types=types)
        await self.handle_message(event)
        return mid

    async def _deliver_heard(self, chat_id: str, key: bytes, text: str) -> bool:
        """Hermes echoed a transcript: tell the 3DS which voice message it belongs to (oldest waiting first)."""
        now = time.monotonic()
        waiting = [w for w in self._awaiting_heard.get(chat_id, []) if now - w[0] < HEARD_WAIT]
        if not waiting:
            self._awaiting_heard.pop(chat_id, None)
            return False
        _, req = waiting.pop(0)
        self._awaiting_heard[chat_id] = waiting
        await self._server.event(key, {"t": "heard", "req": req, "text": emoji_for_3ds(text)[:4000]})
        return True

    # --- outbound: messages -----------------------------------------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        key = self._key(chat_id)
        if key is None or self._server is None:
            return SendResult(success=False, error=f"unknown 3DS chat {chat_id}", error_kind="not_found")
        heard = _heard_text(content or "") if self._awaiting_heard.get(chat_id) else None
        if heard is not None and await self._deliver_heard(chat_id, key, heard):
            self._trace("heard", chat_id=chat_id, text=heard)
            return SendResult(success=True, message_id=self._mid("h"))
        text, streaming = _strip_cursor(content or "")
        streaming = streaming or bool((metadata or {}).get("expect_edits"))
        text = _hide_media(text, final=not streaming)
        mid = self._mid()
        self._trace("send", chat_id=chat_id, mid=mid, streaming=streaming, content=content, metadata=metadata)
        if streaming:
            rich, spans = stream_to_rich(text.lstrip())
            await self._server.post_message(key, mid, rich, final=False, spans=spans or None)
        else:
            rich, spans = markdown_to_rich(text.strip())
            await self._server.post_message(key, mid, rich, final=True, spans=spans or None)
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False) -> SendResult:
        key = self._key(chat_id)
        if key is None or self._server is None:
            return SendResult(success=False, error="unknown chat", error_kind="not_found")
        content = content or ""
        self._trace("edit", chat_id=chat_id, mid=message_id, finalize=finalize, content=content)
        if message_id in self._approval_cards:  # approval timed out: card is rewritten with a notice
            self._approval_cards.pop(message_id, None)
            await self._server.clear_pending(key, message_id,
                                             {"t": "approval_done", "req": message_id, "choice": "timeout"})
            rich, spans = markdown_to_rich(content)
            await self._server.post_message(key, self._mid(), rich, role="system", kind="notice", spans=spans or None)
            return SendResult(success=True, message_id=message_id)
        text, streaming = _strip_cursor(content)
        final = finalize or not streaming
        text = _hide_media(text, final)
        # Spans go with every update, so formatting shows while the text is still being typed out.
        rich, spans = markdown_to_rich(text.strip()) if final else stream_to_rich(text.lstrip())
        ok = await self._server.update_message(key, message_id, rich, final=final, spans=spans)
        if not ok:
            return SendResult(success=False, error="message not found", error_kind="not_found")
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        key = self._key(chat_id)
        if key is None or self._server is None:
            return False
        self._trace("delete", chat_id=chat_id, mid=message_id)
        return await self._server.delete_message(key, message_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        key = self._key(chat_id)
        if key is not None and self._server is not None:
            await self._server.set_typing(key, True)

    async def stop_typing(self, chat_id: str) -> None:
        key = self._key(chat_id)
        if key is not None and self._server is not None:
            await self._server.set_typing(key, False)

    # --- outbound: media (protocol.md §4.7) -------------------------------------------------

    async def _post_attachments(self, chat_id: str, attach: list, caption: Optional[str]) -> SendResult:
        """One `msg` carrying `attach`; the caption goes through the same rich-text/emoji path as `send`."""
        key = self._key(chat_id)
        if key is None or self._server is None:
            return SendResult(success=False, error=f"unknown 3DS chat {chat_id}", error_kind="not_found")
        text = (caption or "").strip()
        rich, spans = markdown_to_rich(text) if text else ("", [])
        # Hermes streams the reply before delivering its MEDIA files and never finalizes a preview that held
        # nothing but the (hidden) tag; drop such blank bubbles so they don't sit open in the chat.
        for blank in self._server.open_blank_messages(key):
            await self._server.delete_message(key, blank)
        mid = self._mid()
        self._trace("send_media", chat_id=chat_id, mid=mid, caption=caption, attach=attach)
        await self._server.post_message(key, mid, rich, final=True, spans=spans or None, attach=attach)
        return SendResult(success=True, message_id=mid)

    async def _send_local(self, chat_id: str, path: str, caption: Optional[str], *, name: Optional[str] = None,
                          try_image: Optional[bool] = None, try_audio: Optional[bool] = None,
                          voice: bool = False) -> SendResult:
        if self._blobs is None:
            return SendResult(success=False, error="media store unavailable")
        p = Path(os.path.expanduser(str(path or "")))
        if not p.is_file():
            return SendResult(success=False, error="file not found", error_kind="not_found")
        try:
            att = await asyncio.to_thread(build_attachment, self._blobs, p, name, None, try_image, try_audio)
        except OSError as exc:
            return SendResult(success=False, error=f"cannot read file: {exc}")
        if voice and att.get("kind") == "audio":
            att["voice"] = True  # a spoken reply: the 3DS may play it as it arrives
        return await self._post_attachments(chat_id, [att], caption)

    async def _download(self, url: str) -> Tuple[bytes, str, str]:
        """GET a media URL: http(s) only, public hosts only (re-checked on every redirect), 25 MiB cap."""
        try:
            from tools.url_safety import is_safe_url
        except Exception:  # not running inside Hermes (tests, tools): nothing to consult
            is_safe_url = None

        async def is_safe(u: str) -> bool:
            return await asyncio.to_thread(is_safe_url, u)

        return await fetch.download(url, max_bytes=DOWNLOAD_MAX, timeout=DOWNLOAD_TIMEOUT, total=DOWNLOAD_TOTAL,
                              is_safe=is_safe if is_safe_url else None)

    async def _attachment_from_url(self, url: str) -> dict:
        import mimetypes
        data, mime, path = await self._download(url)
        name = clean_name(path.rsplit("/", 1)[-1], "image")
        if not os.path.splitext(name)[1] and mime.startswith("image/"):
            name += mimetypes.guess_extension(mime) or ""
        return await asyncio.to_thread(build_attachment_bytes, self._blobs, data, name,
                                       mime if mime.startswith("image/") else None, True)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self._send_local(chat_id, image_path, caption, try_image=True)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        if image_url.startswith("file://"):
            from urllib.request import url2pathname
            return await self.send_image_file(chat_id, url2pathname(image_url[7:]), caption)
        if self._blobs is None:
            return SendResult(success=False, error="media store unavailable")
        try:
            att = await self._attachment_from_url(image_url)
        except Exception as exc:
            logger.warning("3DS: image download failed (%s): %s", type(exc).__name__, exc)
            return SendResult(success=False, error=f"image download failed: {exc}")
        return await self._post_attachments(chat_id, [att], caption)

    async def send_animation(self, chat_id: str, animation_url: str, caption: Optional[str] = None,
                             reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                             **kwargs) -> SendResult:
        """GIFs are sent as pictures: the viewer shows the first frame."""
        return await self.send_image(chat_id, animation_url, caption, reply_to, metadata)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        name = file_name or os.path.basename(str(file_path))
        return await self._send_local(chat_id, file_path, caption, name=name, try_image=is_image_name(name))

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        return await self._send_local(chat_id, audio_path, caption, try_image=False, try_audio=True, voice=True)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        return await self._send_local(chat_id, video_path, caption, try_image=False)

    async def send_multiple_images(self, chat_id: str, images: List[Tuple[str, str]],
                                   metadata: Optional[Dict[str, Any]] = None, human_delay: float = 0.0) -> SendResult:
        """Several pictures in as few messages as fit (one `msg` carries up to ATTACH_PER_MESSAGE).
        Succeeds when at least one was delivered."""
        if not images:
            return SendResult(success=False, error="no images to send")
        if self._blobs is None:
            return SendResult(success=False, error="media store unavailable")

        async def one(url: str) -> Optional[dict]:
            try:
                if url.startswith("file://"):
                    from urllib.request import url2pathname
                    p = Path(url2pathname(url[7:]))
                    return await asyncio.to_thread(build_attachment, self._blobs, p, None, None, True)
                return await self._attachment_from_url(url)
            except Exception as exc:
                logger.warning("3DS: skipping image (%s): %s", type(exc).__name__, exc)
                return None

        built = await asyncio.gather(*(one(url) for url, _ in images))
        ok = [(att, alt) for att, (_, alt) in zip(built, images) if att is not None]
        if not ok:
            return SendResult(success=False, error="all images failed to send")
        last = SendResult(success=False, error="nothing sent")
        for i in range(0, len(ok), ATTACH_PER_MESSAGE):
            group = ok[i:i + ATTACH_PER_MESSAGE]
            alts = "\n".join(a.strip() for _, a in group if a and a.strip())
            last = await self._post_attachments(chat_id, [att for att, _ in group], alts or None)
            if not last.success:
                return last
        return last

    async def open_blob(self, blob: str) -> None:
        """'Open on PC': hand an original file to the desktop's default app."""
        meta = self._blobs.meta(blob) if self._blobs else None
        if meta is None:
            raise RequestError("not_found", "no such blob")
        if meta.get("role", "file") != "file":
            raise RequestError("bad_request", "only original files can be opened")
        path = self._blobs.original_path(blob)
        if path is None:
            raise RequestError("not_found", "file is gone")
        if not path.suffix or path.suffix.lower() in _NO_OPEN_EXTS:  # no extension: could be a program
            raise RequestError("denied", "this file type is not opened remotely")
        if sys.platform not in ("win32", "darwin") and not (os.environ.get("DISPLAY")
                                                            or os.environ.get("WAYLAND_DISPLAY")):
            # A headless gateway (systemd user service, Steam Deck in Game Mode) has no desktop to open it on.
            raise RequestError("unavailable", "the PC has no desktop session to open it on")

        def launch() -> None:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 - default app; code-running types refused above
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            await asyncio.to_thread(launch)
        except OSError as exc:
            raise RequestError("internal", f"cannot open: {exc}")

    # --- outbound: tool activity ---------------------------------------------------------

    def set_status_text(self, chat_id: str, text: Optional[str]) -> None:
        """Hermes' live-status hook: a phrase when a tool starts, None when it completes. Called from the
        agent worker thread, so the protocol event is scheduled onto the gateway loop."""
        super().set_status_text(chat_id, text)
        key = self._key(chat_id)
        if key is None or self._server is None or self._loop is None:
            return
        ev = {"t": "tool", "state": "start", "text": emoji_for_3ds(str(text))[:160]} if text else {"t": "tool", "state": "done"}
        self._trace("status", chat_id=chat_id, text=text)
        try:
            asyncio.run_coroutine_threadsafe(self._server.event(key, ev), self._loop)
        except RuntimeError:
            pass

    # --- outbound: approvals and clarify ---------------------------------------------------

    async def _send_exec_approval_prompt(self, prompt) -> SendResult:
        key = self._key(prompt.chat_id)
        if key is None or self._server is None:
            return SendResult(success=False, error="unknown chat", error_kind="not_found")
        req = self._mid("a")
        self._approval_cards[req] = (prompt.chat_id, prompt.session_key)
        await self._server.set_pending(key, req, {
            "t": "approval", "req": req, "text": markdown_to_rich(_cut(prompt.text, 4000))[0],
            "command": _cut(prompt.command, 8000), "description": _cut(prompt.description, 1000), "actions": [list(a) for a in prompt.actions],
        })
        return SendResult(success=True, message_id=req)

    async def resolve_approval(self, device: Device, req: str, choice: str) -> int:
        from tools.approval import resolve_gateway_approval
        card = self._approval_cards.get(req)
        if card is None or self._server.pending(device.key, req) is None:
            raise RequestError("not_found", "approval already answered or expired")
        if choice not in ("once", "session", "always", "deny"):
            raise RequestError("bad_request", "choice must be once, session, always or deny")
        # Hermes resolves approvals per session in FIFO order (as the Telegram/Discord buttons do).
        n = resolve_gateway_approval(card[1], choice)
        self._approval_cards.pop(req, None)
        await self._server.clear_pending(device.key, req, {"t": "approval_done", "req": req, "choice": choice})
        return n

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        key = self._key(chat_id)
        if key is None or self._server is None:
            return SendResult(success=False, error="unknown chat", error_kind="not_found")
        multi = False
        with contextlib.suppress(Exception):
            from tools import clarify_gateway as cg
            with cg._lock:
                multi = bool(getattr(cg._entries.get(clarify_id), "multi_select", False))
        if not choices:
            with contextlib.suppress(Exception):
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
        await self._server.set_pending(key, f"c:{clarify_id}", {
            "t": "clarify", "cid": clarify_id, "question": markdown_to_rich(question)[0],
            "choices": [str(c) for c in (choices or [])], "multi": multi,
        })
        return SendResult(success=True, message_id=f"c:{clarify_id}")

    async def retire_clarify_card(self, clarify_id: str, notice: Optional[str] = None) -> None:
        for cid, key in list(self._chat_keys.items()):
            if self._server and self._server.pending(key, f"c:{clarify_id}") is not None:
                await self._server.clear_pending(key, f"c:{clarify_id}", {"t": "clarify_done", "cid": clarify_id})
                if notice:
                    await self._server.post_message(key, self._mid(), markdown_to_rich(notice)[0],
                                                    role="system", kind="notice")

    async def answer_clarify(self, device: Device, cid: str, choice: Optional[int], text: Optional[str],
                             picks: Optional[list[int]] = None) -> bool:
        from tools.clarify_gateway import resolve_gateway_clarify
        prompt = self._server.pending(device.key, f"c:{cid}")
        if prompt is None:
            raise RequestError("not_found", "question already answered or expired")
        choices = prompt.get("choices") or []
        if choice is not None or picks:
            idx = picks if picks else [choice]
            if not all(isinstance(i, int) and 0 <= i < len(choices) for i in idx):
                raise RequestError("bad_request", "choice out of range")
            labels = list(dict.fromkeys(choices[i] for i in idx))
            # The clarify tool decodes a multi-select answer from a JSON array of labels.
            response = json.dumps(labels, ensure_ascii=False) if prompt.get("multi") else labels[0]
        elif text is not None:
            response = str(text)
            if choices:  # "2" or "1,3" means those choices, as on the other platforms
                with contextlib.suppress(Exception):
                    from tools import clarify_gateway as cg
                    with cg._lock:
                        entry = cg._entries.get(cid)
                    if entry is not None:
                        coerced, _ = cg._coerce_text_response_detailed(entry, response)
                        if coerced is not None:
                            response = coerced
        else:
            raise RequestError("bad_request", "answer needs choice, choices or text")
        ok = resolve_gateway_clarify(cid, response)
        await self._server.clear_pending(device.key, f"c:{cid}", {"t": "clarify_done", "cid": cid})
        return ok


class _Backend:
    """Request handling for one adapter (protocol.md §4.4–4.6)."""

    def __init__(self, adapter: ThreeDSAdapter):
        self.a = adapter

    async def welcome_state(self, device: Device) -> dict[str, Any]:
        self.a.register_device(device)
        runner = getattr(self.a, "gateway_runner", None)
        state: dict[str, Any] = {"chat_id": chat_id_for(device)}
        # Whether Hermes speaks its replies in this chat (/voice on|off), so the 3DS can match its own setting.
        speaks = getattr(self.a, "_should_auto_tts_for_chat", None)
        if callable(speaks):
            with contextlib.suppress(Exception):
                state["voice"] = bool(speaks(state["chat_id"]))
        if runner is not None:
            with contextlib.suppress(Exception):
                state["session"] = await compat.current_session(runner, self.a.source_for(device))
                state["session"].pop("key", None)
        return state

    async def on_connect(self, device: Device) -> None:
        logger.info("3DS: %s (%s) connected", device.name, device.fp)

    async def on_disconnect(self, device: Device) -> None:
        logger.info("3DS: %s (%s) disconnected", device.name, device.fp)

    def _runner(self):
        runner = getattr(self.a, "gateway_runner", None)
        if runner is None:
            raise RequestError("unavailable", "gateway runner not ready")
        return runner

    def _busy(self, device: Device) -> bool:
        with contextlib.suppress(Exception):
            key = self.a._source_session_key(self.a.source_for(device))
            return key in self.a._active_sessions
        return False

    async def handle(self, device: Device, msg: dict[str, Any]) -> dict[str, Any] | None:
        t = msg["t"]
        a = self.a

        def page() -> tuple[int, int]:
            return max(0, int(msg.get("offset") or 0)), max(1, min(50, int(msg.get("limit") or 20)))

        if t == "send":
            text = str(msg.get("text") or "").strip()
            voice, attach = msg.get("voice"), msg.get("attach")
            if len(text) > 16000:
                raise RequestError("too_large", "message too long")
            if voice is not None:
                if not isinstance(voice, dict):
                    raise RequestError("bad_request", "voice must be an object")
                return {"t": "send_r", "mid": await a.inject_voice(device, voice, text, msg.get("id"))}
            if attach is not None:
                if not isinstance(attach, list) or not attach or len(attach) > PHOTOS_PER_SEND:
                    raise RequestError("bad_request", f"attach must list 1 to {PHOTOS_PER_SEND} uploads")
                return {"t": "send_r", "mid": await a.inject_photos(device, attach, text)}
            if not text:
                raise RequestError("bad_request", "empty message")
            return {"t": "send_r", "mid": await a.inject_text(device, text)}
        if t == "stop":
            await a.inject_text(device, "/stop")
            return {"t": "stop_r"}
        if t == "blob.open":
            await a.open_blob(str(msg.get("blob")))
            return {"t": "blob.open_r"}
        if t == "approve":
            n = await a.resolve_approval(device, str(msg.get("req")), str(msg.get("choice")))
            return {"t": "approve_r", "resolved": n}
        if t == "answer":
            choice, picks = msg.get("choice"), msg.get("choices")
            if picks is not None and not isinstance(picks, list):
                raise RequestError("bad_request", "choices must be a list")
            ok = await a.answer_clarify(device, str(msg.get("cid")), int(choice) if choice is not None else None,
                                        msg.get("text"), picks)
            return {"t": "answer_r", "ok": ok}

        runner = self._runner()
        source = a.source_for(device)
        if t == "status":
            return {"t": "status_r", "now": time.time(), **await compat.status(runner, source, self._busy(device))}
        if t == "sessions.list":
            off, lim = page()
            return {"t": "sessions.list_r", "items": await compat.sessions_list(runner, source, off, lim, msg.get("q"))}
        if t == "sessions.messages":
            lim = max(1, min(100, int(msg.get("limit") or 30)))
            before = msg.get("before")
            items = await compat.session_messages(runner, source, str(msg.get("sid")), lim,
                                                  int(before) if before is not None else None)
            return {"t": "sessions.messages_r", "items": items}
        if t == "sessions.resume":
            sid = str(msg.get("sid") or "").strip()
            if not sid or any(c.isspace() for c in sid):
                raise RequestError("bad_request", "bad session id")
            await a.inject_text(device, f"/resume {sid}")
            return {"t": "sessions.resume_r"}
        if t == "sessions.new":
            title = " ".join(str(msg.get("title") or "").split())[:80]
            await a.inject_text(device, f"/new {title}".strip())
            return {"t": "sessions.new_r"}
        if t == "commands.list":
            return {"t": "commands.list_r", "items": await compat.commands_list()}
        if t == "models.list":
            return {"t": "models.list_r", **await compat.models_list()}
        if t == "skills.list":
            return {"t": "skills.list_r", "items": await compat.skills_list()}
        if t == "tools.list":
            return {"t": "tools.list_r", "items": await compat.tools_list(PLATFORM)}
        if t == "jobs.list":
            return {"t": "jobs.list_r", "items": await compat.jobs_list()}
        if t == "jobs.action":
            return {"t": "jobs.action_r", "job": await compat.jobs_action(str(msg.get("job")), str(msg.get("op")))}
        if "id" in msg:
            raise RequestError("bad_request", f"unknown request {t!r}")
        return None  # unknown events are ignored (forward compatibility)


def _hermes_version() -> str:
    with contextlib.suppress(Exception):
        from importlib.metadata import version
        return version("hermes-agent")
    return ""
