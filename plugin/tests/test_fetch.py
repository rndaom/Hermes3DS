"""URL downloads (h3proto/fetch.py) and the adapter's send_image path, offline via httpx.MockTransport."""

import asyncio
import dataclasses
import gzip
import importlib
import io
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest

httpx = pytest.importorskip("httpx")
from PIL import Image

from h3proto import fetch
from h3proto.blobs import BlobStore
from h3proto.fetch import DownloadError


def png_bytes(size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def get(url, handler, **kw):
    return asyncio.run(fetch.download(url, transport=httpx.MockTransport(handler), **kw))


# --- fetch.download ------------------------------------------------------------------

def test_success_returns_body_mime_and_path():
    body = png_bytes()
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, content=body, headers={"content-type": "image/PNG; charset=binary"})

    data, mime, path = get("https://example.com/pics/a.png?x=1", handler)
    assert data == body and mime == "image/png" and path == "/pics/a.png"
    assert seen["ua"] == fetch.USER_AGENT


def test_size_cap_from_content_length():
    def handler(request):
        return httpx.Response(200, content=b"x" * 2000)  # Content-Length: 2000

    with pytest.raises(DownloadError, match="too large"):
        get("https://example.com/big", handler, max_bytes=1000)


def test_size_cap_while_streaming_without_length():
    async def chunks():
        for _ in range(10):
            yield b"y" * 500

    def handler(request):
        return httpx.Response(200, content=chunks())  # streamed: no Content-Length

    with pytest.raises(DownloadError, match="too large"):
        get("https://example.com/stream", handler, max_bytes=1000)
    data, _, _ = get("https://example.com/stream", handler, max_bytes=5000)
    assert len(data) == 5000


def test_size_cap_applies_after_decompression():
    packed = gzip.compress(b"z" * 50000)

    def handler(request):
        return httpx.Response(200, content=packed, headers={"content-encoding": "gzip"})

    assert len(packed) < 1000
    with pytest.raises(DownloadError, match="too large"):
        get("https://example.com/bomb", handler, max_bytes=10000)


@pytest.mark.parametrize("url", ["ftp://example.com/a.png", "file:///etc/passwd", "javascript:alert(1)", "",
                                 "/local/path.png", "data:image/png;base64,AAAA"])
def test_non_http_scheme_refused_without_a_request(url):
    def handler(request):
        raise AssertionError("no request should be made")

    with pytest.raises(DownloadError, match="not an http"):
        get(url, handler)


def test_redirect_followed_and_final_path_reported():
    body = png_bytes()

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://cdn.example.net/img/final.png"})
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    data, mime, path = get("https://example.com/start", handler)
    assert data == body and path == "/img/final.png"


def test_redirect_target_is_safety_checked():
    checked = []

    async def is_safe(url):
        checked.append(url)
        return "127.0.0.1" not in url

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/secret"})
        raise AssertionError("the private host must not be contacted")

    with pytest.raises(DownloadError, match="blocked"):
        get("https://example.com/start", handler, is_safe=is_safe)
    assert checked == ["https://example.com/start", "http://127.0.0.1:8080/secret"]


def test_unsafe_first_url_blocked():
    async def is_safe(url):
        return False

    def handler(request):
        raise AssertionError("no request should be made")

    with pytest.raises(DownloadError, match="blocked"):
        get("http://169.254.169.254/latest/meta-data", handler, is_safe=is_safe)


def test_redirect_to_non_http_scheme_refused():
    def handler(request):
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(DownloadError, match="non-http"):
        get("https://example.com/start", handler)


def test_redirect_loop_limited():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"location": f"/r{len(calls)}"})

    with pytest.raises(DownloadError, match="too many redirects"):
        get("https://example.com/r0", handler)
    assert len(calls) == fetch.MAX_REDIRECTS + 1


def test_http_error_status():
    def handler(request):
        return httpx.Response(404, content=b"not here")

    with pytest.raises(DownloadError, match="HTTP 404"):
        get("https://example.com/missing", handler)


def test_connection_failure():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(DownloadError, match="ConnectError: connection refused"):
        get("https://example.com/a.png", handler)


def test_total_timeout():
    async def slow():
        yield b"a"
        await asyncio.sleep(5)
        yield b"b"

    def handler(request):
        return httpx.Response(200, content=slow())

    with pytest.raises(DownloadError, match="timed out"):
        get("https://example.com/slow", handler, total=0.2)


def test_bad_content_length_header_ignored():
    def handler(request):
        r = httpx.Response(200, content=b"abc")
        r.headers["content-length"] = "nonsense"
        return r

    data, _, _ = get("https://example.com/x", handler)
    assert data == b"abc"


# --- the adapter's send_image / send_multiple_images ------------------------------------

@dataclasses.dataclass
class _SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
    error_kind: Optional[str] = None


