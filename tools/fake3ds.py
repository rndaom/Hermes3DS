"""Fake 3DS: a terminal client that speaks the Hermes 3DS protocol exactly like the console will.

  python tools/fake3ds.py pair "hermes3ds:1?h=...&k=...&t=..."     # pair using the link from `hermes 3ds pair`
  python tools/fake3ds.py pair --host 192.168.1.5 --code ABCD2345  # manual code (key from discovery)
  python tools/fake3ds.py discover
  python tools/fake3ds.py chat                                      # interactive
  python tools/fake3ds.py chat --script lines.txt                   # scripted (one line per step)
  python tools/fake3ds.py chat --plaintext --host 127.0.0.1         # dev server in plaintext mode

In chat, plain text is sent as a message ("/model x" etc. work too). Protocol requests start with ':' —
:status :sessions :history [sid] :resume <sid> :new [title] :models :skills :tools :jobs :commands
:stop :approve <once|session|always|deny> :answer <n|text> :get <blob> [file] :open <blob> :voice <audio file>
:wait :quit
Attachments print as `[image name 800x600 view=b1 thumb=b2 file=b3]`; `:get b1 out.jpg` downloads a blob (or
start with --save-blobs DIR to download every attachment blob as it arrives), `:open b3` asks the PC to open it.
`:voice speech.wav` sends the file as a voice message, encoded like the 3DS microphone (16360 Hz IMA-ADPCM; needs
ffmpeg); Hermes' transcript prints as `heard`. Spoken replies play as `speak` parts, then land whole as
`[audio ... voice ...]`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from h3proto import audio  # noqa: E402
from h3proto.client import Client, ServerError, discover, load_device_key  # noqa: E402
from h3proto.pairing import fingerprint, parse_pairing_uri, token_from_code, unb64u  # noqa: E402

STATE = Path(__file__).resolve().parents[1] / ".fake3ds"
DEVICE_NAME = "Fake 3DS"
CAPS = ["jpeg", "png", "pcm16"]  # as the library client says by default

C_BLUE, C_DIM, C_YEL, C_RED, C_GRN, C_OFF = "\033[94m", "\033[2m", "\033[93m", "\033[91m", "\033[92m", "\033[0m"


def save_server(info: dict) -> None:
    STATE.mkdir(exist_ok=True)
    (STATE / "server.json").write_text(json.dumps(info, indent=1))


def load_server() -> dict:
    p = STATE / "server.json"
    if not p.exists():
        sys.exit("Not paired. Run: python tools/fake3ds.py pair <link>")
    return json.loads(p.read_text())


async def cmd_pair(args) -> None:
    key = load_device_key(STATE / "device_key")
    if args.link:
        info = parse_pairing_uri(args.link)
        hosts, port, server_key, token = info.hosts, info.port, info.server_key, info.token
    else:
        if not (args.host and args.code):
            sys.exit("give a pairing link, or --host and --code")
        found = await asyncio.to_thread(discover, 1.5, args.discovery_port, args.host)
        if not found:
            sys.exit(f"no Hermes 3DS server answered discovery at {args.host}")
        server_key = unb64u(found[0]["key"])
        print(f"PC fingerprint: {fingerprint(server_key)}  — check it matches the PC before continuing.")
        hosts, port, token = [args.host], int(found[0]["port"]), token_from_code(args.code)
    last_err = None
    for host in hosts:
        try:
            c = await Client.connect(host, port, device=DEVICE_NAME, static=key, server_key=server_key,
                                     pair_token=token, timeout=5)
        except (OSError, asyncio.TimeoutError, ServerError) as exc:
            last_err = exc
            continue
        save_server({"host": host, "port": port, "key": server_key.hex(), "name": c.welcome.get("server")})
        print(f"{C_GRN}Paired with {c.welcome.get('server')} at {host}:{port} as {c.welcome.get('device_id')}{C_OFF}")
        await c.close()
        return
    sys.exit(f"pairing failed: {last_err}")


class Printer:
    def __init__(self):
        self.streaming: str | None = None
        self.busy = False
        self.idle = asyncio.Event()
        self.idle.set()
        self.prompts: dict = {}
        self.prompted = asyncio.Event()
        self.client: Client | None = None
        self.save_dir: Path | None = None
        self.saves: set[asyncio.Task] = set()

    def _attachments(self, ev: dict) -> None:
        for att in ev.get("attach") or []:
            refs = {k: att[k]["id"] for k in ("view", "thumb", "file", "text", "audio") if isinstance(att.get(k), dict)}
            dims = f" {att['w']}x{att['h']}" if "w" in att else ""
            if att.get("voice"):
                dims += f" voice {att.get('ms', 0) / 1000:.1f}s"  # a spoken reply (or a part of one)
            print(f"{C_GRN}  [{att.get('kind')} {att.get('name')}{dims} {att.get('size')}B "
                  + " ".join(f"{k}={v}" for k, v in refs.items()) + f"]{C_OFF}")
            if self.save_dir and self.client:
                for kind, bid in refs.items():
                    task = asyncio.create_task(self._save(bid, f"{att.get('name')}.{kind}"))
                    self.saves.add(task)
                    task.add_done_callback(self.saves.discard)

    async def _save(self, bid: str, name: str) -> None:
        try:
            data = await self.client.get_blob(bid)
            out = self.save_dir / f"{bid}_{Path(name).name}"
            out.write_bytes(data)
            print(f"{C_DIM}  saved {out} ({len(data)} bytes){C_OFF}")
        except Exception as exc:
            print(f"{C_RED}  blob {bid} failed: {exc}{C_OFF}")

    def _end_stream(self):
        if self.streaming:
            print(C_OFF)
            self.streaming = None

    def on(self, ev: dict) -> None:
        t = ev["t"]
        if os.environ.get("H3DS_RAW"):
            print(f"{C_DIM}<< {json.dumps(ev, ensure_ascii=False)}{C_OFF}")
        if t == "msg":
            self._end_stream()
            color = C_DIM if ev.get("role") == "system" else ""
            print(f"{color}◆ {ev['text']}", end="" if not ev.get("final") else f"{C_OFF}\n", flush=True)
            if not ev.get("final"):
                self.streaming = ev["mid"]
            self._attachments(ev)
        elif t == "append":
            print(ev["text"], end="", flush=True)
            if ev.get("final"):
                self._end_stream()
        elif t == "edit":
            if ev.get("final"):
                self._end_stream()
                spans = f" {C_DIM}[{len(ev.get('spans') or [])} spans]{C_OFF}" if ev.get("spans") else ""
                print(f"◆ {ev['text']}{spans}")
            else:
                print(f"\r◆ {ev['text']}", end="", flush=True)
        elif t == "heard":
            self._end_stream()
            print(f"{C_DIM}  heard: {ev.get('text')}{C_OFF}")
        elif t == "speak":  # a part of a spoken reply, played and not shown; the chat gets the whole at the end
            self._end_stream()
            print(f"{C_DIM}  speak {ev.get('speech')}:{C_OFF}", end="")
            self._attachments({"attach": [ev.get("attach") or {}]})
        elif t == "typing":
            self.busy = ev["on"]
            if ev["on"]:
                self.idle.clear()
                print(f"{C_DIM}⠋ thinking…{C_OFF}")
            else:
                self._end_stream()
                print(f"{C_DIM}· idle{C_OFF}")
                self.idle.set()
        elif t == "tool":
            self._end_stream()
            if ev.get("state") == "start":
                print(f"{C_BLUE}  ▸ {ev.get('text', '')}{C_OFF}")
            else:
                print(f"{C_DIM}    ✓ done{C_OFF}")
        elif t == "approval":
            self._end_stream()
            self.prompts["approval"] = ev
            self.prompted.set()
            acts = " / ".join(f"{a[1]}" for a in ev.get("actions", []))
            print(f"{C_YEL}⚠ APPROVAL {ev['req']}: {ev.get('command')}\n  {ev.get('description')}\n"
                  f"  answer with :approve <{acts}>{C_OFF}")
        elif t == "clarify":
            self._end_stream()
            self.prompts["clarify"] = ev
            self.prompted.set()
            print(f"{C_YEL}? {ev['question']}{C_OFF}")
            for i, ch in enumerate(ev.get("choices") or []):
                print(f"{C_YEL}  {i}. {ch}{C_OFF}")
            print(f"{C_YEL}  answer with :answer <n|text>{C_OFF}")
        elif t in ("approval_done", "clarify_done"):
            self.prompts.pop("approval" if t == "approval_done" else "clarify", None)
            print(f"{C_DIM}  ({t} {ev.get('choice', '')}){C_OFF}")
        elif t == "_closed":
            print(f"{C_RED}connection closed: {ev.get('reason')}{C_OFF}")
        else:
            print(f"{C_DIM}{json.dumps(ev, ensure_ascii=False)}{C_OFF}")


def show_items(items: list, fields: list[str]) -> None:
    for i, it in enumerate(items):
        print(f"  {i:>2}. " + "  ".join(str(it.get(f, ""))[:60] for f in fields if it.get(f) not in (None, "")))


MIC_RATE = 16360  # the 3DS microphone


def _mic_adpcm(path: str) -> dict:
    """Any audio file → mono IMA-ADPCM at the microphone's rate, the way the app encodes a recording."""
    exe = audio.ffmpeg_path()
    if not exe:
        raise SystemExit("ffmpeg is needed to encode a voice message")
    with tempfile.TemporaryDirectory(prefix="fake3ds-") as tmp:
        out = os.path.join(tmp, "voice.wav")
        subprocess.run([exe, "-nostdin", "-loglevel", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", str(MIC_RATE),
                        "-c:a", "adpcm_ima_wav", "-block_size", str(audio.BLOCK), "-f", "wav", out], check=True)
        wav = audio.parse_wav(Path(out).read_bytes())
    if wav is None:
        raise SystemExit("ffmpeg produced no ADPCM")
    return wav


