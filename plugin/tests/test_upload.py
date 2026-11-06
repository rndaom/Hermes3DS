"""Uploads from the 3DS (protocol.md §5.1) and what the adapter makes of them (§4.4 `voice`, `attach`)."""

import asyncio
import io
import math
import struct
import sys
import time
import types
import wave

import pytest

from h3proto import audio
from h3proto.blobs import UPLOAD_MAX, UPLOAD_TTL, BlobError, BlobStore
from h3proto.client import Client, ServerError
from h3proto.noise import KeyPair
from h3proto.pairing import DeviceStore, TokenStore
from h3proto.server import Hermes3DSServer, ServerConfig
from test_audio import STEPS, INDEX, decode as reference_decode
from test_fetch import adapter_mod, png_bytes  # noqa: F401 - fixture


# --- BlobStore --------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs")


def test_upload_in_pieces_then_readable(store):
    data = bytes(range(256)) * 300
    assert store.begin_upload("uabc", len(data), "audio/x-ima-adpcm", "voice", "dev1") == 0
    assert store.meta("uabc") is None  # partial uploads are not blobs yet
    assert store.write_upload("uabc", 0, data[:30000], "dev1") == (30000, False)
    assert store.begin_upload("uabc", len(data), "audio/x-ima-adpcm", "voice", "dev1") == 30000  # resume
    assert store.write_upload("uabc", 30000, data[30000:], "dev1") == (len(data), True)
    assert store.meta("uabc")["role"] == "upload" and "partial" not in store.meta("uabc")
    assert b"".join(d for _, d in store.open_chunks("uabc")) == data
    assert store.upload_path("uabc", "dev1").read_bytes() == data
    assert store.upload_path("uabc", "dev2") is None
    assert store.begin_upload("uabc", len(data), "", "", "dev1") == len(data)  # already complete


@pytest.mark.parametrize("bid,size,code", [("b123", 10, "bad_request"), ("u", 10, "bad_request"),
                                           ("uABC", 10, "bad_request"), ("u../x", 10, "bad_request"),
                                           ("u" + "a" * 32, 10, "bad_request"), ("uok", 0, "bad_request"),
                                           ("uok", UPLOAD_MAX + 1, "too_large")])
def test_upload_rejects_bad_requests(store, bid, size, code):
    with pytest.raises(BlobError) as e:
        store.begin_upload(bid, size, "image/jpeg", "", "dev1")
    assert str(e.value) == code


def test_upload_ids_belong_to_their_device(store):
    store.begin_upload("uone", 10, "image/jpeg", "", "dev1")
    with pytest.raises(BlobError) as e:
        store.begin_upload("uone", 10, "image/jpeg", "", "dev2")
    assert str(e.value) == "denied"
    with pytest.raises(BlobError) as e:
        store.begin_upload("uone", 11, "image/jpeg", "", "dev1")  # same id, other content
    assert str(e.value) == "denied"
    with pytest.raises(BlobError) as e:
        store.write_upload("uone", 0, b"x", "dev2")
    assert str(e.value) == "not_found"


@pytest.mark.parametrize("offset,data", [(5, b"abc"), (0, b"x" * 11)])
def test_upload_piece_must_fit(store, offset, data):
    store.begin_upload("upiece", 10, "image/jpeg", "", "dev1")
    with pytest.raises(BlobError) as e:
        store.write_upload("upiece", offset, data, "dev1")
    assert str(e.value) == "bad_request"


def test_stale_partial_upload_restarts_and_is_cleaned_up(store):
    store.begin_upload("uold", 10, "image/jpeg", "", "dev1")
    store.write_upload("uold", 0, b"12345", "dev1")
    meta = store._raw_meta("uold")
    meta["created"] = time.time() - UPLOAD_TTL - 5
    store._write_meta("uold", meta)
    assert store.begin_upload("uold", 12, "image/jpeg", "", "dev1") == 0  # expired: starts over
    meta = store._raw_meta("uold")
    meta["created"] = time.time() - UPLOAD_TTL - 5
    store._write_meta("uold", meta)
    assert store.cleanup() == 1
    assert not (store.root / "uold.json").exists() and not (store.root / "uold.part").exists()


