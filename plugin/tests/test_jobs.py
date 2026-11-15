"""Jobs made, changed and removed from the 3DS (docs/protocol.md §4.6), against a stand-in for Hermes' cron tool."""
import asyncio
import importlib
import json
import sys
import types

import pytest

from test_fetch import adapter_mod  # noqa: F401 - fixture


class FakeCron:
    """The `cronjob_manage` tool, as far as the plugin uses it. Records the session context of each create."""

    def __init__(self, session_env):
        self.jobs, self.calls, self.origins, self._env = {}, [], [], session_env

    def dispatch_tool(self, name, args):
        assert name == "cronjob_manage"
        self.calls.append(dict(args))
        action = args["action"]
        if action == "list":
            return json.dumps({"success": True, "jobs": [self._view(j) for j in self.jobs.values()]})
        if action == "create":
            if args["schedule"] == "whenever":
                return json.dumps({"success": False, "error": "Invalid schedule 'whenever'"})
            jid = f"j{len(self.jobs) + 1}"
            self.origins.append(dict(self._env))
            self.jobs[jid] = {"id": jid, "name": args.get("name") or args["prompt"][:50], "prompt": args["prompt"],
                              "schedule_display": args["schedule"], "enabled": True}
            return json.dumps({"success": True, "job": self._view(self.jobs[jid])})
        job = self.jobs.get(args["job_id"])
        if job is None:
            return json.dumps({"success": False, "error": f"Job '{args['job_id']}' not found"})
        if action == "update" and job.get("state") == "completed" and "schedule" in args:
            return json.dumps({"success": False, "error": f"Cannot activate terminal cron job '{job['name']}' "
                                                          "through update_job; use cron resume --run-now or --at."})
        if action == "update":
            for key in ("name", "prompt"):
                if key in args:
                    job[key] = args[key]
            if "schedule" in args:
                job["schedule_display"] = args["schedule"]
            return json.dumps({"success": True, "job": self._view(job)})
        if action == "remove":
            del self.jobs[job["id"]]
            return json.dumps({"success": True, "removed_job": {"id": job["id"]}})
        raise AssertionError(action)

    @staticmethod
    def _view(job):  # what Hermes' tool lists: the prompt cut to a preview
        return {"job_id": job["id"], "name": job["name"], "prompt_preview": job["prompt"][:100],
                "schedule": job["schedule_display"], "enabled": job["enabled"]}


@pytest.fixture
def cron(adapter_mod, monkeypatch):  # noqa: F811
    compat = importlib.import_module("plugin.compat")
    env: dict = {}
    fake = FakeCron(env)
    ctx_mod = types.ModuleType("gateway.session_context")

    def set_session_vars(**kw):
        env.update(kw)
        return ["token"]

    ctx_mod.set_session_vars = set_session_vars
    ctx_mod.clear_session_vars = lambda tokens: env.clear()
    jobs_mod = types.ModuleType("cron.jobs")
    jobs_mod.get_job = lambda jid: fake.jobs.get(jid)
    monkeypatch.setitem(sys.modules, "gateway.session_context", ctx_mod)
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs_mod)
    monkeypatch.setattr(compat, "_ctx", fake)
    return compat, fake


ORIGIN = {"platform": "3ds", "chat_id": "dev:abc", "user_id": "abc", "user_name": "Test 3DS"}


def test_a_job_made_on_the_3ds_reports_back_to_its_chat(cron):
    compat, fake = cron
    job = asyncio.run(compat.jobs_create(ORIGIN, " every  day at 9am ", "Summarize my inbox\nand be brief", "Inbox"))
    assert job["id"] == "j1" and job["name"] == "Inbox" and job["schedule"] == "every day at 9am"
    assert job["prompt"] == "Summarize my inbox\nand be brief" and job["state"] == "scheduled"
    assert fake.origins == [{"platform": "3ds", "chat_id": "dev:abc", "chat_type": "dm", "user_id": "abc",
                             "user_name": "Test 3DS"}]
    assert fake._env == {}  # nothing of the chat is left behind for the next call
    asyncio.run(compat.jobs_create(ORIGIN, "30m", "ping", "  "))
    assert "name" not in fake.calls[-1]  # a blank name is left for Hermes to choose


def test_what_hermes_refuses_is_the_users_to_fix(cron):
    compat, _ = cron
    with pytest.raises(compat.RequestError) as exc:
        asyncio.run(compat.jobs_create(ORIGIN, "whenever", "ping"))
    assert exc.value.code == "bad_request" and "whenever" in exc.value.msg
    for bad in ((None, "ping"), ("30m", None), ("30m", "   "), ("30m", "x" * 1001)):
        with pytest.raises(compat.RequestError) as exc:
            asyncio.run(compat.jobs_create(ORIGIN, *bad))
        assert exc.value.code in ("bad_request", "too_large")


def test_changing_and_removing_a_job(cron):
    compat, fake = cron
    asyncio.run(compat.jobs_create(ORIGIN, "30m", "ping"))
    job = asyncio.run(compat.jobs_update("j1", schedule="every 2h"))
    assert job["schedule"] == "every 2h" and job["prompt"] == "ping"
    assert fake.calls[-1] == {"action": "update", "job_id": "j1", "schedule": "every 2h"}  # only what changed
    assert asyncio.run(compat.jobs_update("j1", name="Pinger", prompt="pong"))["name"] == "Pinger"
    with pytest.raises(compat.RequestError) as exc:
        asyncio.run(compat.jobs_update("j1"))
    assert exc.value.code == "bad_request"
    with pytest.raises(compat.RequestError) as exc:
        asyncio.run(compat.jobs_update("nope", name="x"))
    assert exc.value.code == "not_found"
    assert asyncio.run(compat.jobs_delete("j1")) == "j1" and not fake.jobs
    with pytest.raises(compat.RequestError) as exc:
        asyncio.run(compat.jobs_delete("j1"))
    assert exc.value.code == "not_found"


def test_the_list_carries_whole_prompts_and_marks_long_ones(cron):
    compat, fake = cron
    asyncio.run(compat.jobs_create(ORIGIN, "30m", "p" * 400))
    fake.jobs["j1"]["prompt"] = "q" * 3000  # written on the PC: too long for the 3DS keyboard
    asyncio.run(compat.jobs_create(ORIGIN, "1h", "short one"))
    first, second = asyncio.run(compat.jobs_list())
    assert len(first["prompt"]) == 1000 and first["prompt_more"] is True
    assert second["prompt"] == "short one" and "prompt_more" not in second


def test_a_finished_job_cannot_be_given_a_new_time_and_says_so_plainly(cron):
    compat, fake = cron
    asyncio.run(compat.jobs_create(ORIGIN, "in 2m", "ping"))
    fake.jobs["j1"]["state"] = "completed"  # the one-time job has run
    with pytest.raises(compat.RequestError) as exc:
        asyncio.run(compat.jobs_update("j1", schedule="every 2h"))
    assert exc.value.code == "bad_request" and exc.value.msg == "This job has finished. Run it now, or make a new one."
    assert asyncio.run(compat.jobs_update("j1", name="Still renamable"))["name"] == "Still renamable"
