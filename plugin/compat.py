"""Every read of Hermes internals lives here (protocol.md §4.6), so upstream churn breaks one file.

Each function runs blocking Hermes code via asyncio.to_thread where needed and returns plain JSON-able
data for the 3DS. Failures raise RequestError("unavailable", ...) instead of crashing the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
from typing import Any

from .h3proto.emoji import emoji_for_3ds
from .h3proto.server import RequestError

log = logging.getLogger("hermes3ds")


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


def _raw_db(runner):
    db = getattr(runner, "_session_db", None)
    if db is None:
        raise RequestError("unavailable", "session database not available")
    return getattr(db, "_db", db)


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


# --- sessions -------------------------------------------------------------------

async def current_session(runner, source) -> dict[str, Any]:
    entry = await runner.async_session_store.get_or_create_session(source)
    return {"id": entry.session_id, "key": entry.session_key}


@_guard
async def status(runner, source, busy: bool) -> dict[str, Any]:
    cur = await current_session(runner, source)
    db = _raw_db(runner)
    row = await asyncio.to_thread(db.get_session, cur["id"]) or {}
    try:
        title = await asyncio.to_thread(db.get_session_title, cur["id"])
    except Exception:
        title = row.get("title")
    key = cur["key"]
    queued = 0
    model = _live_model(runner, key) or row.get("model") or ""
    try:
        queued = int(runner._queue_depth(key) or 0)
    except Exception:
        pass
    return {
        "session": {
            "id": cur["id"], "title": emoji_for_3ds(title or ""), "model": model,
            "started": _ts(row.get("started_at")), "msgs": row.get("message_count") or 0,
            "tokens_in": row.get("input_tokens") or 0, "tokens_out": row.get("output_tokens") or 0,
            "cost": row.get("actual_cost_usd") or row.get("estimated_cost_usd") or 0,
        },
        "busy": busy, "queued": queued,
    }


def _live_model(runner, key: str) -> str:
    """The model the next turn will use: the session's /model override, else the configured default.
    The session row only records the model the session started with."""
    with contextlib.suppress(Exception):
        override = runner._session_model_override(key)
        if override and override.get("model"):
            return str(override["model"])
    with contextlib.suppress(Exception):
        from hermes_cli.config import load_config
        m = load_config().get("model") or {}
        return str(m.get("default") or m.get("model") or "") if isinstance(m, dict) else str(m)
    return ""


@_guard
async def sessions_list(runner, source, offset: int, limit: int, q: str | None) -> list[dict]:
    from hermes_cli.session_listing import query_session_listing
    cur = await current_session(runner, source)
    rows = await asyncio.to_thread(
        query_session_listing, _raw_db(runner), source=source.platform.value, session_key=cur["key"],
        current_session_id=cur["id"], include_current_session=True, include_unnamed=True,
        search_query=q or None, limit=offset + limit)
    items = []
    for r in rows[offset:offset + limit]:
        items.append({
            "id": r.get("id"), "title": _clip(r.get("title") or "", 80), "preview": _clip(r.get("preview"), 120),
            "last_active": _ts(r.get("last_active") or r.get("started_at")),
            "msgs": r.get("message_count") or 0, "model": r.get("model") or "", "current": r.get("id") == cur["id"],
        })
    return items


async def _visible(runner, source, row: dict | None) -> bool:
    if not row:
        return False
    check = getattr(runner, "_resume_row_visible", None)
    if check is None:
        return row.get("source") == source.platform.value
    seen = check(source, row, allow_all=False)
    if inspect.isawaitable(seen):  # async since Hermes 0.22; bool(coroutine) would let every row through
        seen = await seen
    return bool(seen)


@_guard
async def session_messages(runner, source, sid: str, limit: int, before: int | None) -> list[dict]:
    from .h3proto.richtext import markdown_to_rich
    db = _raw_db(runner)
    row = await asyncio.to_thread(db.get_session, sid)
    if not await _visible(runner, source, row):
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

@_guard
async def commands_list() -> list[dict]:
    from hermes_cli import commands as c
    gate = getattr(c, "_is_gateway_available", None)
    items = []
    for cmd in c.COMMAND_REGISTRY:
        if gate is not None:
            if not gate(cmd):
                continue
        elif getattr(cmd, "cli_only", False):
            continue
        desc = cmd.describe() if hasattr(cmd, "describe") else getattr(cmd, "description", "")
        items.append({"name": cmd.name, "desc": _clip(desc, 120), "args": getattr(cmd, "args_hint", "") or "",
                      "aliases": list(getattr(cmd, "aliases", ()) or ())})
    try:
        from hermes_cli.plugins import get_plugin_commands
        for name, info in (get_plugin_commands() or {}).items():
            items.append({"name": name, "desc": _clip((info or {}).get("description", ""), 120),
                          "args": (info or {}).get("args_hint", "") or "", "aliases": []})
    except Exception:
        log.debug("3ds: plugin commands unavailable", exc_info=True)
    return items


@_guard
async def models_list() -> dict:
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    def build():
        return build_model_options_payload(load_picker_context(), include_unconfigured=False, refresh=False)

    payload = await asyncio.to_thread(build)
    cur_model, cur_provider = payload.get("model"), payload.get("provider")
    providers = []
    for p in payload.get("providers") or []:
        providers.append({
            "provider": p.get("slug"), "name": p.get("name"), "current": bool(p.get("is_current")),
            "models": [{"id": m, "current": bool(p.get("is_current")) and m == cur_model}
                       for m in (p.get("models") or [])][:40],
        })
    return {"items": providers, "model": cur_model, "provider": cur_provider}


@_guard
async def skills_list() -> list[dict]:
    from tools import skills_tool

    # Public names since Hermes 0.22; the underscored ones before.
    find_all = getattr(skills_tool, "find_all_skills", None) or skills_tool._find_all_skills
    sort = getattr(skills_tool, "sort_skills", None) or skills_tool._sort_skills

    def build():
        everything = find_all(skip_disabled=True)
        enabled = {s["name"] for s in find_all()}
        return [{"name": s["name"], "desc": _clip(s.get("description"), 160), "category": s.get("category") or "",
                 "enabled": s["name"] in enabled} for s in sort(everything)]

    return await asyncio.to_thread(build)


@_guard
async def tools_list(platform: str) -> list[dict]:
    from hermes_cli.config import load_config
    from hermes_cli import tools_config as tc
    import toolsets

    def build():
        enabled = tc._get_platform_tools(load_config(), platform)
        out = []
        for name, label, desc in tc._get_effective_configurable_toolsets():
            try:
                tools = list(toolsets.resolve_toolset(name))
            except Exception:
                tools = []
            out.append({"toolset": name, "label": label, "desc": _clip(desc, 120), "enabled": name in enabled,
                        "tools": tools[:40]})
        return out

    return await asyncio.to_thread(build)


def _job(j: dict) -> dict:
    sched = j.get("schedule")
    if isinstance(sched, dict):
        sched = sched.get("display") or sched.get("expr")
    return {"id": j.get("id"), "name": j.get("name") or _clip(j.get("prompt"), 40),
            "schedule": _clip(j.get("schedule_display") or sched, 60),
            "state": j.get("state") or ("scheduled" if j.get("enabled") else "paused"),
            "next_run": _ts(j.get("next_run_at")), "last_run": _ts(j.get("last_run_at")),
            "last_status": j.get("last_status"), "last_error": _clip(j.get("last_error"), 200) or None}


@_guard
async def jobs_list() -> list[dict]:
    from cron import jobs
    return [_job(j) for j in await asyncio.to_thread(jobs.list_jobs, include_disabled=True)]


@_guard
async def jobs_action(job_id: str, op: str) -> dict:
    from cron import jobs
    fn = {"pause": jobs.pause_job, "resume": jobs.resume_job, "run": jobs.trigger_job}.get(op)
    if fn is None:
        raise RequestError("bad_request", "op must be pause, resume or run")
    result = await asyncio.to_thread(fn, job_id)
    if not result:
        raise RequestError("not_found", "no such job")
    job = await asyncio.to_thread(jobs.get_job, job_id)
    return _job(job or {"id": job_id})