# --- server --------------------------------------------------------------------------------

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
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0), _Backend(), KeyPair(),
                          DeviceStore(tmp_path / "devices.json"), TokenStore(tmp_path / "tokens.json"),
                          blobs=BlobStore(tmp_path / "blobs"))
    await srv.start()
    yield srv
    await srv.stop()


async def _paired(srv):
    key = KeyPair()
    c = await Client.connect("127.0.0.1", srv.port, static=key, server_key=srv.static.public,
                             pair_token=srv.tokens.create())
    return c, key


async def test_upload_over_the_wire_resumes_after_reconnect(server):
    c, key = await _paired(server)
    assert c.welcome["upload"] == {"max": UPLOAD_MAX}
    data = bytes((i * 7) & 255 for i in range(100_000))
    assert await c.put_blob("uvoice1", data, "audio/x-ima-adpcm", stop_at=40_000, chunk=10_000) == 40_000
    await asyncio.sleep(0.2)  # let the server store the pieces before the connection goes
    await c.close()
    c = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    assert await c.put_blob("uvoice1", data, "audio/x-ima-adpcm") == len(data)
    dev = server.devices.get(key.public)
    assert server.upload_path(dev, "uvoice1").read_bytes() == data
    # Asking again for a finished upload: offset at the end, and the done event once more.
    assert await c.put_blob("uvoice1", data, "audio/x-ima-adpcm") == len(data)
    await c.close()


async def test_upload_errors(server):
    c, _ = await _paired(server)
    with pytest.raises(ServerError) as e:
        await c.request("blob.put", blob="uhuge", size=UPLOAD_MAX + 1, mime="image/jpeg")
    assert e.value.code == "too_large"
    with pytest.raises(ServerError) as e:
        await c.request("blob.put", blob="b0", size=5, mime="image/jpeg")
    assert e.value.code == "bad_request"
    # A piece at the wrong place gets an error naming the blob; the connection stays up.
    from h3proto.framing import encode_blob_chunk
    await c.request("blob.put", blob="ugap", size=10, mime="image/jpeg")
    await c.channel.send_body(encode_blob_chunk("ugap", 4, b"abc"))
    ev = await c.next_event("error")
    assert ev["blob"] == "ugap" and ev["code"] == "bad_request"
    await c.channel.send_body(encode_blob_chunk("unever", 0, b"abc"))
    ev = await c.next_event("error")
    assert ev["blob"] == "unever" and ev["code"] == "not_found"
    assert await c.put_blob("ugap", b"0123456789", "image/jpeg") == 10
    await c.close()


# --- ADPCM decoding (a 3DS voice message) ------------------------------------------------------------

def _encode(samples, block=1024):
    """A plain IMA-ADPCM encoder (as app/source/adpcm.c does it) for test input."""
    out, spb, i = bytearray(), (block - 4) * 2 + 1, 0
    idx = 0
    while i < len(samples):
        chunk = samples[i:i + spb]
        pred = chunk[0]
        blk = bytearray(struct.pack("<hBB", pred, idx, 0))
        nibs = []
        for s in chunk[1:]:
            step = STEPS[idx]
            diff = s - pred
            nib = 0
            if diff < 0:
                nib, diff = 8, -diff
            if diff >= step:
                nib |= 4
                diff -= step
            if diff >= step >> 1:
                nib |= 2
                diff -= step >> 1
            if diff >= step >> 2:
                nib |= 1
            d = step >> 3
            if nib & 1: d += step >> 2
            if nib & 2: d += step >> 1
            if nib & 4: d += step
            pred = max(-32768, min(32767, pred - d if nib & 8 else pred + d))
            idx = max(0, min(88, idx + INDEX[nib & 7]))
            nibs.append(nib)
        if len(nibs) % 2:
            nibs.append(0)
        blk += bytes(nibs[k] | nibs[k + 1] << 4 for k in range(0, len(nibs), 2))
        out += blk
        i += spb
    return bytes(out)


def test_decode_matches_the_reference_decoder():
    pcm = [int(9000 * math.sin(i / 7.0) + 3000 * math.sin(i / 1.3)) for i in range(5000)]
    data = _encode(pcm)
    got = audio.decode_adpcm(data, 1024)
    assert list(struct.unpack(f"<{len(got) // 2}h", got)) == reference_decode(data, 1024)
    assert len(audio.decode_adpcm(data, 1024, samples=4321)) == 4321 * 2
    whole = data[:2048]
    assert audio.decode_adpcm(whole + b"\x01\x02", 1024) == audio.decode_adpcm(whole, 1024)  # torn header dropped


