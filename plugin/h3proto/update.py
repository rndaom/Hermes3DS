"""The update check: is there a newer Hermes 3DS release than the one in use?

The computer asks, not the 3DS (the app's TLS doesn't verify certificates; Noise authenticates the PC instead).
About once a day the gateway asks GitHub for the newest release of the public repository and keeps the answer in
`update.json` in the plugin's data folder. Each `welcome` then carries it (`update.latest`, protocol.md §4.3) and
the app says so when it is older; `hermes 3ds doctor` reads the same file. The request names no user, device or
version: it is one GET of a public page. `update_check: false` under `platforms.3ds` turns it off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

RELEASE_URL = "https://api.github.com/repos/rndaom/Hermes3DS/releases/latest"
FILE = "update.json"
INTERVAL = 24 * 3600   # between checks that got an answer
RETRY = 3600           # after one that didn't
TIMEOUT = 10.0
MAX_BODY = 1 << 20
_VERSION = re.compile(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})")


def parse(version: object) -> tuple[int, int, int] | None:
    """`0.2.1`, `v0.2.1` or the app's `3ds-0.2.1` as numbers; None for anything else (a dev build, junk)."""
    if not isinstance(version, str):
        return None
    m = _VERSION.fullmatch(version.strip().removeprefix("3ds-"))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def newer(latest: object, mine: object) -> bool:
    """Whether `latest` is a later release than `mine`. False when either is not a release version."""
    a, b = parse(latest), parse(mine)
    return a is not None and b is not None and a > b


def fetch_latest() -> str:
    """The newest release's version as `X.Y.Z`, straight from GitHub. Raises when it can't be had."""
    req = urllib.request.Request(RELEASE_URL, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": "hermes3ds-update-check"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (a fixed https URL)
        tag = json.loads(resp.read(MAX_BODY)).get("tag_name")
    v = parse(tag)
    if v is None:
        raise ValueError(f"unexpected release tag {tag!r}")
    return "%d.%d.%d" % v


def read(data: Path) -> tuple[str, float]:
    """What the last check found: (`X.Y.Z` or "", when it was checked or 0). Never raises."""
    try:
        raw = json.loads((data / FILE).read_text(encoding="utf-8"))
        v = parse(raw.get("latest"))
        checked = float(raw.get("checked") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return "", 0.0
    return ("%d.%d.%d" % v if v else ""), checked


class UpdateChecker:
    """Keeps `update.json` fresh while the gateway runs and tells `on_latest` each version it learns."""

    def __init__(self, data: Path, on_latest: Callable[[str], None], *,
                 fetch: Callable[[], str] = fetch_latest, now: Callable[[], float] = time.time):
        self.data = data
        self.on_latest = on_latest
        self.fetch = fetch
        self.now = now

    async def once(self) -> float:
        """Check if the last answer is old; returns how many seconds until the next look is due."""
        latest, checked = read(self.data)
        if latest:
            self.on_latest(latest)
        age = self.now() - checked
        if latest and 0 <= age < INTERVAL:
            return INTERVAL - age
        try:
            latest = await asyncio.get_running_loop().run_in_executor(None, self.fetch)
        except Exception as exc:  # offline, rate-limited, GitHub down: what was known stays, try again later
            log.debug("3ds update check failed: %s", exc)
            return RETRY
        try:
            tmp = self.data / (FILE + ".tmp")
            tmp.write_text(json.dumps({"latest": latest, "checked": self.now()}), encoding="utf-8")
            tmp.replace(self.data / FILE)
        except OSError as exc:
            log.debug("3ds update check: cannot save %s: %s", FILE, exc)
        self.on_latest(latest)
        return INTERVAL

    async def run(self) -> None:
        while True:
            await asyncio.sleep(max(60.0, await self.once()))
