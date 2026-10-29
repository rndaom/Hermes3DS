"""Noise_IK_25519_ChaChaPoly_BLAKE2b (Noise spec rev. 34), initiator and responder.

Only what the Hermes 3DS protocol needs: the IK pattern, no PSK, no rekey.
Dependencies: hashlib/hmac (stdlib) and `cryptography` (X25519, ChaCha20-Poly1305).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidTag

PROTOCOL_NAME = b"Noise_IK_25519_ChaChaPoly_BLAKE2b"
PROLOGUE = b"hermes3ds/1"
DHLEN = 32
HASHLEN = 64
TAGLEN = 16
MAX_MESSAGE = 65535
_MAX_NONCE = 2**64 - 1


class NoiseError(Exception):
    """Handshake or transport failure; the connection must be closed."""


# --- primitives -------------------------------------------------------------

def _hash(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=HASHLEN).digest()


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, lambda: hashlib.blake2b(digest_size=HASHLEN)).digest()


def _hkdf2(ck: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    temp = _hmac(ck, ikm)
    out1 = _hmac(temp, b"\x01")
    out2 = _hmac(temp, out1 + b"\x02")
    return out1, out2


class KeyPair:
    """X25519 key pair as raw 32-byte strings."""

    def __init__(self, private: bytes | None = None):
        sk = X25519PrivateKey.from_private_bytes(private) if private else X25519PrivateKey.generate()
        self._sk = sk
        self.private = sk.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        self.public = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def dh(self, public: bytes) -> bytes:
        try:
            return self._sk.exchange(X25519PublicKey.from_public_bytes(public))
        except ValueError as exc:  # low-order point -> all-zero shared secret
            raise NoiseError("invalid public key") from exc


# --- CipherState / SymmetricState -------------------------------------------

class CipherState:
    def __init__(self, key: bytes | None = None):
        self._aead = ChaCha20Poly1305(key) if key is not None else None
        self.n = 0

    @property
    def has_key(self) -> bool:
        return self._aead is not None

    def _nonce(self) -> bytes:
        if self.n >= _MAX_NONCE:
            raise NoiseError("nonce exhausted")
        return b"\x00\x00\x00\x00" + struct.pack("<Q", self.n)

    def encrypt(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._aead is None:
            return plaintext
        ct = self._aead.encrypt(self._nonce(), plaintext, ad)
        self.n += 1
        return ct

    def decrypt(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._aead is None:
            return ciphertext
        try:
            pt = self._aead.decrypt(self._nonce(), ciphertext, ad)
        except InvalidTag as exc:
            raise NoiseError("decrypt failed") from exc
        self.n += 1
        return pt


class _SymmetricState:
    def __init__(self, protocol_name: bytes):
        if len(protocol_name) <= HASHLEN:
            self.h = protocol_name.ljust(HASHLEN, b"\x00")
        else:
            self.h = _hash(protocol_name)
        self.ck = self.h
        self.cs = CipherState()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = _hkdf2(self.ck, ikm)
        self.cs = CipherState(temp_k[:32])

    def mix_hash(self, data: bytes) -> None:
        self.h = _hash(self.h + data)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ct = self.cs.encrypt(self.h, plaintext)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        pt = self.cs.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return pt

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _hkdf2(self.ck, b"")
        return CipherState(k1[:32]), CipherState(k2[:32])


# --- IK handshake -----------------------------------------------------------

class Transport:
    """Post-handshake cipher pair for one side of the connection."""

    def __init__(self, send: CipherState, recv: CipherState, handshake_hash: bytes, remote_static: bytes):
        self._send = send
        self._recv = recv
        self.handshake_hash = handshake_hash
        self.remote_static = remote_static

    def encrypt(self, plaintext: bytes) -> bytes:
        if len(plaintext) + TAGLEN > MAX_MESSAGE:
            raise NoiseError("message too large")
        return self._send.encrypt(b"", plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._recv.decrypt(b"", ciphertext)


class Initiator:
    """Client side (the 3DS). Knows the server static key in advance."""

    def __init__(self, static: KeyPair, remote_static: bytes, prologue: bytes = PROLOGUE,
                 ephemeral: KeyPair | None = None):
        self._s = static
        self._rs = remote_static
        self._e = ephemeral
        self._ss = _SymmetricState(PROTOCOL_NAME)
        self._ss.mix_hash(prologue)
        self._ss.mix_hash(remote_static)  # <- s

    def write_message_1(self, payload: bytes) -> bytes:
        ss = self._ss
        if self._e is None:
            self._e = KeyPair()
        out = self._e.public
        ss.mix_hash(self._e.public)                     # e
        ss.mix_key(self._e.dh(self._rs))                # es
        out += ss.encrypt_and_hash(self._s.public)      # s
        ss.mix_key(self._s.dh(self._rs))                # ss
        out += ss.encrypt_and_hash(payload)
        if len(out) > MAX_MESSAGE:
            raise NoiseError("message too large")
        return out

    def read_message_2(self, message: bytes) -> tuple[bytes, Transport]:
        ss = self._ss
        if len(message) < DHLEN + TAGLEN:
            raise NoiseError("short handshake message")
        re = message[:DHLEN]
        ss.mix_hash(re)                                 # e
        ss.mix_key(self._e.dh(re))                      # ee
        ss.mix_key(self._s.dh(re))                      # se
        payload = ss.decrypt_and_hash(message[DHLEN:])
        c1, c2 = ss.split()
        return payload, Transport(c1, c2, ss.h, self._rs)


class Responder:
    """Server side (the plugin)."""

    def __init__(self, static: KeyPair, prologue: bytes = PROLOGUE, ephemeral: KeyPair | None = None):
        self._s = static
        self._e = ephemeral
        self._re: bytes | None = None
        self.remote_static: bytes | None = None
        self._ss = _SymmetricState(PROTOCOL_NAME)
        self._ss.mix_hash(prologue)
        self._ss.mix_hash(static.public)  # <- s

    def read_message_1(self, message: bytes) -> bytes:
        """Returns the payload; `remote_static` is set afterwards for the caller to authorize."""
        ss = self._ss
        if len(message) < DHLEN + DHLEN + TAGLEN + TAGLEN:
            raise NoiseError("short handshake message")
        self._re = message[:DHLEN]
        ss.mix_hash(self._re)                                           # e
        ss.mix_key(self._s.dh(self._re))                                # es
        self.remote_static = ss.decrypt_and_hash(message[DHLEN:DHLEN + DHLEN + TAGLEN])  # s
        ss.mix_key(self._s.dh(self.remote_static))                      # ss
        return ss.decrypt_and_hash(message[DHLEN + DHLEN + TAGLEN:])

    def write_message_2(self, payload: bytes) -> tuple[bytes, Transport]:
        ss = self._ss
        if self._e is None:
            self._e = KeyPair()
        out = self._e.public
        ss.mix_hash(self._e.public)                     # e
        ss.mix_key(self._e.dh(self._re))                # ee
        ss.mix_key(self._e.dh(self.remote_static))      # se
        out += ss.encrypt_and_hash(payload)
        if len(out) > MAX_MESSAGE:
            raise NoiseError("message too large")
        c1, c2 = ss.split()
        return out, Transport(c2, c1, ss.h, self.remote_static)


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