def test_voice_to_wav_is_a_16360_hz_pcm_wav():
    pcm = [int(12000 * math.sin(2 * math.pi * 440 * i / 16360)) for i in range(16360)]
    wav = audio.voice_to_wav(_encode(pcm), 16360, 1024, ms=1000)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16360, 16360)
        frames = struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes()))
    assert max(frames) > 10000 and min(frames) < -10000
    with pytest.raises(ValueError):
        audio.voice_to_wav(b"\0" * 1024, 100, 1024)
    with pytest.raises(ValueError):
        audio.decode_adpcm(b"\0\0\x99\0" + b"\0" * 20, 24)  # step index out of range


# --- adapter: voice and photo sends --------------------------------------------------------------

class _Event:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _UploadServer:
    def __init__(self, blobs):
        self.blobs, self.events, self.posted = blobs, [], []
        self.caps = set()  # what the connected app's hello advertised

    def has_cap(self, key, cap, *, offline=False):
        return cap in self.caps

    def upload_path(self, device, bid):
        return self.blobs.upload_path(bid, device.fp)

    async def event(self, key, msg):
        self.events.append(msg)

    def open_blank_messages(self, key, agent=""):
        return []

    async def post_message(self, key, mid, text, **kw):
        self.posted.append({"mid": mid, "text": text, **kw})


@pytest.fixture
def ad(adapter_mod, tmp_path, monkeypatch):  # noqa: F811
    base = sys.modules["gateway.platforms.base"]
    cache = tmp_path / "cache"
    cache.mkdir()

    async def cache_bytes(data, ext):
        if ext != ".wav" and not data.startswith((b"\xff\xd8", b"\x89PNG")):
            raise ValueError("not an image")
        p = cache / f"c{len(list(cache.iterdir()))}{ext}"
        p.write_bytes(data)
        return str(p)

    monkeypatch.setattr(base, "cache_audio_from_bytes_async", cache_bytes, raising=False)
    monkeypatch.setattr(base, "cache_image_from_bytes_async", cache_bytes, raising=False)
    monkeypatch.setattr(adapter_mod, "MessageEvent", _Event)
    monkeypatch.setattr(adapter_mod, "MessageType", types.SimpleNamespace(TEXT="text", VOICE="voice", PHOTO="photo"))
    a = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
    a._blobs = BlobStore(tmp_path / "blobs")
    a._server = _UploadServer(a._blobs)
    a._chat_keys = {"dev:fp1": b"k" * 32}
    a._ids = iter(range(1, 100))
    a._queued = {}
    a._trace_path = None
    a._awaiting_heard = {}
    a.handled = []

    async def handle_message(event):
        a.handled.append(event)

    a.handle_message = handle_message
    a.source_for = lambda device, agent=None: "src"
    return a


def _device():
    return types.SimpleNamespace(fp="fp1", key=b"k" * 32, name="3DS")


def _upload(store, bid, data, mime):
    store.begin_upload(bid, len(data), mime, "", "fp1")
    store.write_upload(bid, 0, data, "fp1")


def test_voice_send_becomes_a_voice_event_and_its_echo_a_heard_event(ad, adapter_mod):
    pcm = [int(8000 * math.sin(i / 5)) for i in range(8180)]
    _upload(ad._blobs, "uv1", _encode(pcm), "audio/x-ima-adpcm")
    backend = adapter_mod._Backend(ad)
    reply = asyncio.run(backend.handle(_device(), {"t": "send", "id": 41, "text": "",
                                                    "voice": {"blob": "uv1", "rate": 16360, "block": 1024,
                                                              "ms": 500}}))
    assert reply["t"] == "send_r"
    (ev,) = ad.handled
    assert ev.message_type == "voice" and ev.media_types == ["audio/wav"]
    with wave.open(ev.media_urls[0]) as w:
        assert w.getframerate() == 16360 and w.getnframes() == 8180
    # Hermes echoes the transcript through send(): it becomes `heard` for request 41, not a chat bubble.
    res = asyncio.run(ad.send("dev:fp1", '\U0001f399️ "turn on the lights"'))
    assert res.success
    assert ad._server.events == [{"t": "heard", "req": 41, "text": "turn on the lights"}]
    assert ad._server.posted == []
    # With nothing waiting, the same text is an ordinary message.
    asyncio.run(ad.send("dev:fp1", '\U0001f399️ "again"'))
    assert len(ad._server.posted) == 1


