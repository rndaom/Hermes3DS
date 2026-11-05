"""End-to-end tests of the server core with a stub backend (no Hermes needed)."""

import asyncio

import pytest

from h3proto.client import Client, ServerError, discover
from h3proto.emoji import CAP, emoji_for_3ds
from h3proto.noise import KeyPair
from h3proto.pairing import DeviceStore, TokenStore, code_from_token, token_from_code, \
    pairing_uri, parse_pairing_uri
from h3proto.server import Hermes3DSServer, RequestError, ServerConfig



class StubBackend:
    def __init__(self):
        self.server: Hermes3DSServer | None = None
        self.connected = []

    async def welcome_state(self, device):
        return {"session": {"id": "s1"}}

    async def handle(self, device, msg):
        if msg["t"] == "send":
            await self.server.post_message(device.key, "m1", "echo: ", final=False)
            await self.server.update_message(device.key, "m1", "echo: " + msg["text"], final=True)
            return {"t": "send_r", "mid": "u1"}
        if msg["t"] == "boom":
            raise RequestError("not_found", "no boom here")
        if msg["t"] == "crash":
            raise RuntimeError("bug")
        return None

    async def on_connect(self, device):
        self.connected.append(device.name)

    async def on_disconnect(self, device):
        pass


@pytest.fixture
async def server(tmp_path):
    backend = StubBackend()
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0, name="TEST-PC"),
                          backend, KeyPair(), DeviceStore(tmp_path / "devices.json"),
                          TokenStore(tmp_path / "tokens.json"))
    backend.server = srv
    await srv.start()
    yield srv
    await srv.stop()


async def _pair(srv, key=None):
    key = key or KeyPair()
    token = srv.tokens.create()
    c = await Client.connect("127.0.0.1", srv.port, device="Test 3DS", static=key,
                             server_key=srv.static.public, pair_token=token)
    return c, key


async def test_pair_then_reconnect(server):
    c, key = await _pair(server)
    assert c.welcome["paired"] is True
    assert c.welcome["chat"] == {"session": {"id": "s1"}}
    await c.close()
    assert [d.name for d in server.devices.all()] == ["Test 3DS"]
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    assert "paired" not in c2.welcome
    await c2.close()


async def test_unpaired_and_reused_token_rejected(server):
    with pytest.raises(ServerError) as e:
        await Client.connect("127.0.0.1", server.port, static=KeyPair(), server_key=server.static.public)
    assert e.value.code == "not_paired"
    token = server.tokens.create()
    c = await Client.connect("127.0.0.1", server.port, static=KeyPair(), server_key=server.static.public,
                             pair_token=token)
    await c.close()
    with pytest.raises(ServerError):
        await Client.connect("127.0.0.1", server.port, static=KeyPair(), server_key=server.static.public,
                             pair_token=token)


async def test_wrong_server_key_fails(server):
    with pytest.raises(Exception):
        await Client.connect("127.0.0.1", server.port, static=KeyPair(), server_key=KeyPair().public,
                             timeout=3)


async def test_requests_streaming_and_errors(server):
    c, _ = await _pair(server)
    r = await c.request("send", text="hi")
    assert r["t"] == "send_r" and r["mid"] == "u1"
    ev = await c.next_event("msg")
    assert ev["mid"] == "m1" and ev["text"] == "echo: " and ev["final"] is False
    ev = await c.next_event("append", "edit")
    assert ev == {"t": "append", "mid": "m1", "text": "hi", "final": True}
    with pytest.raises(ServerError) as e:
        await c.request("boom")
    assert e.value.code == "not_found"
    with pytest.raises(ServerError) as e:
        await c.request("crash")
    assert e.value.code == "internal"
    await c.channel.send({"t": "ping", "n": 5})
    assert await c.next_event("pong") == {"t": "pong", "n": 5}
    await c.close()


