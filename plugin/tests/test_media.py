"""Blob store, blob.get streaming and attachment building (protocol.md §4.7, §5)."""

import asyncio
import io
import json
import os
import time

import pytest
from PIL import Image

from h3proto import blobs as blobs_mod
from h3proto.blobs import BlobError, BlobStore
from h3proto.client import Client, ServerError
from h3proto.framing import KIND_BLOB, KIND_JSON, decode_body
from h3proto.media import build_attachment, build_attachment_bytes
from h3proto.noise import KeyPair
from h3proto.pairing import DeviceStore, TokenStore
from h3proto.server import Hermes3DSServer, ServerConfig


# --- BlobStore ----------------------------------------------------------------------

def test_put_get_and_ids(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    ref = store.put_bytes(b"hello", "text/plain", "a.txt")
    assert set(ref) == {"id", "size", "mime"} and ref["size"] == 5 and ref["mime"] == "text/plain"
    assert ref["id"].isascii() and ref["id"].isalnum() and len(ref["id"]) <= 32
    assert store.put_bytes(b"hello")["id"] != ref["id"]
    meta = store.meta(ref["id"])
    assert meta["name"] == "a.txt" and meta["size"] == 5 and meta["role"] == "file" and "path" not in meta
    assert b"".join(d for _, d in store.open_chunks(ref["id"])) == b"hello"
    assert store.meta("nope") is None and store.meta("../x") is None and store.meta("") is None
    with pytest.raises(BlobError):
        store.open_chunks("nope")


def test_chunk_framing_offsets_and_resume(tmp_path):
    store = BlobStore(tmp_path)
    data = os.urandom(70000)
    bid = store.put_bytes(data)["id"]
    pieces = list(store.open_chunks(bid))
    assert [o for o, _ in pieces] == [0, 32768, 65536]
    assert [len(d) for _, d in pieces] == [32768, 32768, 4464]
    assert pieces[-1][0] + len(pieces[-1][1]) == 70000  # final chunk ends exactly at size
    assert b"".join(d for _, d in pieces) == data
    resumed = list(store.open_chunks(bid, 40000))
    assert resumed[0][0] == 40000 and b"".join(d for _, d in resumed) == data[40000:]
    assert list(store.open_chunks(bid, 70000)) == []  # nothing left
    with pytest.raises(BlobError):
        store.open_chunks(bid, 70001)
    with pytest.raises(BlobError):
        store.open_chunks(bid, -1)
    exact = store.put_bytes(b"x" * 32768)["id"]
    assert [len(d) for _, d in store.open_chunks(exact)] == [32768]


def test_expiry_on_construction(tmp_path):
    store = BlobStore(tmp_path)
    old = store.put_bytes(b"old")["id"]
    new = store.put_bytes(b"new")["id"]
    meta_path = tmp_path / (old + ".json")
    meta = json.loads(meta_path.read_text())
    meta["created"] = time.time() - 8 * 86400
    meta_path.write_text(json.dumps(meta))
    assert store.meta(old) is None  # expired even before cleanup runs
    again = BlobStore(tmp_path)
    assert not (tmp_path / old).exists() and not meta_path.exists()
    assert again.meta(new) is not None and (tmp_path / new).exists()
    assert BlobStore(tmp_path, ttl=0.0).cleanup() >= 0
    time.sleep(0.01)
    BlobStore(tmp_path, ttl=0.001)
    assert not (tmp_path / new).exists()


def test_put_file_copies_so_it_survives_the_original(tmp_path):
    src = tmp_path / "doc.bin"
    src.write_bytes(b"0123456789")
    store = BlobStore(tmp_path / "blobs")
    ref = store.put_file(src, "application/octet-stream", "doc.bin")
    assert store.meta(ref["id"])["path"] == str(src.resolve())
    src.unlink()
    assert b"".join(d for _, d in store.open_chunks(ref["id"])) == b"0123456789"
    assert store.original_path(ref["id"]) == tmp_path / "blobs" / ref["id"]  # source gone: the copy


def test_big_file_is_referenced_not_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(blobs_mod, "COPY_LIMIT", 8)
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 100)
    store = BlobStore(tmp_path / "blobs")
    ref = store.put_file(src)
    assert not (tmp_path / "blobs" / ref["id"]).exists()
    assert sum(len(d) for _, d in store.open_chunks(ref["id"])) == 100
    src.write_bytes(b"x" * 50)  # changed under us: refuse rather than send a torn blob
    with pytest.raises(BlobError):
        store.open_chunks(ref["id"])


