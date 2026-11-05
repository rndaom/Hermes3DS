"""`hermes 3ds pair | devices | unpair <name>` — runs outside the gateway, shares files with it."""

from __future__ import annotations

import os
import socket
import time


def setup_cli(parser) -> None:
    sub = parser.add_subparsers(dest="threeds_cmd")
    pair = sub.add_parser("pair", help="Show a one-time pairing code for a 3DS")
    pair.add_argument("--port", type=int, default=None, help="Listener port (default: from config, 7333)")
    pair.add_argument("--host", action="append", default=None, help="Address the 3DS should use (repeatable)")
    sub.add_parser("devices", help="List paired 3DS devices")
    unpair = sub.add_parser("unpair", help="Remove a paired 3DS")
    unpair.add_argument("device", help="Device name or fingerprint")
    away = sub.add_parser("away", help="Away-from-home access through Tailscale Funnel: status, on, off")
    away.add_argument("action", nargs="?", choices=("status", "on", "off"), default="status")


def lan_addresses() -> list[str]:
    addrs: list[str] = []
    try:  # the address of the default route; no packet is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addrs and not ip.startswith("127.") and not ip.startswith("169.254."):
                addrs.append(ip)
    except OSError:
        pass
    return addrs or ["127.0.0.1"]


def _platform_config() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = (load_config().get("platforms") or {}).get("3ds") or {}
        extra = cfg.get("extra") if isinstance(cfg.get("extra"), dict) else {}
        return {**{k: v for k, v in cfg.items() if k != "extra"}, **extra}  # like PlatformConfig.from_dict
    except Exception:
        return {}


def _configured_port() -> int:
    try:
        return int(_platform_config().get("port") or 7333)
    except (TypeError, ValueError):
        return 7333


def _away_port() -> int:
    try:
        return int(_platform_config().get("away_port") or os.environ.get("THREEDS_AWAY_PORT") or 7339)
    except (TypeError, ValueError):
        return 7339


def _configured_away() -> str:
    """parse_mode() of `platforms.3ds.away` (or THREEDS_AWAY): '' when unset."""
    from .h3proto.away import parse_mode
    value = _platform_config().get("away")
    return parse_mode(value if value is not None else os.environ.get("THREEDS_AWAY"))


def _away_mode(data) -> str:
    """The mode the gateway will run with (h3proto.away.effective_mode), from the config and the CLI flags."""
    from .h3proto.away import effective_mode, parse_address
    address = parse_address(_platform_config().get("away_address") or os.environ.get("THREEDS_AWAY_ADDRESS"))
    return effective_mode(_configured_away(), data, address)


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def away_summary(data) -> str:
    """One plain-language line about away-from-home access, from the gateway's last check."""
    from .h3proto.away import disabled_by_user, read_status
    if _away_mode(data) == "off":
        if disabled_by_user(data):
            return "Away from home: turned off (`hermes 3ds away on` to turn it back on)."
        if _configured_away() == "off":
            return "Away from home: off (`away: off` under `platforms.3ds` in config.yaml)."
        return "Away from home: off. To use the 3DS over the internet, install Tailscale and run `hermes 3ds away on`."
    st = read_status(data)
    if not st:
        return "Away from home: not checked yet. Start the gateway, then run `hermes 3ds away`."
    if st.get("source") == "off":  # the gateway's last check was before `away on`
        if _listening(_away_port()):
            return "Away from home: turned on; the gateway picks it up within 10 minutes."
        return "Away from home: turned on; restart the gateway to start it (`hermes gateway restart`)."
    if st.get("ok"):
        return f"Away from home: ready at {st.get('host')}:{st.get('port')} (the 3DS learns it automatically)."
    if not st.get("installed", True):
        return "Away from home: install Tailscale on this PC and sign in (https://tailscale.com/download)."
    return f"Away from home: not working. {st.get('error') or ''}".strip()


