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


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h3ds-it-"))
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
    await c.close()

    await ad.disconnect()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