# --- server: blob.get ordering, errors, cancel, replay -------------------------------

class _Backend:
    async def welcome_state(self, device):
        return {}

    async def handle(self, device, msg):
        return None

    async def on_connect(self, device):
        pass

    async def on_disconnect(self, device):
        pass


@pytest.fixture
async def server(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0), _Backend(), KeyPair(),
                          DeviceStore(tmp_path / "devices.json"), TokenStore(tmp_path / "tokens.json"), blobs=store)
    await srv.start()
    yield srv
    await srv.stop()


async def _pair(srv, key=None):
    key = key or KeyPair()
    c = await Client.connect("127.0.0.1", srv.port, device="Test 3DS", static=key, server_key=srv.static.public,
                             pair_token=srv.tokens.create())
    return c, key


async def _raw(c: Client):
    """Stop the client's reader so a test can look at the frames in wire order."""
    c._reader.cancel()
    await asyncio.sleep(0)
    return c.channel


async def test_blob_get_reply_precedes_chunks(server):
    data = os.urandom(100000)
    ref = server.blobs.put_bytes(data, "application/octet-stream", "d.bin")
    c, _ = await _pair(server)
    ch = await _raw(c)
    await ch.send({"t": "blob.get", "id": 9, "blob": ref["id"], "offset": 0})
    kind, first = await ch.recv()
    assert kind == KIND_JSON and first == {"t": "blob.get_r", "id": 9, "blob": ref["id"], "size": 100000}
    got, expect = bytearray(), 0
    while len(got) < 100000:
        kind, (bid, off, chunk) = await ch.recv()
        assert kind == KIND_BLOB and bid == ref["id"] and off == expect and 0 < len(chunk) <= 32768
        got += chunk
        expect += len(chunk)
    assert bytes(got) == data
    await c.channel.close()


async def test_blob_get_resume_and_client_helper(server):
    data = os.urandom(90000)
    bid = server.blobs.put_bytes(data)["id"]
    c, _ = await _pair(server)
    assert await c.get_blob(bid) == data
    assert await c.get_blob(bid, offset=50000) == data[50000:]
    tiny = server.blobs.put_bytes(b"")["id"]
    assert await c.get_blob(tiny) == b""
    await c.close()


async def test_blob_get_errors_and_other_json_keeps_flowing(server):
    c, _ = await _pair(server)
    with pytest.raises(ServerError) as e:
        await c.request("blob.get", blob="bnope", offset=0)
    assert e.value.code == "not_found"
    bid = server.blobs.put_bytes(b"abc")["id"]
    with pytest.raises(ServerError) as e:
        await c.request("blob.get", blob=bid, offset=99)
    assert e.value.code == "bad_request"
    big = server.blobs.put_bytes(os.urandom(2_000_000))["id"]
    fetch = asyncio.create_task(c.get_blob(big))
    await c.channel.send({"t": "ping", "n": 3})  # answered while the transfer is running
    assert (await c.next_event("pong"))["n"] == 3
    assert len(await fetch) == 2_000_000
    await c.close()


