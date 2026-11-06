"""Bots: Hermes profiles reached through the one gateway (protocol.md §4.8)."""

import asyncio
import contextlib
import types

import pytest

from h3proto.client import Client, ServerError
from h3proto.noise import KeyPair
from h3proto.pairing import DeviceStore, TokenStore
from h3proto.server import Hermes3DSServer, ServerConfig
from test_fetch import adapter_mod  # noqa: F401 - fixture


# --- the server core: every event of a bot's chat carries its name -------------------------------------

class EchoBackend:
    def __init__(self):
        self.server = None

    async def welcome_state(self, device):
        return {}

    slow_requests = frozenset({"slow"})

    async def handle(self, device, msg):
        if msg["t"] == "status":
            return {"t": "status_r", "busy": False}
        if msg["t"] == "slow":
            await asyncio.sleep(0.5)
            return {"t": "slow_r"}
        if msg["t"] == "nope":
            from h3proto.server import RequestError
            raise RequestError("not_found", "no such bot")
        return None

    async def on_connect(self, device):
        pass

    async def on_disconnect(self, device):
        pass


@pytest.fixture
async def server(tmp_path):
    backend = EchoBackend()
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


async def test_a_slow_request_does_not_hold_up_the_connection(server):
    c, key = await _pair(server)
    slow = asyncio.create_task(c.request("slow"))
    assert (await c.request("status", timeout=0.3))["t"] == "status_r"  # answered while the slow one runs
    assert not slow.done()
    assert (await slow)["t"] == "slow_r"
    await c.close()


async def test_bot_events_are_tagged_and_the_main_agents_are_not(server):
    c, key = await _pair(server)
    k = key.public
    await server.post_message(k, "m1", "main", final=True)
    assert "agent" not in await c.next_event("msg")
    await server.post_message(k, "m2", "Hi", final=False, agent="research")
    ev = await c.next_event("msg")
    assert ev["agent"] == "research" and ev["text"] == "Hi"
    await server.update_message(k, "m2", "Hi there", final=True)
    assert await c.next_event("append", "edit") == {"t": "append", "mid": "m2", "text": " there", "final": True,
                                                    "agent": "research"}
    await server.delete_message(k, "m2")
    assert await c.next_event("delete") == {"t": "delete", "mid": "m2", "agent": "research"}
    await c.close()


async def test_typing_is_per_agent(server):
    c, key = await _pair(server)
    k = key.public
    await server.set_typing(k, True, "research")
    assert await c.next_event("typing") == {"t": "typing", "on": True, "agent": "research"}
    await server.set_typing(k, True)  # the main agent starts too: its own state
    assert await c.next_event("typing") == {"t": "typing", "on": True}
    await server.set_typing(k, False, "research")
    assert await c.next_event("typing") == {"t": "typing", "on": False, "agent": "research"}
    await server.set_typing(k, False, "research")  # no change, no event
    await server.set_typing(k, False)
    assert await c.next_event("typing") == {"t": "typing", "on": False}
    await c.close()


async def test_offline_replay_keeps_each_bots_tag(server):
    c, key = await _pair(server)
    await c.close()
    await asyncio.sleep(0.05)
    k = key.public
    await server.post_message(k, "m5", "routine done", agent="news")
    await server.post_message(k, "m6", "", final=False, agent="news")
    await server.delete_message(k, "m6")
    await server.set_pending(k, "a1", {"t": "approval", "req": "a1", "text": "ok?", "agent": "news"})
    await server.set_typing(k, True, "news")
    await server.set_typing(k, True)
    c2 = await Client.connect("127.0.0.1", server.port, static=key, server_key=server.static.public)
    assert await c2.next_event("delete") == {"t": "delete", "mid": "m6", "agent": "news"}
    ev = await c2.next_event("msg")
    assert ev["mid"] == "m5" and ev["agent"] == "news"
    assert (await c2.next_event("approval"))["agent"] == "news"
    typing = [await c2.next_event("typing"), await c2.next_event("typing")]
    assert sorted(t.get("agent", "") for t in typing) == ["", "news"]
    # The settled notice goes to the same bot's chat, whoever settles it.
    await server.clear_pending(k, "a1", {"t": "approval_done", "req": "a1", "choice": "deny"})
    assert (await c2.next_event("approval_done"))["agent"] == "news"
    await c2.close()


