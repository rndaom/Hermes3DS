#!/usr/bin/env python3
"""Scripted stand-in for the PC side, for testing app screens that Hermes can't produce on demand.

It runs the real protocol server (Noise IK, pairing, replay) with a canned backend, and reads commands
from stdin (or, with `--commands FILE`, lines appended to that file) to push events at the connected 3DS:

    approval [command]   an approval prompt (default: a recursive delete)
    clarify [q|choice|…] a question; with choices after the first `|`
    multi q|choice|…     a question that takes several choices
    say <text>           an assistant reply
    md <markdown>        an assistant reply in Markdown, "\\n" for a line break (rendered as Hermes replies are)
    typing on|off
    away <host:port>|none    change the away-from-home address the 3DS is told (protocol.md §2.4)
    image <path> [caption]   a picture (view + thumb + original); quote the path if it has spaces
    file <path> [caption]    any file as a download attachment (text, code, PDF and Office files also get
                             a reader preview; audio files become playable `audio` when ffmpeg is on PATH)
    voice <path> [caption]   an audio file as a spoken reply (`voice: true`: the 3DS may play it at once)

    @<bot> <command>     any of the above in a bot's chat (protocol.md §4.8), e.g. `@research say hi`
    bots on|off          whether the mock serves bots (`off`: as with gateway.multiplex_profiles off)

Voice messages and photos from the 3DS (protocol.md §4.4.1) are saved in the mock's work folder (the path is
printed) and answered with a `heard` event (voice) and a short reply describing what arrived.
    quit

Use the gateway's key and device list (`--data`) so an already-paired 3DS connects without pairing again;
it listens on its own port (default 7340) so the gateway can keep running. Point the 3DS at it by setting
`port` in sdmc:/3ds/hermes/server.json.

Away route without Tailscale: `--away-port 7339 --away PC-NAME:8443` plus `tools/tls_funnel.py` (a local
TLS-terminating stand-in for the funnel) forwarding :8443 to 127.0.0.1:7339.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from h3proto import avatar  # noqa: E402
from h3proto.away import parse_address  # noqa: E402
from h3proto.blobs import BlobStore  # noqa: E402
from h3proto.media import build_attachment  # noqa: E402
from h3proto.richtext import markdown_to_rich  # noqa: E402
from h3proto.pairing import Device, DeviceStore, TokenStore, load_or_create_key  # noqa: E402
from h3proto.server import Hermes3DSServer, ServerConfig  # noqa: E402

ACTIONS = [["Allow once", "once", "primary"], ["This session", "session", ""], ["Always", "always", ""],
           ["Deny", "deny", "danger"]]  # (label, choice, style) as docs/protocol.md shows
SESSION = {"id": "mock-session", "title": "Mock PC", "model": "mock-model", "started": 0, "msgs": 0,
           "tokens_in": 0, "tokens_out": 0, "cost": 0}
_ids = itertools.count(1)
# The bots the mock serves (agents.list), after the main agent; agents.create adds to it. `ui` is Bot Mode's
# `ui_meta.hermes-bots` look; without one a bot gets the shape and colour Bot Mode picks from its name.
BOTS = [{"id": "research", "title": "Research", "desc": "Digs through papers and the web, cites its sources.",
         "model": "mock-research", "ui": {"color": "#3d5afe", "shape": "hexagon"}},
        {"id": "writer", "title": "Writer", "desc": "", "model": "mock-writer"}]


def split_path(arg: str) -> tuple[str, str]:
    """'<path> [rest]' -> (path, rest); the path may be quoted."""
    arg = arg.strip()
    if arg[:1] in ('"', "'"):
        end = arg.find(arg[0], 1)
        if end > 0:
            return arg[1:end], arg[end + 1:].strip()
    path, _, rest = arg.partition(" ")
    return path, rest.strip()


class MockBackend:
    def __init__(self, blobs: BlobStore) -> None:
        self.server: Hermes3DSServer | None = None
        self.device: Device | None = None
        self.blobs = blobs
        self.multiplex = True
        self.avatars: dict[str, dict] = {}

    def _bot(self, msg: dict) -> str:
        from h3proto.server import RequestError
        name = msg.get("agent") or ""
        if name in ("", "default"):
            return ""
        if not self.multiplex or name not in [b["id"] for b in BOTS]:
            raise RequestError("not_found", f"no bot called {name} on this gateway")
        return name

    def _agents(self) -> dict:
        main = {"id": "default", "title": "Hermes", "desc": "", "model": "mock-model", "provider": "mock",
                "primary": True, "bot": False, "busy": False}
        bots = [{"provider": "mock", "primary": False, "bot": True, "busy": False, "avatar": self._avatar(b),
                 **{k: v for k, v in b.items() if k != "ui"}} for b in BOTS]
        return {"t": "agents.list_r", "items": [main] + (bots if self.multiplex else []), "multiplex": self.multiplex}

    def _avatar(self, bot: dict) -> dict | None:
        if bot["id"] not in self.avatars:
            png = avatar.render(avatar.look(bot["id"], self.blobs.root, bot.get("ui")))
            if png:
                self.avatars[bot["id"]] = self.blobs.put_bytes(png, "image/png", bot["id"] + ".png", "thumb")
        return self.avatars.get(bot["id"])

    async def welcome_state(self, device: Device) -> dict:
        return {"chat_id": "mock", "session": dict(SESSION)}

    async def on_connect(self, device: Device) -> None:
        self.device = device
        print(f"connected: {device.name} ({device.fp})", flush=True)

    async def on_disconnect(self, device: Device) -> None:
        print(f"disconnected: {device.name}", flush=True)

    async def handle(self, device: Device, msg: dict) -> dict | None:
        t = msg["t"]
        print(f"<- {msg}", flush=True)
        if t == "agents.list":
            return self._agents()
        if t == "agents.create":
            from h3proto.server import RequestError
            name = str(msg.get("name") or "")
            if not self.multiplex:
                raise RequestError("unavailable", "this gateway serves one profile (gateway.multiplex_profiles is off)")
            if name in ("default", "main") or name in [b["id"] for b in BOTS]:
                raise RequestError("bad_request", f"there is already a profile called {name}")
            await asyncio.sleep(2)  # the real one copies a profile
            BOTS.append({"id": name, "title": name.replace("-", " ").replace("_", " ").capitalize(),
                         "desc": str(msg.get("desc") or ""), "model": "mock-model"})
            item = {k: v for k, v in self._agents()["items"][-1].items() if k != "busy"}
            return {"t": "agents.create_r", "item": item}
        bot = self._bot(msg)
        if t == "approve":
            await self.server.clear_pending(device.key, str(msg.get("req")),
                                            {"t": "approval_done", "req": msg.get("req"), "choice": msg.get("choice")})
            return {"t": "approve_r", "resolved": 1}
        if t == "answer":
            await self.server.clear_pending(device.key, f"c:{msg.get('cid')}",
                                            {"t": "clarify_done", "cid": msg.get("cid")})
            return {"t": "answer_r", "ok": True}
        if t == "send" and (msg.get("voice") or msg.get("attach")):
            return await self._media_send(device, msg, bot)
        if t == "send":
            mid = f"m{next(_ids)}"
            who = f"mock {bot}" if bot else "mock"
            await self.server.post_message(device.key, f"u{mid}", str(msg.get("text")), role="user", agent=bot)
            await self.server.post_message(device.key, mid, f"({who}) you said: {msg.get('text')}", agent=bot)
            return {"t": "send_r", "mid": mid}
        if t == "status":
            sess = dict(SESSION)
            if bot:
                sess.update(id=f"mock-{bot}", title=f"Mock {bot}", model=f"mock-{bot}")
            return {"t": "status_r", "now": time.time(), "session": sess, "busy": False, "queued": 0}
        if t == "models.list":
            return {"t": "models.list_r", "providers": []}
        if t.endswith(".list") or t == "sessions.messages":
            return {"t": f"{t}_r", "items": []}
        if "id" in msg:
            return {"t": f"{t}_r"}
        return None

    async def _media_send(self, device: Device, msg: dict, bot: str = "") -> dict:
        """A voice message or photos: keep them where they can be looked at, and say what arrived."""
        from h3proto.audio import voice_to_wav
        from h3proto.server import RequestError
        mid = f"m{next(_ids)}"
        voice = msg.get("voice")
        refs = [voice] if voice else list(msg.get("attach") or [])
        paths = [self.server.upload_path(device, str((r or {}).get("blob"))) for r in refs]
        if not all(paths):
            raise RequestError("not_found", "upload isn't complete on the PC")
        if voice:
            wav = voice_to_wav(paths[0].read_bytes(), int(voice["rate"]), int(voice["block"]), int(voice.get("ms") or 0))
            out = self.blobs.root.parent / f"voice-{mid}.wav"
            out.write_bytes(wav)
            pcm = memoryview(wav)[44:].cast("h")
            peak = max((abs(v) for v in pcm), default=0)
            secs = len(pcm) / int(voice["rate"])
            print(f"voice message: {secs:.1f} s, peak {peak}, saved {out}", flush=True)
            heard = {"t": "heard", "req": msg.get("id"), "text": f"(mock) {secs:.1f} seconds, loudest {peak * 100 // 32767}%"}
            await self.server.event(device.key, {**heard, "agent": bot} if bot else heard)
            await self.server.post_message(device.key, mid, f"(mock) got your voice message: {secs:.1f} s.", agent=bot)
        else:
            sizes = []
            for i, p in enumerate(paths):
                out = self.blobs.root.parent / f"photo-{mid}-{i}.jpg"
                shutil.copy(p, out)
                try:
                    from PIL import Image
                    with Image.open(out) as im:
                        sizes.append(f"{im.width}x{im.height}")
                except Exception as exc:  # Pillow missing or not a picture
                    sizes.append(f"unreadable ({exc})")
                print(f"photo: {out} ({p.stat().st_size} bytes, {sizes[-1]})", flush=True)
            caption = str(msg.get("text") or "").strip()
            await self.server.post_message(device.key, mid, f"(mock) got {len(paths)} photo(s): {', '.join(sizes)}" +
                                           (f", caption \"{caption}\"" if caption else ""), agent=bot)
        return {"t": "send_r", "mid": mid}

    async def command(self, line: str) -> bool:
        cmd, _, arg = line.strip().partition(" ")
        if not cmd:
            return True
        if cmd == "quit":
            return False
        if cmd == "bots":
            self.multiplex = arg.strip() != "off"
            print(f"bots {'on' if self.multiplex else 'off'}", flush=True)
            return True
        bot = ""
        if cmd.startswith("@"):  # `@research say hi`: the rest is for that bot's chat
            bot = cmd[1:]
            cmd, _, arg = arg.strip().partition(" ")
        if cmd == "away":
            await self.server.set_away(None if arg.strip() in ("", "none") else parse_address(arg))
            print(f"away address: {self.server.away}", flush=True)
            return True
        if self.device is None:
            print("no 3DS connected yet", flush=True)
            return True
        key = self.device.key
        if cmd == "approval":
            req = f"a{next(_ids)}"
            command = arg or "rm -rf ~/h3ds-approval-test"
            await self.server.set_pending(key, req, tag({
                "t": "approval", "req": req, "text": "Hermes wants to run a command", "command": command,
                "description": "recursive delete", "actions": ACTIONS}, bot))
        elif cmd in ("clarify", "multi"):
            parts = [p.strip() for p in (arg or "Which one?|Red|Green|Blue").split("|")]
            cid = f"q{next(_ids)}"
            await self.server.set_pending(key, f"c:{cid}", tag({
                "t": "clarify", "cid": cid, "question": parts[0], "choices": parts[1:], "multi": cmd == "multi"}, bot))
        elif cmd == "say":
            await self.server.post_message(key, f"m{next(_ids)}", arg, agent=bot)
        elif cmd == "md":
            text, spans = markdown_to_rich(arg.replace("\\n", "\n"))
            await self.server.post_message(key, f"m{next(_ids)}", text, spans=spans, agent=bot)
        elif cmd in ("image", "file", "voice"):
            path, caption = split_path(arg)
            if not Path(path).is_file():
                print(f"no such file: {path}", flush=True)
                return True
            att = await asyncio.to_thread(build_attachment, self.blobs, path, None, None, cmd == "image",
                                          True if cmd == "voice" else None)
            if cmd == "voice" and att.get("kind") == "audio":
                att["voice"] = True
            print(f"attachment: {json.dumps(att)}", flush=True)
            await self.server.post_message(key, f"m{next(_ids)}", caption, attach=[att], agent=bot)
        elif cmd == "typing":
            await self.server.set_typing(key, arg != "off", bot)
        else:
            print(f"unknown command {cmd!r}", flush=True)
        return True


def tag(ev: dict, bot: str) -> dict:
    """A prompt for a bot's chat carries its name (protocol.md §4.8)."""
    if bot:
        ev["agent"] = bot
    return ev


