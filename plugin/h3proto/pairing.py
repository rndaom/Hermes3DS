"""Server key, paired-device store and pairing tokens (protocol.md §2.2)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .noise import KeyPair

TOKEN_TTL = 300.0
QR_TOKEN_BYTES = 16
CODE_TOKEN_BYTES = 5  # 8 base32 chars for manual entry


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fingerprint(public_key: bytes) -> str:
    """8 base32 chars of BLAKE2b(key); shown to users and used in chat ids."""
    digest = hashlib.blake2b(public_key, digest_size=32).digest()
    return base64.b32encode(digest).decode("ascii")[:8]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_or_create_key(path: Path) -> KeyPair:
    if path.exists():
        raw = path.read_bytes()
        if len(raw) == 32:
            return KeyPair(raw)
        raise ValueError(f"corrupt key file {path}")
    kp = KeyPair()
    _atomic_write(path, kp.private)
    return kp


@dataclass
class Device:
    key: bytes
    name: str
    paired_at: float
    last_seen: float = 0.0

    @property
    def fp(self) -> str:
        return fingerprint(self.key)


class DeviceStore:
    """devices.json: the static keys allowed to connect."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._devices: dict[bytes, Device] = {}
        self._stamp: tuple[int, int] | None = None
        self._sync()

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size

    def _sync(self) -> None:
        """Re-read devices.json when another process (`hermes 3ds unpair` while the gateway runs) changed it,
        so a removed device is refused at once and our next save doesn't write it back."""
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return
        devices: dict[bytes, Device] = {}
        if stamp is not None:
            for d in json.loads(self.path.read_text("utf-8")).get("devices", []):
                key = unb64u(d["key"])
                devices[key] = Device(key, d.get("name", ""), d.get("paired_at", 0.0), d.get("last_seen", 0.0))
        self._devices, self._stamp = devices, stamp

    def _save(self) -> None:
        data = {"devices": [
            {"key": b64u(d.key), "name": d.name, "fp": d.fp, "paired_at": d.paired_at, "last_seen": d.last_seen}
            for d in self._devices.values()
        ]}
        _atomic_write(self.path, json.dumps(data, indent=1).encode("utf-8"))
        self._stamp = self._file_stamp()

    def get(self, key: bytes) -> Device | None:
        with self._lock:
            self._sync()
            return self._devices.get(key)

    def all(self) -> list[Device]:
        with self._lock:
            self._sync()
            return list(self._devices.values())

    def add(self, key: bytes, name: str) -> Device:
        with self._lock:
            self._sync()
            dev = Device(key, name, time.time())
            self._devices[key] = dev
            self._save()
            return dev

    def touch(self, key: bytes, name: str | None = None) -> None:
        with self._lock:
            self._sync()
            dev = self._devices.get(key)
            if dev:
                dev.last_seen = time.time()
                if name:
                    dev.name = name
                self._save()

    def remove(self, name_or_fp: str) -> bool:
        with self._lock:
            self._sync()
            for key, dev in list(self._devices.items()):
                if name_or_fp in (dev.name, dev.fp):
                    del self._devices[key]
                    self._save()
                    return True
            return False


class TokenStore:
    """One-time pairing tokens. Persisted so the `hermes 3ds pair` CLI can create one for the gateway."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return []

    def _store(self, tokens: list[dict]) -> None:
        now = time.time()
        _atomic_write(self.path, json.dumps([t for t in tokens if t["exp"] > now]).encode("utf-8"))

    def create(self, nbytes: int = QR_TOKEN_BYTES, ttl: float = TOKEN_TTL) -> bytes:
        token = secrets.token_bytes(nbytes)
        with self._lock:
            tokens = self._load()
            tokens.append({"h": hashlib.blake2b(token, digest_size=32).hexdigest(), "exp": time.time() + ttl})
            self._store(tokens)
        return token

    def consume(self, token: bytes) -> bool:
        digest = hashlib.blake2b(token, digest_size=32).hexdigest()
        with self._lock:
            tokens = self._load()
            now = time.time()
            for t in tokens:
                if t["exp"] > now and hmac.compare_digest(t["h"], digest):
                    tokens.remove(t)
                    self._store(tokens)
                    return True
            return False


# --- pairing URI ------------------------------------------------------------

def pairing_uri(hosts: list[str], port: int, server_key: bytes, token: bytes, name: str) -> str:
    query = urlencode({"h": ",".join(hosts), "p": port, "k": b64u(server_key), "t": b64u(token), "n": name},
                      quote_via=quote, safe=",")
    return f"hermes3ds:1?{query}"


@dataclass
class PairingInfo:
    hosts: list[str]
    port: int
    server_key: bytes
    token: bytes
    name: str


def parse_pairing_uri(uri: str) -> PairingInfo:
    parts = urlsplit(uri)
    if parts.scheme != "hermes3ds" or parts.path != "1":
        raise ValueError("not a hermes3ds:1 pairing uri")
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    key = unb64u(q["k"])
    if len(key) != 32:
        raise ValueError("bad server key")
    return PairingInfo([h for h in q["h"].split(",") if h], int(q["p"]), key, unb64u(q["t"]), q.get("n", ""))


def code_from_token(token: bytes) -> str:
    return base64.b32encode(token).decode("ascii").rstrip("=")


def token_from_code(code: str) -> bytes:
    code = code.strip().upper().replace("-", "").replace(" ", "")
    return base64.b32decode(code + "=" * (-len(code) % 8))
