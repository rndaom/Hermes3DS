"""`hermes 3ds doctor`: check this computer's side of the setup and say what to fix, in plain words.

It only looks: it reads Hermes' config and the plugin's data folder, tries the listener on this computer (a TCP
connect and one discovery question to 127.0.0.1) and looks for programs on PATH. It changes nothing, creates no
key, runs no other program and sends nothing off this computer (a newer release is what the gateway's own daily
check last found, read from `update.json`).

`collect()` gathers the facts, `evaluate()` turns them into checks (no I/O, so the tests feed it facts), `render()`
prints them. Exit status: 1 when a check failed, else 0.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
LABEL = {OK: "[ ok ]", INFO: "[info]", WARN: "[warn]", FAIL: "[FAIL]"}


@dataclass
class Check:
    level: str
    title: str
    fix: str = ""  # what to do about it; empty when there is nothing to do


@dataclass
class Bot:
    name: str
    enabled: bool       # platforms.3ds.enabled in its config.yaml
    copy: str | None    # the version of its copy of the plugin; None when it has none


@dataclass
class Facts:
    enabled: bool = False             # platforms.3ds.enabled
    port: int = 7333
    discovery_port: int = 7334        # 0: discovery turned off in the config
    listening: bool = False           # something accepts TCP on 127.0.0.1:port
    discovery: dict | None = None     # the `here` reply from 127.0.0.1, if any
    key: str | None = None            # this profile's server key (base64url, public), if the key file is there
    key_loose: bool = False           # the key file is readable by other users (POSIX)
    plaintext: bool = False           # security: plaintext
    streaming: bool = False           # streaming.enabled
    addresses: list[str] = field(default_factory=list)  # this computer's LAN addresses
    devices: list[Any] = field(default_factory=list)    # paired consoles (pairing.Device)
    ffmpeg: bool = False
    stt: bool = True                  # speech-to-text is on in Hermes
    docker: bool = False
    firewall: str = ""                # "firewalld", "ufw", "windows", "macos" or ""
    multiplex: bool = False           # gateway.multiplex_profiles
    version: str = ""                 # this copy of the plugin
    latest: str = ""                  # the newest release the gateway's update check found, if it ran
    bots: list[Bot] = field(default_factory=list)
    away: str = ""                    # cli.away_summary()
    now: float = 0.0


# --- gathering -----------------------------------------------------------------------------------

def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _copy_version(plugin_dir: Path) -> str | None:
    try:
        for line in (plugin_dir / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        return None
    return ""


def _firewall() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if shutil.which("firewall-cmd"):
        return "firewalld"
    if shutil.which("ufw"):
        return "ufw"
    return ""


def _bots(own_home: Path, root: Path) -> list[Bot]:
    """The other Hermes profiles under the Hermes root: whether the 3ds platform is on in each, and which
    release its copy of the plugin is (Hermes copies `plugins/` into a profile made with --clone)."""
    from . import compat
    name = Path(__file__).resolve().parent.name
    homes: list[tuple[str, Path]] = []
    if own_home.resolve() != root.resolve():
        homes.append(("default", root))
    try:
        homes += [(p.name, p) for p in sorted((root / "profiles").iterdir())
                  if p.is_dir() and not p.name.startswith((".", "_"))]  # `.deleted` and the like are not profiles
    except OSError:
        pass
    out = []
    for bot, home in homes:
        if home.resolve() == own_home.resolve():
            continue
        cfg = (compat._yaml(home / "config.yaml").get("platforms") or {}).get("3ds") or {}
        out.append(Bot(bot, _truthy(cfg.get("enabled")) if isinstance(cfg, dict) else False,
                       _copy_version(home / "plugins" / name)))
    return out


def collect(data: Path) -> Facts:
    from . import cli, compat
    from .adapter import _plugin_version
    from .h3proto.client import discover
    from .h3proto import update
    from .h3proto.audio import ffmpeg_path
    from .h3proto.noise import KeyPair
    from .h3proto.pairing import DeviceStore, b64u

    f = Facts(now=time.time(), version=_plugin_version())
    try:
        cfg = compat._config()
    except Exception:
        cfg = {}
    pc = cli._platform_config()
    f.enabled = _truthy(pc.get("enabled"))
    f.port = cli._configured_port()
    f.discovery_port = _int(pc.get("discovery_port"), 7334)
    f.plaintext = str(pc.get("security") or "").strip().lower() == "plaintext"
    f.streaming = _truthy((cfg.get("streaming") or {}).get("enabled")) if isinstance(cfg.get("streaming"), dict) else False
    stt = cfg.get("stt")
    f.stt = _truthy(stt.get("enabled", True)) if isinstance(stt, dict) else True
    gw = cfg.get("gateway")
    f.multiplex = _truthy(gw.get("multiplex_profiles")) if isinstance(gw, dict) else False

    f.listening = cli._listening(f.port)
    if f.discovery_port:
        try:
            replies = discover(timeout=0.7, port=f.discovery_port, target="127.0.0.1")
            f.discovery = replies[0] if replies else None
        except OSError:
            f.discovery = None
    key_file = data / "server_key"
    try:  # read only: `hermes 3ds pair` and the gateway are the ones that make the key
        raw = key_file.read_bytes()
        if len(raw) == 32:
            f.key = b64u(KeyPair(raw).public)
        if os.name != "nt":
            f.key_loose = bool(key_file.stat().st_mode & 0o077)
    except OSError:
        pass
    f.addresses = [a for a in cli.lan_addresses() if not a.startswith("127.")]
    try:
        f.devices = DeviceStore(data / "devices.json").all()
    except Exception:
        f.devices = []
    f.ffmpeg = bool(ffmpeg_path())
    f.docker = Path("/.dockerenv").exists()
    f.firewall = _firewall()
    try:
        from hermes_constants import get_default_hermes_root, get_process_hermes_home
        f.bots = _bots(Path(get_process_hermes_home()), Path(get_default_hermes_root()))
    except Exception:
        f.bots = []
    f.latest = update.read(data)[0]  # the gateway's last answer: the doctor itself asks nobody
    try:
        f.away = cli.away_summary(data)
    except Exception:
        f.away = ""
    return f


# --- judging -------------------------------------------------------------------------------------

def _firewall_fix(f: Facts) -> str:
    tcp, udp = f"{f.port}/tcp", f"{f.discovery_port}/udp"
    if f.firewall == "firewalld":
        ports = f"--add-port={tcp}" + (f" --add-port={udp}" if f.discovery_port else "")
        return f"sudo firewall-cmd --permanent {ports} && sudo firewall-cmd --reload"
    if f.firewall == "ufw":
        return f"sudo ufw allow {tcp}" + (f" && sudo ufw allow {udp}" if f.discovery_port else "")
    if f.firewall == "windows":
        return ("In Windows Security > Firewall > Allow an app, allow Hermes' Python on the network profile "
                "your Wi-Fi uses (Private or Public).")
    if f.firewall == "macos":
        return "If macOS asks whether Python may accept incoming connections, choose Allow."
    return f"If this computer has a firewall, allow TCP {f.port}" + (f" and UDP {f.discovery_port}." if f.discovery_port else ".")


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "never connected"
    days = int(max(0, now - ts) // 86400)
    return "last seen today" if days == 0 else f"last seen {days} day{'s' if days != 1 else ''} ago"


def evaluate(f: Facts) -> list[Check]:
    out: list[Check] = []
    restart = "hermes gateway restart"

    # 1. the platform and its listener
    if f.enabled:
        out.append(Check(OK, "The 3ds platform is turned on in Hermes' config."))
    else:
        out.append(Check(FAIL, "The 3ds platform is not turned on.",
                         f"hermes config set platforms.3ds.enabled true   then: {restart}"))
    mine = bool(f.discovery and f.key and f.discovery.get("key") == f.key)
    if not f.listening:
        if f.enabled:
            out.append(Check(FAIL, f"Nothing is listening on port {f.port}: the gateway is not running, or it "
                             "started without the plugin.",
                             f"{restart}   then check `hermes plugins list` shows 3ds-platform enabled."))
    elif f.discovery and f.key and not mine:
        who = str(f.discovery.get("name") or "another program")
        out.append(Check(FAIL, f"Port {f.port} is answered by a different Hermes 3DS listener ({who}), not this "
                         "profile's. A 3DS paired here cannot log in to it.",
                         "Stop the other gateway, or give this one its own `port:` under `platforms.3ds` in "
                         "config.yaml, then restart."))
    else:
        out.append(Check(OK, f"The gateway is listening for the 3DS on port {f.port}."))
        if not f.discovery_port:
            out.append(Check(INFO, "Finding the computer by itself is turned off (`discovery_port: 0`): on the "
                             "3DS, use Enter the PC's address."))
        elif f.discovery:
            out.append(Check(OK, f"The 3DS can find this computer by itself (discovery on UDP {f.discovery_port})."))
        else:
            out.append(Check(WARN, f"The gateway did not answer a discovery question on UDP {f.discovery_port}, so "
                             "the 3DS may not find this computer by itself.",
                             f"Check nothing else uses UDP {f.discovery_port}, then {restart}. Meanwhile, on the "
                             "3DS, use Enter the PC's address."))
    if f.plaintext:
        out.append(Check(WARN, "`security: plaintext` is set: connections are not encrypted or checked. It is "
                         "for development only.", "Remove `security: plaintext` under `platforms.3ds`, then restart."))
    if f.key_loose:
        out.append(Check(WARN, "The plugin's key file can be read by other users of this computer.",
                         "chmod 600 the `server_key` file in the plugin's data folder."))

    # 2. the network between the two
    if f.addresses:
        where = ", ".join(f"{a}:{f.port}" for a in f.addresses)
        out.append(Check(INFO, f"This computer's address for the 3DS: {where}. The 3DS must be on the same "
                         "network (and not on a guest Wi-Fi that keeps devices apart)."))
    else:
        out.append(Check(FAIL, "This computer has no network address: it is not on a network the 3DS could reach.",
                         "Connect it to your Wi-Fi or router."))
    out.append(Check(INFO, "A firewall on this computer cannot be checked from here. If the 3DS says "
                     "\"PC not reachable\", allow the ports:", _firewall_fix(f)))
    if f.docker:
        out.append(Check(WARN, "Hermes runs in a container: the 3DS can only find it with host networking.",
                         "Run the container with `--network host`, or publish the ports and use Enter the PC's "
                         "address on the 3DS."))

    # 3. pairing
    if f.devices:
        names = "; ".join(f"{d.name} ({_ago(getattr(d, 'last_seen', 0) or 0, f.now)})" for d in f.devices[:6])
        more = f" and {len(f.devices) - 6} more" if len(f.devices) > 6 else ""
        out.append(Check(OK, f"Paired 3DS: {names}{more}."))
    else:
        out.append(Check(INFO, "No 3DS is paired yet.", "hermes 3ds pair   (then type the code on the 3DS)"))

    # 4. how it feels in use
    if not f.streaming:
        out.append(Check(WARN, "Streaming is off: replies arrive all at once instead of as they are written.",
                         f"hermes config set streaming.enabled true   then: {restart}"))
    else:
        out.append(Check(OK, "Replies stream to the 3DS as they are written."))
    if not f.ffmpeg:
        out.append(Check(WARN, "ffmpeg is not installed: audio files arrive as plain files, and there are no "
                         "spoken replies or voice messages.", "Install ffmpeg and make sure it is on PATH."))
    else:
        out.append(Check(OK, "ffmpeg is installed (audio, spoken replies, voice messages)."))
    if not f.stt:
        out.append(Check(INFO, "Speech-to-text is off in Hermes: voice messages from the 3DS arrive but are "
                         "not understood.", "Set it up with `hermes tools`."))

    # 5. bots
    if f.bots:
        if not f.multiplex:
            out.append(Check(INFO, f"You have {len(f.bots)} other Hermes profile{'s' if len(f.bots) != 1 else ''}, "
                             "but the Bots tab is off.",
                             f"hermes config set gateway.multiplex_profiles true   then: {restart}"))
        else:
            for b in f.bots:
                if b.copy is not None and b.copy != f.version:
                    out.append(Check(FAIL, f"Bot \"{b.name}\" has plugin {b.copy or 'of an unknown version'}, this "
                                     f"profile has {f.version}: copies of different releases refuse to work together.",
                                     f"Update the plugin in that profile too (hermes -p {b.name} plugins update "
                                     f"3ds-platform), then {restart}."))
                elif b.copy is None:
                    out.append(Check(INFO, f"Bot \"{b.name}\" is not reachable from the 3DS (it has no copy of the "
                                     "plugin). Fine if you do not want it there.",
                                     f"hermes -p {b.name} plugins install 3ds-platform   then: hermes -p {b.name} "
                                     f"config set platforms.3ds.enabled true   then: {restart}"))
                elif not b.enabled:
                    out.append(Check(INFO, f"Bot \"{b.name}\" is not reachable from the 3DS (the 3ds platform is "
                                     "off in it). Fine if you do not want it there.",
                                     f"hermes -p {b.name} config set platforms.3ds.enabled true   then: {restart}"))
                else:
                    out.append(Check(OK, f"Bot \"{b.name}\" is reachable from the 3DS."))

    # 6. away from home
    if f.away:
        away_ok = "ready at" in f.away
        bad = "not working" in f.away
        out.append(Check(OK if away_ok else WARN if bad else INFO, f.away))

    # 7. a newer release
    from .h3proto.update import newer
    if newer(f.latest, f.version):
        out.append(Check(INFO, f"Hermes 3DS {f.latest} is out; this plugin is {f.version}.",
                         f"hermes plugins update 3ds-platform   then: {restart}   (the 3DS app says so itself "
                         "when it is older)"))
    elif f.latest and f.version:
        out.append(Check(OK, f"This plugin ({f.version}) is the newest release."))
    return out


# --- printing ------------------------------------------------------------------------------------

def render(checks: list[Check]) -> str:
    lines = ["Hermes 3DS: checking this computer's side of the setup", ""]
    for c in checks:
        lines.append(f"{LABEL[c.level]} {c.title}")
        if c.fix:
            lines.append(f"       -> {c.fix}")
    fails = sum(c.level == FAIL for c in checks)
    warns = sum(c.level == WARN for c in checks)
    lines.append("")
    if fails:
        lines.append(f"{fails} problem{'s' if fails != 1 else ''} to fix"
                     + (f", {warns} thing{'s' if warns != 1 else ''} to look at." if warns else "."))
    elif warns:
        lines.append(f"It should work. {warns} thing{'s' if warns != 1 else ''} to look at.")
    else:
        lines.append("Everything on this computer looks right.")
    return "\n".join(lines)


def run(data: Path) -> int:
    checks = evaluate(collect(data))
    print(render(checks))
    return 1 if any(c.level == FAIL for c in checks) else 0