async def test_a_departed_bots_prompts_are_settled(server):
    c, key = await _pair(server)
    k = key.public
    await server.set_pending(k, "a1", {"t": "approval", "req": "a1", "text": "ok?", "agent": "news"})
    await server.set_pending(k, "c:q1", {"t": "clarify", "cid": "q1", "text": "which?", "agent": "news"})
    await server.set_pending(k, "a2", {"t": "approval", "req": "a2", "text": "ok?"})
    await c.next_event("approval")
    await server.clear_agent_pending("news")
    assert await c.next_event("approval_done") == {"t": "approval_done", "req": "a1", "choice": "timeout",
                                                   "agent": "news"}
    assert await c.next_event("clarify_done") == {"t": "clarify_done", "cid": "q1", "agent": "news"}
    assert server.pending(k, "a1") is None and server.pending(k, "c:q1") is None
    assert server.pending(k, "a2") is not None  # the main agent's stays
    await c.close()


async def test_replies_name_the_bot_they_answer_for(server):
    c, _ = await _pair(server)
    assert (await c.request("status", agent="research"))["agent"] == "research"
    assert "agent" not in await c.request("status")
    with pytest.raises(ServerError) as e:
        await c.request("nope", agent="ghost")
    assert e.value.code == "not_found"
    await c.close()


def test_blank_previews_are_per_bot(server):
    k = b"k" * 32
    asyncio.run(server.post_message(k, "m1", "", final=False, agent="a"))
    asyncio.run(server.post_message(k, "m2", "", final=False))
    assert server.open_blank_messages(k, "a") == ["m1"]
    assert server.open_blank_messages(k) == ["m2"]


# --- the adapter: chat ids, the bot's profile on the source, request checks -----------------------------

class _Server:
    def __init__(self):
        self.posted, self.events, self.typing = [], [], []
        self.devices = types.SimpleNamespace(all=lambda: [types.SimpleNamespace(fp="fp1", key=b"k" * 32)])

    def open_blank_messages(self, key, agent=""):
        return []

    async def post_message(self, key, mid, text, **kw):
        self.posted.append({"mid": mid, "text": text, **kw})

    async def set_typing(self, key, on, agent=""):
        self.typing.append((on, agent))

    async def event(self, key, msg):
        self.events.append(msg)


class _Runner:
    def __init__(self, served=("default", "research"), multiplex=True):
        self.served = list(served)
        self.config = types.SimpleNamespace(multiplex_profiles=multiplex)

    def served_profile_names(self):
        return self.served


@pytest.fixture
def ad(adapter_mod, monkeypatch):  # noqa: F811
    a = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
    a._server = _Server()
    a._chat_keys = {"dev:fp1": b"k" * 32}
    a._ids = iter(range(1, 100))
    a._queued, a._awaiting_heard, a._active_sessions = {}, {}, {}
    a._trace_path = None
    a._approval_cards = {}
    a.gateway_runner = _Runner()
    a.handled = []
    a.build_source = lambda **kw: types.SimpleNamespace(profile=None, **kw)
    a._source_session_key = lambda source: f"{source.profile}:{source.chat_id}"

    async def handle_message(event):
        a.handled.append(event)

    a.handle_message = handle_message
    scopes = []

    @contextlib.asynccontextmanager
    async def scope(home):
        scopes.append(home)
        yield

    # The bot's own adapter, as the multiplexed gateway builds one per served profile: it binds nothing and
    # shares the listener's state; turns for the bot enter Hermes through it.
    bot = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
    bot._bot_profile = "research"
    bot.handled, bot._active_sessions = [], {}
    bot.build_source = lambda **kw: types.SimpleNamespace(profile=None, built_by="research", **kw)
    bot._source_session_key = lambda source: f"{source.profile}:{source.chat_id}"

    async def bot_handle(event):
        bot.handled.append(event)

    bot.handle_message = bot_handle
    monkeypatch.setattr(adapter_mod._HUB, "listener", a)
    monkeypatch.setattr(adapter_mod._HUB, "bots", {"research": bot})
    a.bot = bot
    monkeypatch.setattr(adapter_mod, "MessageEvent", lambda **kw: types.SimpleNamespace(**kw))
    monkeypatch.setattr(adapter_mod, "MessageType", types.SimpleNamespace(TEXT="text", VOICE="voice", PHOTO="photo"))
    monkeypatch.setattr(adapter_mod.compat, "profile_scope", scope)
    monkeypatch.setattr(adapter_mod.compat, "agent_home", lambda runner, name: f"/h/profiles/{name}")
    a.scopes = scopes
    return a


