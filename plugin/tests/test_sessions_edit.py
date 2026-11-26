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


# --- Hermes 0.23.3 and later: the storage interface ----------------------------------------------------------

class SessionWriteGuarded(RuntimeError):
    pass


class Row:
    """A record of the storage interface: the dict the old class returned, under one of its `as_..._row` names."""

    def __init__(self, data):
        self.as_session_row = self.as_list_row = self.as_wire_row = lambda: dict(data)


class FakeStore:
    """One handle on a profile's store, over the same rows as `FakeDB`."""

    def __init__(self, db, home, log, read_only):
        self.db, self.scope, self.log, self.read_only = db, types.SimpleNamespace(profile_dir=home), log, read_only
        self.sessions = self.lineage = self.messages = self
        self.closed = False

    def _use(self):
        assert not self.closed, "a handle used after close"
        if self.db.broken:
            raise OSError("the store went away")

    def get(self, sid):
        self._use()
        row = self.db.rows.get(sid)
        return Row(row) if row else None

    def list_sessions(self, query, window):
        self._use()
        self.log.append(("list", query, window))
        return [Row(r) for r in self.db.rows.values() if r["session_key"] == query.session_key]

    def resolve_resume_target(self, sid):
        self._use()
        return self.db.tips.get(sid, sid)

    def read_transcript(self, sid, request):
        self._use()
        self.log.append(("read", sid, request))
        return [Row({"id": 1, "role": "user", "content": f"hello from {sid}", "timestamp": 5.0})]

    def set_title(self, sid, title):
        assert not self.read_only
        return self.db.set_session_title(sid, title)

    def delete(self, sid, **kw):
        assert not self.read_only
        self.log.append(("delete", sid, kw))
        if sid in self.db.busy:
            raise SessionWriteGuarded("a turn is writing")
        return (sid,) if self.db.rows.pop(sid, None) is not None else ()

    def close(self):
        self.closed = True
        self.log.append(("closed", "reader" if self.read_only else "writer"))


@pytest.fixture
def new(env, monkeypatch):
    """`env` on a Hermes with the storage interface. `hermes_state` is gone there: using it fails the test."""
    db, log = env.db, []
    db.broken = False

    def no_legacy(*a, **k):
        raise AssertionError("hermes_state must not be used where the storage interface exists")
    monkeypatch.setattr(env.compat, "_session_db", no_legacy)
    monkeypatch.setattr(env.compat, "_open_writer", no_legacy)
    monkeypatch.setattr(env.compat, "_media_root", lambda: "media-root")
    monkeypatch.setattr(env.compat, "_readers", {})

    @contextlib.contextmanager
    def open_persistence_reader(home):
        log.append(("reader", home))
        store = FakeStore(db, home, log, True)
        try:
            yield store
        finally:
            store.close()

    def open_persistence(scope):
        log.append(("writer", scope.profile_dir))
        return FakeStore(db, scope.profile_dir, log, False)

    def module(name, **names):
        mod = types.ModuleType(name)
        vars(mod).update(names)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("persistence_assembly", open_persistence=open_persistence, open_persistence_reader=open_persistence_reader,
           profile_scope=lambda home: types.SimpleNamespace(profile_dir=home))
    module("persistence")
    module("persistence.conversation")
    module("persistence.conversation.sessions", sanitize_title=lambda title: FakeDB.sanitize_title(db, title),
           SessionOrder=types.SimpleNamespace(LAST_ACTIVE="last_active", STARTED_AT="started_at"),
           SessionQuery=lambda **kw: types.SimpleNamespace(**kw),
           SessionWindow=lambda **kw: types.SimpleNamespace(**kw))
    module("persistence.conversation.messages", TranscriptRead=lambda **kw: types.SimpleNamespace(**kw))
    module("session_retention", after_sessions_removed=lambda store, home, removed, **kw: log.append(
        ("after", home, tuple(removed), kw)))
    env.log = log
    env.source = types.SimpleNamespace(platform=types.SimpleNamespace(value="3ds"))
    return env


