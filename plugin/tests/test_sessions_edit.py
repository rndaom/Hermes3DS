"""Sessions renamed and deleted from the 3DS (docs/protocol.md §4.6), against a stand-in for Hermes' session
database and the gateway's routing store."""
import asyncio
import contextlib
import importlib
import sys
import types

import pytest

from test_fetch import adapter_mod  # noqa: F401 - fixture

KEY = "agent:main:3ds:dm:dev:fp1"


class SessionActiveWriteGuardError(RuntimeError):
    pass


class FakeDB:
    """The few SessionDB methods the plugin uses. `tips` maps a compressed copy to the newest copy of its chat."""
    MAX = 100

    def __init__(self):
        self.rows = {
            "s-now": {"id": "s-now", "session_key": KEY, "title": "Today"},
            "s-old": {"id": "s-old", "session_key": KEY, "title": None},
            "s-taken": {"id": "s-taken", "session_key": KEY, "title": "Taxes"},
            "s-copy": {"id": "s-copy", "session_key": KEY, "title": None},  # an older copy of s-now
            "s-else": {"id": "s-else", "session_key": "agent:main:telegram:dm:42", "title": "Telegram"},
        }
        self.tips = {"s-copy": "s-now"}
        self.busy: set[str] = set()
        self.delete_calls: list[tuple[str, dict]] = []
        self.closed = 0  # times a writer was handed back

    def get_session(self, sid):
        return self.rows.get(sid)

    def resolve_resume_session_id(self, sid):
        return self.tips.get(sid, sid)

    def sanitize_title(self, title):
        clean = " ".join("".join(c for c in title if c.isprintable()).split())
        if len(clean) > self.MAX:
            raise ValueError(f"Title too long ({len(clean)} chars, max {self.MAX})")
        return clean or None

    def set_session_title(self, sid, title):
        if sid not in self.rows:
            return False
        if any(r["title"] == title and r["id"] != sid for r in self.rows.values()):
            raise ValueError(f"Title '{title}' is already in use by another session")
        self.rows[sid]["title"] = title
        return True

    def delete_session(self, sid, sessions_dir=None, exclude_active_write_guards=False,
                       include_compression_chain=False):
        self.delete_calls.append((sid, {"sessions_dir": sessions_dir, "guards": exclude_active_write_guards,
                                        "chain": include_compression_chain}))
        if sid in self.busy:
            raise SessionActiveWriteGuardError("a turn is writing")
        return self.rows.pop(sid, None) is not None

    def close(self):
        self.closed += 1


class OldDB(FakeDB):
    """A Hermes from before the guard and chain arguments."""

    def delete_session(self, sid, sessions_dir=None):
        self.delete_calls.append((sid, {"sessions_dir": sessions_dir}))
        return self.rows.pop(sid, None) is not None


class Runner:
    """The gateway. Its own session database is private to Hermes: touching it fails the test."""

    def __init__(self):
        self.removed: list[str] = []
        self.session_store = types.SimpleNamespace(remove_by_session_id=self.removed.append)
        entry = types.SimpleNamespace(session_id="s-now", session_key=KEY)

        async def get_or_create_session(source):
            return entry
        self.async_session_store = types.SimpleNamespace(get_or_create_session=get_or_create_session)

    @property
    def _session_db(self):
        raise AssertionError("the plugin must not use the gateway's private session database")


@pytest.fixture
def env(adapter_mod, monkeypatch, tmp_path):  # noqa: F811
    compat = importlib.import_module("plugin.compat")
    db = FakeDB()
    opened: list = []  # the database path each writer was opened for (None = the main agent's)
    monkeypatch.setattr(compat, "_session_db", lambda: db)
    monkeypatch.setattr(compat, "_open_writer", lambda path: opened.append(path) or db)
    consts = types.ModuleType("hermes_constants")
    consts.get_hermes_home = lambda: tmp_path
    consts.get_hermes_home_override = lambda: None
    monkeypatch.setitem(sys.modules, "hermes_constants", consts)
    return types.SimpleNamespace(compat=compat, db=db, runner=Runner(), home=tmp_path, opened=opened,
                                 err=adapter_mod.RequestError)


def refused(env, coro) -> tuple[str, str]:
    with pytest.raises(env.err) as e:
        asyncio.run(coro)
    return e.value.code, e.value.msg


