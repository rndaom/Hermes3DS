#!/usr/bin/env python3
"""Local stand-in for Tailscale Funnel's TLS-terminated TCP forward, for testing the 3DS away route.

Accepts TLS on --listen (self-signed ECDSA P-256 certificate, like the Let's Encrypt one a funnel serves; the
3DS doesn't check it), then forwards the plain stream to --target, just as
`tailscale funnel --tls-terminated-tcp=443 tcp://127.0.0.1:7339` does. Logs SNI, protocol, cipher, session
resumption and the handshake time of every connection.

    python tools/tls_funnel.py --listen 0.0.0.0:8443 --target 127.0.0.1:7339

Like Go's TLS server (which terminates the real funnel), it lets the client's cipher order win, so a client
that puts ChaCha20 first gets ChaCha20.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import ssl
import tempfile
import time
from pathlib import Path

log = logging.getLogger("tls_funnel")


def self_signed(dirpath: Path, name: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = dirpath / "cert.pem", dirpath / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return host or "0.0.0.0", int(port)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    n = 0
    try:
        while data := await reader.read(65536):
            n += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
    return n


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:8443")
    ap.add_argument("--target", default="127.0.0.1:7339")
    ap.add_argument("--name", default="hermes-pc.example.ts.net", help="certificate name")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    tmp = Path(tempfile.mkdtemp(prefix="tls_funnel-"))
    cert, key = self_signed(tmp, args.name)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    ctx.options &= ~ssl.OP_CIPHER_SERVER_PREFERENCE
    sni: dict[int, tuple[str | None, float]] = {}

    def on_sni(sslobj, name, _ctx):
        sni[id(sslobj)] = (name, time.monotonic())

    ctx.sni_callback = on_sni
    target = hostport(args.target)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        obj = writer.get_extra_info("ssl_object")
        peer = writer.get_extra_info("peername")
        name, t0 = sni.pop(id(obj), (None, time.monotonic()))
        log.info("%s TLS ok: SNI=%s %s %s resumed=%s (%.0f ms after ClientHello)", peer, name, obj.version(),
                 obj.cipher()[0], obj.session_reused, (time.monotonic() - t0) * 1000)
        try:
            ur, uw = await asyncio.open_connection(*target)
        except OSError as exc:
            log.info("%s target %s:%d: %s", peer, *target, exc)
            writer.close()
            return
        up, down = await asyncio.gather(pipe(reader, uw), pipe(ur, writer))
        log.info("%s closed (%d bytes up, %d down)", peer, up, down)

    host, port = hostport(args.listen)
    server = await asyncio.start_server(handle, host, port, ssl=ctx, ssl_handshake_timeout=30)
    log.info("TLS %s:%d -> %s:%d (cert CN=%s)", host, port, *target, args.name)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
