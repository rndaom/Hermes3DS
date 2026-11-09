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
import secrets
import socket
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from . import compat, speech
from .h3proto.away import AwayManager, effective_mode, parse_address, parse_mode
from .h3proto.audio import MAX_SECONDS as MAX_SPEECH_SECONDS, ffmpeg_path, voice_to_wav
from .h3proto import avatar
from .h3proto.blobs import BlobStore
from .h3proto import fetch
from .h3proto.media import (build_attachment, build_attachment_bytes, build_speech_attachment, clean_name,
                            is_image_name)
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
# Message ids must not repeat after a gateway restart: Hermes keeps every inbound id (its session store
# refuses a reused id with other content, `admission_conflict`, and treats one with the same content as a
# replay it doesn't run again), and the 3DS keeps outbound ids in its chat cache. Each adapter gets its own
# tag (Hermes can make a new one in the same process: reconnects, profiles); this one is for tests that skip
# __init__. 40 bits; with the counter it stays inside the 3DS's 23-character speech ids.
_RUN_TAG = secrets.token_hex(5)
ATTACH_PER_MESSAGE = 8    # keeps the msg JSON far below the 64 KiB frame limit
PHOTOS_PER_SEND = 4       # pictures one 3DS `send` may carry (protocol.md §4.4)
HEARD_WAIT = 600.0        # seconds a voice message waits for Hermes' transcript echo
# "Open on PC" hands the file to its default app, so only types that open in a viewer: pictures, sound, video
# and documents without macros. A blocklist can't keep up with everything a desktop will run (.pif, .msc, .chm,
# .iso, xdg-open guessing from content, ...), and legacy Office files, HTML and SVG can carry scripts.
_OPEN_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif",
              ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac",
              ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi",
              ".pdf", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".toml",
              ".rtf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"}


def _data_dir():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLATFORM)


def chat_id_for(device: Device, agent: Optional[str] = None) -> str:
    """The Hermes chat of a device: `dev:<fp>` with the main agent, `dev:<fp>/<profile>` with a bot (protocol.md
    §4.8). Hermes keys sessions by profile already; the suffix is what tells the 3DS whose reply a send is."""
    return f"dev:{device.fp}/{agent}" if agent else f"dev:{device.fp}"


def split_chat_id(chat_id: str) -> tuple[str, str]:
    """`dev:<fp>/<profile>` → (`dev:<fp>`, `<profile>`); the main agent's chat has "" for the profile."""
    base, _, agent = str(chat_id).partition("/")
    return base, agent


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
        from gateway.platforms.base import t
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


class _Speech:
    """One streamed spoken reply (Hermes' streaming-TTS handle). The PCM is posted as one message when the model
    is done. For an app that plays `speak` events (`speech_id` set), it goes out in groups as it comes (`pacer`)
    as those events instead, and the chat gets no voice message. For an app that only plays voice messages one
    after another (`grouped`), the groups are the messages."""

    def __init__(self, chat_id: str, audio_format: Any, grouped: bool = False, speech_id: Optional[str] = None):
        self.chat_id = chat_id
        self.audio_format = audio_format
        self.audible = False
        self.aborted = False
        self.grouped = grouped
        self.speech_id = speech_id     # the `speak` events' id: played by the app, nothing posted in the chat
        self.pcm = bytearray()         # not posted yet
        self.accepted = 0              # bytes taken so far, posted or not
        self.posts = 0                 # messages posted
        self.pacer = speech.Pacer()
        self.lock = asyncio.Lock()     # one post at a time, in order
        self.timer: Optional[asyncio.TimerHandle] = None  # posts what is pending before the app runs dry
        self.tasks: set[asyncio.Task] = set()

    @property
    def seconds(self) -> float:
        return len(self.pcm) / (2.0 * self.audio_format.sample_rate)


# Bots (protocol.md §4.8). A multiplexed gateway builds one adapter per served profile and lets a profile's turns
# in only through that profile's own adapter. There is one listener (one port, one pairing), owned by the main
# profile's adapter; a bot's adapter binds nothing and shares the listener's state, and the listener hands a
# bot's messages to it. Each profile loads its own copy of this plugin (`hermes profile create --clone` copies
# plugins), so they meet in a process-wide hub rather than in this module. Only copies of the same release work
# together (COPY): a bot's copy calls the listener's server with that release's arguments and reads its shared
# state by that release's names. Bump STATE_LAYOUT when the shared attributes below change between dev builds.
STATE_LAYOUT = 1


