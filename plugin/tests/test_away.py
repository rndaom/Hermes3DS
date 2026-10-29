"""Away from home (protocol.md §2.4): the loopback away listener, LAN-only pairing, the away address in
welcome / status / `away` events, the Tailscale CLI wrapper and the AwayManager loop."""

import asyncio
import json

import pytest

from h3proto import tailscale
from h3proto.away import AwayManager, DISABLED_FILE, parse_address, parse_mode, read_status
from h3proto.client import Client, ServerError
from h3proto.noise import KeyPair
from h3proto.pairing import DeviceStore, TokenStore
from h3proto.server import AWAY_MAX_HANDSHAKES, Hermes3DSServer, ServerConfig, is_lan_peer

DNS = "my-pc.tail1234.ts.net"


class Backend:
    async def welcome_state(self, device):
        return {}

    async def handle(self, device, msg):
        if msg["t"] == "status":
            return {"t": "status_r", "busy": False}
        return None

    async def on_connect(self, device):
        pass

    async def on_disconnect(self, device):
        pass


@pytest.fixture
async def server(tmp_path):
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0, name="PC"),
                          Backend(), KeyPair(), DeviceStore(tmp_path / "devices.json"),
                          TokenStore(tmp_path / "tokens.json"))
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
async def away_server(tmp_path):
    srv = Hermes3DSServer(ServerConfig(host="127.0.0.1", port=0, discovery_port=0, name="PC",
                                       away_port=_free_port()),
                          Backend(), KeyPair(), DeviceStore(tmp_path / "devices.json"),
                          TokenStore(tmp_path / "tokens.json"))
    await srv.start()
    yield srv
    await srv.stop()


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _pair(srv):
    key = KeyPair()
    c = await Client.connect("127.0.0.1", srv.port, device="3DS", static=key, server_key=srv.static.public,
                             pair_token=srv.tokens.create())
    return c, key


# --- server -------------------------------------------------------------------

def test_lan_peer_rules():
    for ip in ("192.168.1.20", "10.0.0.5", "172.16.3.4", "127.0.0.1", "169.254.1.1", "fd00::1", "::1",
               "::ffff:192.168.1.2"):
        assert is_lan_peer((ip, 1234)), ip
    for ip in ("8.8.8.8", "100.64.0.1", "1.1.1.1", "::ffff:8.8.8.8", "224.0.0.1", "garbage"):
        assert not is_lan_peer((ip, 1234)), ip
    assert not is_lan_peer(None)


async def test_away_listener_is_loopback_and_refuses_pairing(away_server):
    srv = away_server
    assert srv.away_port and srv._away_tcp.sockets[0].getsockname()[0] == "127.0.0.1"
    with pytest.raises(ServerError) as e:
        await Client.connect("127.0.0.1", srv.away_port, static=KeyPair(), server_key=srv.static.public,
                             pair_token=srv.tokens.create())
    assert e.value.code == "pair_lan_only"
    assert srv.devices.all() == []


async def test_paired_device_connects_through_away_listener(away_server):
    srv = away_server
    c, key = await _pair(srv)  # at home
    await c.close()
    c = await Client.connect("127.0.0.1", srv.away_port, static=key, server_key=srv.static.public)
    assert c.welcome["t"] == "welcome"
    await c.close()


async def test_pairing_refused_from_a_public_address(server):
    reply = await server._authorize({"t": "hello", "v": 1, "pair": "AAAA"}, KeyPair().public, ("8.8.8.8", 5000))
    assert reply["code"] == "pair_lan_only"
    reply = await server._authorize({"t": "hello", "v": 1, "pair": "AAAA"}, KeyPair().public,
                                    ("192.168.1.9", 5000))
    assert reply["code"] == "not_paired"  # a LAN address gets as far as checking the code


async def test_unknown_away_is_left_out_then_sent(server):
    c, key = await _pair(server)
    assert "away" not in c.welcome
    assert "away" not in await c.request("status")
    await server.set_away({"host": DNS, "port": 443})
    assert await c.next_event("away") == {"t": "away", "away": {"host": DNS, "port": 443}}
    assert (await c.request("status"))["away"] == {"host": DNS, "port": 443}
    await server.set_away({"host": DNS, "port": 443})  # unchanged: no new event
    await server.set_away(None)
    assert await c.next_event("away") == {"t": "away", "away": None}
    await c.close()
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    assert c2.welcome["away"] is None
    await c2.close()


