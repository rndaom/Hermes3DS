"""Every read of Hermes' state lives here (protocol.md §4.6), so upstream churn breaks one file.

Only public Hermes surfaces are used (the plugin catalog's rule 9): for sessions, Hermes' storage interface
(`persistence_assembly.open_persistence_reader` for reads, `open_persistence` for each rename or delete, and the
query types of `persistence.conversation`) on Hermes 0.23.3 and later, and `hermes_state.SessionDB` (read-only; a
writer borrowed from `hermes_state_registry` for each change) on a Hermes from before it; the runner's
session store and served-profile list, `hermes_cli.config`, `tools.skills_tool`, `tools.registry`, the
`cronjob_manage` tool through the plugin context, and `hermes_constants` / `agent.secret_scope` to read another
profile's state (a bot's) under that profile's home and secrets, as Hermes' own multiplexed gateway does. Where Hermes has no public API yet (its command catalog, the model picker's provider
inventory, toolset labels) the 3DS gets a simpler view built from those.

Each function runs blocking Hermes code via asyncio.to_thread where needed and returns plain JSON-able
data for the 3DS. Failures raise RequestError("unavailable", ...) instead of crashing the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .h3proto import avatar
from .h3proto.emoji import emoji_for_3ds
from .h3proto.server import RequestError

log = logging.getLogger("hermes3ds")

_ctx: Any = None  # the PluginContext register() got; tools are dispatched through it
_dbs: dict[str, Any] = {}  # Hermes before 0.23.3: read-only SessionDBs, "" = the main agent's, else a bot's state.db path
_readers: dict[str, tuple[Any, Any]] = {}  # Hermes home -> (read-only storage interface handle, what closes it)
_db_lock = threading.Lock()


def bind(ctx: Any) -> None:
    global _ctx
    _ctx = ctx


def _guard(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except RequestError:
            raise
        except Exception as exc:
            log.warning("3ds compat %s failed: %s", fn.__name__, exc, exc_info=True)
            raise RequestError("unavailable", f"{fn.__name__.lstrip('_')} unavailable on this Hermes version") from exc
    return wrapper


def _store_home() -> Path:
    """The Hermes home of the profile in scope (the main agent's, or a bot's inside `profile_scope`). Read on the
    event loop: the scope is what picks it."""
    from hermes_constants import get_hermes_home, get_hermes_home_override
    return Path(get_hermes_home_override() or get_hermes_home())


# --- Hermes' sessions, behind one small set of calls ------------------------------------------------------
#
# Hermes 0.23.3 moved sessions behind its storage interface and keeps `hermes_state` only as a stand-in that
# goes away in 0.27. `_StoreSessions` speaks the interface; `_LegacySessions` speaks `SessionDB` for a Hermes
# from before it. Both return the plain dicts `SessionDB` always returned.

MESSAGES_READ = 500  # the newest rows of a chat read for its history


def _storage_interface() -> bool:
    """Whether this Hermes has the storage interface (0.23.3 and later)."""
    try:
        import persistence_assembly
    except ImportError:
        return False
    return hasattr(persistence_assembly, "open_persistence") and hasattr(persistence_assembly, "open_persistence_reader")


class _StoreSessions:
    """Sessions through a storage interface handle."""

    def __init__(self, store):
        self.store = store

    def get_session(self, sid: str) -> dict | None:
        record = self.store.sessions.get(sid)
        return None if record is None else record.as_session_row()

    def list_sessions(self, *, source: str, session_key: str, search: str | None, limit: int, offset: int) -> list[dict]:
        from persistence.conversation.sessions import SessionOrder, SessionQuery, SessionWindow
        query = SessionQuery(sources=(source,), session_key=session_key, search_text=search,
                             order=SessionOrder.LAST_ACTIVE if search else SessionOrder.STARTED_AT)
        return [entry.as_list_row() for entry in
                self.store.sessions.list_sessions(query, SessionWindow(limit=max(limit, 0), offset=max(offset, 0)))]

    def resume_target(self, sid: str) -> str | None:
        return self.store.lineage.resolve_resume_target(sid)

    def messages(self, sid: str) -> list[dict]:
        from persistence.conversation.messages import TranscriptRead
        request = TranscriptRead(limit=MESSAGES_READ, latest=True, include_lineage=True)
        return [row.as_wire_row() for row in self.store.messages.read_transcript(sid, request)]

    def clean_title(self, title: str) -> str | None:
        from persistence.conversation.sessions import sanitize_title
        return sanitize_title(title)

    def set_title(self, sid: str, title: str) -> bool:
        return bool(self.store.sessions.set_title(sid, title))

    def delete(self, sid: str, sessions_dir: Path) -> bool:
        """As Hermes' own delete (its API server's): refused while a turn is writing to the session, the chat's
        compressed copies go with it, then Hermes' work after a delete (its hook, media nothing holds any more,
        the transcript files)."""
        removed = self.store.sessions.delete(sid, refuse_guarded=True, include_compression_chain=True,
                                             media_root=_media_root())
        # Not one of the plugin guide's names: it is what removes the deleted chat's transcript files and tells
        # other plugins (`on_session_delete`), and Hermes' own delete calls it right after the same `delete`.
        with contextlib.suppress(Exception):
            from session_retention import after_sessions_removed
            after_sessions_removed(self.store, self.store.scope.profile_dir, removed, reason="delete", primary=sid,
                                   sessions_dir=sessions_dir)
        return bool(removed)

    def collect_images(self) -> None:
        from gateway.session_ingress_media import collect_unheld_api_images
        collect_unheld_api_images(self.store)


def _media_root() -> str:
    """Where the profile in scope keeps the files its sessions hold, which a delete removes with them (the place
    Hermes' session storage guide gives)."""
    from hermes_constants import get_hermes_dir
    return str((get_hermes_dir("cache/documents", "document_cache") / "native-inputs").resolve())


class _LegacySessions:
    """Sessions through `hermes_state.SessionDB` (Hermes before 0.23.3)."""

    def __init__(self, db):
        self.db = db

    def get_session(self, sid: str) -> dict | None:
        return self.db.get_session(sid)

    def list_sessions(self, *, source: str, session_key: str, search: str | None, limit: int, offset: int) -> list[dict]:
        return self.db.list_sessions_rich(source=source, session_key=session_key, search_query=search,
                                          order_by_last_active=bool(search), limit=limit, offset=offset)

    def resume_target(self, sid: str) -> str | None:
        return self.db.resolve_resume_session_id(sid)

    def messages(self, sid: str) -> list[dict]:
        return self.db.get_messages(sid, latest=True, include_ancestors=True, limit=MESSAGES_READ)

    def clean_title(self, title: str) -> str | None:
        return self.db.sanitize_title(title)

    def set_title(self, sid: str, title: str) -> bool:
        return bool(self.db.set_session_title(sid, title))

    def delete(self, sid: str, sessions_dir: Path) -> bool:
        # Newer ones refuse while a turn is writing to it, and take the compressed copies of the chat with it.
        accepted = inspect.signature(self.db.delete_session).parameters
        kwargs = {k: v for k, v in (("sessions_dir", sessions_dir), ("exclude_active_write_guards", True),
                                    ("include_compression_chain", True)) if k in accepted}
        return bool(self.db.delete_session(sid, **kwargs))

    def collect_images(self) -> None:
        from gateway.session_ingress_media import collect_unheld_api_images
        collect_unheld_api_images(self.db)


def _session_db():
    """Hermes before 0.23.3: the `SessionDB` of the profile in scope, opened read-only once (the gateway keeps its
    own writer)."""
    from hermes_constants import get_hermes_home_override
    override = get_hermes_home_override()
    key = str(Path(override) / "state.db") if override else ""
    with _db_lock:
        db = _dbs.get(key)
        if db is None:
            from hermes_state import SessionDB
            db = _dbs[key] = SessionDB(db_path=Path(key), read_only=True) if key else SessionDB(read_only=True)
        return db


def _open_reader(home: Path):
    """A read-only storage interface handle on a profile's store, and what closes it."""
    from persistence_assembly import open_persistence_reader
    stack = contextlib.ExitStack()
    try:
        return stack.enter_context(open_persistence_reader(home)), stack
    except BaseException:
        stack.close()
        raise


def _drop_reader(home: Path, stack) -> None:
    with _db_lock:
        if (_readers.get(str(home)) or (None, None))[1] is stack:
            del _readers[str(home)]
    with contextlib.suppress(Exception):
        stack.close()


def _read(home: Path, fn):
    """`fn(sessions)` on the store of the profile whose home is `home`. Runs off the event loop. The read-only handle
    is kept between calls; one that fails is closed, so the next read opens a fresh one."""
    if not _storage_interface():
        return fn(_LegacySessions(_session_db()))
    with _db_lock:
        held = _readers.get(str(home))
    if held is None:
        opened = _open_reader(home)
        with _db_lock:
            held = _readers.setdefault(str(home), opened)
        if held is not opened:  # another thread opened one first
            with contextlib.suppress(Exception):
                opened[1].close()
    try:
        return fn(_StoreSessions(held[0]))
    except RequestError:
        raise
    except Exception:
        _drop_reader(home, held[1])
        raise


async def _reading(fn):
    """`_read` for the profile in scope, off the event loop."""
    return await asyncio.to_thread(_read, _store_home(), fn)


def _config() -> dict[str, Any]:
    from hermes_cli.config import load_config
    cfg = load_config()
    return cfg if isinstance(cfg, dict) else {}


def _ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(getattr(value, "timestamp")())
    except Exception:
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(value)).timestamp()
        except Exception:
            return None