def _away(action: str, data) -> int:
    from .h3proto import tailscale
    from .h3proto.away import DISABLED_FILE, ENABLED_FILE
    off_flag, on_flag = data / DISABLED_FILE, data / ENABLED_FILE
    if action == "off":
        off_flag.parent.mkdir(parents=True, exist_ok=True)
        off_flag.write_text("off\n", "utf-8")
        on_flag.unlink(missing_ok=True)
        res = tailscale.disable_funnel(_away_port())
        print("Away-from-home access is off. The 3DS works at home only." if res.ok or not res.installed
              else f"Turned off for the 3DS, but Tailscale said: {res.error}")
        return 0
    if action == "on":
        if _configured_away() == "off":
            print("`away: off` under `platforms.3ds` in config.yaml keeps it off: remove that line first.")
            return 1
        off_flag.unlink(missing_ok=True)
        on_flag.parent.mkdir(parents=True, exist_ok=True)
        on_flag.write_text("on\n", "utf-8")
        print("This publishes a Tailscale Funnel: a public address that only your paired 3DSes can log in to.")
        res = tailscale.ensure_funnel(_away_port())
        if not res.ok:
            print(res.error)
            print("Away mode stays on: fix that, then run `hermes 3ds away` to check.")
            return 1
        print(f"Away-from-home access is on: {res.host}:{res.port}")
        if _listening(_away_port()):
            print("Paired 3DSes pick it up the next time they connect at home (within 10 minutes if online).")
        else:
            print("Restart the gateway to start it: hermes gateway restart. Paired 3DSes pick up the address the "
                  "next time they connect at home.")
        return 0
    print(away_summary(data))
    cli = tailscale.find_cli()
    res = tailscale.check(cli, _away_port())
    if res.ok:
        print(f"Tailscale Funnel: {res.host}:{res.port} -> 127.0.0.1:{_away_port()}")
    elif cli:
        print(f"Tailscale ({cli}): {res.error}")
    return 0


def handle_cli(args) -> int:
    from .adapter import _data_dir
    from .h3proto.pairing import (CODE_TOKEN_BYTES, DeviceStore, TokenStore, code_from_token,
                                  fingerprint, load_or_create_key)

    data = _data_dir()
    cmd = getattr(args, "threeds_cmd", None)
    if cmd == "pair":
        key = load_or_create_key(data / "server_key")
        tokens = TokenStore(data / "pair_tokens.json")
        # The app pairs by code (QR pairing, protocol.md §2.2, is not in the app yet): one token, so nothing
        # else stays valid after it is used.
        code_token = tokens.create(CODE_TOKEN_BYTES)
        hosts = args.host or lan_addresses()
        port = args.port or _configured_port()
        print("Pair a 3DS with this PC (valid 5 minutes, single use).\n")
        print(f"Code:           {code_from_token(code_token)}   (PC address: {', '.join(f'{h}:{port}' for h in hosts)})")
        print(f"PC fingerprint: {fingerprint(key.public)}   (check the 3DS shows the same before you pair)")
        print("\nThe gateway must be running with the 3ds platform enabled. Pair at home, on the same Wi-Fi.")
        print(away_summary(data))
        return 0
    if cmd == "devices":
        devices = DeviceStore(data / "devices.json").all()
        if not devices:
            print("No paired 3DS devices. Run: hermes 3ds pair")
        for d in devices:
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.last_seen)) if d.last_seen else "never"
            print(f"{d.name:<24} {d.fp}   paired {time.strftime('%Y-%m-%d', time.localtime(d.paired_at))}   "
                  f"last seen {seen}")
        return 0
    if cmd == "unpair":
        ok = DeviceStore(data / "devices.json").remove(args.device)
        print("Removed. If it is connected, the gateway closes its connection at its next message." if ok
              else "No such device.")
        return 0 if ok else 1
    if cmd == "away":
        return _away(getattr(args, "action", "status") or "status", data)
    print("usage: hermes 3ds {pair,devices,unpair,away}")
    return 2