async def send_voice(c: Client, path: str) -> None:
    wav = await asyncio.to_thread(_mic_adpcm, path)
    blob = "u" + os.urandom(10).hex()  # a client-chosen id, new for every recording
    await c.put_blob(blob, wav["data"], audio.MIME, "voice", wait=False)  # chat's event printer reads put_done
    ms = wav["samples"] * 1000 // wav["rate"]
    await c.request("send", text="", voice={"blob": blob, "rate": wav["rate"], "block": wav["block"], "ms": ms})
    print(f"{C_DIM}  voice message sent ({ms / 1000:.1f} s, {len(wav['data']) // 1024} KB){C_OFF}")


async def run_command(c: Client, p: Printer, line: str) -> bool:
    """Returns False to quit."""
    parts = line[1:].split(maxsplit=1)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    try:
        if cmd in ("quit", "q"):
            return False
        if cmd == "wait":
            await asyncio.wait_for(p.idle.wait(), float(arg or 600))
        elif cmd == "status":
            print(json.dumps(await c.request("status"), indent=1, ensure_ascii=False))
        elif cmd == "sessions":
            show_items((await c.request("sessions.list", q=arg or None))["items"], ["id", "title", "msgs", "current"])
        elif cmd == "history":
            sid = arg or (await c.request("status"))["session"]["id"]
            for m in (await c.request("sessions.messages", sid=sid, limit=20))["items"]:
                print(f"  [{m['role']}] {m['text'][:200]}")
        elif cmd == "resume":
            await c.request("sessions.resume", sid=arg)
        elif cmd == "new":
            await c.request("sessions.new", title=arg)
        elif cmd == "models":
            r = await c.request("models.list")
            print(f"  current: {r.get('provider')} / {r.get('model')}")
            for pr in r["items"]:
                print(f"  {pr['provider']}: " + ", ".join(m["id"] + ("*" if m["current"] else "") for m in pr["models"]))
        elif cmd == "skills":
            show_items((await c.request("skills.list"))["items"], ["name", "category", "enabled"])
        elif cmd == "tools":
            show_items((await c.request("tools.list"))["items"], ["toolset", "enabled", "desc"])
        elif cmd == "jobs":
            show_items((await c.request("jobs.list"))["items"], ["id", "name", "schedule", "state"])
        elif cmd == "commands":
            show_items((await c.request("commands.list"))["items"], ["name", "args", "desc"])
        elif cmd == "stop":
            await c.request("stop")
        elif cmd == "get":
            bid, _, dest = arg.partition(" ")
            data = await c.get_blob(bid)
            dest = dest.strip() or f"{bid}.bin"
            Path(dest).write_bytes(data)
            print(f"  {len(data)} bytes -> {dest}")
        elif cmd == "open":
            await c.request("blob.open", blob=arg.strip())
            print("  opened on the PC")
        elif cmd == "voice":
            await send_voice(c, arg.strip())
        elif cmd == "approve":
            ap = p.prompts.get("approval")
            if not ap:
                print("  no pending approval")
            else:
                print(f"  resolved: {(await c.request('approve', req=ap['req'], choice=arg or 'once'))['resolved']}")
        elif cmd == "answer":
            cl = p.prompts.get("clarify")
            if not cl:
                print("  no pending question")
            elif arg.isdigit():
                await c.request("answer", cid=cl["cid"], choice=int(arg))
            else:
                await c.request("answer", cid=cl["cid"], text=arg)
        else:
            print(f"  unknown command :{cmd}")
    except ServerError as exc:
        print(f"{C_RED}  error {exc.code}: {exc.msg}{C_OFF}")
    except asyncio.TimeoutError:
        print(f"{C_RED}  timed out{C_OFF}")
    return True


