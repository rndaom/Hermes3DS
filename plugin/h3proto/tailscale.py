"""Away-from-home access through Tailscale Funnel (protocol.md §2.4).

Tailscale runs only on the PC. `tailscale funnel --bg --tls-terminated-tcp=443 tcp://127.0.0.1:<away port>`
publishes `<machine>.<tailnet>.ts.net:443` on the internet; Tailscale terminates TLS there and hands the plain
stream to the plugin's loopback-only away listener, where Noise runs exactly as on the LAN.

Everything here shells out to the `tailscale` CLI and parses its JSON, so it works with whatever version the
user installed and needs no Tailscale API key. Blocking: call it from a worker thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("hermes3ds")

FUNNEL_PORTS = (443, 8443, 10000)  # the only public ports Funnel allows
CMD_TIMEOUT = 15.0
# Funnel not yet allowed for this machine: the CLI prints a consent link and may wait for it to be used.
_CONSENT_URL = re.compile(r"https://login\.tailscale\.com/\S+")


@dataclass
class FunnelResult:
    ok: bool
    host: str = ""        # public name, e.g. "my-pc.tail1234.ts.net"
    port: int = 0         # public port (one of FUNNEL_PORTS)
    error: str = ""       # plain-language reason when not ok
    url: str = ""         # a consent / admin link the user should open, if any
    installed: bool = True

    @property
    def address(self) -> dict | None:
        return {"host": self.host, "port": self.port} if self.ok else None


class TailscaleError(Exception):
    pass


def find_cli() -> str | None:
    """The tailscale CLI: on PATH, else the default install location."""
    found = shutil.which("tailscale")
    if found:
        return found
    candidates: list[Path] = []
    if sys.platform == "win32":
        for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Tailscale" / "tailscale.exe")
        candidates.append(Path(r"C:\Program Files\Tailscale\tailscale.exe"))
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"))
    else:
        # ~/.local/bin: a user-level wrapper (e.g. userspace tailscaled on a Steam Deck), which systemd user
        # services often leave off PATH.
        candidates += [Path("/usr/bin/tailscale"), Path("/usr/local/bin/tailscale"),
                       Path.home() / ".local" / "bin" / "tailscale"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _run(cli: str, args: list[str], timeout: float = CMD_TIMEOUT) -> tuple[int, str]:
    """Run the CLI; returns (exit code, stdout+stderr). A timeout returns code -1 with the output so far."""
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flash from the gateway
    try:
        p = subprocess.run([cli, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, creationflags=flags)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        return -1, out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
    except OSError as exc:
        raise TailscaleError(f"cannot run {cli}: {exc}") from exc


def _json(cli: str, args: list[str]) -> dict:
    code, out = _run(cli, args)
    if code != 0:
        raise TailscaleError(out.strip() or f"tailscale {' '.join(args)} failed ({code})")
    try:
        data = json.loads(out[out.find("{"):] if "{" in out else out)
    except ValueError as exc:
        raise TailscaleError(f"unexpected output from tailscale {' '.join(args)}: {out.strip()[:200]}") from exc
    return data if isinstance(data, dict) else {}


def dns_name(status: dict) -> str:
    """This machine's MagicDNS name without the trailing dot."""
    return str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")


def find_forward(serve: dict, dns: str, target: str) -> int:
    """The public Funnel port already forwarding TLS-terminated TCP to `target`, else 0."""
    allow = serve.get("AllowFunnel") or {}
    for port, h in (serve.get("TCP") or {}).items():
        if isinstance(h, dict) and h.get("TCPForward") == target and h.get("TerminateTLS") \
                and allow.get(f"{dns}:{port}"):
            try:
                return int(port)
            except ValueError:
                continue
    return 0


def free_ports(serve: dict, dns: str) -> list[int]:
    """Funnel ports not already used by another serve/funnel handler on this machine."""
    used = {str(p) for p in (serve.get("TCP") or {})}
    for hostport in serve.get("Web") or {}:
        if hostport.rpartition(":")[0] == dns:
            used.add(hostport.rpartition(":")[2])
    return [p for p in FUNNEL_PORTS if str(p) not in used]


def explain(output: str) -> tuple[str, str]:
    """A plain-language reason and a link to open, from a failed `tailscale funnel` run."""
    text = output.lower()
    m = _CONSENT_URL.search(output)
    url = m.group(0).rstrip(".,)") if m else ""
    if url:
        return ("Funnel is not turned on for your Tailscale account yet. Open this link on the PC, allow it, "
                "then restart the Hermes gateway: " + url, url)
    if "funnel" in text and any(w in text for w in ("not available", "attribute", "not allowed", "policy",
                                                     "not enabled", "denied by")):
        url = "https://login.tailscale.com/admin/acls"
        return ("Funnel is not allowed in your tailnet policy. In the Tailscale admin console, give this PC the "
                "\"funnel\" node attribute (Access controls), then restart the Hermes gateway: " + url, url)
    if "https" in text and ("not enabled" in text or "certificate" in text):
        url = "https://login.tailscale.com/admin/dns"
        return ("HTTPS certificates are off in your tailnet. Turn on \"HTTPS Certificates\" in the Tailscale "
                "admin console (DNS page), then restart the Hermes gateway: " + url, url)
    if "access denied" in text or "permission" in text:
        return ("Tailscale refused the change (access denied). On Linux, make your user the Tailscale operator "
                "once (as root: `tailscale set --operator=$USER`), then restart the Hermes gateway.", "")
    if "flag provided but not defined" in text or "unknown flag" in text:
        return "This Tailscale is too old for Funnel TCP forwarding. Update Tailscale, then restart the gateway.", ""
    tail = output.strip().splitlines()[-1] if output.strip() else "no output"
    return f"Tailscale Funnel could not be set up: {tail}", ""