def test_a_session_gets_a_new_name(env):
    r = asyncio.run(env.compat.sessions_rename(env.runner, None, "s-old", "  Holiday\x00   plans "))
    assert r == {"sid": "s-old", "title": "Holiday plans"}  # cleaned by Hermes' own rule
    assert env.db.rows["s-old"]["title"] == "Holiday plans"
    # The session the chat is in can be renamed too.
    assert asyncio.run(env.compat.sessions_rename(env.runner, None, "s-now", "Now"))["title"] == "Now"


def test_a_name_hermes_refuses_is_a_bad_request_with_its_reason(env):
    code, msg = refused(env, env.compat.sessions_rename(env.runner, None, "s-old", "Taxes"))
    assert code == "bad_request" and "already in use" in msg
    code, msg = refused(env, env.compat.sessions_rename(env.runner, None, "s-old", "x" * 101))
    assert code == "bad_request" and "too long" in msg
    for empty in ("", "   ", "\x00", None, 7):
        assert refused(env, env.compat.sessions_rename(env.runner, None, "s-old", empty))[0] == "bad_request"
    assert env.db.rows["s-old"]["title"] is None


def test_only_this_chats_sessions_can_be_touched(env):
    for sid in ("s-else", "nope", "", None):
        assert refused(env, env.compat.sessions_rename(env.runner, None, sid, "Mine"))[0] == "not_found"
        assert refused(env, env.compat.sessions_delete(env.runner, None, sid))[0] == "not_found"
    assert env.db.rows["s-else"]["title"] == "Telegram" and env.db.delete_calls == []


def test_a_deleted_session_is_gone_with_its_routing(env):
    r = asyncio.run(env.compat.sessions_delete(env.runner, None, "s-old"))
    assert r == {"sid": "s-old"} and "s-old" not in env.db.rows
    # Hermes' own delete: transcripts scrubbed, a running turn respected, compressed copies taken along.
    assert env.db.delete_calls == [("s-old", {"sessions_dir": env.home / "sessions", "guards": True, "chain": True})]
    assert env.runner.removed == ["s-old"]  # or the next message would bring it back


def test_the_session_you_are_in_stays(env):
    for sid in ("s-now", "s-copy"):  # the current one, and an older copy of the same chat
        code, msg = refused(env, env.compat.sessions_delete(env.runner, None, sid))
        assert code == "bad_request" and "You are in this session" in msg
    assert env.db.delete_calls == [] and env.runner.removed == []


def test_a_session_hermes_is_working_in_stays(env):
    env.db.busy.add("s-old")
    code, msg = refused(env, env.compat.sessions_delete(env.runner, None, "s-old"))
    assert code == "unavailable" and "working" in msg
    assert "s-old" in env.db.rows and env.runner.removed == []


def test_an_older_hermes_gets_only_the_arguments_it_knows(env, monkeypatch):
    db = OldDB()
    monkeypatch.setattr(env.compat, "_session_db", lambda: db)
    monkeypatch.setattr(env.compat, "_open_writer", lambda path: db)
    assert asyncio.run(env.compat.sessions_delete(env.runner, None, "s-taken")) == {"sid": "s-taken"}
    assert db.delete_calls == [("s-taken", {"sessions_dir": env.home / "sessions"})]


def test_a_writer_that_will_not_open_is_unavailable(env, monkeypatch):
    def broken(path):
        raise OSError("disk says no")
    monkeypatch.setattr(env.compat, "_open_writer", broken)
    assert refused(env, env.compat.sessions_rename(env.runner, None, "s-old", "X"))[0] == "unavailable"
    assert refused(env, env.compat.sessions_delete(env.runner, None, "s-old"))[0] == "unavailable"
    assert env.db.rows["s-old"]["title"] is None and env.runner.removed == []


def test_each_change_opens_its_own_writer_and_hands_it_back(env):
    asyncio.run(env.compat.sessions_rename(env.runner, None, "s-old", "A"))
    asyncio.run(env.compat.sessions_delete(env.runner, None, "s-old"))
    assert env.opened == [None, None] and env.db.closed == 2  # the main agent's database, twice
    # Refused changes hand it back too.
    refused(env, env.compat.sessions_rename(env.runner, None, "s-taken", "Today"))
    env.db.busy.add("s-taken")
    refused(env, env.compat.sessions_delete(env.runner, None, "s-taken"))
    assert env.db.closed == 4