async def _lines(path: Path | None):
    if path is None:
        loop = asyncio.get_running_loop()
        while line := await loop.run_in_executor(None, sys.stdin.readline):
            yield line
        return
    path.touch()
    with path.open(encoding="utf-8") as f:
        f.seek(0, 2)  # only commands written from now on
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                await asyncio.sleep(0.2)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path.home() / ".hermes" / "plugin-data" / "3ds",
                    help="folder holding server_key and devices.json (copied, never written)")
    ap.add_argument("--port", type=int, default=7340)
    ap.add_argument("--commands", type=Path, help="read commands appended to this file instead of stdin")
    ap.add_argument("--away-port", type=int, default=0, help="loopback away listener port (0 = none)")
    ap.add_argument("--away", help="away address to advertise, host:port")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    work = Path(tempfile.mkdtemp(prefix="mock3ds-"))
    for name in ("server_key", "devices.json"):
        if (args.data / name).exists():
            shutil.copy(args.data / name, work / name)
    blobs = BlobStore(work / "blobs")
    backend = MockBackend(blobs)
    server = Hermes3DSServer(ServerConfig(port=args.port, discovery_port=0, name="mock-pc",
                                          away_port=args.away_port), backend,
                             load_or_create_key(work / "server_key"), DeviceStore(work / "devices.json"),
                             TokenStore(work / "pair_tokens.json"), server_info={"hermes": "mock"}, blobs=blobs)
    backend.server = server
    await server.start()
    if args.away:
        await server.set_away(parse_address(args.away))
    print(f"mock PC listening on port {args.port}" +
          (f", away listener 127.0.0.1:{server.away_port}, advertising {server.away}" if args.away_port else ""),
          flush=True)
    try:
        async for line in _lines(args.commands):
            if not await backend.command(line):
                break
    finally:
        await server.stop()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
