"""Every read of Hermes' state lives here (protocol.md §4.6), so upstream churn breaks one file.

Only public Hermes surfaces are used (the plugin catalog's rule 9): `hermes_state.SessionDB`, the runner's
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
_dbs: dict[str, Any] = {}  # read-only session databases: "" = the main agent's, else a bot's state.db path
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


def _session_db():
    """The session database of the profile in scope (the main agent's, or a bot's inside `profile_scope`),
    opened read-only once (the gateway keeps its own writer)."""
    from hermes_constants import get_hermes_home_override
    override = get_hermes_home_override()
    key = str(Path(override) / "state.db") if override else ""
    with _db_lock:
        db = _dbs.get(key)
        if db is None:
            from hermes_state import SessionDB
            db = _dbs[key] = SessionDB(db_path=Path(key), read_only=True) if key else SessionDB(read_only=True)
        return db


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
    """A served profile's Hermes home: the gateway's own for the main agent, else `<root>/profiles/<name>`."""
    from hermes_constants import get_process_hermes_home
    root = Path(get_process_hermes_home())
    return root if name == served_agents(runner)[0] else root / "profiles" / name


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


def _yaml(path: Path) -> dict:
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
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
    db = _session_db()
    row = await asyncio.to_thread(db.get_session, cur["id"]) or {}
    try:
        title = await asyncio.to_thread(db.get_session_title, cur["id"])
    except Exception:
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
    rows = await asyncio.to_thread(
        _session_db().list_sessions_rich, source=source.platform.value, session_key=cur["key"],
        search_query=search, order_by_last_active=bool(search), limit=limit, offset=offset)
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
    db = _session_db()
    row = await asyncio.to_thread(db.get_session, sid)
    if not _visible(row, cur["key"]):
        raise RequestError("not_found", "no such session for this device")
    resolved = await asyncio.to_thread(db.resolve_resume_session_id, sid)
    msgs = await asyncio.to_thread(db.get_messages, resolved or sid, latest=True, include_ancestors=True,
                                   limit=500)
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


@_guard
async def skills_list() -> list[dict]:
    from tools.skills_tool import find_all_skills, sort_skills

    def build():
        everything = find_all_skills(skip_disabled=True)
        enabled = {s["name"] for s in find_all_skills()}
        return [{"name": s["name"], "desc": _clip(s.get("description"), 160), "category": s.get("category") or "",
                 "enabled": s["name"] in enabled} for s in sort_skills(everything)]

    return await asyncio.to_thread(build)


@_guard
async def tools_list(platform: str) -> list[dict]:
    from tools.registry import registry

    def build():
        chosen = (_config().get("platform_toolsets") or {}).get(platform)
        chosen = {str(n) for n in chosen} if isinstance(chosen, list) else None  # None: Hermes' platform default
        out = []
        for name, info in sorted(registry.get_available_toolsets().items()):
            available = bool(info.get("available", True))
            out.append({"toolset": name, "label": name.replace("_", " ").replace("-", " "),
                        "desc": _clip(info.get("description"), 120),
                        "enabled": available and (chosen is None or name in chosen),
                        "tools": list(info.get("tools") or [])[:40]})
        return out

    return await asyncio.to_thread(build)


def _job(j: dict) -> dict:
    sched = j.get("schedule")
    if isinstance(sched, dict):
        sched = sched.get("display") or sched.get("expr")
    return {"id": j.get("job_id") or j.get("id"), "name": j.get("name") or _clip(j.get("prompt_preview"), 40),
            "schedule": _clip(j.get("schedule_display") or sched, 60),
            "state": j.get("state") or ("scheduled" if j.get("enabled") else "paused"),
            "next_run": _ts(j.get("next_run_at")), "last_run": _ts(j.get("last_run_at")),
            "last_status": j.get("last_status"), "last_error": _clip(j.get("last_error"), 200) or None}


def _cron(args: dict[str, Any]) -> dict[str, Any]:
    """One `cronjob_manage` call (Hermes' cron tool) through the plugin context. Blocking."""
    if _ctx is None:
        raise RequestError("unavailable", "cron unavailable (plugin context not bound)")
    out = json.loads(_ctx.dispatch_tool("cronjob_manage", args))
    if not isinstance(out, dict):
        raise RequestError("unavailable", "unexpected reply from the cron tool")
    if out.get("success") is False or out.get("error"):
        err = str(out.get("error") or out.get("message") or "cron tool failed")
        raise RequestError("not_found" if "not found" in err.lower() else "unavailable", err)
    return out


@_guard
async def jobs_list() -> list[dict]:
    out = await asyncio.to_thread(_cron, {"action": "list", "include_disabled": True})
    return [_job(j) for j in out.get("jobs") or [] if isinstance(j, dict)]


@_guard
async def jobs_action(job_id: str, op: str) -> dict:
    if op not in ("pause", "resume", "run"):
        raise RequestError("bad_request", "op must be pause, resume or run")
    args: dict[str, Any] = {"action": op, "job_id": job_id}
    if op == "pause":
        args["reason"] = "Paused from the Nintendo 3DS"
    out = await asyncio.to_thread(_cron, args)  # `run` hands the job to the background and returns at once
    job = out.get("job") if isinstance(out.get("job"), dict) else out
    return _job(job if job.get("job_id") or job.get("id") else {"id": job_id})
