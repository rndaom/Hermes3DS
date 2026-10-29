"""On-disk blob store for media attachments (docs/protocol.md §4.7, §5).

Layout: `<root>/<id>` holds the bytes and `<root>/<id>.json` the metadata
`{mime, name, size, created, role, path?, ref?, owner?}`. Entries older than `ttl` are removed when the store is
created, so an attachment replayed after a reconnect or gateway restart still loads for 7 days.

Uploads from the 3DS (§5.1) arrive in pieces into `<root>/<id>.part`, with `partial: true` in their metadata
until the last byte is in; then the file is renamed to `<id>` and the blob reads like any other. A partial
upload is kept for an hour so a reconnect can resume it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from .pairing import _atomic_write

log = logging.getLogger("hermes3ds")

BLOB_TTL = 7 * 86400.0
CHUNK = 32768  # protocol.md §5: at most 32768 data bytes per BLOB frame
COPY_LIMIT = 16 * 1024 * 1024  # larger originals are served from their own path instead of being copied
MAX_BLOB = 0xFFFFFFFF  # offsets are u32
UPLOAD_MAX = 8 * 1024 * 1024  # one upload from the 3DS: a 2 min voice message is ~1 MB, a photo ~100 KB
CLEANUP_EVERY = 3600.0
UPLOAD_TTL = 3600.0           # an unfinished upload waits this long for its device to come back
_UPLOAD_ID = re.compile(r"u[a-z0-9]{1,31}")


class BlobError(Exception):
    """Unknown/expired blob or an impossible request (offset past the end)."""


def new_id() -> str:
    """'b' + 16 random base32 chars: opaque, ASCII, 17 characters."""
    return "b" + base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()


class BlobStore:
    def __init__(self, root: Path, ttl: float = BLOB_TTL):
        self.root = Path(root)
        self.ttl = ttl
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleaned = 0.0
        self.cleanup()

    def _maybe_cleanup(self) -> None:
        """The gateway runs for weeks: expire old blobs about hourly as new ones are written, not only at start."""
        if time.time() - self._cleaned > CLEANUP_EVERY:
            self.cleanup()

    # --- writing -----------------------------------------------------------------

    def _write_meta(self, bid: str, meta: dict[str, Any]) -> None:
        self._maybe_cleanup()
        _atomic_write(self.root / (bid + ".json"), json.dumps(meta, ensure_ascii=False).encode("utf-8"))

    @staticmethod
    def _ref(bid: str, meta: dict[str, Any]) -> dict[str, Any]:
        return {"id": bid, "size": meta["size"], "mime": meta["mime"]}

    def put_bytes(self, data: bytes, mime: str = "application/octet-stream", name: str = "",
                  role: str = "file") -> dict[str, Any]:
        """Store `data`; returns the protocol ref `{id,size,mime}`.

        `role` is file, view, thumb, text or audio."""
        if len(data) > MAX_BLOB:
            raise BlobError("blob too large")
        bid = new_id()
        _atomic_write(self.root / bid, data)
        meta = {"mime": mime, "name": name, "size": len(data), "created": time.time(), "role": role}
        self._write_meta(bid, meta)
        return self._ref(bid, meta)

    def put_file(self, path: str | os.PathLike, mime: str = "application/octet-stream", name: str = "",
                 role: str = "file") -> dict[str, Any]:
        """Store the file at `path`. Originals up to COPY_LIMIT are copied (Save to SD keeps working after
        the file moves); bigger ones are referenced by path and served from there."""
        src = Path(path)
        size = src.stat().st_size
        if size > MAX_BLOB:
            raise BlobError("blob too large")
        bid = new_id()
        meta: dict[str, Any] = {"mime": mime, "name": name or src.name, "size": size, "created": time.time(),
                                "role": role, "path": str(src.resolve())}
        if size <= COPY_LIMIT:
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=bid + ".")
            try:
                with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
                    shutil.copyfileobj(inp, out, 1 << 20)
                os.replace(tmp, self.root / bid)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        else:
            meta["ref"] = True
        self._write_meta(bid, meta)
        return self._ref(bid, meta)

    # --- uploads from the 3DS (protocol.md §5.1) ------------------------------------------

    @staticmethod
    def is_upload_id(bid: str) -> bool:
        return isinstance(bid, str) and _UPLOAD_ID.fullmatch(bid) is not None

    def _raw_meta(self, bid: str) -> dict[str, Any] | None:
        try:
            meta = json.loads((self.root / (bid + ".json")).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def begin_upload(self, bid: str, size: int, mime: str, name: str, owner: str) -> int:
        """Start or resume upload `bid`; returns how many bytes are already here (== size when complete).

        Raises BlobError: `bad_request` (id, size), `too_large`, `denied` (another device's id, or the same id
        with a different size)."""
        if not self.is_upload_id(bid):
            raise BlobError("bad_request")
        if not isinstance(size, int) or size < 1:
            raise BlobError("bad_request")
        if size > UPLOAD_MAX:
            raise BlobError("too_large")
        meta = self._raw_meta(bid)
        now = time.time()
        if meta is not None:
            stale = now - float(meta.get("created", 0)) > (UPLOAD_TTL if meta.get("partial") else self.ttl)
            if meta.get("owner") != owner or (not stale and meta.get("size") != size):
                raise BlobError("denied")
            if not stale:
                if not meta.get("partial"):
                    return size
                try:
                    have = (self.root / (bid + ".part")).stat().st_size
                except OSError:
                    have = 0
                if have <= size:
                    return have
            (self.root / (bid + ".part")).unlink(missing_ok=True)
        mime = str(mime or "application/octet-stream")[:100]
        meta = {"mime": mime, "name": str(name or "")[:120], "size": size, "created": now, "role": "upload",
                "owner": owner, "partial": True}
        (self.root / (bid + ".part")).write_bytes(b"")
        self._write_meta(bid, meta)
        return 0

    def write_upload(self, bid: str, offset: int, data: bytes, owner: str) -> tuple[int, bool]:
        """Append a piece at `offset` (must be where the upload stands). Returns (bytes now here, complete).
        Raises BlobError: `not_found` (no such upload in progress), `bad_request` (offset or overflow)."""
        meta = self._raw_meta(bid) if self.is_upload_id(bid) else None
        if meta is None or meta.get("owner") != owner:
            raise BlobError("not_found")
        size = int(meta["size"])
        if not meta.get("partial"):
            raise BlobError("bad_request")
        part = self.root / (bid + ".part")
        try:
            have = part.stat().st_size
        except OSError as exc:
            raise BlobError("not_found") from exc
        if offset != have or have + len(data) > size:
            raise BlobError("bad_request")
        with open(part, "ab") as fh:
            fh.write(data)
        have += len(data)
        if have < size:
            return have, False
        os.replace(part, self.root / bid)
        meta.pop("partial", None)
        self._write_meta(bid, meta)
        return have, True

    def upload_path(self, bid: str, owner: str) -> Path | None:
        """A complete upload of `owner`'s, or None."""
        meta = self.meta(bid) if self.is_upload_id(bid) else None
        if meta is None or meta.get("owner") != owner or meta.get("role") != "upload":
            return None
        return self.root / bid

    # --- reading -----------------------------------------------------------------

    def meta(self, bid: str) -> dict[str, Any] | None:
        """Metadata for `bid`, or None when unknown or expired."""
        if not bid or not bid.isascii() or not bid.isalnum():  # ids are never paths
            return None
        try:
            meta = json.loads((self.root / (bid + ".json")).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict) or meta.get("partial") or time.time() - float(meta.get("created", 0)) > self.ttl:
            return None
        return meta

    def data_path(self, bid: str) -> Path | None:
        """Where the bytes live: the stored copy, or the original for a by-reference blob."""
        meta = self.meta(bid)
        if meta is None:
            return None
        return Path(meta["path"]) if meta.get("ref") else self.root / bid

    def original_path(self, bid: str) -> Path | None:
        """The file to open for 'Open on PC': the source file if it still exists, else the stored copy."""
        meta = self.meta(bid)
        if meta is None:
            return None
        src = meta.get("path")
        if src and Path(src).is_file():
            return Path(src)
        p = self.root / bid
        return p if p.is_file() else None

    def open_chunks(self, bid: str, offset: int = 0, chunk: int = CHUNK) -> Iterator[tuple[int, bytes]]:
        """Validate the request now, then yield `(offset, bytes)` pieces of at most `chunk` bytes from `offset`.
        Raises BlobError (unknown id, bad offset, source changed) before the first piece."""
        meta = self.meta(bid)
        path = self.data_path(bid) if meta else None
        if meta is None or path is None:
            raise BlobError("not_found")
        size = meta["size"]
        if not 0 <= offset <= size:
            raise BlobError("offset out of range")
        try:
            if path.stat().st_size != size:
                raise BlobError("not_found")
            fh = open(path, "rb")
        except OSError as exc:
            raise BlobError("not_found") from exc
        return self._chunks(fh, offset, size, min(max(1, chunk), CHUNK))

    @staticmethod
    def _chunks(fh, offset: int, size: int, chunk: int) -> Iterator[tuple[int, bytes]]:
        with fh:
            fh.seek(offset)
            while offset < size:
                data = fh.read(min(chunk, size - offset))
                if not data:
                    return
                yield offset, data
                offset += len(data)

    # --- expiry --------------------------------------------------------------------

    def cleanup(self) -> int:
        """Delete blobs older than the TTL (and strays without metadata). Returns how many were removed."""
        now, removed = time.time(), 0
        self._cleaned = now
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return 0
        names = {e.name for e in entries}
        for e in entries:
            try:
                if e.name.endswith(".json"):
                    try:
                        meta = json.loads(Path(e.path).read_text("utf-8"))
                        created, partial = float(meta.get("created", 0)), bool(meta.get("partial"))
                    except (OSError, ValueError, AttributeError, TypeError):
                        created, partial = 0.0, False
                    if now - created > (UPLOAD_TTL if partial else self.ttl):
                        Path(e.path).unlink(missing_ok=True)
                        (self.root / e.name[:-5]).unlink(missing_ok=True)
                        (self.root / (e.name[:-5] + ".part")).unlink(missing_ok=True)
                        removed += 1
                elif "." in e.name:  # leftover temp file from an interrupted write (or a stale partial upload)
                    if now - e.stat().st_mtime > max(3600, UPLOAD_TTL):
                        Path(e.path).unlink(missing_ok=True)
                elif e.name + ".json" not in names and now - e.stat().st_mtime > 3600:
                    Path(e.path).unlink(missing_ok=True)  # data without metadata
            except OSError:
                continue
        return removed