async def test_offline_outbox_and_pending_replay(server):
    c, key = await _pair(server)
    await c.close()
    await asyncio.sleep(0.05)
    dev_key = key.public
    await server.post_message(dev_key, "m9", "partial", final=False)
    await server.update_message(dev_key, "m9", "partial and done", final=True)
    await server.set_pending(dev_key, "a1", {"t": "approval", "req": "a1", "text": "ok?"})
    await server.set_typing(dev_key, True)
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    ev = await c2.next_event("msg")
    assert ev["mid"] == "m9" and ev["text"] == "partial and done" and ev["final"] is True
    assert (await c2.next_event("approval"))["req"] == "a1"
    assert (await c2.next_event("typing"))["on"] is True
    await server.clear_pending(dev_key, "a1", {"t": "approval_done", "req": "a1", "choice": "deny"})
    assert (await c2.next_event("approval_done"))["choice"] == "deny"
    await c2.close()


async def test_emoji_sprites_only_for_matching_app(server):
    c, key = await _pair(server)
    await c.close()
    await asyncio.sleep(0.05)
    rocket = emoji_for_3ds("🚀")
    server.backend.welcome_state = lambda device: asyncio.sleep(0, {"title": rocket})
    await server.post_message(key.public, "m1", f"{rocket} go", spans=[[5, 7, "b"]])
    old = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    assert old.welcome["chat"] == {"title": ":rocket:"}
    ev = await old.next_event("msg")
    assert ev["text"] == ":rocket: go" and ev["spans"] == [[9, 11, "b"]]
    await server.event(key.public, {"t": "tool", "state": "start", "text": rocket})
    assert (await old.next_event("tool"))["text"] == ":rocket:"
    await old.close()
    await asyncio.sleep(0.05)
    await server.update_message(key.public, "m1", f"{rocket} go!", final=True)
    new = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public,
                               caps=["png", CAP])
    assert new.welcome["chat"] == {"title": rocket}
    ev = await new.next_event("msg")
    assert ev["text"] == f"{rocket} go!" and ev["spans"] == [[5, 7, "b"]]
    await new.close()


async def test_the_apps_hello_caps_are_remembered_for_its_connection(server):
    c, key = await _pair(server)  # the test client says jpeg, png, pcm16
    assert server.has_cap(key.public, "png") and not server.has_cap(key.public, "voiceq")
    assert not server.has_cap(b"x" * 32, "png")  # unknown device
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public,
                              caps=["voiceq", 7])  # junk entries are ignored
    assert (await c.next_event("_closed"))["t"] == "_closed"
    assert server.has_cap(key.public, "voiceq") and not server.has_cap(key.public, "png")
    await c2.close()
    await asyncio.sleep(0.05)
    assert not server.has_cap(key.public, "voiceq")  # gone with the connection
    assert server.has_cap(key.public, "voiceq", offline=True)  # still known as what that app can do
    assert not server.has_cap(b"x" * 32, "voiceq", offline=True)


async def test_newest_connection_wins(server):
    c, key = await _pair(server)
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    assert (await c.next_event("_closed"))["t"] == "_closed"
    r = await c2.request("send", text="x")
    assert r["t"] == "send_r"
    await c2.close()


async def test_plaintext_mode(tmp_path):
    backend = StubBackend()
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0, secure=False),
                          backend, KeyPair(), DeviceStore(tmp_path / "d.json"), TokenStore(tmp_path / "t.json"))
    backend.server = srv
    await srv.start()
    try:
        c = await Client.connect("127.0.0.1", srv.port, device="Dev 3DS")
        assert c.welcome["t"] == "welcome"
        assert (await c.request("send", text="yo"))["t"] == "send_r"
        await c.close()
    finally:
        await srv.stop()


async def test_discovery(tmp_path):
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=47334, name="DISCO"),
                          StubBackend(), KeyPair(), DeviceStore(tmp_path / "d.json"), TokenStore(tmp_path / "t.json"))
    await srv.start()
    try:
        found = await asyncio.to_thread(discover, 1.0, 47334, "127.0.0.1")
        assert found and found[0]["name"] == "DISCO" and found[0]["port"] == srv.port
    finally:
        await srv.stop()


async def test_callback_pair_and_reconnect(tmp_path):
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=47335, name="CB"),
                          StubBackend(), KeyPair(), DeviceStore(tmp_path / "d.json"), TokenStore(tmp_path / "t.json"))
    await srv.start()
    try:
        key = KeyPair()
        c = await Client.connect_callback("127.0.0.1", 47335, device="CB 3DS", static=key,
                                          server_key=srv.static.public, pair_token=srv.tokens.create(), timeout=5)
        assert c.welcome["t"] == "welcome"
        await c.close()
        await asyncio.sleep(1.05)  # per-address rate limit
        c = await Client.connect_callback("127.0.0.1", 47335, static=key, server_key=srv.static.public, timeout=5)
        assert c.welcome["t"] == "welcome"
        await c.close()
    finally:
        await srv.stop()


