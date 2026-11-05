"""Away from home (protocol.md §2.4): the loopback away listener, LAN-only pairing, the away address in
welcome / status / `away` events, the Tailscale CLI wrapper and the AwayManager loop."""

import asyncio
import importlib
import json

import pytest

from h3proto import tailscale
from h3proto.away import (AwayManager, DISABLED_FILE, ENABLED_FILE, effective_mode, parse_address, parse_mode,
                          read_status)
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


async def test_away_listener_bounds_pending_handshakes_dropping_the_oldest(away_server):
    srv = away_server
    socks, readers = [], []
    for _ in range(AWAY_MAX_HANDSHAKES):
        r, w = await asyncio.open_connection("127.0.0.1", srv.away_port)
        socks.append(w)
        readers.append(r)
    for _ in range(50):
        if len(srv._away_pending) >= AWAY_MAX_HANDSHAKES:
            break
        await asyncio.sleep(0.02)
    # One more: the oldest idle socket makes way (closed at once), the newcomer gets its chance.
    r, w = await asyncio.open_connection("127.0.0.1", srv.away_port)
    assert await asyncio.wait_for(readers[0].read(), 2) == b""
    await asyncio.sleep(0.05)
    assert len(srv._away_pending) == AWAY_MAX_HANDSHAKES and not r.at_eof()
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
    assert parse_mode(None) == "" and parse_mode("") == "" and parse_mode("OFF") == "off" and parse_mode(True) == "on"
    assert parse_mode("auto") == "auto"
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


def test_away_is_opt_in(tmp_path):
    addr = {"host": "tunnel.example.com", "port": 443}
    assert effective_mode("", tmp_path, None) == "off"  # nothing asked for: no Funnel, no listener
    assert effective_mode("", tmp_path, addr) == "on"
    assert effective_mode("auto", tmp_path, None) == "auto" and effective_mode("on", tmp_path, None) == "on"
    (tmp_path / ENABLED_FILE).write_text("on")
    assert effective_mode("", tmp_path, None) == "on"
    assert effective_mode("off", tmp_path, None) == "off"  # the config's `off` beats the CLI
    (tmp_path / DISABLED_FILE).write_text("off")
    assert effective_mode("on", tmp_path, addr) == "off"  # `hermes 3ds away off` beats the config


def test_cli_away_on_and_off(tmp_path, monkeypatch, capsys):
    cli = importlib.import_module("plugin.cli")
    ts = importlib.import_module("plugin.h3proto.tailscale")  # the copy cli.py imports
    calls = []
    monkeypatch.setattr(ts, "ensure_funnel", lambda port, **k: calls.append(("on", port))
                        or tailscale.FunnelResult(True, DNS, 443))
    monkeypatch.setattr(ts, "disable_funnel", lambda port, **k: calls.append(("off", port))
                        or tailscale.FunnelResult(True))
    monkeypatch.setattr(ts, "find_cli", lambda: None)
    monkeypatch.setattr(cli, "_listening", lambda port: False)
    config = {}
    monkeypatch.setattr(cli, "_platform_config", lambda: config)
    monkeypatch.delenv("THREEDS_AWAY", raising=False)
    monkeypatch.delenv("THREEDS_AWAY_ADDRESS", raising=False)

    assert "install Tailscale and run `hermes 3ds away on`" in cli.away_summary(tmp_path)
    assert cli._away("on", tmp_path) == 0 and calls == [("on", 7339)]
    assert (tmp_path / ENABLED_FILE).exists() and "restart" in capsys.readouterr().out
    assert cli._away_mode(tmp_path) == "on"
    assert cli._away("off", tmp_path) == 0 and calls[-1] == ("off", 7339)
    assert not (tmp_path / ENABLED_FILE).exists() and cli._away_mode(tmp_path) == "off"
    assert "turned off" in cli.away_summary(tmp_path)
    config["away"] = "off"
    calls.clear()
    assert cli._away("on", tmp_path) == 1 and calls == []  # `away: off` in the config: Tailscale untouched