def _device():
    return types.SimpleNamespace(fp="fp1", key=b"k" * 32, name="3DS")


def test_chat_ids(adapter_mod):
    dev = _device()
    assert adapter_mod.chat_id_for(dev) == "dev:fp1"
    assert adapter_mod.chat_id_for(dev, "research") == "dev:fp1/research"
    assert adapter_mod.split_chat_id("dev:fp1/research") == ("dev:fp1", "research")
    assert adapter_mod.split_chat_id("dev:fp1") == ("dev:fp1", "")


def test_a_bots_chat_reaches_the_same_device_and_is_tagged(ad):
    assert ad._key("dev:fp1/research") == b"k" * 32
    assert ad._key("dev:other/research") is None
    asyncio.run(ad.send("dev:fp1/research", "Found three papers."))
    asyncio.run(ad.send("dev:fp1", "Main agent here."))
    assert [p["agent"] for p in ad._server.posted] == ["research", ""]
    asyncio.run(ad.send_typing("dev:fp1/research"))
    asyncio.run(ad.stop_typing("dev:fp1/research"))
    assert ad._server.typing == [(True, "research"), (False, "research")]


def test_the_source_of_a_bots_chat_runs_as_that_profile(ad):
    src = ad.source_for(_device(), "research")
    assert src.chat_id == "dev:fp1/research" and src.profile == "research" and src.role_authorized is True
    assert src.built_by == "research"  # Hermes admits a bot's turn only from the bot's own adapter
    main = ad.source_for(_device())
    assert main.chat_id == "dev:fp1" and main.profile is None


def test_reauthorize_checks_the_device_and_the_bot(ad):
    ok = types.SimpleNamespace(chat_type="dm", user_id="fp1", chat_id="dev:fp1/research", profile="research")
    assert asyncio.run(ad.reauthorize_native_roles(ok)) is True
    assert asyncio.run(ad.reauthorize_native_roles(types.SimpleNamespace(**{**ok.__dict__, "profile": "other"}))) is False
    assert asyncio.run(ad.reauthorize_native_roles(types.SimpleNamespace(**{**ok.__dict__, "user_id": "fp2"}))) is False
    main = types.SimpleNamespace(chat_type="dm", user_id="fp1", chat_id="dev:fp1", profile=None)
    assert asyncio.run(ad.reauthorize_native_roles(main)) is True


def test_a_send_to_a_bot_goes_to_its_chat(ad, adapter_mod):
    backend = adapter_mod._Backend(ad)
    asyncio.run(backend.handle(_device(), {"t": "send", "id": 1, "text": "hi", "agent": "research"}))
    (ev,) = ad.bot.handled  # through the bot's adapter, not the listener's
    assert ad.handled == []
    assert ev.source.chat_id == "dev:fp1/research" and ev.source.profile == "research"
    assert ad._queued == {"dev:fp1/research": 0}
    assert ad.scopes == ["/h/profiles/research"]  # reads and the send happen as the bot
    # The main agent by its profile name is the main chat, outside any bot's scope.
    asyncio.run(backend.handle(_device(), {"t": "send", "id": 2, "text": "hi", "agent": "default"}))
    (main,) = ad.handled
    assert main.source.chat_id == "dev:fp1" and ad.scopes == ["/h/profiles/research"]


