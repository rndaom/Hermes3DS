"""`hermes 3ds doctor` (plugin/doctor.py): what it concludes from the facts, and that gathering them only looks."""

import socket
import threading
import time
from types import SimpleNamespace

import importlib

import pytest

from h3proto.noise import KeyPair
from h3proto.pairing import b64u
from test_fetch import adapter_mod  # noqa: F401 - fixture

FAIL, INFO, OK, WARN = "fail", "info", "ok", "warn"


@pytest.fixture
def doctor(adapter_mod):  # noqa: F811
    """plugin.doctor, imported as Hermes imports it (a package beside adapter.py and cli.py)."""
    return importlib.import_module("plugin.doctor")


@pytest.fixture
def good(doctor):
    return lambda **over: _good(doctor, **over)


def _good(doctor, **over):
    f = doctor.Facts(enabled=True, listening=True, key="K", discovery={"t": "here", "name": "pc", "key": "K"},
              streaming=True, addresses=["192.168.1.5"], ffmpeg=True, version="0.2.1", now=1_000_000.0,
              devices=[SimpleNamespace(name="My 3DS", last_seen=1_000_000.0 - 3 * 86400)])
    for k, v in over.items():
        setattr(f, k, v)
    return f


def levels(checks):
    return [c.level for c in checks]


def find(checks, text):
    hits = [c for c in checks if text in c.title]
    assert hits, f"no check mentions {text!r}: {[c.title for c in checks]}"
    return hits[0]


def test_a_good_setup_has_no_problems(doctor, good):
    checks = doctor.evaluate(good())
    assert FAIL not in levels(checks) and WARN not in levels(checks)
    assert find(checks, "Paired 3DS").title == "Paired 3DS: My 3DS (last seen 3 days ago)."
    assert "192.168.1.5:7333" in find(checks, "address for the 3DS").title
    assert doctor.render(checks).endswith("Everything on this computer looks right.")


def test_a_newer_release_is_mentioned_not_a_problem(doctor, good):
    checks = doctor.evaluate(good(latest="0.2.2"))
    c = find(checks, "0.2.2 is out")
    assert c.level == INFO and "plugins update 3ds-platform" in c.fix and "0.2.1" in c.title
    assert FAIL not in levels(checks) and WARN not in levels(checks)
    assert find(doctor.evaluate(good(latest="0.2.1")), "newest release").level == OK
    for latest in ("", "0.2.0"):  # never checked (or turned off); a dev copy ahead of the release
        assert not [c for c in doctor.evaluate(good(latest=latest)) if "is out" in c.title]


def test_platform_off_is_the_one_failure_named(doctor, good):
    checks = doctor.evaluate(good(enabled=False, listening=False, discovery=None))
    fails = [c for c in checks if c.level == FAIL]
    assert len(fails) == 1 and "not turned on" in fails[0].title
    assert "platforms.3ds.enabled true" in fails[0].fix
    assert doctor.render(checks).splitlines()[-1].startswith("1 problem to fix")


def test_gateway_not_running(doctor, good):
    c = find(doctor.evaluate(good(listening=False, discovery=None)), "Nothing is listening on port 7333")
    assert c.level == FAIL and "hermes gateway restart" in c.fix


def test_another_listener_on_the_port(doctor, good):
    c = find(doctor.evaluate(good(discovery={"name": "other-pc", "key": "X"})), "different Hermes 3DS listener (other-pc)")
    assert c.level == FAIL
    # without this profile's key there is nothing to compare: no accusation
    assert FAIL not in levels(doctor.evaluate(good(key=None, discovery={"name": "other-pc", "key": "X"})))


def test_discovery_silent_and_discovery_off(doctor, good):
    c = find(doctor.evaluate(good(discovery=None)), "did not answer a discovery question")
    assert c.level == WARN and "Enter the PC's address" in c.fix
    c = find(doctor.evaluate(good(discovery=None, discovery_port=0)), "turned off (`discovery_port: 0`)")
    assert c.level == INFO


def test_warnings(doctor, good):
    checks = doctor.evaluate(good(streaming=False, ffmpeg=False, plaintext=True, key_loose=True, docker=True, stt=False))
    for text in ("Streaming is off", "ffmpeg is not installed", "security: plaintext", "key file can be read",
                 "runs in a container"):
        assert find(checks, text).level == WARN
    assert find(checks, "Speech-to-text is off").level == INFO
    assert FAIL not in levels(checks)
    assert doctor.render(checks).splitlines()[-1] == "It should work. 5 things to look at."


def test_no_network_and_nothing_paired(doctor, good):
    checks = doctor.evaluate(good(addresses=[], devices=[]))
    assert find(checks, "no network address").level == FAIL
    c = find(checks, "No 3DS is paired yet")
    assert c.level == INFO and c.fix.startswith("hermes 3ds pair")


def test_firewall_commands_name_the_configured_ports(doctor, good):
    fix = lambda **o: find(doctor.evaluate(good(**o)), "firewall on this computer").fix
    assert fix(firewall="firewalld", port=7400) == ("sudo firewall-cmd --permanent --add-port=7400/tcp "
                                                    "--add-port=7334/udp && sudo firewall-cmd --reload")
    assert fix(firewall="ufw") == "sudo ufw allow 7333/tcp && sudo ufw allow 7334/udp"
    assert fix(firewall="ufw", discovery_port=0, discovery=None) == "sudo ufw allow 7333/tcp"
    assert "Allow an app" in fix(firewall="windows")
    assert "TCP 7333 and UDP 7334" in fix(firewall="")