class _Base:
    def __init__(self, config=None, platform=None):
        self.config, self.platform = config, platform

    @staticmethod
    def strip_media_directives_for_display(text):
        return text

    async def on_processing_start(self, event):
        pass


class _FakeServer:
    def __init__(self):
        self.posted = []

    def open_blank_messages(self, key, agent=""):
        return []

    async def post_message(self, key, mid, text, **kw):
        self.posted.append({"mid": mid, "text": text, **kw})


@pytest.fixture
def adapter_mod(monkeypatch):
    """plugin.adapter imported against minimal stand-ins for Hermes' gateway modules."""
    mods = {
        "gateway": types.ModuleType("gateway"),
        "gateway.config": types.ModuleType("gateway.config"),
        "gateway.platforms": types.ModuleType("gateway.platforms"),
        "gateway.platforms.base": types.ModuleType("gateway.platforms.base"),
        "gateway.platforms.event": types.ModuleType("gateway.platforms.event"),
    }
    mods["gateway.config"].Platform = lambda name: name
    mods["gateway.platforms.base"].BasePlatformAdapter = _Base
    mods["gateway.platforms.base"].SendResult = _SendResult
    mods["gateway.platforms.event"].MessageEvent = object
    mods["gateway.platforms.event"].MessageType = types.SimpleNamespace(TEXT="text")
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    for name in [n for n in sys.modules if n == "plugin" or n.startswith("plugin.")]:
        monkeypatch.delitem(sys.modules, name)
    mod = importlib.import_module("plugin.adapter")
    yield mod
    for name in [n for n in sys.modules if n == "plugin" or n.startswith("plugin.")]:
        del sys.modules[name]


def make_adapter(mod, tmp_path, monkeypatch, handler):
    real = mod.fetch.download

    async def download(url, **kw):
        return await real(url, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(mod.fetch, "download", download)
    ad = mod.ThreeDSAdapter.__new__(mod.ThreeDSAdapter)
    ad._server = _FakeServer()
    ad._blobs = mod.BlobStore(tmp_path / "blobs")
    ad._chat_keys = {"dev:1": b"k" * 32}
    ad._ids = iter(range(1, 100))
    ad._trace_path = None
    return ad


def test_send_image_downloads_and_posts_picture(adapter_mod, tmp_path, monkeypatch):
    body = png_bytes((300, 200))

    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    ad = make_adapter(adapter_mod, tmp_path, monkeypatch, handler)
    res = asyncio.run(ad.send_image("dev:1", "https://example.com/render", caption="**Done**"))
    assert res.success and res.message_id
    (msg,) = ad._server.posted
    (att,) = msg["attach"]
    assert att["kind"] == "image" and att["name"] == "render.png" and att["w"] == 300 and att["h"] == 200
    assert msg["text"] == "Done"


def test_send_image_failures_become_send_results(adapter_mod, tmp_path, monkeypatch):
    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * 100)
        if request.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        return httpx.Response(500)

    ad = make_adapter(adapter_mod, tmp_path, monkeypatch, handler)
    monkeypatch.setattr(adapter_mod, "DOWNLOAD_MAX", 50)
    for url, why in [("https://example.com/big", "too large"), ("https://example.com/loop", "too many redirects"),
                     ("https://example.com/err", "HTTP 500"), ("ftp://example.com/a.png", "not an http")]:
        res = asyncio.run(ad.send_image("dev:1", url))
        assert not res.success and why in res.error, (url, res.error)
    assert ad._server.posted == []


def test_send_image_not_a_picture_arrives_as_file(adapter_mod, tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"<html>nope</html>", headers={"content-type": "text/html"})

    ad = make_adapter(adapter_mod, tmp_path, monkeypatch, handler)
    res = asyncio.run(ad.send_image("dev:1", "https://example.com/page"))
    assert res.success
    (att,) = ad._server.posted[0]["attach"]
    assert att["kind"] == "file" and "view" not in att


def test_send_multiple_images_skips_failures(adapter_mod, tmp_path, monkeypatch):
    body = png_bytes()

    def handler(request):
        if request.url.path.startswith("/ok"):
            return httpx.Response(200, content=body, headers={"content-type": "image/png"})
        return httpx.Response(404)

    ad = make_adapter(adapter_mod, tmp_path, monkeypatch, handler)
    res = asyncio.run(ad.send_multiple_images("dev:1", [("https://example.com/ok1.png", "first"),
                                                        ("https://example.com/gone.png", "lost"),
                                                        ("https://example.com/ok2.png", "")]))
    assert res.success
    (msg,) = ad._server.posted
    assert [a["name"] for a in msg["attach"]] == ["ok1.png", "ok2.png"] and msg["text"] == "first"
    res = asyncio.run(ad.send_multiple_images("dev:1", [("https://example.com/gone.png", "")]))
    assert not res.success