def test_a_bots_sessions_are_written_in_the_bots_own_database(env, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home_override", lambda: str(env.home / "bot"))
    asyncio.run(env.compat.sessions_rename(env.runner, None, "s-old", "A"))
    assert env.opened == [env.home / "bot" / "state.db"]


def test_the_writer_is_hermes_shared_one_or_a_plain_one(env, monkeypatch):
    compat = importlib.import_module("plugin.compat")
    monkeypatch.undo()  # the real _open_writer
    calls = []
    reg = types.ModuleType("hermes_state_registry")
    reg.acquire = lambda path: calls.append(("shared", path)) or "shared-db"
    state = types.ModuleType("hermes_state")
    state.SessionDB = lambda **kw: calls.append(("plain", kw)) or "plain-db"
    monkeypatch.setitem(sys.modules, "hermes_state", state)
    monkeypatch.setitem(sys.modules, "hermes_state_registry", reg)
    assert compat._open_writer(None) == "shared-db" and compat._open_writer(env.home / "state.db") == "shared-db"
    monkeypatch.setitem(sys.modules, "hermes_state_registry", None)  # a Hermes without the registry
    assert compat._open_writer(None) == "plain-db" and compat._open_writer(env.home / "state.db") == "plain-db"
    assert calls == [("shared", None), ("shared", env.home / "state.db"),
                     ("plain", {}), ("plain", {"db_path": env.home / "state.db"})]  # never read-only


def _gone(self):
    raise AttributeError("hermes_state.SessionDB has no such attribute")


class NewDB(FakeDB):
    """Hermes 0.23.3 and later: the storage interface answers what these three `SessionDB` methods did."""
    resolve_resume_session_id = property(_gone)
    sanitize_title = property(_gone)
    get_session_title = property(_gone)


def test_a_hermes_with_the_storage_interface_renames_and_deletes(env, monkeypatch):
    db = NewDB()
    monkeypatch.setattr(env.compat, "_session_db", lambda: db)
    monkeypatch.setattr(env.compat, "_open_writer", lambda path: db)
    read: list = []  # the Hermes home each reader was opened on

    @contextlib.contextmanager
    def open_persistence_reader(home):
        read.append(home)
        yield types.SimpleNamespace(lineage=types.SimpleNamespace(
            resolve_resume_target=lambda sid: db.tips.get(sid, sid)))

    assembly = types.ModuleType("persistence_assembly")
    assembly.open_persistence_reader = open_persistence_reader
    rules = types.ModuleType("persistence.conversation.sessions")
    rules.sanitize_title = lambda title: FakeDB.sanitize_title(db, title)
    monkeypatch.setitem(sys.modules, "persistence_assembly", assembly)
    for name in ("persistence", "persistence.conversation"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "persistence.conversation.sessions", rules)

    r = asyncio.run(env.compat.sessions_rename(env.runner, None, "s-old", "  Holiday\x00   plans "))
    assert r == {"sid": "s-old", "title": "Holiday plans"}
    assert refused(env, env.compat.sessions_rename(env.runner, None, "s-old", "x" * 101))[0] == "bad_request"
    # An older copy of the chat you are in is still the chat you are in.
    assert refused(env, env.compat.sessions_delete(env.runner, None, "s-copy"))[0] == "bad_request"
    assert asyncio.run(env.compat.sessions_delete(env.runner, None, "s-old")) == {"sid": "s-old"}
    assert "s-old" not in db.rows and read and set(read) == {env.home}
    # A bot's sessions are read in the bot's own home.
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home_override", lambda: str(env.home / "bot"))
    asyncio.run(env.compat.sessions_delete(env.runner, None, "s-taken"))
    assert read[-1] == env.home / "bot"


def test_the_requests_reach_the_backend(env, adapter_mod, monkeypatch):  # noqa: F811
    seen = []

    async def rename(runner, source, sid, title):
        seen.append(("rename", sid, title))
        return {"sid": sid, "title": title}

    async def delete(runner, source, sid):
        seen.append(("delete", sid))
        return {"sid": sid}

    monkeypatch.setattr(env.compat, "sessions_rename", rename)
    monkeypatch.setattr(env.compat, "sessions_delete", delete)
    ad = types.SimpleNamespace(source_for=lambda device, agent: None)
    backend = adapter_mod._Backend(ad)
    monkeypatch.setattr(backend, "_runner", lambda: env.runner, raising=False)
    device = types.SimpleNamespace(fp="fp1", name="3DS")
    r = asyncio.run(backend.handle(device, {"t": "sessions.rename", "id": 1, "sid": "s-old", "title": "A"}))
    assert r == {"t": "sessions.rename_r", "sid": "s-old", "title": "A"}
    r = asyncio.run(backend.handle(device, {"t": "sessions.delete", "id": 2, "sid": "s-old"}))
    assert r == {"t": "sessions.delete_r", "sid": "s-old"}
    assert seen == [("rename", "s-old", "A"), ("delete", "s-old")]