async def test_away_in_welcome(server):
    await server.set_away({"host": DNS, "port": 8443})
    c, _ = await _pair(server)
    assert c.welcome["away"] == {"host": DNS, "port": 8443}
    await c.close()


async def test_away_listener_bounds_pending_handshakes(away_server):
    srv = away_server
    socks = []
    for _ in range(AWAY_MAX_HANDSHAKES):
        r, w = await asyncio.open_connection("127.0.0.1", srv.away_port)
        socks.append(w)
    for _ in range(50):
        if srv._away_handshakes >= AWAY_MAX_HANDSHAKES:
            break
        await asyncio.sleep(0.02)
    r, w = await asyncio.open_connection("127.0.0.1", srv.away_port)
    assert await asyncio.wait_for(r.read(), 2) == b""  # closed at once
    w.close()
    for w in socks:
        w.close()


# --- tailscale CLI wrapper -----------------------------------------------------

class FakeCli:
    """Scripted `tailscale` responses keyed by the joined argument list."""

    def __init__(self, state="Running", serve=None, on_funnel=None):
        self.state = state
        self.serve = serve or {}
        self.on_funnel = on_funnel  # callable(args) -> (code, output), may change self.serve
        self.calls = []

    def __call__(self, cli, args, timeout=0):
        self.calls.append(args)
        if args == ["status", "--json"]:
            return 0, json.dumps({"BackendState": self.state, "Self": {"DNSName": DNS + "."}})
        if args == ["funnel", "status", "--json"]:
            return 0, json.dumps(self.serve)
        if args[0] == "funnel":
            return self.on_funnel(args) if self.on_funnel else (1, "unexpected")
        return 1, "unknown command"


def _forward(port, target="127.0.0.1:7339"):
    return {"TCP": {str(port): {"TCPForward": target, "TerminateTLS": DNS}}, "AllowFunnel": {f"{DNS}:{port}": True}}


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(**kw):
        f = FakeCli(**kw)
        monkeypatch.setattr(tailscale, "_run", f)
        holder["f"] = f
        return f
    return install


def test_ensure_sets_up_funnel(fake):
    f = fake()

    def enable(args):
        assert args == ["funnel", "--bg", "--yes", "--tls-terminated-tcp=443", "tcp://127.0.0.1:7339"]
        f.serve = _forward(443)
        return 0, "Available on the internet: ..."
    f.on_funnel = enable
    res = tailscale.ensure_funnel(7339, cli="tailscale")
    assert res.ok and res.address == {"host": DNS, "port": 443}


def test_ensure_is_idempotent(fake):
    f = fake(serve=_forward(8443))
    res = tailscale.ensure_funnel(7339, cli="tailscale")
    assert res.ok and res.port == 8443
    assert not any(a[0] == "funnel" and "--bg" in a for a in f.calls)


def test_ensure_skips_ports_in_use(fake):
    f = fake(serve={"TCP": {"443": {"HTTPS": True}}, "Web": {f"{DNS}:8443": {}}})

    def enable(args):
        assert "--tls-terminated-tcp=10000" in args
        f.serve = {**_forward(10000), "TCP": {**f.serve["TCP"], **_forward(10000)["TCP"]}}
        return 0, ""
    f.on_funnel = enable
    assert tailscale.ensure_funnel(7339, cli="tailscale").port == 10000


def test_forward_without_funnel_allowed_is_not_ok(fake):
    serve = _forward(443)
    serve["AllowFunnel"] = {}
    f = fake(serve=serve)
    f.on_funnel = lambda args: (0, "")
    assert not tailscale.ensure_funnel(7339, cli="tailscale").ok


def test_consent_link_reported(fake):
    url = "https://login.tailscale.com/f/funnel?node=nABC123"
    f = fake()
    f.on_funnel = lambda args: (0, f"\nFunnel is not enabled on your tailnet.\nTo enable, visit:\n\n         {url}\n")
    res = tailscale.ensure_funnel(7339, cli="tailscale")
    assert not res.ok and res.url == url and url in res.error and "restart" in res.error