def test_voice_send_needs_a_finished_upload(ad, adapter_mod):
    backend = adapter_mod._Backend(ad)
    ad._blobs.begin_upload("uv2", 100, "audio/x-ima-adpcm", "", "fp1")
    with pytest.raises(adapter_mod.RequestError) as e:
        asyncio.run(backend.handle(_device(), {"t": "send", "id": 1, "voice": {"blob": "uv2", "rate": 16360,
                                                                               "block": 1024}}))
    assert e.value.code == "not_found"
    _upload(ad._blobs, "uv3", b"\0" * 1024, "audio/x-ima-adpcm")
    with pytest.raises(adapter_mod.RequestError) as e:
        asyncio.run(backend.handle(_device(), {"t": "send", "id": 2, "voice": {"blob": "uv3", "rate": 1,
                                                                               "block": 1024}}))
    assert e.value.code == "bad_request"


def test_photo_send_becomes_a_photo_event(ad, adapter_mod):
    import PIL.Image
    buf = io.BytesIO()
    PIL.Image.new("RGB", (640, 480), (200, 30, 30)).save(buf, "JPEG")
    _upload(ad._blobs, "up1", buf.getvalue(), "image/jpeg")
    _upload(ad._blobs, "up2", png_bytes((40, 30)), "image/png")
    backend = adapter_mod._Backend(ad)
    asyncio.run(backend.handle(_device(), {"t": "send", "id": 5, "text": "what is this?",
                                           "attach": [{"blob": "up1"}, {"blob": "up2"}]}))
    (ev,) = ad.handled
    assert ev.message_type == "photo" and ev.text == "what is this?"
    assert ev.media_types == ["image/jpeg", "image/png"]
    assert [open(u, "rb").read()[:2] for u in ev.media_urls] == [b"\xff\xd8", b"\x89P"]
    _upload(ad._blobs, "ubad", b"not a picture", "image/jpeg")
    with pytest.raises(adapter_mod.RequestError):
        asyncio.run(backend.handle(_device(), {"t": "send", "id": 6, "attach": [{"blob": "ubad"}]}))
    for bad in ([], [{"blob": "up1"}] * 5, "up1"):
        with pytest.raises(adapter_mod.RequestError):
            asyncio.run(backend.handle(_device(), {"t": "send", "id": 7, "attach": bad}))


def test_welcome_says_whether_hermes_speaks_in_this_chat(ad):
    ad.register_device = lambda device: "dev:fp1"
    ad._auto_tts_enabled_chats = set()
    ad._should_auto_tts_for_chat = lambda chat_id: chat_id in ad._auto_tts_enabled_chats
    backend = sys.modules[type(ad).__module__]._Backend(ad)
    assert asyncio.run(backend.welcome_state(_device()))["voice"] is False
    ad._auto_tts_enabled_chats.add("dev:fp1")
    assert asyncio.run(backend.welcome_state(_device()))["voice"] is True


def test_queued_counts_messages_sent_during_a_turn(ad, adapter_mod):
    backend = adapter_mod._Backend(ad)
    busy = [False]
    backend._busy = lambda device, agent=None: busy[0]
    send = {"t": "send", "id": 1, "text": "hi"}
    asyncio.run(backend.handle(_device(), send))
    assert ad._queued == {"dev:fp1": 0}
    busy[0] = True
    asyncio.run(backend.handle(_device(), send))
    asyncio.run(backend.handle(_device(), send))
    assert ad._queued["dev:fp1"] == 2
    started = types.SimpleNamespace(source=types.SimpleNamespace(chat_id="dev:fp1"))
    asyncio.run(ad.on_processing_start(started))
    assert ad._queued["dev:fp1"] == 1
    # Hermes merged the rest into that turn: the next message after it finishes starts the count over.
    busy[0] = False
    asyncio.run(backend.handle(_device(), send))
    assert ad._queued["dev:fp1"] == 0
    asyncio.run(ad.on_processing_start(started))
    assert ad._queued["dev:fp1"] == 0