def _plugin_version() -> str:
    with contextlib.suppress(OSError):
        for line in Path(__file__).with_name("plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    return ""


COPY = (STATE_LAYOUT, _plugin_version())
_HUB = sys.modules.setdefault("hermes3ds_bots_hub", types.ModuleType("hermes3ds_bots_hub"))
if not hasattr(_HUB, "bots"):
    _HUB.listener = None  # the main profile's adapter, which owns the listener
    _HUB.bots = {}        # served bot profile -> its adapter


def _shared(name: str) -> property:
    """Transport state a bot's adapter reads from the listener's adapter (its own copy until there is one)."""
    own = "_own" + name

    def get(self):
        listener = _HUB.listener
        src = listener if getattr(self, "_bot_profile", None) and listener is not None else self
        try:
            return src.__dict__[own]
        except KeyError:
            raise AttributeError(name) from None

    def put(self, value):
        self.__dict__[own] = value

    return property(get, put)


class ThreeDSAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    supports_status_text = True
    REQUIRES_EDIT_FINALIZE = True
    MAX_MESSAGE_LENGTH = _MAX_CHARS
    speech_groups = True  # config `speech_groups`: post a spoken reply in parts as it is written (see speech.py)
    _bot_profile: Optional[str] = None  # set for a bot's adapter (set_owner_profile)
    COPY = COPY

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = getattr(config, "extra", {}) or {}
        self.host = str(_cfg(extra, "host", "0.0.0.0"))
        self.port = int(_cfg(extra, "port", DEFAULT_PORT))
        self.discovery_port = int(_cfg(extra, "discovery_port", DEFAULT_DISCOVERY_PORT))
        self.secure = str(_cfg(extra, "security", "secure")).lower() != "plaintext"
        self.server_name = str(_cfg(extra, "name", "") or socket.gethostname())
        self.away_mode = parse_mode(_cfg(extra, "away", ""))  # resolved against the CLI flags in connect()
        self.away_port = int(_cfg(extra, "away_port", DEFAULT_AWAY_PORT))
        self.away_public_port = int(_cfg(extra, "away_public_port", 0) or 0)
        self.away_address = parse_address(_cfg(extra, "away_address", ""))
        self.speech_groups = str(_cfg(extra, "speech_groups", "true")).strip().lower() not in ("0", "false", "no", "off")
        self._away_task: Optional[asyncio.Task] = None
        self._server: Optional[Hermes3DSServer] = None
        self._chat_keys: dict[str, bytes] = {}   # chat_id -> device key
        self._chat_names: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._run_tag = secrets.token_hex(5)
        self._queued: dict[str, int] = {}  # chat_id -> messages sent while a turn was running, for `status`
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._trace_path = None
        self._approval_cards: dict[str, tuple[str, str, Optional[str]]] = {}  # card mid -> (chat_id, session_key, request_id)
        self._blobs: Optional[BlobStore] = None
        self._awaiting_heard: dict[str, list[tuple[float, int]]] = {}  # chat_id -> [(sent at, send request id)]

    @property
    def name(self) -> str:
        return "Nintendo 3DS"

    def set_owner_profile(self, profile_name: Optional[str]) -> None:
        """The multiplexed gateway names the profile a secondary adapter serves: this one is that bot's."""
        super().set_owner_profile(profile_name)
        name = (profile_name or "").strip()
        self._bot_profile = name if name and name != "default" else None

    def _receiver(self, agent: Optional[str]) -> Optional["ThreeDSAdapter"]:
        """The adapter a chat's turns enter Hermes through: the bot's own, or this one for the main agent."""
        if not agent:
            return self
        bot = _HUB.bots.get(agent)
        return bot if bot is not None and bot is not self else None

    # --- lifecycle --------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._bot_profile:  # served through the main profile's listener
            listener = _HUB.listener
            if listener is None:  # the gateway starts the main profile's adapters first
                self._set_fatal_error("no_listener", f"profile {self._bot_profile} runs the 3ds platform but the main "
                                      "profile doesn't: bots answer through the main profile's 3DS listener, so turn "
                                      "3ds on there", retryable=True)
                return False
            if getattr(listener, "COPY", None) != COPY:
                self._set_fatal_error("plugin_mismatch", f"profile {self._bot_profile} has another version of the "
                                      "3ds plugin than the main profile: update its copy", retryable=False)
                return False
            _HUB.bots[self._bot_profile] = self
            self._mark_connected()
            self._wire_plugin_handlers(None)  # handlers other plugins register for the 3ds platform, as the main one
            logger.info("3DS: bot %s answers through the main listener", self._bot_profile)
            return True
        if not self._acquire_platform_lock(PLATFORM, f"port:{self.port}", f"3DS listener on port {self.port}"):
            return False
        self._loop = asyncio.get_running_loop()
        data = _data_dir()
        self._trace_path = data / "trace.log" if (data / "trace").exists() else None
        try:
            static = load_or_create_key(data / "server_key")
        except Exception as exc:
            self._release_platform_lock()
            self._set_fatal_error("key_error", f"cannot load 3DS server key: {exc}", retryable=False)
            return False
        try:
            self._blobs = BlobStore(data / "blobs")
        except OSError as exc:
            logger.warning("3DS: blob store unavailable, media attachments disabled: %s", exc)
        away_mode = effective_mode(self.away_mode, data, self.away_address)
        server = Hermes3DSServer(
            ServerConfig(host=self.host, port=self.port, discovery_port=self.discovery_port,
                         secure=self.secure, name=self.server_name,
                         away_port=0 if away_mode == "off" or not self.secure else self.away_port),
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
        _HUB.listener = self
        for name, bot in list(_HUB.bots.items()):  # bots that came up against an earlier listener
            if getattr(bot, "COPY", None) != COPY:
                del _HUB.bots[name]
                logger.warning("3DS: bot %s has another version of the 3ds plugin than the main profile: update "
                               "its copy", name)
        for dev in server.devices.all():
            self._chat_keys[chat_id_for(dev)] = dev.key
            self._chat_names[chat_id_for(dev)] = dev.name
        if not self.secure:
            logger.warning("3DS: PLAINTEXT dev mode — traffic is unencrypted and unauthenticated")
        self._mark_connected()
        self._wire_plugin_handlers(None)
        if speech.is_pinned():  # the first spoken reply shouldn't wait for the voice model to load
            self._loop.run_in_executor(None, speech.warm_voice)
        logger.info("3DS: listening on %s:%s (%s)", self.host, server.port, "secure" if self.secure else "plaintext")
        if server.away_port:
            away = AwayManager(server, data, mode=away_mode, address=self.away_address,
                               public_port=self.away_public_port)
            self._away_task = asyncio.get_running_loop().create_task(away.run())
        else:  # paired 3DSes forget their away address; `hermes 3ds away` reports it off
            await AwayManager(server, data, mode="off").once()
        return True

    async def disconnect(self) -> None:
        if self._bot_profile:
            if _HUB.bots.get(self._bot_profile) is self:
                del _HUB.bots[self._bot_profile]
                # Its turns are gone with it: drop the approval cards and questions the 3DS still shows for it.
                for req, card in list(self._approval_cards.items()):
                    if split_chat_id(card[0])[1] == self._bot_profile:
                        self._approval_cards.pop(req, None)
                if self._server is not None:
                    with contextlib.suppress(Exception):
                        await self._server.clear_agent_pending(self._bot_profile)
            self._mark_disconnected()
            return
        if _HUB.listener is self:
            _HUB.listener = None
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
        return {"name": self._chat_names.get(split_chat_id(chat_id)[0], chat_id), "type": "dm"}

    # --- helpers ----------------------------------------------------------------

    def _key(self, chat_id: str) -> Optional[bytes]:
        return self._chat_keys.get(split_chat_id(chat_id)[0])

    def _agent(self, chat_id: str) -> str:
        """The bot a chat belongs to ("" = the main agent); events for its chat carry it (protocol.md §4.8). A bot's
        adapter sending to a plain `dev:<fp>` (an explicit `3ds:` target, a home channel copied into its profile)
        still speaks as that bot, not into the main agent's chat."""
        return split_chat_id(chat_id)[1] or (self._bot_profile or "")

    def _tag(self, ev: dict, chat_id: str) -> dict:
        agent = self._agent(chat_id)
        if agent:
            ev["agent"] = agent
        return ev

    def _mid(self, prefix: str = "m") -> str:
        return f"{prefix}{getattr(self, '_run_tag', _RUN_TAG)}-{next(self._ids)}"

    def _trace(self, what: str, **fields: Any) -> None:
        """Opt-in debug log: create <plugin-data>/3ds/trace to record every outbound call."""
        if self._trace_path is None:
            return
        with contextlib.suppress(Exception):
            line = json.dumps({"ts": round(datetime.datetime.now().timestamp(), 3), "what": what, **fields},
                              ensure_ascii=False, default=str)
            with open(self._trace_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def source_for(self, device: Device, agent: Optional[str] = None):
        cid = chat_id_for(device, agent)
        # The device proved its identity with its paired static key (Noise IK), so the adapter
        # vouches for it; no Hermes allowlist entry is needed. A bot's source comes from the bot's adapter,
        # which Hermes then treats as the one that received it.
        source = (self._receiver(agent) or self).build_source(chat_id=cid, chat_name=device.name, chat_type="dm", user_id=device.fp,
                                   user_name=device.name, role_authorized=True)
        if agent:
            # A bot's chat runs as that profile (Hermes' admission checks that it is the receiving adapter's): the
            # turn runs under the bot's home and secrets, its session is keyed in the bot's namespace, and the reply
            # goes out through the bot's adapter.
            source.profile = agent
        return source

    async def reauthorize_native_roles(self, source) -> bool:
        """Hermes' session authority re-checks a role_authorized source before it runs a turn (and when
        it recovers one after a restart); the flag alone grants nothing. Vouch again only while the device
        is still paired, so `hermes 3ds unpair` also revokes turns already queued for it."""
        server = self._server
        if server is None or source.chat_type != "dm":
            return False
        base, agent = split_chat_id(source.chat_id)
        if agent and (source.profile or "") != agent:  # a bot's chat only ever runs as that bot
            return False
        return any(dev.fp == source.user_id and chat_id_for(dev) == base for dev in server.devices.all())

    def register_device(self, device: Device) -> str:
        cid = chat_id_for(device)
        self._chat_keys[cid] = device.key
        self._chat_names[cid] = device.name
        return cid

    async def inject_text(self, device: Device, text: str, agent: Optional[str] = None) -> str:
        mid = self._mid("u")
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=self.source_for(device, agent),
                             message_id=mid, timestamp=datetime.datetime.now())
        await self._entry(agent).handle_message(event)
        return mid

    def _entry(self, agent: Optional[str]) -> "ThreeDSAdapter":
        receiver = self._receiver(agent)
        if receiver is None:
            raise RequestError("unavailable", f"{agent} isn't reachable from the 3DS: its profile needs the 3ds "
                                              f"platform on (hermes -p {agent} config set platforms.3ds.enabled true) "
                                              "and the same 3ds plugin version as the main profile")
        return receiver

    def _upload(self, device: Device, ref: Any) -> Path:
        bid = str(ref.get("blob") if isinstance(ref, dict) else "")
        path = self._server.upload_path(device, bid) if self._server else None
        if path is None:
            raise RequestError("not_found", f"upload {bid!r} isn't complete on the PC")
        return path

    async def inject_voice(self, device: Device, voice: dict, text: str, req: Any, agent: Optional[str] = None) -> str:
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
        cid = chat_id_for(device, agent)
        if isinstance(req, int):
            now = time.monotonic()
            waiting = [w for w in self._awaiting_heard.get(cid, []) if now - w[0] < HEARD_WAIT][-7:]
            self._awaiting_heard[cid] = waiting + [(now, req)]
        self._trace("voice", chat_id=cid, mid=mid, path=cached, ms=ms, bytes=len(wav))
        event = MessageEvent(text=text, message_type=MessageType.VOICE, source=self.source_for(device, agent),
                             message_id=mid, timestamp=datetime.datetime.now(), media_urls=[cached],
                             media_types=["audio/wav"])
        await self._entry(agent).handle_message(event)
        return mid

    async def inject_photos(self, device: Device, refs: list, text: str, agent: Optional[str] = None) -> str:
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
        self._trace("photos", chat_id=chat_id_for(device, agent), mid=mid, paths=urls, text=text)
        event = MessageEvent(text=text, message_type=MessageType.PHOTO, source=self.source_for(device, agent),
                             message_id=mid, timestamp=datetime.datetime.now(), media_urls=urls, media_types=types)
        await self._entry(agent).handle_message(event)
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
        await self._server.event(key, self._tag({"t": "heard", "req": req, "text": emoji_for_3ds(text)[:4000]}, chat_id))
        return True

    async def on_processing_start(self, event: MessageEvent) -> None:
        cid = event.source.chat_id
        if self._queued.get(cid, 0) > 0:
            self._queued[cid] -= 1
        await super().on_processing_start(event)

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
        agent = self._agent(chat_id)
        if streaming:
            rich, spans = stream_to_rich(text.lstrip())
            await self._server.post_message(key, mid, rich, final=False, spans=spans or None, agent=agent)
        else:
            rich, spans = markdown_to_rich(text.strip())
            await self._server.post_message(key, mid, rich, final=True, spans=spans or None, agent=agent)
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
            await self._server.post_message(key, self._mid(), rich, role="system", kind="notice", spans=spans or None,
                                            agent=self._agent(chat_id))
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
            await self._server.set_typing(key, True, self._agent(chat_id))

    async def stop_typing(self, chat_id: str) -> None:
        key = self._key(chat_id)
        if key is not None and self._server is not None:
            await self._server.set_typing(key, False, self._agent(chat_id))

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
        agent = self._agent(chat_id)
        for blank in self._server.open_blank_messages(key, agent):
            await self._server.delete_message(key, blank)
        mid = self._mid()
        self._trace("send_media", chat_id=chat_id, mid=mid, caption=caption, attach=attach)
        await self._server.post_message(key, mid, rich, final=True, spans=spans or None, attach=attach, agent=agent)
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

    async def play_tts(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        """A spoken reply made in one piece (Hermes' auto-TTS without streaming): an app that plays `speak` events
        gets it as one, so it is heard without a voice message under the reply; any other gets the voice message."""
        key = self._key(chat_id)
        if key is None or self._server is None or not self._server.has_cap(key, speech.SPEAK_CAP, offline=True):
            return await self.send_voice(chat_id, audio_path, **kwargs)
        if self._blobs is None:
            return SendResult(success=False, error="media store unavailable")
        p = Path(os.path.expanduser(str(audio_path or "")))
        if not p.is_file():
            return SendResult(success=False, error="file not found", error_kind="not_found")
        try:
            att = await asyncio.to_thread(build_attachment, self._blobs, p, None, None, False, True)
        except OSError as exc:
            return SendResult(success=False, error=f"cannot read file: {exc}")
        if att.get("kind") != "audio":
            return SendResult(success=False, error="the spoken reply could not be converted for the 3DS")
        att["voice"] = True
        self._trace("speech", chat_id=chat_id, ms=att.get("ms"), file=p.name)
        await self._server.event(key, self._tag({"t": "speak", "speech": self._mid("s"), "attach": att}, chat_id))
        return SendResult(success=True)

    # --- spoken replies as they are written (Hermes' streaming TTS; see speech.py) ------------------

    def supports_streaming_tts(self, chat_id: str, audio_format: Any) -> bool:
        return (self._blobs is not None and self._server is not None and self._key(chat_id) is not None
                and ffmpeg_path() is not None and audio_format.channels == 1 and audio_format.sample_width == 2
                and 4000 <= audio_format.sample_rate <= 48000)

    async def begin_streaming_tts(self, chat_id: str, audio_format: Any,
                                  metadata: Optional[Dict[str, Any]] = None) -> Optional[_Speech]:
        key = self._key(chat_id)
        live = bool(self.speech_groups and key is not None and self._server is not None)
        if live and self._server.has_cap(key, speech.SPEAK_CAP, offline=True):  # an app away now hears none of it
            return _Speech(chat_id, audio_format, speech_id=self._mid("s"))
        return _Speech(chat_id, audio_format, live and self._server.has_cap(key, speech.VOICE_QUEUE_CAP))

    async def write_streaming_tts(self, handle: _Speech, chunk: bytes) -> None:
        room = (MAX_SPEECH_SECONDS * handle.audio_format.sample_rate * 2 - handle.accepted) & ~1
        if handle.aborted or room <= 0:
            return
        chunk = chunk[:room]
        handle.pcm += chunk
        handle.accepted += len(chunk)
        if handle.grouped or handle.speech_id:
            await self._pace_speech(handle)

    async def _pace_speech(self, handle: _Speech) -> None:
        """Post the pending speech when the pacer says so, else make sure it goes out before the app runs dry."""
        if handle.timer is not None:
            handle.timer.cancel()
            handle.timer = None
        if handle.pcm and handle.pacer.due(handle.seconds):
            await self._post_speech(handle)
        elif handle.pcm:
            handle.timer = asyncio.get_running_loop().call_later(handle.pacer.wait(), self._speech_timer, handle)

    def _speech_timer(self, handle: _Speech) -> None:
        handle.timer = None
        task = asyncio.ensure_future(self._post_speech(handle, quiet=True))
        handle.tasks.add(task)
        task.add_done_callback(handle.tasks.discard)

    async def _post_speech(self, handle: _Speech, quiet: bool = False) -> None:
        """Encode what is pending and post it as one voice message, or as a `speak` event when the app plays
        those. `quiet`: a background post, so a failure is logged (nobody awaits it) instead of raised."""
        async with handle.lock:
            pcm, handle.pcm = bytes(handle.pcm), bytearray()
            if handle.aborted or not pcm:
                return
            try:
                att = await asyncio.to_thread(build_speech_attachment, self._blobs, pcm, handle.audio_format.sample_rate)
                if att is None:
                    raise RuntimeError("spoken reply could not be encoded")
                if handle.aborted:  # the reply was cut while this was being encoded
                    return
                self._trace("speech", chat_id=handle.chat_id, ms=att["ms"], pcm=len(pcm), part=handle.posts + 1)
                key = self._key(handle.chat_id)
                if handle.speech_id and key is not None and self._server is not None:
                    await self._server.event(key, self._tag({"t": "speak", "speech": handle.speech_id, "attach": att},
                                                            handle.chat_id))
                elif not handle.speech_id:
                    await self._post_attachments(handle.chat_id, [att], None)
            except Exception as exc:
                if not quiet:
                    raise
                logger.warning("3DS: a part of the spoken reply was not sent: %s", exc)
                return
            handle.posts += 1
            handle.pacer.posted(att["ms"] / 1000.0)

    async def finish_streaming_tts(self, handle: _Speech, *, interrupted: bool = False) -> None:
        if handle.timer is not None:
            handle.timer.cancel()
            handle.timer = None
        if interrupted:
            handle.aborted = True
        if handle.aborted:
            handle.pcm = bytearray()
        await self._post_speech(handle)
        if handle.tasks:  # a timed post still running was queued before this one, so it has gone out
            await asyncio.gather(*handle.tasks, return_exceptions=True)

    async def abort_streaming_tts(self, handle: _Speech, error: Optional[str] = None) -> None:
        handle.aborted = True
        handle.pcm = bytearray()
        if handle.timer is not None:
            handle.timer.cancel()
            handle.timer = None

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
        if path.suffix.lower() not in _OPEN_EXTS:
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
        self._tag(ev, chat_id)
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
        # Hermes 0.23+ names the request the card answers; 0.22.x sends no id (FIFO, below).
        self._approval_cards[req] = (prompt.chat_id, prompt.session_key, getattr(prompt, "request_id", None))
        await self._server.set_pending(key, req, self._tag({
            "t": "approval", "req": req, "text": markdown_to_rich(_cut(prompt.text, 4000))[0],
            "command": _cut(prompt.command, 8000), "description": _cut(prompt.description, 1000), "actions": [list(a) for a in prompt.actions],
        }, prompt.chat_id))
        return SendResult(success=True, message_id=req)

    async def resolve_approval(self, device: Device, req: str, choice: str) -> int:
        from tools.approval import resolve_gateway_approval
        card = self._approval_cards.get(req)
        if card is None or self._server.pending(device.key, req) is None:
            raise RequestError("not_found", "approval already answered or expired")
        if choice not in ("once", "session", "always", "deny"):
            raise RequestError("bad_request", "choice must be once, session, always or deny")
        _, session_key, request_id = card
        if request_id:  # only this card's request: a stale card can't answer the next one in the queue
            n = resolve_gateway_approval(session_key, choice, request_id=request_id)
        else:  # Hermes 0.22.x: per session, oldest first (as its own buttons did)
            oldest = next((r for r, c in self._approval_cards.items() if c[1] == session_key), req)
            if oldest != req:  # Hermes would apply this answer to the older request, not the one tapped
                raise RequestError("bad_request", "answer the earlier request first")
            n = resolve_gateway_approval(session_key, choice)
        self._approval_cards.pop(req, None)
        # n == 0: the request already settled (timed out, or /approve answered it), so nothing ran.
        await self._server.clear_pending(device.key, req, {"t": "approval_done", "req": req,
                                                           "choice": choice if n else "timeout"})
        return n

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        key = self._key(chat_id)
        if key is None or self._server is None:
            return SendResult(success=False, error="unknown chat", error_kind="not_found")
        multi = False
        with contextlib.suppress(Exception):
            from tools.clarify_gateway import get_pending
            multi = bool(getattr(get_pending(clarify_id), "multi_select", False))
        if not choices:
            with contextlib.suppress(Exception):
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
        await self._server.set_pending(key, f"c:{clarify_id}", self._tag({
            "t": "clarify", "cid": clarify_id, "question": markdown_to_rich(question)[0],
            "choices": [str(c) for c in (choices or [])], "multi": multi,
        }, chat_id))
        return SendResult(success=True, message_id=f"c:{clarify_id}")

    async def retire_clarify_card(self, clarify_id: str, notice: Optional[str] = None) -> None:
        for cid, key in list(self._chat_keys.items()):
            prompt = self._server.pending(key, f"c:{clarify_id}") if self._server else None
            if prompt is not None:
                await self._server.clear_pending(key, f"c:{clarify_id}", {"t": "clarify_done", "cid": clarify_id})
                if notice:
                    await self._server.post_message(key, self._mid(), markdown_to_rich(notice)[0],
                                                    role="system", kind="notice", agent=str(prompt.get("agent") or ""))

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
                    from tools.clarify_gateway import SKIPPED, get_pending
                    from .h3proto.clarify_text import coerce
                    entry = get_pending(cid)
                    coerced = coerce(entry, response, SKIPPED) if entry is not None else None
                    if coerced is not None:
                        response = coerced
        else:
            raise RequestError("bad_request", "answer needs choice, choices or text")
        ok = resolve_gateway_clarify(cid, response)
        await self._server.clear_pending(device.key, f"c:{cid}", {"t": "clarify_done", "cid": cid})
        return ok


for _n in ("_server", "_chat_keys", "_chat_names", "_queued", "_loop", "_trace_path", "_approval_cards", "_blobs",
           "_awaiting_heard", "_ids", "_run_tag"):
    setattr(ThreeDSAdapter, _n, _shared(_n))
del _n


class _Backend:
    """Request handling for one adapter (protocol.md §4.4–4.6)."""

    # Answered beside the device's read loop: making a profile takes seconds to minutes, and meanwhile the device's
    # pings, sends and approvals must still get through.
    slow_requests = frozenset({"agents.create"})

    def __init__(self, adapter: ThreeDSAdapter):
        self.a = adapter
        self._avatars: dict[str, tuple[Any, dict[str, Any]]] = {}  # profile -> (what it was drawn from, blob ref)

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

    def _busy(self, device: Device, agent: Optional[str] = None) -> bool:
        try:
            receiver = self.a._receiver(agent) or self.a  # a bot's turns run on its own adapter
            key = receiver._source_session_key(self.a.source_for(device, agent))
            return key in receiver._active_sessions
        except Exception as exc:  # Hermes internals: if they move, say so once instead of "never busy" in silence
            if not getattr(self, "_busy_warned", False):
                self._busy_warned = True
                logger.warning("3DS: cannot tell whether a turn is running (%s): the busy mark stays off", exc)
            return False

    def _agent(self, msg: dict[str, Any]) -> Optional[str]:
        """The bot a request is for (protocol.md §4.8): None for the main agent, else a profile this gateway serves."""
        name = msg.get("agent")
        if name is None or name == "":
            return None
        if not isinstance(name, str) or not compat.PROFILE_ID.match(name):
            raise RequestError("bad_request", "bad agent name")
        runner = getattr(self.a, "gateway_runner", None)
        served = compat.served_agents(runner) if runner is not None else []
        if served and name == served[0]:
            return None
        if name not in served[1:]:
            raise RequestError("not_found", f"no bot called {name} on this gateway")
        return name

    def _avatar(self, item: dict[str, Any]) -> None:
        """Swap the item's `_look` for `avatar`, the blob of its picture. A picture is drawn once per look and
        reused while its blob lasts (blobs expire after a week; the gateway runs for longer). Blocking."""
        spec = item.pop("_look", None)
        store = getattr(self.a, "_blobs", None)
        if not spec or store is None:
            return
        key = (spec["photo"], spec["stamp"], spec["shape"], tuple(spec["color"]))
        hit = self._avatars.get(item["id"])
        meta = store.meta(hit[1]["id"]) if hit and hit[0] == key else None
        if meta is not None and time.time() - float(meta.get("created", 0)) < store.ttl / 2:
            item["avatar"] = hit[1]
            return
        png = avatar.render(spec)
        if png is None:
            return
        ref = store.put_bytes(png, "image/png", f"{item['id']}.png", "thumb")
        self._avatars[item["id"]] = (key, ref)
        item["avatar"] = ref

    async def _agents(self, device: Device) -> dict[str, Any]:
        """The roster with this device's live state per bot: a turn running, Hermes speaking its replies."""
        out = await compat.agents_list(self._runner())
        await asyncio.to_thread(lambda: [self._avatar(it) for it in out["items"]])
        for it in out["items"]:
            self._live(device, it)
        return out

    def _live(self, device: Device, it: dict[str, Any]) -> None:
        agent = None if it["primary"] else it["id"]
        receiver = self.a._receiver(agent)
        it["busy"] = self._busy(device, agent)
        it["ready"] = receiver is not None  # its turns have a way in (its profile runs the 3ds platform)
        speaks = getattr(receiver, "_should_auto_tts_for_chat", None)  # /voice is kept per adapter
        if callable(speaks):
            with contextlib.suppress(Exception):
                it["voice"] = bool(speaks(chat_id_for(device, agent)))

    async def handle(self, device: Device, msg: dict[str, Any]) -> dict[str, Any] | None:
        t = msg["t"]
        if t == "agents.list":
            return {"t": "agents.list_r", **await self._agents(device)}
        if t == "agents.create":
            name = compat.slug(str(msg.get("name") or ""))
            desc = " ".join(str(msg.get("desc") or "").split())
            item = await compat.agents_create(self._runner(), name, desc)
            await asyncio.to_thread(self._avatar, item)
            self._live(device, item)
            return {"t": "agents.create_r", "item": item}
        if t in ("approve", "answer"):  # keyed by the prompt itself; a bot that left can still have its card cleared
            return await self._handle(device, msg, None)
        agent = self._agent(msg)
        if agent is None:
            return await self._handle(device, msg, None)
        # Only profiles that run the 3ds platform themselves are open to the 3DS: Bot Mode lists every profile, but
        # one that never turned 3ds on keeps its sessions, skills and jobs to itself.
        self.a._entry(agent)
        async with compat.profile_scope(compat.agent_home(self._runner(), agent)):
            return await self._handle(device, msg, agent)

    async def _handle(self, device: Device, msg: dict[str, Any], agent: Optional[str]) -> dict[str, Any] | None:
        t = msg["t"]
        a = self.a

        def page() -> tuple[int, int]:
            return max(0, int(msg.get("offset") or 0)), max(1, min(50, int(msg.get("limit") or 20)))

        if t == "send":
            # Count what waits behind a running turn; Hermes may merge queued messages into one turn, so the
            # count is only trusted while the chat is busy and starts over when it isn't.
            cid = chat_id_for(device, agent)
            a._queued[cid] = a._queued.get(cid, 0) + 1 if self._busy(device, agent) else 0
            text = str(msg.get("text") or "").strip()
            voice, attach = msg.get("voice"), msg.get("attach")
            if len(text) > 16000:
                raise RequestError("too_large", "message too long")
            if voice is not None:
                if not isinstance(voice, dict):
                    raise RequestError("bad_request", "voice must be an object")
                return {"t": "send_r", "mid": await a.inject_voice(device, voice, text, msg.get("id"), agent)}
            if attach is not None:
                if not isinstance(attach, list) or not attach or len(attach) > PHOTOS_PER_SEND:
                    raise RequestError("bad_request", f"attach must list 1 to {PHOTOS_PER_SEND} uploads")
                return {"t": "send_r", "mid": await a.inject_photos(device, attach, text, agent)}
            if not text:
                raise RequestError("bad_request", "empty message")
            return {"t": "send_r", "mid": await a.inject_text(device, text, agent)}
        if t == "stop":
            await a.inject_text(device, "/stop", agent)
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
        source = a.source_for(device, agent)
        if t == "status":
            busy = self._busy(device, agent)
            queued = getattr(a, "_queued", {}).get(chat_id_for(device, agent), 0) if busy else 0
            return {"t": "status_r", "now": time.time(), **await compat.status(runner, source, busy, queued)}
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
            await a.inject_text(device, f"/resume {sid}", agent)
            return {"t": "sessions.resume_r"}
        if t == "sessions.new":
            title = " ".join(str(msg.get("title") or "").split())[:80]
            await a.inject_text(device, f"/new {title}".strip(), agent)
            return {"t": "sessions.new_r"}
        if t == "commands.list":
            return {"t": "commands.list_r", "items": await compat.commands_list()}
        if t == "models.list":
            key = (await compat.current_session(runner, source))["key"]
            return {"t": "models.list_r", **await compat.models_list(runner, key)}
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