def _clip(text: Any, n: int) -> str:
    s = "" if text is None else emoji_for_3ds(str(text))
    return s if len(s) <= n else s[: n - 1] + "…"


# --- agents: Hermes profiles, which Bot Mode shows as bots (protocol.md §4.8) -----------------------------

PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # hermes_constants.PROFILE_ID_RE
DESC_MAX = 400
CREATE_TIMEOUT = 180.0  # seconds for `hermes profile create` (it copies the skill tree)
RESERVED_NAMES = frozenset({"hermes", "default", "main", "test", "tmp", "root", "sudo"})  # + hermes_cli.profiles'
RETRY_WINDOW = 600.0    # a create asked again this soon after it succeeded gets the same bot back
_creating: set[str] = set()
_created: dict[str, float] = {}  # profile -> when this gateway made it (time.monotonic)


def multiplexed(runner) -> bool:
    """Whether this gateway serves several profiles (`gateway.multiplex_profiles`, Hermes' default). Without it
    the gateway runs one profile, and a bot can only be reached through its own gateway."""
    return bool(getattr(getattr(runner, "config", None), "multiplex_profiles", False))


def served_agents(runner) -> list[str]:
    """Profiles this gateway runs turns for, the main one first (`GatewayRunner.served_profile_names`)."""
    names: list[str] = []
    with contextlib.suppress(Exception):
        names = [str(n) for n in runner.served_profile_names() if n]
    names = names or ["default"]
    return names if multiplexed(runner) else names[:1]