async def test_callback_refusals(tmp_path):
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0),
                          StubBackend(), KeyPair(), DeviceStore(tmp_path / "d.json"), TokenStore(tmp_path / "t.json"))
    await srv.start()
    try:
        assert not srv.request_callback("8.8.8.8", 7335)      # public address: never dialed
        assert not srv.request_callback("127.0.0.1", 80)      # privileged port
        assert not srv.request_callback("not-an-ip", 7335)
        assert srv.request_callback("127.0.0.1", 47399)       # nothing listens there: dial fails quietly
        assert srv.request_callback("127.0.0.1", 47399)       # retry of the same request is accepted
        assert not srv.request_callback("127.0.0.1", 47398)   # but a new one from that address is rate limited
    finally:
        await srv.stop()


def test_pairing_uri_and_code():
    key, token = KeyPair().public, bytes(range(16))
    info = parse_pairing_uri(pairing_uri(["192.168.1.5", "10.0.0.2"], 7333, key, token, "RANDOM-PC"))
    assert info.hosts == ["192.168.1.5", "10.0.0.2"] and info.port == 7333
    assert info.server_key == key and info.token == token and info.name == "RANDOM-PC"
    t5 = bytes([1, 2, 3, 4, 5])
    code = code_from_token(t5)
    assert len(code) == 8 and token_from_code(code.lower()) == t5


async def test_streaming_append_carries_changed_spans(server):
    c, key = await _pair(server)
    await c.close()
    await asyncio.sleep(0.05)
    c = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public,
                             caps=["png", CAP])  # draws sprites: bodies go out unfiltered
    dev_key = key.public
    await server.post_message(dev_key, "s1", "Run ", final=False, spans=None)
    assert (await c.next_event("msg"))["text"] == "Run "
    await server.update_message(dev_key, "s1", "Run al", final=False, spans=[[4, 6, "b"]])
    assert await c.next_event("append", "edit") == {"t": "append", "mid": "s1", "text": "al", "final": False,
                                                     "spans": [[4, 6, "b"]]}
    await server.update_message(dev_key, "s1", "Run all", final=False, spans=[[4, 7, "b"]])
    assert (await c.next_event("append", "edit"))["spans"] == [[4, 7, "b"]]
    await server.update_message(dev_key, "s1", "Run all now", final=False, spans=[[4, 7, "b"]])
    assert await c.next_event("append", "edit") == {"t": "append", "mid": "s1", "text": " now", "final": False}
    await server.update_message(dev_key, "s1", "Run all now", final=False, spans=[])
    assert await c.next_event("append", "edit") == {"t": "append", "mid": "s1", "text": "", "final": False,
                                                     "spans": []}
    await server.update_message(dev_key, "s1", "Run all now", final=True, spans=[[4, 7, "b"]])
    assert await c.next_event("append", "edit") == {"t": "edit", "mid": "s1", "text": "Run all now",
                                                     "final": True, "spans": [[4, 7, "b"]]}
    await c.close()


async def test_streaming_spans_restate_whole_text_for_downgraded_app(server):
    c, key = await _pair(server)  # no sprite cap: sprites become `:name:` text, so span offsets shift
    dev_key = key.public
    rocket = emoji_for_3ds("🚀")
    await server.post_message(dev_key, "s1", f"{rocket} ", final=False)
    assert (await c.next_event("msg"))["text"] == ":rocket: "
    n = len(f"{rocket} ".encode())
    await server.update_message(dev_key, "s1", f"{rocket} al", final=False, spans=[[n, n + 2, "b"]])
    assert await c.next_event("append", "edit") == {"t": "edit", "mid": "s1", "text": ":rocket: al",
                                                     "final": False, "spans": [[9, 11, "b"]]}
    await server.update_message(dev_key, "s1", f"{rocket} al ok", final=False, spans=[[n, n + 2, "b"]])
    assert await c.next_event("append", "edit") == {"t": "append", "mid": "s1", "text": " ok", "final": False}
    await c.close()