async def cmd_chat(args) -> None:
    # Like the app: voiceq plays voice messages one after another, speak plays `speak` events.
    caps = CAPS + ([] if args.no_voiceq else ["voiceq"]) + ([] if args.no_speak else ["speak"])
    if args.plaintext:
        c = await Client.connect(args.host or "127.0.0.1", args.port or 7333, device=DEVICE_NAME, caps=caps)
    else:
        srv = load_server()
        c = await Client.connect(args.host or srv["host"], args.port or srv["port"], device=DEVICE_NAME,
                                 static=load_device_key(STATE / "device_key"), server_key=bytes.fromhex(srv["key"]),
                                 caps=caps)
    print(f"{C_GRN}connected to {c.welcome.get('server')} (hermes {c.welcome.get('hermes')}), "
          f"{'secure' if c.channel.secure else 'PLAINTEXT'}; chat {json.dumps(c.welcome.get('chat'))}{C_OFF}")
    p = Printer()
    p.client = c
    if args.save_blobs:
        p.save_dir = Path(args.save_blobs)
        p.save_dir.mkdir(parents=True, exist_ok=True)

    async def pump():
        while True:
            ev = await c.events.get()
            p.on(ev)
            if ev["t"] == "_closed":
                return

    async def keepalive():
        n = 0
        while True:
            await asyncio.sleep(15)
            n += 1
            await c.channel.send({"t": "ping", "n": n})

    tasks = [asyncio.create_task(pump()), asyncio.create_task(keepalive())]
    lines = Path(args.script).read_text("utf-8").splitlines() if args.script else None
    try:
        while True:
            if lines is not None:
                if not lines:
                    break
                line = lines.pop(0).strip()
                if not line or line.startswith("#"):
                    continue
                print(f"{C_BLUE}> {line}{C_OFF}")
            else:
                line = (await asyncio.to_thread(input, "")).strip()
                if not line:
                    continue
            if line.startswith(":"):
                if not await run_command(c, p, line):
                    break
                continue
            t0 = time.monotonic()
            await c.request("send", text=line)
            if lines is not None:  # scripted: wait for the turn to start and finish
                try:
                    await asyncio.wait_for(_wait_turn(p), args.turn_timeout)
                except asyncio.TimeoutError:
                    print(f"{C_RED}  turn still running after {args.turn_timeout}s{C_OFF}")
                print(f"{C_DIM}  ({time.monotonic() - t0:.1f}s){C_OFF}")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await c.close()


