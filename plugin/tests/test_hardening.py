"""Regression tests for the 2026-10 release audit: revocation, frame-size limits, replay, slow regex, compat."""

import asyncio
import importlib
import itertools
import sys
import time
import types

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


def test_session_visibility_is_this_chats_lane(adapter_mod):  # noqa: F811
    compat = importlib.import_module("plugin.compat")
    assert compat._visible({"session_key": "agent:3ds:dm:x"}, "agent:3ds:dm:x") is True
    assert compat._visible({"session_key": "agent:discord:dm:y"}, "agent:3ds:dm:x") is False
    assert compat._visible(None, "agent:3ds:dm:x") is False


async def test_open_on_pc_refusals(adapter_mod, tmp_path, monkeypatch):  # noqa: F811
    from h3proto.blobs import BlobStore
    RequestError = adapter_mod.RequestError

    a = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
    a._blobs = BlobStore(tmp_path / "blobs")
    for name in ("run.sh", "setup.deb", "payload", "app.pif", "macro.doc", "page.html", "disk.iso"):
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


async def test_reauthorize_native_roles_follows_pairing(adapter_mod, tmp_path):  # noqa: F811
    store = DeviceStore(tmp_path / "devices.json")
    dev = store.add(KeyPair().public, "3DS")
    a = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
    a._server = types.SimpleNamespace(devices=store)

    def source(**kw):
        fields = {"chat_type": "dm", "user_id": dev.fp, "chat_id": adapter_mod.chat_id_for(dev)}
        return types.SimpleNamespace(**{**fields, **kw})

    assert await a.reauthorize_native_roles(source()) is True
    assert await a.reauthorize_native_roles(source(user_id="someone-else")) is False
    assert await a.reauthorize_native_roles(source(chat_id="dev:other")) is False
    assert await a.reauthorize_native_roles(source(chat_type="group")) is False
    store.remove(dev.fp)
    assert await a.reauthorize_native_roles(source()) is False
    a._server = None
    assert await a.reauthorize_native_roles(source()) is False


async def test_message_ids_do_not_repeat_after_a_gateway_restart(adapter_mod, monkeypatch):  # noqa: F811
    # Hermes refuses a reused inbound id with other content (admission_conflict), so a restarted gateway
    # must not hand out u1, u2... again.
    def run(tag):
        monkeypatch.setattr(adapter_mod, "_RUN_TAG", tag)
        a = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
        a._ids = itertools.count(1)
        return [a._mid("u"), a._mid("m")]

    first, second = run("a1b2c3d4e5"), run("f6a7b8c9d0")
    assert first == ["ua1b2c3d4e5-1", "ma1b2c3d4e5-2"] and not set(first) & set(second)
    assert len(adapter_mod._RUN_TAG) == 10
    # Each adapter Hermes makes in the same process (reconnect watcher, profiles) gets a tag of its own.
    a, b = (adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter) for _ in range(2))
    for ad, tag in ((a, "0123456789"), (b, "9876543210")):
        ad._ids, ad._run_tag = itertools.count(1), tag
    assert a._mid("u") != b._mid("u")
    assert len(f"s{'f' * 10}-{10 ** 9}") <= 23  # speech ids live in char[24] on the 3DS (mids in char[32])


async def test_what_went_into_a_dead_connection_is_replayed(server):
    # A 3DS that sleeps or loses Wi-Fi leaves a socket that still takes writes until the idle timeout, so
    # whatever changed after it was last heard from must go out again on reconnect (the app dedupes by mid).
    c, key = await _pair(server)
    await server.post_message(key.public, "m1", "seen")
    await c.next_event("msg")
    await c.channel.send({"t": "pong"})  # heard from after m1
    await asyncio.sleep(0.05)
    await server.post_message(key.public, "m2", "maybe lost")  # written, but the app goes quiet
    await server.post_message(key.public, "m3", "gone", final=False)
    await server.delete_message(key.public, "m3")
    await c.close()
    await asyncio.sleep(0.05)
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    assert (await c2.next_event("delete"))["mid"] == "m3"
    assert (await c2.next_event("msg"))["mid"] == "m2"
    await asyncio.sleep(0.1)
    assert all(ev.get("mid") != "m1" for ev in list(c2.events._queue))
    await c2.close()