def test_storage_interface_reads(new):
    items = asyncio.run(new.compat.sessions_list(new.runner, new.source, 0, 20, " tax "))
    assert {i["id"] for i in items} == {"s-now", "s-old", "s-taken", "s-copy"}  # this chat's lane only
    assert [i["id"] for i in items if i["current"]] == ["s-now"]
    query, window = next((e[1], e[2]) for e in new.log if e[0] == "list")
    assert (query.sources, query.session_key, query.search_text, query.order) == (("3ds",), KEY, "tax", "last_active")
    assert (window.limit, window.offset) == (20, 0)
    # An older copy of a compressed chat reads on into its newest copy.
    msgs = asyncio.run(new.compat.session_messages(new.runner, new.source, "s-copy", 50, None))
    assert [m["text"] for m in msgs] == ["hello from s-now"]
    sid, request = next((e[1], e[2]) for e in new.log if e[0] == "read")
    assert sid == "s-now" and (request.limit, request.latest, request.include_lineage) == (500, True, True)
    assert refused(new, new.compat.session_messages(new.runner, new.source, "s-else", 50, None))[0] == "not_found"
    # One read-only handle serves every read, and nothing was opened for writing.
    assert [e for e in new.log if e[0] in ("reader", "writer", "closed")] == [("reader", new.home)]


def test_storage_interface_renames(new):
    r = asyncio.run(new.compat.sessions_rename(new.runner, None, "s-old", "  Holiday\x00   plans "))
    assert r == {"sid": "s-old", "title": "Holiday plans"} and new.db.rows["s-old"]["title"] == "Holiday plans"
    code, msg = refused(new, new.compat.sessions_rename(new.runner, None, "s-old", "Taxes"))
    assert code == "bad_request" and "already in use" in msg
    assert refused(new, new.compat.sessions_rename(new.runner, None, "s-old", "x" * 101))[0] == "bad_request"
    assert refused(new, new.compat.sessions_rename(new.runner, None, "s-else", "Mine"))[0] == "not_found"
    # Each change took its own writable handle on the main agent's store and gave it back.
    opened = [e for e in new.log if e[0] == "writer"]
    assert opened == [("writer", new.home)] * 3 and new.log.count(("closed", "writer")) == 3


def test_storage_interface_deletes(new):
    for sid in ("s-now", "s-copy"):  # the chat you are in, and its copy from before a compression
        assert refused(new, new.compat.sessions_delete(new.runner, None, sid))[0] == "bad_request"
    new.db.busy.add("s-taken")
    code, msg = refused(new, new.compat.sessions_delete(new.runner, None, "s-taken"))
    assert code == "unavailable" and "working" in msg and "s-taken" in new.db.rows and new.runner.removed == []
    assert asyncio.run(new.compat.sessions_delete(new.runner, None, "s-old")) == {"sid": "s-old"}
    assert "s-old" not in new.db.rows and new.runner.removed == ["s-old"]
    assert ("delete", "s-old", {"refuse_guarded": True, "include_compression_chain": True,
                                "media_root": "media-root"}) in new.log
    # Hermes' own work after a delete ran for it, with the transcript folder.
    assert ("after", new.home, ("s-old",), {"reason": "delete", "primary": "s-old",
                                            "sessions_dir": new.home / "sessions"}) in new.log
    assert refused(new, new.compat.sessions_delete(new.runner, None, "s-old"))[0] == "not_found"
    assert new.log.count(("writer", new.home)) == new.log.count(("closed", "writer")) == 2


def test_storage_interface_reads_and_writes_a_bots_own_store(new, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home_override", lambda: str(new.home / "bot"))
    asyncio.run(new.compat.sessions_rename(new.runner, None, "s-old", "A"))
    assert [e for e in new.log if e[0] in ("reader", "writer")] == [("reader", new.home / "bot"),
                                                                    ("writer", new.home / "bot")]


def test_a_read_handle_that_fails_is_replaced(new):
    status = lambda: asyncio.run(new.compat.status(new.runner, None, False))  # noqa: E731
    assert status()["session"]["title"] == "Today"
    new.db.broken = True
    assert refused(new, new.compat.status(new.runner, None, False))[0] == "unavailable"
    assert ("closed", "reader") in new.log  # the failed handle was closed, not kept
    new.db.broken = False
    assert status()["session"]["title"] == "Today"
    assert new.log.count(("reader", new.home)) == 2


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


def test_the_welcome_gives_hermes_its_real_version(adapter_mod, monkeypatch):  # noqa: F811
    import importlib.metadata as metadata
    info = types.ModuleType("hermes_cli.version_info")
    info.hermes_user_agent = lambda product: f"{product}/0.23.3"
    monkeypatch.setitem(sys.modules, "hermes_cli.version_info", info)
    assert adapter_mod._hermes_version() == "0.23.3"
    # An older Hermes has no such helper, and a source install's metadata only says 0.0.0: better nothing.
    monkeypatch.delattr(info, "hermes_user_agent")
    monkeypatch.setattr(metadata, "version", lambda name: "0.0.0")
    assert adapter_mod._hermes_version() == ""
    monkeypatch.setattr(metadata, "version", lambda name: "0.22.5")
    assert adapter_mod._hermes_version() == "0.22.5"
