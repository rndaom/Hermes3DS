"""Regression tests for the 2026-10 release audit: revocation, frame-size limits, replay, slow regex, compat."""

import asyncio
import importlib
import sys
import time

import pytest

from h3proto.client import Client, ServerError
from h3proto.framing import MAX_BODY_DATA, encode_json, fit_reply
from h3proto.noise import KeyPair
from h3proto.pairing import DeviceStore, TokenStore
from h3proto.richtext import markdown_to_rich
from h3proto.server import Hermes3DSServer, ServerConfig
from test_fetch import adapter_mod  # noqa: F401 - fixture


class Backend:
    def __init__(self):
        self.server = None

    async def welcome_state(self, device):
        return {}

    async def handle(self, device, msg):
        if msg["t"] == "huge":
            return {"t": "huge_r", "blob": "x" * (MAX_BODY_DATA + 10)}
        if msg["t"] == "sessions.messages":
            return {"t": "sessions.messages_r", "items": [{"mid": i, "text": "y" * 8000} for i in range(20)]}
        return None

    async def on_connect(self, device):
        pass

    async def on_disconnect(self, device):
        pass


@pytest.fixture
async def server(tmp_path):
    backend = Backend()
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0, name="TEST-PC"),
                          backend, KeyPair(), DeviceStore(tmp_path / "devices.json"),
                          TokenStore(tmp_path / "tokens.json"))
    backend.server = srv
    await srv.start()
    yield srv
    await srv.stop()


async def _pair(srv):
    key = KeyPair()
    c = await Client.connect("127.0.0.1", srv.port, device="Test 3DS", static=key,
                             server_key=srv.static.public, pair_token=srv.tokens.create())
    return c, key


async def test_unpair_from_another_process_revokes_at_once(server, tmp_path):
    c, key = await _pair(server)
    # `hermes 3ds unpair` runs in its own process with its own DeviceStore on the same file.
    assert DeviceStore(tmp_path / "devices.json").remove("Test 3DS")
    with pytest.raises((ConnectionError, ServerError, asyncio.TimeoutError, asyncio.IncompleteReadError)):
        await c.request("ping", timeout=3)
        await c.request("ping", timeout=3)
    await c.close()
    with pytest.raises(Exception):
        await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    # A later save by the gateway (another device connecting) must not write the removed device back.
    c2, _ = await _pair(server)
    await c2.close()
    assert [d.name for d in DeviceStore(tmp_path / "devices.json").all()] == ["Test 3DS"]
    assert all(d.key != key.public for d in server.devices.all())


async def test_oversized_reply_answers_with_an_error_and_keeps_the_connection(server):
    c, _ = await _pair(server)
    with pytest.raises(ServerError) as err:
        await c.request("huge", timeout=5)
    assert err.value.code == "too_large"
    page = await c.request("sessions.messages", timeout=5)  # 20 x 8000 chars: trimmed, newest kept
    assert page["cut"] > 0 and page["items"][-1]["mid"] == 19
    await c.close()


def test_fit_reply():
    small = {"t": "skills.list_r", "items": [1, 2, 3]}
    assert fit_reply(small) is small
    big = {"t": "skills.list_r", "items": [{"n": i, "d": "z" * 1000} for i in range(100)]}
    fitted = fit_reply(big)
    assert fitted["items"][0]["n"] == 0 and len(encode_json(fitted)) <= MAX_BODY_DATA + 1
    hist = fit_reply({"t": "sessions.messages_r", "items": [{"mid": i, "x": "z" * 1000} for i in range(100)]})
    assert hist["items"][-1]["mid"] == 99 and len(encode_json(hist)) <= MAX_BODY_DATA + 1


@pytest.mark.parametrize("text", ["[" * 65536, "[a](" * 16000])
def test_link_pattern_is_not_quadratic(text):
    t = time.monotonic()
    markdown_to_rich(text)
    assert time.monotonic() - t < 3


def test_links_still_parse():
    text, spans = markdown_to_rich("see [the docs](https://example.com/a) now")
    assert text == "see the docs now" and spans == [[4, 12, "link", "https://example.com/a"]]


async def test_session_visibility_awaits_hermes_async_check(adapter_mod):  # noqa: F811
    compat = importlib.import_module("plugin.compat")

    class Runner:
        async def _resume_row_visible(self, source, row, allow_all):
            return row["source"] == "3ds"

    assert await compat._visible(Runner(), None, {"source": "3ds"}) is True
    assert await compat._visible(Runner(), None, {"source": "discord"}) is False


async def test_open_on_pc_refusals(adapter_mod, tmp_path, monkeypatch):  # noqa: F811
    from h3proto.blobs import BlobStore
    RequestError = adapter_mod.RequestError

    a = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
    a._blobs = BlobStore(tmp_path / "blobs")
    for name in ("run.sh", "setup.deb", "payload"):
        src = tmp_path / name
        src.write_bytes(b"#!/bin/sh\n")
        ref = a._blobs.put_file(src, name=name)
        with pytest.raises(RequestError) as err:
            await a.open_blob(ref["id"])
        assert err.value.code == "denied"
    if sys.platform not in ("win32", "darwin"):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        doc = tmp_path / "notes.txt"
        doc.write_text("hi")
        ref = a._blobs.put_file(doc, name="notes.txt")
        with pytest.raises(RequestError) as err:
            await a.open_blob(ref["id"])
        assert err.value.code == "unavailable"
