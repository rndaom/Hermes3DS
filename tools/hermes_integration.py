"""Adapter integration test inside the Hermes environment, no running gateway or LLM needed.

Drives the real ThreeDSAdapter + a protocol client through the paths a live chat can't trigger on demand
(approvals, clarify timeouts, cursor handling, offline outbox). Run with the Hermes venv python:

  <hermes venv>\\Scripts\\python.exe tools/hermes_integration.py [path-to-hermes-agent]
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".hermes" / "hermes-agent"
# A throwaway Hermes home: the adapter records its state (gateway_state.json) there like a gateway would,
# and must not touch the real one.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="h3ds-it-home-")
# A Hermes checkout that sees a fresh home starts building itself a dependency environment there (hundreds of MB,
# and it restarts the process, so the cleanup below never runs): this keeps it from doing so.
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
atexit.register(shutil.rmtree, os.environ["HERMES_HOME"], True)
sys.path.insert(0, str(HERMES))
sys.path.insert(0, str(ROOT))

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402
from gateway.platforms.base import ExecApprovalPrompt  # noqa: E402

import plugin as h3ds  # noqa: E402
import plugin.adapter as adapter_mod  # noqa: E402
from plugin.h3proto.client import Client  # noqa: E402
from plugin.h3proto.noise import KeyPair  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))


class _Ctx:
    def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None,
                          required_env=None, install_hint="", **kw):
        platform_registry.register(PlatformEntry(
            name=name, label=label, adapter_factory=adapter_factory, check_fn=check_fn,
            validate_config=validate_config, required_env=required_env or [], install_hint=install_hint,
            source="bundled", **kw))  # global scope: no plugin manager in this test

    def register_cli_command(self, **kw):
        pass

    def dispatch_tool(self, tool_name, args, **kw):  # as Hermes' PluginContext does, minus the plugin scope
        from tools.registry import registry
        return registry.dispatch(tool_name, args, **kw)


class _SeedDB:
    """The test's own hand on Hermes' sessions, to set a scene and to look at the result: the storage interface
    where Hermes has it (0.23.3 and later), `hermes_state` on a Hermes from before it."""

    def __init__(self):
        try:
            from persistence_assembly import open_persistence, profile_scope
        except ImportError:
            from hermes_state_registry import acquire
            self.store, self.db = None, acquire()
        else:
            self.store, self.db = open_persistence(profile_scope()), None

    def create_session(self, sid, source, **kw):
        if self.db is not None:
            return self.db.create_session(sid, source, **kw)
        from persistence.conversation.sessions import NewSession
        return self.store.sessions.create(NewSession(session_id=sid, source=source, **kw))

    def append_message(self, sid, role, content):
        if self.db is not None:
            return self.db.append_message(sid, role=role, content=content)
        from persistence.conversation.messages import NewMessage
        return self.store.messages.append(sid, NewMessage(role=role, content=content))

    def end_session(self, sid, reason):
        return self.db.end_session(sid, reason) if self.db is not None else self.store.sessions.end(sid, reason)

    def set_session_title(self, sid, title):
        if self.db is not None:
            return self.db.set_session_title(sid, title)
        return self.store.sessions.set_title(sid, title)

    def get_session(self, sid):
        if self.db is not None:
            return self.db.get_session(sid)
        record = self.store.sessions.get(sid)
        return None if record is None else record.as_session_row()

    def get_messages(self, sid):
        if self.db is not None:
            return self.db.get_messages(sid)
        return [row.as_wire_row() for row in self.store.messages.read_transcript(sid)]

    def close(self):
        (self.db if self.db is not None else self.store).close()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h3ds-it-"))
    atexit.register(shutil.rmtree, tmp, True)
    adapter_mod._data_dir = lambda: tmp
    h3ds.register(_Ctx())

    # away off: the check must not touch this computer's Tailscale Funnel
    cfg = PlatformConfig.from_dict({"enabled": True, "port": 0, "discovery_port": 0, "host": "127.0.0.1",
                                    "away": "off"})
    ad = adapter_mod.ThreeDSAdapter(cfg)
    check("adapter connects", await ad.connect())
    srv = ad._server
    device_key = KeyPair()
    c = await Client.connect("127.0.0.1", srv.port, device="IT 3DS", static=device_key,
                             server_key=srv.static.public, pair_token=srv.tokens.create())
    chat = c.welcome["chat"]["chat_id"]
    check("pairing welcome has chat id", chat.startswith("dev:"), str(c.welcome))

    # --- streaming with the cursor inside closed markdown, then finalize twice ---------------
    r = await ad.send(chat, "\n\nThe first command printed ` ▉`", metadata={"expect_edits": True})
    mid = r.message_id
    ev = await c.next_event("msg", timeout=5)
    check("stream start not final", ev["final"] is False and "▉" not in ev["text"], str(ev))
    await ad.edit_message(chat, mid, "\n\nThe first command printed `one` and ▉")
    ev = await c.next_event("append", "edit", timeout=5)
    check("cursor stripped mid-stream", "▉" not in ev["text"] and ev["final"] is False, str(ev))
    await ad.edit_message(chat, mid, "The first command printed `one` and that's it.", finalize=True)
    ev = await c.next_event("append", "edit", timeout=5)
    check("final edit carries spans", ev["final"] is True and ev.get("spans"), str(ev))
    await ad.edit_message(chat, mid, "The first command printed `one` and that's it.", finalize=True)
    await ad.send(chat, "marker")
    ev = await c.next_event(timeout=5)
    check("duplicate finalize suppressed", ev["t"] == "msg" and ev["text"] == "marker", str(ev))

    # --- tool activity from the live-status hook (called from a worker thread) ----------------
    await asyncio.to_thread(ad.set_status_text, chat, "is running echo hi…")
    ev = await c.next_event("tool", timeout=5)
    check("tool start event", ev.get("state") == "start" and "echo hi" in ev.get("text", ""), str(ev))
    await asyncio.to_thread(ad.set_status_text, chat, None)
    ev = await c.next_event("tool", timeout=5)
    check("tool done event", ev.get("state") == "done", str(ev))

    # --- approval: prompt, answer, resolve call ------------------------------------------------------
    calls = []
    import tools.approval as approval
    approval.resolve_gateway_approval = lambda key, choice, **kw: calls.append((key, choice)) or 1
    prompt = ExecApprovalPrompt(chat_id=chat, session_key="sk-1", text="**Dangerous** command",
                                actions=[("Allow once", "once", "primary"), ("Deny", "deny", "danger")],
                                command="rm -rf build/", description="delete build", smart_denied=False)
    res = await ad._send_exec_approval_prompt(prompt)
    ev = await c.next_event("approval", timeout=5)
    check("approval prompt delivered", ev["command"] == "rm -rf build/" and ev["text"] == "Dangerous command"
          and ev["actions"][1] == ["Deny", "deny", "danger"], str(ev))
    r = await c.request("approve", req=ev["req"], choice="deny")
    check("approve request resolves via Hermes", calls == [("sk-1", "deny")] and r["resolved"] == 1, str(calls))
    ev = await c.next_event("approval_done", timeout=5)
    check("approval_done sent", ev["choice"] == "deny", str(ev))
    try:
        await c.request("approve", req=res.message_id, choice="once")
        check("second answer rejected", False)
    except Exception as exc:
        check("second answer rejected", "not_found" in str(exc), str(exc))

    # approval timeout: Hermes rewrites the card via edit_message
    res = await ad._send_exec_approval_prompt(prompt)
    await c.next_event("approval", timeout=5)
    await ad.edit_message(chat, res.message_id, "⏱ Approval timed out — the command did **not** run.")
    ev = await c.next_event("approval_done", timeout=5)
    check("approval timeout clears prompt", ev["choice"] == "timeout", str(ev))
    ev = await c.next_event("msg", timeout=5)
    check("timeout notice posted", ev["kind"] == "notice" and "did not run" in ev["text"], str(ev))

    # --- clarify: answer by index; retire on timeout ---------------------------------------------------
    import tools.clarify_gateway as cg
    answers = []
    cg.resolve_gateway_clarify = lambda cid, resp: answers.append((cid, resp)) or True
    await ad.send_clarify(chat, "Which color?", ["red (Recommended)", "blue"], "cl1", "sk-1")
    ev = await c.next_event("clarify", timeout=5)
    check("clarify delivered", ev["choices"] == ["red (Recommended)", "blue"], str(ev))
    await c.request("answer", cid="cl1", choice=1)
    check("clarify resolves with choice text", answers == [("cl1", "blue")], str(answers))
    await c.next_event("clarify_done", timeout=5)
    await ad.send_clarify(chat, "Anything else?", [], "cl2", "sk-1")
    await c.next_event("clarify", timeout=5)
    await ad.retire_clarify_card("cl2", "Question timed out.")
    ev = await c.next_event("clarify_done", timeout=5)
    check("retired clarify cleared", ev["cid"] == "cl2", str(ev))

    # --- offline outbox: reply finishes while the 3DS is asleep -------------------------------------
    r = await ad.send(chat, "Working on it ▉", metadata={"expect_edits": True})
    await c.next_event("msg", timeout=5)
    await c.close()
    await asyncio.sleep(0.1)
    await ad.edit_message(chat, r.message_id, "Done: **3** files changed.", finalize=True)
    c = await Client.connect("127.0.0.1", srv.port, static=device_key, server_key=srv.static.public)
    # The reconnect also replays what was sent since the 3DS last spoke (the clarify notice); find the reply.
    ev = await c.next_event("msg", timeout=5)
    while ev["mid"] != r.message_id:
        ev = await c.next_event("msg", timeout=5)
    check("offline reply delivered on reconnect", ev["mid"] == r.message_id and ev["final"]
          and ev["text"] == "Done: 3 files changed." and ev.get("spans"), str(ev))

    # --- a file from the SD card: Hermes' own document cache, and what its gateway makes of the event -------
    from gateway.platforms.event import MessageType
    from gateway.run import _build_media_placeholder
    got = []

    async def record(event):
        got.append(event)

    ad.handle_message = record
    await c.put_blob("uitfile1", b"alpha,beta\n1,2\n", "text/csv", "table.csv")
    r = await c.request("send", text="what is in this?", attach=[{"blob": "uitfile1"}])
    ev = got[0] if got else None
    kept = Path(ev.media_urls[0]) if ev and ev.media_urls else None
    check("a sent file becomes a document event", bool(ev) and ev.message_type == MessageType.DOCUMENT
          and ev.media_types == ["text/csv"] and r.get("mid") == ev.message_id, str(ev))
    check("the file is in Hermes' document cache under its name", bool(kept) and kept.name.endswith("table.csv")
          and kept.read_bytes() == b"alpha,beta\n1,2\n", str(kept))
    check("the gateway sees a file, not a photo", bool(ev) and "sent a file" in _build_media_placeholder(ev),
          _build_media_placeholder(ev) if ev else "")
    if kept:
        kept.unlink(missing_ok=True)

    # --- jobs from the 3DS: Hermes' own cron tool and job store (in the temp HERMES_HOME) ---------------------
    import tools.cronjob_tools  # noqa: F401 - registers cronjob_manage, as Hermes' tool discovery does
    from cron.jobs import get_job
    ad.gateway_runner = object()  # job requests want a gateway to exist; they never ask it anything
    try:
        r = await c.request("jobs.create", schedule="every day at 9am", prompt="Say good morning.", name="Morning")
        job = r["job"]
        stored = get_job(job["id"]) or {}
        origin = stored.get("origin") or {}
        check("a job made on the 3DS is stored by Hermes", job["name"] == "Morning" and job["prompt"] ==
              "Say good morning." and stored.get("prompt") == "Say good morning.", str(r))
        check("it reports back to the 3DS chat it was made in", origin.get("platform") == "3ds"
              and origin.get("chat_id") == chat and stored.get("deliver") in ("origin", None), str(stored))
        r = await c.request("jobs.update", job=job["id"], schedule="every 2h", prompt="Say hello.")
        stored = get_job(job["id"]) or {}
        check("a change from the 3DS reaches the stored job", stored.get("prompt") == "Say hello."
              and r["job"]["prompt"] == "Say hello." and ("2h" in r["job"]["schedule"] or "120m" in r["job"]["schedule"])
              and (stored.get("origin") or {}).get("chat_id") == chat, str(r))
        items = (await c.request("jobs.list"))["items"]
        check("the list carries the whole prompt", [i.get("prompt") for i in items] == ["Say hello."], str(items))
        try:
            await c.request("jobs.create", schedule="when the moon is full-ish", prompt="x")
            check("a schedule Hermes can't read is refused", False)
        except Exception as exc:
            check("a schedule Hermes can't read is refused", "bad_request" in str(exc), str(exc))
        r = await c.request("jobs.delete", job=job["id"])
        check("a job removed from the 3DS is gone", r["job"] == job["id"] and get_job(job["id"]) is None, str(r))
    except Exception as exc:
        check("jobs from the 3DS", False, repr(exc))

    # --- skills and tools switched from the 3DS: Hermes' own config and resolution -----------------------------
    try:
        from agent.skill_utils import get_disabled_skill_names
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        from toolsets import resolve_toolset

        def turn_tools():  # the tool names a 3DS turn is given, as the gateway resolves them
            return {t for ts in _get_platform_tools(load_config(), "3ds") for t in resolve_toolset(ts)}

        check("the welcome says the switches are there", c.welcome.get("switch") == {"skills": True, "tools": True},
              str(c.welcome))
        rows = {i["toolset"]: i for i in (await c.request("tools.list"))["items"]}
        before, browser = turn_tools(), set(resolve_toolset("browser"))
        check("the tools list is what a 3DS chat gets", rows.get("terminal", {}).get("enabled") is True
              and rows.get("browser", {}).get("enabled") is True and "discord" not in rows
              and "terminal" in before and browser <= before, str(sorted(rows)))
        r = await c.request("tools.set", toolset="browser", enabled=False)
        after = turn_tools()
        saved = (load_config().get("platform_toolsets") or {}).get("3ds") or []
        check("a toolset turned off on the 3DS leaves the 3DS chat", r["enabled"] is False
              and "browser" not in saved and "hermes-3ds" not in saved, str(saved))
        check("the chat loses those tools and no others", before - after == browser - {
              t for ts in saved for t in resolve_toolset(ts)} and after <= before and "terminal" in after,
              str(sorted((before - after) ^ browser)))
        check("other platforms are untouched", browser <= {
              t for ts in _get_platform_tools(load_config(), "telegram") for t in resolve_toolset(ts)})
        rows = {i["toolset"]: i for i in (await c.request("tools.list"))["items"]}
        check("the list shows it off", rows["browser"]["enabled"] is False and rows["terminal"]["enabled"] is True)
        r = await c.request("tools.set", toolset="browser", enabled=True)
        check("turned on again, the chat has what it had", r["enabled"] is True and turn_tools() == before,
              str(sorted(turn_tools() ^ before)))

        demo = Path(os.environ["HERMES_HOME"]) / "skills" / "it-demo"
        demo.mkdir(parents=True, exist_ok=True)
        (demo / "SKILL.md").write_text("---\nname: it-demo\ndescription: A skill for this check.\n---\n\nSay hi.\n",
                                       encoding="utf-8")
        skills = (await c.request("skills.list"))["items"]
        free = [i["name"] for i in skills if not i.get("lock")]
        if free:
            name = free[0]
            r = await c.request("skills.set", name=name, enabled=False)
            check("a skill turned off on the 3DS is off for 3DS chats only", r["enabled"] is False
                  and name in get_disabled_skill_names("3ds") and name not in get_disabled_skill_names("telegram"),
                  str(load_config().get("skills")))
            listed = {i["name"]: i for i in (await c.request("skills.list"))["items"]}
            check("the list shows it off", listed[name]["enabled"] is False and "lock" not in listed[name])
            r = await c.request("skills.set", name=name, enabled=True)
            check("turned on again", r["enabled"] is True and name not in get_disabled_skill_names("3ds"))
        else:
            print("SKIP  skill switch (this Hermes home has no skill to switch)")
        try:
            await c.request("skills.set", name="hermes-agent", enabled=False)
            check("the skill Hermes needs stays on", False)
        except Exception as exc:
            check("the skill Hermes needs stays on", "bad_request" in str(exc) or "not_found" in str(exc), str(exc))
    except Exception as exc:
        check("skills and tools from the 3DS", False, repr(exc))
    # --- sessions renamed and deleted from the 3DS: Hermes' own session database and routing store ------------
    try:
        import types
        from gateway.config import GatewayConfig
        from gateway.session import AsyncSessionStore, SessionStore
        from hermes_constants import get_hermes_home
        db = _SeedDB()  # the gateway's writer; the plugin takes a handle of its own for each change
        store = SessionStore(Path(get_hermes_home()) / "sessions", GatewayConfig())
        ad.gateway_runner = types.SimpleNamespace(session_store=store, async_session_store=AsyncSessionStore(store))
        check("the welcome says sessions can be changed", c.welcome.get("sessions") == {"edit": True}, str(c.welcome))
        # Where Hermes has the storage interface the plugin must not touch `hermes_state`, which Hermes 0.27 drops.
        import plugin.compat as compat_mod
        legacy_used: list[str] = []
        if compat_mod._storage_interface():
            def legacy(*a, **k):
                legacy_used.append("hermes_state")
                raise RuntimeError("hermes_state used where the storage interface exists")
            compat_mod._session_db = compat_mod._open_writer = legacy
        me = srv.devices.get(device_key.public)
        entry = store.get_or_create_session(ad.source_for(me, None))
        key, now_id = entry.session_key, entry.session_id
        # "it-before" is the chat the 3DS is in as it was before Hermes compressed it: ended by compression, with
        # the current session as its continuation.
        db.create_session("it-before", "3ds", session_key=key, chat_id=chat, chat_type="dm")
        db.append_message("it-before", role="user", content="hello from it-before")
        db.end_session("it-before", "compression")
        for sid in (now_id, "it-old", "it-other"):
            db.create_session(sid, "3ds", session_key=key, chat_id=chat, chat_type="dm",
                              **({"parent_session_id": "it-before"} if sid == now_id else {}))
            db.append_message(sid, role="user", content=f"hello from {sid}")
        db.set_session_title("it-other", "Taxes")
        db.create_session("it-telegram", "telegram", session_key="agent:main:telegram:dm:42")
        ids = {i["id"] for i in (await c.request("sessions.list"))["items"]}
        check("the list is this chat's sessions", {now_id, "it-old", "it-other"} <= ids <= {
            now_id, "it-old", "it-other", "it-before"}, str(ids))
        r = await c.request("sessions.messages", sid="it-before")
        check("a compressed chat's messages run on into its newest copy",
              [i["text"] for i in r["items"]] == ["hello from it-before", f"hello from {now_id}"], str(r))
        try:
            await c.request("sessions.delete", sid="it-before")
            check("the chat's copy from before a compression can't be deleted", False)
        except Exception as exc:
            check("the chat's copy from before a compression can't be deleted", "bad_request" in str(exc)
                  and db.get_session("it-before") is not None, str(exc))
        r = await c.request("sessions.rename", sid="it-old", title="  Holiday   plans ")
        check("a session renamed on the 3DS is renamed in Hermes", r["title"] == "Holiday plans"
              and (db.get_session("it-old") or {}).get("title") == "Holiday plans", str(r))
        r = await c.request("sessions.messages", sid="it-other")
        check("a session's messages can be read", [i["text"] for i in r["items"]] == ["hello from it-other"], str(r))
        r = await c.request("status")
        check("the status carries the session's name", "session" in r and r["session"]["id"] == now_id, str(r))
        for what, kw in (("a name another session has is refused", {"sid": "it-old", "title": "Taxes"}),
                         ("a name too long is refused", {"sid": "it-old", "title": "x" * 300}),
                         ("another chat's session can't be renamed", {"sid": "it-telegram", "title": "Mine"})):
            try:
                await c.request("sessions.rename", **kw)
                check(what, False)
            except Exception as exc:
                check(what, ("not_found" if "another chat" in what else "bad_request") in str(exc), str(exc))
        try:
            await c.request("sessions.delete", sid=now_id)
            check("the session the chat is in can't be deleted", False)
        except Exception as exc:
            check("the session the chat is in can't be deleted", "bad_request" in str(exc)
                  and db.get_session(now_id) is not None, str(exc))
        try:
            await c.request("sessions.delete", sid="it-telegram")
            check("another chat's session can't be deleted", False)
        except Exception as exc:
            check("another chat's session can't be deleted", "not_found" in str(exc)
                  and db.get_session("it-telegram") is not None, str(exc))
        r = await c.request("sessions.delete", sid="it-old")
        ids = {i["id"] for i in (await c.request("sessions.list"))["items"]}
        check("a session deleted on the 3DS is gone from Hermes, with its messages", r["sid"] == "it-old"
              and db.get_session("it-old") is None and not db.get_messages("it-old")
              and "it-old" not in ids and {now_id, "it-other"} <= ids, f"{r} {ids}")
        check("the chat is still in its own session", store.get_or_create_session(
            ad.source_for(me, None)).session_id == now_id)
        found = [i["title"] for i in (await c.request("sessions.list", q="Taxes"))["items"]]
        check("a search of the sessions finds one by its name", found == ["Taxes"], str(found))
        check("sessions go through the storage interface where Hermes has it"
              if compat_mod._storage_interface() else "sessions go through hermes_state on a Hermes without the "
              "storage interface", not legacy_used, str(legacy_used))
        db.close()
    except Exception as exc:
        import traceback
        check("sessions from the 3DS", False, traceback.format_exc())

    await c.close()

    await ad.disconnect()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