def check(cli: str | None, local_port: int, local_host: str = "127.0.0.1") -> FunnelResult:
    """Read-only: is the funnel to the away listener already on?"""
    if cli is None:
        return FunnelResult(False, installed=False, error="Tailscale is not installed on this PC.")
    try:
        status = _json(cli, ["status", "--json"])
        state = status.get("BackendState")
        if state != "Running":
            return FunnelResult(False, error=_state_error(state))
        dns = dns_name(status)
        port = find_forward(_json(cli, ["funnel", "status", "--json"]), dns, f"{local_host}:{local_port}")
    except TailscaleError as exc:
        return FunnelResult(False, error=str(exc))
    if port:
        return FunnelResult(True, dns, port)
    return FunnelResult(False, error="Away-from-home access is not set up yet (the Hermes gateway does it).")


def _state_error(state) -> str:
    if state in ("NeedsLogin", "NoState", None, ""):
        return "Tailscale is installed but not signed in. Open Tailscale on the PC and sign in."
    if state == "Stopped":
        return "Tailscale is turned off. Open Tailscale on the PC and connect."
    if state == "NeedsMachineAuth":
        return "This PC is waiting for approval in the Tailscale admin console."
    return f"Tailscale is not ready ({state}). Open Tailscale on the PC."


def ensure_funnel(local_port: int, *, local_host: str = "127.0.0.1", public_port: int = 0,
                  cli: str | None = None) -> FunnelResult:
    """Turn on the funnel to the away listener (idempotent) and return the public address.

    `public_port` 0 picks the first free Funnel port. The funnel is set with --bg, so Tailscale keeps it across
    reboots; while the gateway is down the forward simply finds nothing listening.
    """
    cli = cli or find_cli()
    if cli is None:
        return FunnelResult(False, installed=False, error="Tailscale is not installed on this PC.")
    target = f"{local_host}:{local_port}"
    try:
        status = _json(cli, ["status", "--json"])
        state = status.get("BackendState")
        if state != "Running":
            return FunnelResult(False, error=_state_error(state))
        dns = dns_name(status)
        if not dns:
            return FunnelResult(False, error="Tailscale has no name for this PC yet (MagicDNS off?). Turn on "
                                             "MagicDNS in the Tailscale admin console.")
        serve = _json(cli, ["funnel", "status", "--json"])
        port = find_forward(serve, dns, target)
        if port:
            return FunnelResult(True, dns, port)
        ports = [public_port] if public_port else free_ports(serve, dns)
        if not ports:
            return FunnelResult(False, error="Ports 443, 8443 and 10000 are all used by other Tailscale "
                                             "Funnel/Serve setups on this PC; free one of them.")
        args = ["funnel", "--bg", "--yes", f"--tls-terminated-tcp={ports[0]}", f"tcp://{target}"]
        code, out = _run(cli, args)
        if code != 0 and "not defined: -yes" in out:  # an older CLI without --yes
            args.remove("--yes")
            code, out = _run(cli, args)
        # The CLI can exit 0 without changing anything (consent link printed), so always read the result back.
        port = find_forward(_json(cli, ["funnel", "status", "--json"]), dns, target)
    except TailscaleError as exc:
        error, url = explain(str(exc))
        return FunnelResult(False, error=error, url=url)
    if port:
        return FunnelResult(True, dns, port)
    log.info("3DS: tailscale %s -> %s: %s", " ".join(args), code, out.strip()[:500])
    error, url = explain(out)
    return FunnelResult(False, error=error, url=url)


def disable_funnel(local_port: int, *, local_host: str = "127.0.0.1", cli: str | None = None) -> FunnelResult:
    """Turn off the funnel to the away listener (used by `hermes 3ds away off`)."""
    cli = cli or find_cli()
    if cli is None:
        return FunnelResult(False, installed=False, error="Tailscale is not installed on this PC.")
    try:
        status = _json(cli, ["status", "--json"])
        dns = dns_name(status)
        port = find_forward(_json(cli, ["funnel", "status", "--json"]), dns, f"{local_host}:{local_port}")
        if not port:
            return FunnelResult(True)
        code, out = _run(cli, ["funnel", f"--tls-terminated-tcp={port}", "off"])
        if code != 0:
            return FunnelResult(False, error=out.strip() or "tailscale funnel off failed")
    except TailscaleError as exc:
        return FunnelResult(False, error=str(exc))
    return FunnelResult(True)


__all__ = ["FunnelResult", "TailscaleError", "find_cli", "check", "ensure_funnel", "disable_funnel", "explain"]