async def test_blob_cancel_and_disconnect_stop_transfers(server):
    big = server.blobs.put_bytes(os.urandom(8_000_000))["id"]
    c, _ = await _pair(server)
    ch = await _raw(c)
    await ch.send({"t": "blob.get", "id": 1, "blob": big})
    await ch.recv()
    assert any(server._transfers.values())
    await ch.send({"t": "blob.cancel", "blob": big})
    for _ in range(100):
        if not any(server._transfers.values()):
            break
        await asyncio.sleep(0.02)
    assert not any(server._transfers.values())
    await ch.send({"t": "blob.get", "id": 2, "blob": big})
    await ch.close()  # disconnect mid-transfer
    for _ in range(100):
        if not server._transfers:
            break
        await asyncio.sleep(0.02)
    assert not server._transfers


async def test_attachments_in_msg_and_replay(server):
    att = [{"kind": "file", "name": "a.txt", "size": 3, "mime": "text/plain",
            "file": {"id": "b1", "size": 3, "mime": "text/plain"}}]
    c, key = await _pair(server)
    await server.post_message(key.public, "m1", "here", attach=att)
    ev = await c.next_event("msg")
    assert ev["attach"] == att and ev["text"] == "here"
    await c.channel.send({"t": "pong"})  # the app is heard from after m1 (as its pings do): m1 arrived
    await asyncio.sleep(0.05)
    await c.close()
    await asyncio.sleep(0.05)
    await server.post_message(key.public, "m2", "", attach=att)  # offline
    await server.post_message(key.public, "m3", "plain")
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    ev = await c2.next_event("msg")
    assert ev["mid"] == "m2" and ev["attach"] == att
    ev = await c2.next_event("msg")
    assert ev["mid"] == "m3" and "attach" not in ev
    await c2.close()


async def test_delete_message_live_and_replayed(server):
    c, key = await _pair(server)
    await server.post_message(key.public, "m1", "", final=False)  # a preview that held only a MEDIA tag
    await server.post_message(key.public, "m2", "real text", final=False)
    await c.next_event("msg")
    await c.next_event("msg")
    assert server.open_blank_messages(key.public) == ["m1"]
    assert await server.delete_message(key.public, "m1")
    ev = await c.next_event("delete")
    assert ev == {"t": "delete", "mid": "m1"}
    assert not await server.delete_message(key.public, "m1")  # already gone
    await c.close()
    await asyncio.sleep(0.05)
    assert await server.delete_message(key.public, "m2")  # offline: replayed on reconnect
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    ev = await c2.next_event("delete")
    assert ev["mid"] == "m2"
    await c2.close()


# --- attachment building -------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs")


def _read(store, ref):
    return b"".join(d for _, d in store.open_chunks(ref["id"]))


def _png(path, size=(300, 200), mode="RGBA", color=(255, 0, 0, 128)):
    Image.new(mode, size, color).save(path)
    return path


def test_png_with_alpha_keeps_png_view(store, tmp_path):
    src = _png(tmp_path / "logo.png")
    att = build_attachment(store, src)
    assert att["kind"] == "image" and att["name"] == "logo.png" and att["size"] == src.stat().st_size
    assert att["view"]["mime"] == "image/png" and (att["w"], att["h"]) == (300, 200)
    view = Image.open(io.BytesIO(_read(store, att["view"])))
    assert view.format == "PNG" and view.mode == "RGBA" and view.getpixel((0, 0))[3] == 128
    assert att["thumb"]["mime"] == "image/jpeg" and (att["tw"], att["th"]) == (120, 80)
    thumb = Image.open(io.BytesIO(_read(store, att["thumb"])))
    assert thumb.format == "JPEG" and thumb.size == (120, 80)
    r, g, b = thumb.convert("RGB").getpixel((60, 40))
    assert r > 240 and 100 < g < 160 and 100 < b < 160  # half-transparent red flattened onto white
    assert att["file"]["mime"] == "image/png" and _read(store, att["file"]) == src.read_bytes()
    assert list(att) == ["kind", "name", "size", "view", "w", "h", "thumb", "tw", "th", "file"]