def agent_home(runner, name: str) -> Path:
    """A served profile's Hermes home: the gateway's own for the main agent, else the Hermes root for `default`
    and `<root>/profiles/<name>` for the rest. (The root is not the gateway's home when the main agent is itself
    a named profile: `hermes -p work gateway run`.)"""
    from hermes_constants import get_default_hermes_root, get_process_hermes_home
    if name == served_agents(runner)[0]:
        return Path(get_process_hermes_home())
    root = Path(get_default_hermes_root())
    return root if name == "default" else root / "profiles" / name


@contextlib.asynccontextmanager
async def profile_scope(home: Path | None):
    """Read a bot's state as that bot: its home (config, sessions, skills, cron) and its own secrets, through the
    same context-local overrides Hermes' multiplexer puts around a routed turn. `None`: the main agent, as is.
    Context variables, so `asyncio.to_thread` work started inside follows them. The secrets stay in Hermes' scope:
    the plugin never reads them itself; Hermes calls made inside (models, skills, cron) resolve that profile's
    settings instead of the main profile's."""
    if home is None:
        yield
        return
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    secrets = await asyncio.to_thread(build_profile_secret_scope, home)  # reads files: not on the event loop
    home_token = set_hermes_home_override(str(home))
    secret_token = set_secret_scope(secrets, profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


@functools.lru_cache(maxsize=1)
def _yaml_loader():
    """Hermes's own YAML reader (`hermes_yaml`, in every Hermes this plugin supports). PyYAML is only a fallback
    for running outside Hermes (the tests): Hermes does not ship it. None, said once in the log, if neither is
    there, so a bot roster without titles or pictures is never a silent mystery."""
    try:
        from hermes_yaml import safe_load
        return safe_load
    except ImportError:
        pass
    try:
        from yaml import safe_load
        return safe_load
    except ImportError:
        log.warning("3ds: no YAML reader (hermes_yaml or PyYAML): bots show without their titles, "
                    "descriptions, models and pictures")
        return None


def _yaml(path: Path) -> dict:
    load = _yaml_loader()
    if load is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}  # a profile without this file is ordinary
    try:
        data = load(text)
    except Exception as exc:  # a broken file: say which, keep the roster working
        log.warning("3ds: could not read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _pretty(name: str) -> str:
    """`research-buddy` → `Research Buddy`, for a profile nobody gave a title."""
    return " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-_]+", name) if w) or name


def _agent_info(name: str, home: Path, primary: bool) -> dict[str, Any]:
    """What the roster shows: the Bot Mode title (`profile.yaml` ui_meta), else the display name, else the id;
    the role description; the configured model. `_look` is what its picture is drawn from (`avatar.look`), for
    the adapter to turn into a blob; it never goes on the wire. Blocking (reads two YAML files)."""
    meta = _yaml(home / "profile.yaml")
    bots = (meta.get("ui_meta") or {}).get("hermes-bots") if isinstance(meta.get("ui_meta"), dict) else None
    title = str((bots or {}).get("title") or "").strip() if isinstance(bots, dict) else ""
    title = title or str(meta.get("display_name") or "").strip() or ("Hermes" if primary else _pretty(name))
    cfg = _yaml(home / "config.yaml")
    model, provider = _current_model(cfg)
    return {"id": name, "title": _clip(title, 48), "desc": _clip(str(meta.get("description") or "").strip(), DESC_MAX),
            "model": _clip(model, 60), "provider": provider, "primary": primary, "bot": isinstance(bots, dict),
            "_look": None if primary else avatar.look(name, home, bots if isinstance(bots, dict) else None)}


@_guard
async def agents_list(runner) -> dict[str, Any]:
    names = served_agents(runner)

    def build():
        return [_agent_info(n, agent_home(runner, n), i == 0) for i, n in enumerate(names)]

    return {"items": await asyncio.to_thread(build), "multiplex": multiplexed(runner)}


def _hermes_cli() -> str | None:
    """The `hermes` entrypoint of this Hermes install (beside its Python), else the one on PATH."""
    exe = Path(sys.executable).with_name("hermes.exe" if os.name == "nt" else "hermes")
    return str(exe) if exe.is_file() else shutil.which("hermes")


def slug(text: str) -> str:
    """A profile id from what was typed: `Research Buddy!` → `research-buddy`."""
    s = re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-_")
    return re.sub(r"-{2,}", "-", s)[:64].rstrip("-_")