def test_a_bot_without_an_adapter_is_listed_but_refuses_sends(ad, adapter_mod, monkeypatch):
    monkeypatch.setattr(adapter_mod._HUB, "bots", {})  # its profile doesn't run the 3ds platform
    with pytest.raises(adapter_mod.RequestError) as e:
        asyncio.run(adapter_mod._Backend(ad).handle(_device(), {"t": "send", "id": 1, "text": "hi", "agent": "research"}))
    assert e.value.code == "unavailable" and "research" in e.value.msg
    assert "hermes -p research config set platforms.3ds.enabled true" in e.value.msg
    assert ad.handled == []
    # Nor can the 3DS read or change that profile's sessions, skills or jobs.
    for t in ("sessions.list", "skills.list", "jobs.list", "status"):
        with pytest.raises(adapter_mod.RequestError) as e:
            asyncio.run(adapter_mod._Backend(ad).handle(_device(), {"t": t, "id": 1, "agent": "research"}))
        assert e.value.code == "unavailable"


def test_a_bots_adapter_shares_the_listener(ad, adapter_mod):
    bot = ad.bot
    assert bot._server is ad._server and bot._chat_keys is ad._chat_keys  # one port, one pairing
    asyncio.run(bot.send("dev:fp1/research", "From the bot's adapter."))
    assert ad._server.posted[-1]["agent"] == "research"
    # Its lifecycle only registers it; the listener stays up.
    hub = adapter_mod._HUB
    del hub.bots["research"]
    bot._mark_connected = bot._mark_disconnected = lambda: None
    bot._wire_plugin_handlers = lambda native: None
    assert asyncio.run(bot.connect()) is True and hub.bots["research"] is bot
    asyncio.run(bot.disconnect())
    assert "research" not in hub.bots and hub.listener is ad and ad._server is not None
    # Each profile loads its own copy of the plugin: one whose shared state is laid out otherwise stays out.
    errors = []
    bot._set_fatal_error = lambda code, msg, retryable: errors.append(code)
    ad.COPY = (adapter_mod.STATE_LAYOUT, "0.1.9")
    assert asyncio.run(bot.connect()) is False and errors == ["plugin_mismatch"] and "research" not in hub.bots
    # No listener (the main profile doesn't run 3ds): say so rather than answer into nothing.
    hub.listener = None
    try:
        assert asyncio.run(bot.connect()) is False and errors[-1] == "no_listener"
    finally:
        hub.listener = ad


def test_a_card_from_a_bot_that_left_can_still_be_answered(ad, adapter_mod, monkeypatch):
    seen = []

    async def resolve_approval(device, req, choice):
        seen.append((req, choice))
        return 0

    ad.resolve_approval = resolve_approval
    out = asyncio.run(adapter_mod._Backend(ad).handle(_device(), {"t": "approve", "id": 1, "req": "a1",
                                                                  "choice": "deny", "agent": "ghost"}))
    assert out == {"t": "approve_r", "resolved": 0} and seen == [("a1", "deny")]


def test_requests_for_unknown_bots_are_refused(ad, adapter_mod):
    backend = adapter_mod._Backend(ad)
    for name, code in (("ghost", "not_found"), ("Bad Name", "bad_request"), (5, "bad_request")):
        with pytest.raises(adapter_mod.RequestError) as e:
            asyncio.run(backend.handle(_device(), {"t": "send", "id": 1, "text": "hi", "agent": name}))
        assert e.value.code == code
    ad.gateway_runner = _Runner(multiplex=False)  # one-profile gateway: only the main agent
    with pytest.raises(adapter_mod.RequestError):
        asyncio.run(backend.handle(_device(), {"t": "send", "id": 1, "text": "hi", "agent": "research"}))
    assert ad.handled == []


def test_roster_has_each_bots_live_state(ad, adapter_mod, monkeypatch):
    async def agents_list(runner):
        return {"items": [{"id": "default", "primary": True}, {"id": "research", "primary": False}],
                "multiplex": True}

    monkeypatch.setattr(adapter_mod.compat, "agents_list", agents_list)
    ad.bot._active_sessions = {"research:dev:fp1/research": object()}  # its turns run on its adapter
    ad._should_auto_tts_for_chat = lambda chat_id: chat_id == "dev:fp1"
    ad.bot._should_auto_tts_for_chat = lambda chat_id: chat_id == "dev:fp1/research"
    out = asyncio.run(adapter_mod._Backend(ad).handle(_device(), {"t": "agents.list", "id": 1}))
    assert [(i["id"], i["busy"], i["voice"], i["ready"]) for i in out["items"]] == [
        ("default", False, True, True), ("research", True, True, True)]