def test_opaque_photo_png_becomes_baseline_jpeg(store, tmp_path):
    src = tmp_path / "photo.png"
    Image.effect_noise((300, 200), 64).convert("RGB").save(src)  # photo-like: JPEG is far smaller
    att = build_attachment(store, src)
    assert att["view"]["mime"] == "image/jpeg"
    view = Image.open(io.BytesIO(_read(store, att["view"])))
    assert view.format == "JPEG" and "progressive" not in view.info and "progression" not in view.info


def test_flat_png_view_stays_png_when_smaller(store, tmp_path):
    src = _png(tmp_path / "flat.png", mode="RGB", color=(10, 200, 30))
    att = build_attachment(store, src)
    assert att["view"]["mime"] == "image/png"
    assert Image.open(io.BytesIO(_read(store, att["view"]))).format == "PNG"


def test_exif_rotation_is_applied(store, tmp_path):
    im = Image.new("RGB", (200, 100), (0, 0, 255))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    src = tmp_path / "phone.jpg"
    im.save(src, exif=exif)
    att = build_attachment(store, src)
    assert (att["w"], att["h"]) == (100, 200)  # landscape pixels, portrait picture
    assert (att["tw"], att["th"]) == (45, 90)
    view = Image.open(io.BytesIO(_read(store, att["view"])))
    assert view.size == (100, 200)
    assert att["file"]["mime"] == "image/jpeg"


def test_large_image_is_downscaled_small_is_not_upscaled(store, tmp_path):
    big = tmp_path / "big.jpg"
    Image.new("RGB", (3000, 2000), (5, 5, 5)).save(big)
    att = build_attachment(store, big)
    assert (att["w"], att["h"]) == (800, 533) and (att["tw"], att["th"]) == (120, 80)
    assert att["view"]["size"] < att["size"] or att["size"] < 20000
    small = _png(tmp_path / "small.png", size=(64, 48), mode="RGB", color=(1, 2, 3))
    att = build_attachment(store, small)
    assert (att["w"], att["h"]) == (64, 48) and (att["tw"], att["th"]) == (64, 48)


def test_gif_sends_first_frame(store, tmp_path):
    frames = [Image.new("P", (40, 30), i) for i in (1, 2)]
    src = tmp_path / "anim.gif"
    frames[0].save(src, save_all=True, append_images=frames[1:])
    att = build_attachment(store, src)
    assert att["kind"] == "image" and (att["w"], att["h"]) == (40, 30)


def test_non_image_file(store, tmp_path):
    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    att = build_attachment(store, src)
    assert att == {"kind": "file", "name": "report.pdf", "size": 13, "mime": "application/pdf",
                   "file": {"id": att["file"]["id"], "size": 13, "mime": "application/pdf"}}
    assert _read(store, att["file"]) == b"%PDF-1.4 fake"


def test_unreadable_image_falls_back_to_file(store, tmp_path):
    src = tmp_path / "broken.png"
    src.write_bytes(b"not really a png")
    att = build_attachment(store, src, try_image=True)
    assert att["kind"] == "file" and "view" not in att and att["file"]["size"] == 16


def test_oversize_original_is_omitted(store, tmp_path):
    src = tmp_path / "huge.bin"
    with open(src, "wb") as fh:
        fh.truncate(16 * 1024 * 1024 + 1)
    att = build_attachment(store, src)
    assert att["kind"] == "file" and att["size"] == 16 * 1024 * 1024 + 1 and "file" not in att


def test_bytes_variant_and_name_cleaning(store, tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (50, 50)).save(buf, "PNG")
    att = build_attachment_bytes(store, buf.getvalue(), "..\\evil/..\\pic\x07.png", None, True)
    assert att["name"] == "pic.png" and att["kind"] == "image" and att["file"]["mime"] == "image/png"
    att = build_attachment_bytes(store, b"hello", "note", "text/plain")
    assert att["kind"] == "file" and att["name"] == "note"