@_guard
async def agents_create(runner, name: str, desc: str) -> dict[str, Any]:
    """A new bot: `hermes profile create <name> --clone-from <main> --no-alias [--description …]`, the CLI twin of
    Bot Mode's New Agent with "Copy API keys" on (config, keys, SOUL, skills, plugins and memory notes come along;
    chats, cron jobs and messaging channels do not). A clone has no `platforms:` section, so the 3ds platform is
    switched back on in it (`hermes -p <name> config set platforms.3ds.enabled true`): without it the bot's copy of
    this plugin never starts and the 3DS can't reach it. Then the gateway is asked to serve it at once."""
    if not multiplexed(runner):
        raise RequestError("unavailable", "this gateway serves one profile (gateway.multiplex_profiles is off)")
    if not PROFILE_ID.match(name):
        raise RequestError("bad_request", "a bot name uses a-z, 0-9, - and _")
    if name in RESERVED_NAMES:
        raise RequestError("bad_request", f"{name} is reserved by Hermes: pick another name")
    names = served_agents(runner)
    made = _created.get(name)
    if made is not None and time.monotonic() - made < RETRY_WINDOW:  # the 3DS lost the reply and asks again
        return await asyncio.to_thread(_agent_info, name, agent_home(runner, name), False)
    if name in names or agent_home(runner, name).exists():
        raise RequestError("bad_request", f"there is already a profile called {name}")
    if name in _creating:
        raise RequestError("bad_request", f"{name} is already being made")
    if _creating:  # one at a time: each is a `hermes` process of up to minutes on the gateway's shared threads
        raise RequestError("unavailable", "another bot is being made: try again in a moment")
    cli = _hermes_cli()
    if cli is None:
        raise RequestError("unavailable", "the hermes command was not found on the PC")
    args = [cli, "profile", "create", name, "--clone-from", names[0], "--no-alias"]
    if desc:
        args.append("--description=" + desc[:DESC_MAX])  # one argument even when it starts with "-"

    def run(argv):
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CREATE_TIMEOUT)

    def last_line(out, what):
        lines = [ln.strip() for ln in (out.stderr or out.stdout or "").splitlines() if ln.strip()]
        return _clip(lines[-1] if lines else f"{what} failed ({out.returncode})", 200)

    _creating.add(name)
    try:
        out = await asyncio.to_thread(run, args)
    except subprocess.TimeoutExpired:
        raise RequestError("unavailable", "creating the profile took too long")
    finally:
        _creating.discard(name)
    if out.returncode != 0:
        raise RequestError("unavailable", last_line(out, "hermes profile create"))
    _created[name] = time.monotonic()
    log.info("3DS: created profile %s", name)
    try:
        out = await asyncio.to_thread(run, [cli, "-p", name, "config", "set", "platforms.3ds.enabled", "true"])
        if out.returncode != 0:
            log.warning("3DS: could not turn on the 3ds platform in %s: %s", name, last_line(out, "config set"))
    except subprocess.TimeoutExpired:
        log.warning("3DS: turning on the 3ds platform in %s took too long", name)
    reconcile = getattr(runner, "reconcile_served_profiles", None)
    if callable(reconcile):  # the gateway also rescans on its own; this makes the new bot reachable right away
        with contextlib.suppress(Exception):
            await reconcile(reason="3ds-create")
    return await asyncio.to_thread(_agent_info, name, agent_home(runner, name), False)


# --- sessions -------------------------------------------------------------------

async def current_session(runner, source) -> dict[str, Any]:
    entry = await runner.async_session_store.get_or_create_session(source)
    return {"id": entry.session_id, "key": entry.session_key}


@_guard
async def status(runner, source, busy: bool, queued: int = 0) -> dict[str, Any]:
    """`queued` is the adapter's count of messages waiting behind the current turn."""
    cur = await current_session(runner, source)
    row = await _reading(lambda s: s.get_session(cur["id"])) or {}
    title = row.get("title")
    model = await _live_model(runner, cur["key"]) or row.get("model") or ""
    return {
        "session": {
            "id": cur["id"], "title": emoji_for_3ds(title or ""), "model": model,
            "started": _ts(row.get("started_at")), "msgs": row.get("message_count") or 0,
            "tokens_in": row.get("input_tokens") or 0, "tokens_out": row.get("output_tokens") or 0,
            "cost": row.get("actual_cost_usd") or row.get("estimated_cost_usd") or 0,
        },
        "busy": busy, "queued": max(0, int(queued or 0)),
    }


def _current_model(cfg: dict[str, Any]) -> tuple[str, str]:
    """`(model, provider)` from `model:` in config.yaml (a string or a dict)."""
    m = cfg.get("model")
    if isinstance(m, dict):
        return str(m.get("default") or m.get("model") or ""), str(m.get("provider") or "")
    return str(m or ""), ""


async def _override(runner, key: str) -> dict[str, Any]:
    """The session's /model override (`{model, provider, ...}`), or {}."""
    with contextlib.suppress(Exception):
        ov = await runner.async_session_store.get_model_override(key)
        if isinstance(ov, dict):
            return ov
    return {}


async def _live_model(runner, key: str) -> str:
    """The model the next turn will use: the session's /model override, else the configured default.
    The session row only records the model the session started with."""
    ov = await _override(runner, key)
    if ov.get("model"):
        return str(ov["model"])
    with contextlib.suppress(Exception):
        return _current_model(await asyncio.to_thread(_config))[0]
    return ""


@_guard
async def sessions_list(runner, source, offset: int, limit: int, q: str | None) -> list[dict]:
    cur = await current_session(runner, source)
    search = (q or "").strip() or None
    # This chat's lane only (session_key), unnamed sessions included, newest activity first when searching.
    rows = await _reading(lambda s: s.list_sessions(source=source.platform.value, session_key=cur["key"],
                                                    search=search, limit=limit, offset=offset))
    items = []
    for r in rows:
        items.append({
            "id": r.get("id"), "title": _clip(r.get("title") or "", 80), "preview": _clip(r.get("preview"), 120),
            "last_active": _ts(r.get("last_active") or r.get("started_at")),
            "msgs": r.get("message_count") or 0, "model": r.get("model") or "", "current": r.get("id") == cur["id"],
        })
    return items


def _visible(row: dict | None, key: str) -> bool:
    """Whether this chat may read a session: only sessions of its own lane, as in the session list."""
    return bool(row) and row.get("session_key") == key