def test_tool_status_and_prompts_are_tagged(ad, adapter_mod):
    ad._loop = None  # set_status_text schedules onto the gateway loop; here it only builds the event
    tagged = ad._tag({"t": "tool", "state": "done"}, "dev:fp1/research")
    assert tagged["agent"] == "research"
    assert "agent" not in ad._tag({"t": "tool", "state": "done"}, "dev:fp1")


# --- compat: what the roster shows, and making a bot -------------------------------------------------

def test_slug_and_titles(adapter_mod):
    compat = adapter_mod.compat
    assert compat.slug("Research Buddy!") == "research-buddy"
    assert compat.slug("  --Ops__Bot--  ") == "ops__bot"
    assert compat.slug("日本") == ""
    assert compat._pretty("research-buddy") == "Research Buddy"


def test_agent_info_reads_bot_mode_metadata(adapter_mod, tmp_path):
    compat = adapter_mod.compat
    home = tmp_path / "profiles" / "scribe"
    home.mkdir(parents=True)
    (home / "profile.yaml").write_text("description: Writes the weekly notes.\n"
                                       "ui_meta:\n  hermes-bots:\n    title: Dr. Scribe\n", encoding="utf-8")
    (home / "config.yaml").write_text("model:\n  default: hermes-4-70b\n  provider: nous\n", encoding="utf-8")
    info = compat._agent_info("scribe", home, False)
    look = info.pop("_look")
    assert (look["photo"], look["shape"]) == (None, "drop")  # Bot Mode's pick for an unstyled "scribe"
    assert info == {"id": "scribe", "title": "Dr. Scribe", "desc": "Writes the weekly notes.", "model": "hermes-4-70b",
                    "provider": "nous", "primary": False, "bot": True}
    bare = tmp_path / "profiles" / "ops-bot"
    bare.mkdir()
    assert compat._agent_info("ops-bot", bare, False)["title"] == "Ops Bot"
    assert compat._agent_info("default", tmp_path, True)["title"] == "Hermes"
    assert compat._agent_info("default", tmp_path, True)["_look"] is None  # the main agent keeps the caduceus


def test_create_refuses_what_hermes_would(adapter_mod):
    compat, RequestError = adapter_mod.compat, adapter_mod.RequestError
    with pytest.raises(RequestError) as e:
        asyncio.run(compat.agents_create(_Runner(multiplex=False), "scribe", ""))
    assert e.value.code == "unavailable"
    for bad in ("", "default", "main", "research", "sudo", "Bad Name"):
        with pytest.raises(RequestError) as e:
            asyncio.run(compat.agents_create(_Runner(), bad, ""))
        assert e.value.code == "bad_request"


def test_create_clones_main_and_turns_the_3ds_back_on(adapter_mod, monkeypatch, tmp_path):
    compat = adapter_mod.compat
    calls = []

    def run(argv, **kw):
        calls.append(argv[1:])
        if argv[1:3] == ["profile", "create"]:
            (tmp_path / "profiles" / "scribe").mkdir(parents=True)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    class R(_Runner):
        async def reconcile_served_profiles(self, reason):
            calls.append(["reconcile", reason])

    monkeypatch.setattr(compat.subprocess, "run", run)
    monkeypatch.setattr(compat, "_hermes_cli", lambda: "hermes")
    monkeypatch.setattr(compat, "agent_home", lambda runner, n: tmp_path / "profiles" / n)
    item = asyncio.run(compat.agents_create(R(), "scribe", "-Writes notes"))
    assert calls == [["profile", "create", "scribe", "--clone-from", "default", "--no-alias",
                      "--description=-Writes notes"],
                     ["-p", "scribe", "config", "set", "platforms.3ds.enabled", "true"],
                     ["reconcile", "3ds-create"]]
    assert item["id"] == "scribe" and item["title"] == "Scribe"
    # The 3DS lost the reply (it dropped the connection meanwhile) and asks again: the same bot, nothing re-run.
    again = asyncio.run(compat.agents_create(R(), "scribe", "-Writes notes"))
    assert again["id"] == "scribe" and len(calls) == 3
    compat._created.pop("scribe")