def test_bots(doctor, good):
    bots = [doctor.Bot("research", True, "0.2.1"), doctor.Bot("old", True, "0.2.0"), doctor.Bot("quiet", False, "0.2.1"),
            doctor.Bot("bare", True, None)]
    checks = doctor.evaluate(good(multiplex=True, bots=bots))
    assert find(checks, 'Bot "research" is reachable').level == OK
    c = find(checks, 'Bot "old" has plugin 0.2.0, this profile has 0.2.1')
    assert c.level == FAIL and "hermes -p old plugins update 3ds-platform" in c.fix
    assert find(checks, 'Bot "quiet" is not reachable').fix.startswith("hermes -p quiet config set platforms.3ds.enabled true")
    c = find(checks, 'Bot "bare"')
    assert "no copy of the plugin" in c.title and c.fix.startswith("hermes -p bare plugins install 3ds-platform")
    # multiplexing off: one hint, no per-bot lines, no failure for the old copy
    checks = doctor.evaluate(good(multiplex=False, bots=bots))
    assert find(checks, "4 other Hermes profiles, but the Bots tab is off").level == INFO
    assert FAIL not in levels(checks)
    # no other profiles: bots are not mentioned at all
    assert not [c for c in doctor.evaluate(good()) if "Bot" in c.title]


def test_away_line_levels(doctor, good):
    assert find(doctor.evaluate(good(away="Away from home: ready at pc.example:443 (the 3DS learns it automatically).")),
                "ready at").level == OK
    assert find(doctor.evaluate(good(away="Away from home: not working. Funnel is off")), "not working").level == WARN
    assert find(doctor.evaluate(good(away="Away from home: off. To use the 3DS over the internet, install Tailscale")),
                "Away from home: off").level == INFO


def test_render_shows_fixes_under_their_check(doctor, good):
    out = doctor.render(doctor.evaluate(good(streaming=False))).splitlines()
    i = next(n for n, line in enumerate(out) if line.startswith("[warn] Streaming is off"))
    assert out[i + 1].startswith("       -> hermes config set streaming.enabled true")


def test_copy_version_and_other_profiles(doctor, good, tmp_path, monkeypatch):
    name = doctor.Path(doctor.__file__).resolve().parent.name
    root = tmp_path / "root"
    for bot, version, enabled in (("a", "0.2.1", "true"), ("b", '"0.2.0"', "false")):
        home = root / "profiles" / bot
        (home / "plugins" / name).mkdir(parents=True)
        (home / "plugins" / name / "plugin.yaml").write_text(f"name: x\nversion: {version}\n", "utf-8")
        (home / "config.yaml").write_text(f"platforms:\n  3ds:\n    enabled: {enabled}\n", "utf-8")
    (root / "profiles" / "c").mkdir()  # a profile with neither a config nor a copy
    (root / "profiles" / ".deleted").mkdir()  # Hermes' own bookkeeping, not a profile
    assert doctor._bots(root, root) == [doctor.Bot("a", True, "0.2.1"), doctor.Bot("b", False, "0.2.0"), doctor.Bot("c", False, None)]
    # run from a named profile: the default profile is a bot, and the profile itself is not listed
    assert [b.name for b in doctor._bots(root / "profiles" / "a", root)] == ["default", "b", "c"]


def test_collect_only_looks(doctor, good, tmp_path, monkeypatch):
    """Against a stand-in listener: the facts come out right and nothing is written (no key is made)."""
    kp = KeyPair()
    (tmp_path / "server_key").write_bytes(kp.private)
    (tmp_path / "server_key").chmod(0o600)
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(4)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(3)

    def answer():
        try:
            data, addr = udp.recvfrom(64)
            if data[:5] == b"H3DS?":
                udp.sendto(('{"t":"here","v":1,"name":"test-pc","port":1,"key":"%s","sec":"secure"}'
                            % b64u(kp.public)).encode(), addr)
        except OSError:
            pass

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    cli = importlib.import_module("plugin.cli")
    ports = {"enabled": True, "port": tcp.getsockname()[1], "discovery_port": udp.getsockname()[1]}
    monkeypatch.setattr(cli, "_platform_config", lambda: ports)
    monkeypatch.setattr(cli, "away_summary", lambda data: "")
    before = sorted(p.name for p in tmp_path.iterdir())
    try:
        f = doctor.collect(tmp_path)
    finally:
        t.join(3)
        tcp.close()
        udp.close()
    assert f.enabled and f.listening
    assert f.discovery and f.discovery["name"] == "test-pc" and f.discovery["key"] == f.key == b64u(kp.public)
    assert not f.key_loose and f.devices == [] and abs(f.now - time.time()) < 60
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    checks = doctor.evaluate(f)
    assert find(checks, "The gateway is listening").level == OK
    assert find(checks, "can find this computer by itself").level == OK

    # no key file: nothing is created, and nothing is compared
    empty = tmp_path / "empty"
    empty.mkdir()
    f = doctor.collect(empty)  # the stand-in listener is closed by now
    assert f.key is None and list(empty.iterdir()) == [] and not f.listening and f.discovery is None