@_guard
async def session_messages(runner, source, sid: str, limit: int, before: int | None) -> list[dict]:
    from .h3proto.richtext import markdown_to_rich
    cur = await current_session(runner, source)

    def read(s) -> list[dict]:
        if not _visible(s.get_session(sid), cur["key"]):
            raise RequestError("not_found", "no such session for this device")
        return s.messages(s.resume_target(sid) or sid)

    msgs = await _reading(read)
    if before is not None:
        msgs = [m for m in msgs if (m.get("id") or 0) < before]
    msgs = msgs[-limit:]
    items = []
    for m in msgs:
        role = m.get("role")
        if role not in ("user", "assistant") or m.get("display_kind") in ("hidden",):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        if not content and m.get("tool_calls"):
            names = [((tc.get("function") or {}).get("name") or tc.get("name") or "tool")
                     for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
            items.append({"mid": m.get("id"), "role": "tool", "text": ", ".join(names), "ts": _ts(m.get("timestamp"))})
            continue
        text, spans = markdown_to_rich(_clip(content, 8000)) if role == "assistant" else (_clip(content, 8000), [])
        item = {"mid": m.get("id"), "role": role, "text": text, "ts": _ts(m.get("timestamp"))}
        if spans:
            item["spans"] = spans
        items.append(item)
    return items


def _open_writer(path: Path | None):
    """Hermes before 0.23.3: a writable `SessionDB` of the plugin's own. Hermes' registry lends the one writer this
    process keeps per database (two writers on one file can race each other's checkpoints); a Hermes without it
    gets a plain one."""
    try:
        from hermes_state_registry import acquire
    except ImportError:
        from hermes_state import SessionDB
        return SessionDB(db_path=path) if path else SessionDB()
    return acquire(path)


@contextlib.contextmanager
def _writing(home: Path, main: bool):
    """The sessions of the profile whose home is `home`, writable for one change and handed back afterwards. The
    storage interface gives a handle on the store this process shares (the gateway's own writer); closing it only
    gives the handle back. The plugin's lasting handle stays read-only. `main` says the home is the main agent's."""
    if _storage_interface():
        from persistence_assembly import open_persistence, profile_scope
        handle = open_persistence(profile_scope(home=home))
        sessions = _StoreSessions(handle)
    else:
        handle = _open_writer(None if main else home / "state.db")
        sessions = _LegacySessions(handle)
    try:
        yield sessions
    finally:
        with contextlib.suppress(Exception):
            handle.close()


def _scope() -> tuple[Path, bool]:
    """`(home, main)` of the profile in scope, for `_writing`. Read on the event loop."""
    from hermes_constants import get_hermes_home_override
    return _store_home(), not get_hermes_home_override()


async def _own_session(runner, source, sid: Any) -> tuple[str, dict[str, Any]]:
    """`(sid, current)` for a session of this chat's lane; anything else is `not_found`."""
    sid = str(sid or "").strip()
    cur = await current_session(runner, source)
    row = await _reading(lambda s: s.get_session(sid)) if sid else None
    if not _visible(row, cur["key"]):
        raise RequestError("not_found", "No such session. Refresh the list.")
    return sid, cur


@_guard
async def sessions_rename(runner, source, sid: Any, title: Any) -> dict:
    """Name a session, as `/title` does for the current one (Hermes' rules: cleaned, at most 100 characters,
    no two sessions with one name)."""
    sid, _ = await _own_session(runner, source, sid)
    if not isinstance(title, str):
        raise RequestError("bad_request", "a session needs a name")

    def write(home: Path, main: bool) -> tuple[str, bool]:
        with _writing(home, main) as sessions:
            clean = sessions.clean_title(title)
            if not clean:
                raise RequestError("bad_request", "The name is empty.")
            return clean, sessions.set_title(sid, clean)

    try:
        clean, found = await asyncio.to_thread(write, *_scope())
    except ValueError as exc:  # too long, taken by another session, a bot's own chat
        raise RequestError("bad_request", _clip(str(exc), 200)) from exc
    if not found:
        raise RequestError("not_found", "No such session. Refresh the list.")
    return {"sid": sid, "title": _clip(clean, 80)}


# A session a turn is writing to: `SessionActiveWriteGuardError` in `hermes_state`, `SessionWriteGuarded` in the
# storage interface.
_BUSY = frozenset({"SessionActiveWriteGuardError", "SessionWriteGuarded"})


@_guard
async def sessions_delete(runner, source, sid: Any) -> dict:
    """Delete a session and its messages for good, the way Hermes' own delete does (api_server): the rows, the
    chat's routing entries (or the next message would bring the session back), and images nothing holds any more.
    Never the session the chat is in."""
    from hermes_constants import get_hermes_home
    sid, cur = await _own_session(runner, source, sid)
    tip, cur_tip = await _reading(lambda s: (s.resume_target(sid), s.resume_target(cur["id"])))
    if sid == cur["id"] or (tip or sid) == (cur_tip or cur["id"]):
        raise RequestError("bad_request", "You are in this session. Start or open another one first.")
    sessions_dir = Path(get_hermes_home()) / "sessions"

    def write(home: Path, main: bool) -> bool:
        with _writing(home, main) as sessions:
            if not sessions.delete(sid, sessions_dir):
                return False
            with contextlib.suppress(Exception):
                store.remove_by_session_id(sid)
            with contextlib.suppress(Exception):
                sessions.collect_images()
            return True

    store = getattr(runner, "session_store", None)
    try:
        deleted = await asyncio.to_thread(write, *_scope())
    except RuntimeError as exc:
        if not _BUSY.intersection(c.__name__ for c in type(exc).__mro__):
            raise
        raise RequestError("unavailable", "Hermes is working in this session. Try again when it is done.") from exc
    if not deleted:
        raise RequestError("not_found", "No such session. Refresh the list.")
    return {"sid": sid}


# --- catalogs -------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _builtin_commands() -> tuple[dict, ...]:
    """Hermes' gateway slash commands, a snapshot built by tools/build_commands.py (no public catalog API yet)."""
    path = Path(__file__).with_name("h3proto") / "hermes_commands.json"
    return tuple(json.loads(path.read_text(encoding="utf-8")))


@_guard
async def commands_list() -> list[dict]:
    return [{"name": c["name"], "desc": _clip(c.get("desc"), 120), "args": c.get("args") or "",
             "aliases": list(c.get("aliases") or [])} for c in _builtin_commands()]


def _model_options(cfg: dict[str, Any], override: dict[str, Any]) -> dict:
    """The models config.yaml names, grouped by provider: the current one, `providers:` entries and their
    models, `custom_providers:` and the fallback chain. (Hermes' full picker inventory is not public yet.)"""
    default_model, default_provider = _current_model(cfg)
    model = str(override.get("model") or default_model)
    provider = str(override.get("provider") or default_provider)
    if provider.lower() == "auto":
        provider = ""
    groups: dict[str, dict] = {}

    def add(slug: Any, name: Any, mid: Any) -> None:
        slug, mid = str(slug or "").strip(), str(mid or "").strip()
        if not slug or not mid:
            return
        group = groups.setdefault(slug, {"provider": slug, "name": str(name or slug), "models": []})
        if mid not in group["models"]:
            group["models"].append(mid)

    add(provider, "", model)
    if default_provider.lower() != "auto":
        add(default_provider, "", default_model)  # after a /model switch, the configured default stays pickable
    providers = cfg.get("providers")
    for key, entry in (providers.items() if isinstance(providers, dict) else ()):
        if isinstance(entry, dict):
            models = entry.get("models")
            names = list(models) if isinstance(models, (dict, list)) else []
            for mid in [entry.get("default_model") or entry.get("model"), *names]:
                add(key, entry.get("name"), mid if isinstance(mid, str) else (mid or {}).get("id"))
    for entry in (cfg.get("custom_providers") or []) if isinstance(cfg.get("custom_providers"), list) else ():
        if isinstance(entry, dict) and str(entry.get("name") or "").strip():
            add(f"custom:{str(entry['name']).strip().lower()}", entry["name"], entry.get("model"))
    for key in ("fallback_providers", "fallback_model"):
        chain = cfg.get(key)
        for entry in ([chain] if isinstance(chain, dict) else chain if isinstance(chain, list) else []):
            if isinstance(entry, dict):
                add(entry.get("provider"), "", entry.get("model"))
    items = [{"provider": g["provider"], "name": g["name"], "current": g["provider"] == provider,
              "models": [{"id": m, "current": g["provider"] == provider and m == model} for m in g["models"]][:40]}
             for g in groups.values()]
    return {"items": items, "model": model, "provider": provider}


@_guard
async def models_list(runner=None, key: str | None = None) -> dict:
    override = await _override(runner, key) if runner is not None and key else {}
    return _model_options(await asyncio.to_thread(_config), override)


_switch_lock = threading.Lock()  # one read-change-save of config.yaml at a time
SKILLS_ON_PC = "Off for every platform. Turn it on at the computer with: hermes skills"
TOOLS_ON_PC = "Off for every platform. Turn it on at the computer with: hermes tools"


def _name_list(value: Any) -> set[str]:
    """A config list of names (`hermes config set` may have stored it as a quoted list)."""
    try:
        from agent.skill_utils import parse_config_string_list
        value = parse_config_string_list(value)
    except ImportError:  # an older Hermes: plain lists only
        value = value if isinstance(value, (list, tuple, set)) else []
    return {str(n).strip() for n in value if str(n).strip()}


def _skill_state(cfg: dict, platform: str) -> tuple[set[str], set[str], set[str]]:
    """(always on, off everywhere, off for this platform only)."""
    try:
        from agent.skill_utils import ESSENTIAL_SKILLS
    except ImportError:  # an older Hermes: the list still has to load
        ESSENTIAL_SKILLS = ("hermes-agent",)
    skills = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
    per = skills.get("platform_disabled") if isinstance(skills.get("platform_disabled"), dict) else {}
    essential = set(ESSENTIAL_SKILLS)
    return essential, _name_list(skills.get("disabled")) - essential, _name_list(per.get(platform)) - essential


@_guard
async def skills_list(platform: str = "3ds") -> list[dict]:
    """Every installed skill, with whether a chat on `platform` gets it. `lock`: "always" (Hermes needs it) or
    "pc" (off for every platform, which only the computer changes); absent when the 3DS may switch it."""
    from tools.skills_tool import find_all_skills, sort_skills

    def build():
        always, everywhere, here = _skill_state(_config(), platform)
        out = []
        for s in sort_skills(find_all_skills(skip_disabled=True)):
            name = s["name"]
            row = {"name": name, "desc": _clip(s.get("description"), 160), "category": s.get("category") or "",
                   "enabled": name not in everywhere and name not in here}
            if name in always:
                row["lock"] = "always"
            elif name in everywhere:
                row["lock"] = "pc"
            out.append(row)
        return out

    return await asyncio.to_thread(build)


@_guard
async def skills_set(platform: str, name: Any, enabled: Any) -> dict:
    """Turn one skill on or off for chats on `platform` only (`skills.platform_disabled.<platform>`, the list
    `hermes skills` keeps per platform). The list for every platform is the computer's to change."""
    from hermes_cli.skills_config import save_disabled_skills
    from tools.skills_tool import find_all_skills
    name = str(name or "").strip()
    if not isinstance(enabled, bool):
        raise RequestError("bad_request", "enabled must be true or false")

    def work():
        with _switch_lock:
            if name not in {s["name"] for s in find_all_skills(skip_disabled=True)}:
                raise RequestError("not_found", "No such skill. Refresh the list.")
            cfg = _config()
            always, everywhere, here = _skill_state(cfg, platform)
            if name in always:
                raise RequestError("bad_request", "Hermes needs this skill. It stays on.")
            if name in everywhere:
                raise RequestError("bad_request", SKILLS_ON_PC)
            if (name not in here) != enabled:
                save_disabled_skills(cfg, (here - {name}) if enabled else (here | {name}), platform=platform)
            return {"name": name, "enabled": name not in _skill_state(_config(), platform)[2]}

    return await asyncio.to_thread(work)


def _toolsets(platform: str) -> list[tuple[str, str, str]]:
    """The toolsets `hermes tools` offers for this platform: (key, label, what is in it)."""
    from hermes_cli.tools_config import _get_effective_configurable_toolsets, _toolset_allowed_for_platform
    try:  # (speech-to-text: a setting, not tools to switch)
        from hermes_cli.tools_config import _CONFIG_ONLY_TOOLSETS as setup_only
    except ImportError:
        setup_only = ()
    return [(str(k), str(label), str(desc)) for k, label, desc in _get_effective_configurable_toolsets()
            if _toolset_allowed_for_platform(k, platform) and k not in setup_only]


def _tool_state(cfg: dict, platform: str) -> tuple[set[str], set[str]]:
    """(toolsets a chat on this platform gets, toolsets `agent.disabled_toolsets` turns off everywhere)."""
    from hermes_cli.tools_config import _get_platform_tools, _platform_default_toolset
    from toolsets import resolve_toolset
    agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    everywhere = _name_list(agent.get("disabled_toolsets"))
    on = {str(n) for n in _get_platform_tools(cfg, platform, include_default_mcp_servers=False)}
    saved = cfg.get("platform_toolsets") if isinstance(cfg.get("platform_toolsets"), dict) else {}
    if saved.get(platform) is None:
        # Nothing saved yet: the chat gets the platform's whole bundle, which also holds toolsets Hermes counts as
        # off by default (the task board). Count them as on, so the first save takes away only what was switched.
        bundle = set(resolve_toolset(_platform_default_toolset(platform)))
        for key, _, _ in _toolsets(platform):
            tools = set(resolve_toolset(key))
            if key not in everywhere and tools and tools <= bundle:
                on.add(key)
    return on, everywhere


def _tool_label(label: str) -> str:
    """Hermes' label without its leading picture ("🔍 Web Search & Scraping")."""
    head, _, rest = label.partition(" ")
    return rest.strip() if rest.strip() and not any(c.isalnum() for c in head) else label.strip()


@_guard
async def tools_list(platform: str) -> list[dict]:
    """The toolsets `hermes tools` offers, with whether a chat on `platform` gets each. `lock: "pc"`: turned off
    for every platform (`agent.disabled_toolsets`), which only the computer changes."""
    from toolsets import resolve_toolset

    def build():
        on, everywhere = _tool_state(_config(), platform)
        out = []
        for key, label, desc in _toolsets(platform):
            row = {"toolset": key, "label": _clip(_tool_label(label), 40), "desc": _clip(desc, 120),
                   "enabled": key in on, "tools": [str(t) for t in resolve_toolset(key)][:40]}
            if key in everywhere:
                row["lock"] = "pc"
            out.append(row)
        return out

    return await asyncio.to_thread(build)


@_guard
async def tools_set(platform: str, toolset: Any, enabled: Any) -> dict:
    """Turn one toolset on or off for chats on `platform` only (`platform_toolsets.<platform>`, what `hermes
    tools` saves). The gateway reads it for each turn, so the next message follows it."""
    from hermes_cli.tools_config import _platform_default_toolset, _save_platform_tools
    key = str(toolset or "").strip()
    if not isinstance(enabled, bool):
        raise RequestError("bad_request", "enabled must be true or false")

    def work():
        with _switch_lock:
            offered = {k for k, _, _ in _toolsets(platform)}
            if key not in offered:
                raise RequestError("not_found", "No such set of tools. Refresh the list.")
            cfg = _config()
            on, everywhere = _tool_state(cfg, platform)
            if key in everywhere:
                raise RequestError("bad_request", TOOLS_ON_PC)
            if not on & offered:
                # Nothing resolved for this platform: saving now would write an empty list and take every tool
                # away from the chat. (Seen only outside a gateway, where the 3ds platform is not registered.)
                raise RequestError("unavailable", "Hermes could not work out this chat's tools. Nothing was changed.")
            if (key in on) != enabled:
                # Without the platform's catch-all name (`hermes-3ds`): kept in a saved list, it would bring every
                # tool back and the switch would do nothing.
                keep = ((on | {key}) if enabled else (on - {key})) - {_platform_default_toolset(platform)}
                _save_platform_tools(cfg, platform, keep)
            return {"toolset": key, "enabled": key in _tool_state(_config(), platform)[0]}

    return await asyncio.to_thread(work)


JOB_PROMPT_MAX = 1000   # the longest prompt the 3DS can show whole and edit (its keyboard's limit)
JOB_NAME_MAX = 60
JOB_SCHEDULE_MAX = 80


def _job(j: dict) -> dict:
    sched = j.get("schedule")
    if isinstance(sched, dict):
        sched = sched.get("display") or sched.get("expr")
    return {"id": j.get("job_id") or j.get("id"), "name": j.get("name") or _clip(j.get("prompt_preview"), 40),
            "schedule": _clip(j.get("schedule_display") or sched, 60),
            "state": j.get("state") or ("scheduled" if j.get("enabled") else "paused"),
            "next_run": _ts(j.get("next_run_at")), "last_run": _ts(j.get("last_run_at")),
            "last_status": j.get("last_status"), "last_error": _clip(j.get("last_error"), 200) or None}


def _with_prompt(item: dict) -> dict:
    """Add the job's whole prompt (Hermes' cron tool lists only its first 100 characters). `prompt_more` marks one
    too long for the 3DS to edit; it then carries the start, to read. Without Hermes' job store: no prompt, and
    the 3DS offers only the name and the schedule to change."""
    try:
        from cron.jobs import get_job
        stored = get_job(str(item.get("id") or ""))
    except Exception as exc:
        log.debug("3ds: job prompt unavailable: %s", exc)
        return item
    if isinstance(stored, dict) and isinstance(stored.get("prompt"), str):
        prompt = stored["prompt"]
        item["prompt"] = prompt[:JOB_PROMPT_MAX]
        if len(prompt) > JOB_PROMPT_MAX:
            item["prompt_more"] = True
    return item


def _cron(args: dict[str, Any], refused: str = "unavailable") -> dict[str, Any]:
    """One `cronjob_manage` call (Hermes' cron tool) through the plugin context. Blocking. `refused` is the error
    code for what the tool itself turns down (other than a job it can't find)."""
    if _ctx is None:
        raise RequestError("unavailable", "cron unavailable (plugin context not bound)")
    out = json.loads(_ctx.dispatch_tool("cronjob_manage", args))
    if not isinstance(out, dict):
        raise RequestError("unavailable", "unexpected reply from the cron tool")
    if out.get("success") is False or out.get("error"):
        err = str(out.get("error") or out.get("message") or "cron tool failed")
        if "terminal cron job" in err.lower():  # a one-time job that has run can't be given a new time
            raise RequestError("bad_request", "This job has finished. Run it now, or make a new one.")
        raise RequestError("not_found" if "not found" in err.lower() else refused, err)
    return out


def _cron_edit(origin: dict[str, str] | None, args: dict[str, Any]) -> dict[str, Any]:
    """A create or update. With `origin` (platform, chat_id, user_id, user_name) it is made as if asked from that
    3DS chat, so the job reports back there: Hermes stamps a new job with the chat its session context names. This
    runs in a worker thread, whose context is a copy, so nothing set here outlives the call. What Hermes refuses
    (a schedule it can't read, a prompt its scan rejects) is the user's to fix: `bad_request`, in Hermes' words."""
    if not origin:
        return _cron(args, "bad_request")
    from gateway.session_context import clear_session_vars, set_session_vars
    tokens = set_session_vars(platform=origin["platform"], chat_id=origin["chat_id"], chat_type="dm",
                              user_id=origin.get("user_id", ""), user_name=origin.get("user_name", ""))
    try:
        return _cron(args, "bad_request")
    finally:
        clear_session_vars(tokens)


def _job_text(value: Any, what: str, limit: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise RequestError("bad_request", f"a job needs a {what}")
        return None
    text = " ".join(str(value).split()) if what != "prompt" else str(value).strip()
    if not text:
        raise RequestError("bad_request", f"the {what} is empty")
    if len(text) > limit:
        raise RequestError("too_large", f"the {what} is longer than {limit} characters")
    return text


def _job_reply(out: dict, job_id: str = "") -> dict:
    job = out.get("job") if isinstance(out.get("job"), dict) else out
    return _with_prompt(_job(job if job.get("job_id") or job.get("id") else {"id": job_id}))


@_guard
async def jobs_list() -> list[dict]:
    def build() -> list[dict]:
        out = _cron({"action": "list", "include_disabled": True})
        return [_with_prompt(_job(j)) for j in out.get("jobs") or [] if isinstance(j, dict)]
    return await asyncio.to_thread(build)


@_guard
async def jobs_create(origin: dict[str, str], schedule: Any, prompt: Any, name: Any = None) -> dict:
    """A new job that reports to the 3DS chat it was made in (protocol.md §4.6)."""
    args: dict[str, Any] = {"action": "create",
                            "schedule": _job_text(schedule, "schedule", JOB_SCHEDULE_MAX, required=True),
                            "prompt": _job_text(prompt, "prompt", JOB_PROMPT_MAX, required=True)}
    if name is not None and str(name).strip():
        args["name"] = _job_text(name, "name", JOB_NAME_MAX, required=False)
    return _job_reply(await asyncio.to_thread(_cron_edit, origin, args))


@_guard
async def jobs_update(job_id: str, name: Any = None, schedule: Any = None, prompt: Any = None) -> dict:
    args: dict[str, Any] = {"action": "update", "job_id": job_id}
    for key, value, limit in (("name", name, JOB_NAME_MAX), ("schedule", schedule, JOB_SCHEDULE_MAX),
                              ("prompt", prompt, JOB_PROMPT_MAX)):
        if value is not None:
            args[key] = _job_text(value, key, limit, required=False)
    if len(args) == 2:
        raise RequestError("bad_request", "nothing to change")
    return _job_reply(await asyncio.to_thread(_cron_edit, None, args), job_id)


@_guard
async def jobs_delete(job_id: str) -> str:
    await asyncio.to_thread(_cron, {"action": "remove", "job_id": job_id})
    return job_id


@_guard
async def jobs_action(job_id: str, op: str) -> dict:
    if op not in ("pause", "resume", "run"):
        raise RequestError("bad_request", "op must be pause, resume or run")
    args: dict[str, Any] = {"action": op, "job_id": job_id}
    if op == "pause":
        args["reason"] = "Paused from the Nintendo 3DS"
    out = await asyncio.to_thread(_cron, args)  # `run` hands the job to the background and returns at once
    return _job_reply(out, job_id)