def test_policy_refusal_explained(fake):
    f = fake()
    f.on_funnel = lambda args: (1, 'Funnel not available; "funnel" node attribute not set. '
                                   'See https://tailscale.com/s/no-funnel.')
    res = tailscale.ensure_funnel(7339, cli="tailscale")
    assert not res.ok and "tailnet policy" in res.error and res.url.endswith("/admin/acls")


def test_old_cli_without_yes_flag(fake):
    f = fake()

    def enable(args):
        if "--yes" in args:
            return 2, "flag provided but not defined: -yes"
        f.serve = _forward(443)
        return 0, ""
    f.on_funnel = enable
    assert tailscale.ensure_funnel(7339, cli="tailscale").ok


@pytest.mark.parametrize("state,words", [("NeedsLogin", "sign in"), ("Stopped", "turned off"),
                                         ("NeedsMachineAuth", "approval")])
def test_not_running_states(fake, state, words):
    fake(state=state)
    res = tailscale.ensure_funnel(7339, cli="tailscale")
    assert not res.ok and words in res.error


def test_not_installed():
    res = tailscale.ensure_funnel(7339, cli=None) if tailscale.find_cli() is None else \
        tailscale.check(None, 7339)
    assert not res.ok and not res.installed


def test_disable(fake):
    f = fake(serve=_forward(443))
    f.on_funnel = lambda args: (0, "") if args == ["funnel", "--tls-terminated-tcp=443", "off"] else (1, "bad")
    assert tailscale.disable_funnel(7339, cli="tailscale").ok


def test_find_cli_default_windows_path(monkeypatch, tmp_path):
    exe = tmp_path / "Tailscale" / "tailscale.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(tailscale.shutil, "which", lambda name: None)
    monkeypatch.setattr(tailscale.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert tailscale.find_cli() == str(exe)


# --- config parsing and the manager ---------------------------------------------

def test_parse_mode_and_address():
    assert parse_mode(None) == "auto" and parse_mode("OFF") == "off" and parse_mode(True) == "on"
    assert parse_address("example.com:8443") == {"host": "example.com", "port": 8443}
    assert parse_address("example.com") == {"host": "example.com", "port": 443}
    assert parse_address("") is None and parse_address("x:99999") is None and parse_address("x:abc") is None


class FakeServer:
    away_port = 7339

    def __init__(self):
        self.sent = []

    async def set_away(self, address):
        self.sent.append(address)


async def test_manager_publishes_funnel(tmp_path):
    srv = FakeServer()
    calls = []

    def ensure(port, public_port=0, cli=None):
        calls.append((port, public_port, cli))
        return tailscale.FunnelResult(True, DNS, 443)
    m = AwayManager(srv, tmp_path, ensure=ensure, find_cli=lambda: "ts")
    assert await m.once() > 0
    assert srv.sent == [{"host": DNS, "port": 443}] and calls == [(7339, 0, "ts")]
    assert read_status(tmp_path)["ok"] is True


async def test_manager_keeps_known_address_on_failure(tmp_path):
    srv = FakeServer()
    m = AwayManager(srv, tmp_path, ensure=lambda *a, **k: tailscale.FunnelResult(False, error="not signed in"),
                    find_cli=lambda: "ts")
    await m.once()
    assert srv.sent == []  # Tailscale may just not be up yet: the 3DS keeps what it has
    assert read_status(tmp_path)["error"] == "not signed in"


async def test_manager_without_tailscale_and_off(tmp_path):
    srv = FakeServer()
    await AwayManager(srv, tmp_path, find_cli=lambda: None).once()
    assert srv.sent == [None] and read_status(tmp_path)["installed"] is False
    srv.sent.clear()
    assert await AwayManager(srv, tmp_path, mode="off").once() is None
    assert srv.sent == [None]
    srv.sent.clear()
    (tmp_path / DISABLED_FILE).write_text("off")
    await AwayManager(srv, tmp_path, ensure=lambda *a, **k: pytest.fail("must not touch Tailscale"),
                      find_cli=lambda: "ts").once()
    assert srv.sent == [None]


async def test_manager_manual_address(tmp_path):
    srv = FakeServer()
    m = AwayManager(srv, tmp_path, address={"host": "tunnel.example.com", "port": 443},
                    find_cli=lambda: pytest.fail("no Tailscale lookup"))
    assert await m.once() is None
    assert srv.sent == [{"host": "tunnel.example.com", "port": 443}]