async def _wait_turn(p: Printer) -> None:
    for _ in range(50):  # give the turn up to 5 s to start
        if p.busy:
            break
        await asyncio.sleep(0.1)
    p.prompted.clear()
    idle, prompted = asyncio.create_task(p.idle.wait()), asyncio.create_task(p.prompted.wait())
    await asyncio.wait({idle, prompted}, return_when=asyncio.FIRST_COMPLETED)
    idle.cancel(), prompted.cancel()
    await asyncio.sleep(0.5)  # trailing edits/finalize


async def cmd_discover(args) -> None:
    for s in await asyncio.to_thread(discover, 1.5, args.discovery_port):
        print(f"{s['addr']}:{s['port']}  {s['name']}  {s['sec']}  fp {fingerprint(unb64u(s['key']))}")


def main() -> None:
    if sys.platform == "win32":
        import os
        os.system("")  # enable ANSI colors in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("pair")
    pp.add_argument("link", nargs="?")
    pp.add_argument("--host")
    pp.add_argument("--code")
    pp.add_argument("--discovery-port", type=int, default=7334)
    cp = sub.add_parser("chat")
    cp.add_argument("--host")
    cp.add_argument("--port", type=int)
    cp.add_argument("--plaintext", action="store_true")
    cp.add_argument("--no-voiceq", action="store_true",
                    help="don't say the app plays voice messages one after another (spoken replies then arrive whole)")
    cp.add_argument("--no-speak", action="store_true",
                    help="don't say the app plays speak events (spoken replies then come as several voice messages)")
    cp.add_argument("--script")
    cp.add_argument("--save-blobs", metavar="DIR", help="download every attachment blob into DIR")
    cp.add_argument("--turn-timeout", type=float, default=600)
    dp = sub.add_parser("discover")
    dp.add_argument("--discovery-port", type=int, default=7334)
    args = ap.parse_args()
    asyncio.run({"pair": cmd_pair, "chat": cmd_chat, "discover": cmd_discover}[args.cmd](args))


if __name__ == "__main__":
    main()
