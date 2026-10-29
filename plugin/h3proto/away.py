"""Keeps the away-from-home address (protocol.md §2.4) current while the gateway runs.

Modes (config `platforms.3ds.away`, env THREEDS_AWAY):
  auto  (default) Tailscale Funnel when Tailscale is installed, otherwise no away route
  on    the same, but a missing Tailscale is logged as a warning
  off   no away listener; the 3DS is told to forget its away address
`away_address: host:port` advertises a tunnel you run yourself (TLS-terminating, forwarding plain TCP to the
away listener) instead of using Tailscale.

The latest outcome goes to away_status.json in the plugin data folder for `hermes 3ds away` / `pair` to show.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import tailscale
from .pairing import _atomic_write

log = logging.getLogger("hermes3ds")

RECHECK_OK = 600.0     # re-verify a working funnel (name change, someone turned it off)
RECHECK_FAIL = 60.0    # Tailscale not running / not signed in yet
STATUS_FILE = "away_status.json"
DISABLED_FILE = "away_off"  # written by `hermes 3ds away off`


def parse_mode(value: Any) -> str:
    v = str(value if value is not None else "auto").strip().lower()
    if v in ("0", "false", "no", "off", "disabled"):
        return "off"
    if v in ("1", "true", "yes", "on", "enabled"):
        return "on"
    return "auto"


def parse_address(value: Any) -> dict | None:
    """'host:port' (or 'host', port 443) -> {"host","port"}; None when empty or malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    host, sep, port = text.rpartition(":")
    if not sep:
        host, port = text, "443"
    try:
        p = int(port)
    except ValueError:
        return None
    if not host or not 0 < p < 65536 or len(host) > 127:
        return None
    return {"host": host, "port": p}


def read_status(data_dir: Path) -> dict | None:
    try:
        return json.loads((data_dir / STATUS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def disabled_by_user(data_dir: Path) -> bool:
    return (data_dir / DISABLED_FILE).exists()


class AwayManager:
    def __init__(self, server, data_dir: Path, *, mode: str = "auto", address: dict | None = None,
                 public_port: int = 0, ensure: Callable[..., tailscale.FunnelResult] | None = None,
                 find_cli: Callable[[], str | None] | None = None):
        self.server = server
        self.data_dir = data_dir
        self.mode = mode
        self.address = address
        self.public_port = public_port
        self._ensure = ensure or tailscale.ensure_funnel
        self._find_cli = find_cli or tailscale.find_cli
        self.last: tailscale.FunnelResult | None = None

    def _write_status(self, res: tailscale.FunnelResult, source: str) -> None:
        data = {"ok": res.ok, "host": res.host, "port": res.port, "error": res.error, "url": res.url,
                "installed": res.installed, "source": source, "ts": time.time()}
        try:
            _atomic_write(self.data_dir / STATUS_FILE, json.dumps(data, indent=1).encode("utf-8"))
        except OSError as exc:
            log.debug("3DS: cannot write %s: %s", STATUS_FILE, exc)

    async def once(self) -> float | None:
        """One check. Returns seconds until the next one, or None when there is nothing more to do."""
        if self.mode == "off" or disabled_by_user(self.data_dir):
            await self.server.set_away(None)
            self._write_status(tailscale.FunnelResult(False, error="Away-from-home access is turned off."), "off")
            return RECHECK_OK if self.mode != "off" else None  # `away on` can bring it back without a restart
        if self.address:
            await self.server.set_away(self.address)
            self._write_status(tailscale.FunnelResult(True, self.address["host"], self.address["port"]), "manual")
            log.info("3DS: away address %s:%d (configured)", self.address["host"], self.address["port"])
            return None
        cli = await asyncio.to_thread(self._find_cli)
        if cli is None:
            res = tailscale.FunnelResult(False, installed=False, error="Tailscale is not installed on this PC.")
            (log.warning if self.mode == "on" else log.info)(
                "3DS: away-from-home access off: Tailscale is not installed (https://tailscale.com/download)")
            await self.server.set_away(None)
            self._write_status(res, "tailscale")
            return RECHECK_OK  # installed later: picked up without a gateway restart
        res = await asyncio.to_thread(self._ensure, self.server.away_port, public_port=self.public_port, cli=cli)
        changed = self.last is None or (res.ok, res.host, res.port, res.error) != \
            (self.last.ok, self.last.host, self.last.port, self.last.error)
        self.last = res
        self._write_status(res, "tailscale")
        if res.ok:
            if changed:
                log.info("3DS: away from home via Tailscale Funnel at %s:%d", res.host, res.port)
            await self.server.set_away(res.address)
            return RECHECK_OK
        # Keep whatever the 3DS already knows: Tailscale may just not be running yet.
        if changed:
            log.warning("3DS: away-from-home access not available: %s", res.error)
        return RECHECK_FAIL

    async def run(self) -> None:
        while True:
            try:
                delay = await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("3DS: away-from-home check failed")
                delay = RECHECK_FAIL
            if delay is None:
                return
            await asyncio.sleep(delay)
