import asyncio
import sys
import types

import pytest

from test_fetch import adapter_mod  # noqa: F401 - fixture


class _PendingServer:
    def __init__(self):
        self.cards, self.done = {}, []

    async def set_pending(self, key, req, msg):
        self.cards[req] = msg

    def pending(self, key, req):
        return self.cards.get(req)

    async def clear_pending(self, key, req, msg):
        self.cards.pop(req, None)
        self.done.append(msg)


@pytest.fixture
def ad(adapter_mod, monkeypatch):  # noqa: F811
    """An adapter with one paired chat, and Hermes' approval queue as a list of request ids per session."""
    queue = {"sess": ["r1", "r2"]}
    calls = []

    def resolve_gateway_approval(session_key, choice, resolve_all=False, reason=None, request_id=None):
        calls.append((session_key, choice, request_id))
        q = queue.get(session_key, [])
        if request_id:
            if request_id not in q:
                return 0
            q.remove(request_id)
            return 1
        if not q:
            return 0
        q.pop(0)
        return 1

    tools = types.ModuleType("tools")
    approval = types.ModuleType("tools.approval")
    approval.resolve_gateway_approval = resolve_gateway_approval
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    a = adapter_mod.ThreeDSAdapter.__new__(adapter_mod.ThreeDSAdapter)
    a._server = _PendingServer()
    a._chat_keys = {"dev:1": b"k" * 32}
    a._ids = iter(range(1, 100))
    a._approval_cards = {}
    a.queue, a.calls = queue, calls
    return a


def prompt(request_id=None):
    p = types.SimpleNamespace(chat_id="dev:1", session_key="sess", text="Run it?", command="rm -rf build",
                              description="delete", actions=[("Allow", "once", "primary")])
    if request_id is not None:
        p.request_id = request_id
    return p


DEVICE = types.SimpleNamespace(key=b"k" * 32)


def test_a_card_answers_only_its_own_request_on_hermes_0_23(ad):
    async def go():
        req = (await ad._send_exec_approval_prompt(prompt("r2"))).message_id
        return await ad.resolve_approval(DEVICE, req, "once")

    assert asyncio.run(go()) == 1
    assert ad.calls == [("sess", "once", "r2")]
    assert ad.queue["sess"] == ["r1"]  # the older request is still waiting for its own answer
    assert ad._server.done[-1]["choice"] == "once"


def test_a_stale_card_resolves_nothing_and_says_so(ad):
    async def go():
        req = (await ad._send_exec_approval_prompt(prompt("gone"))).message_id
        return await ad.resolve_approval(DEVICE, req, "once")

    assert asyncio.run(go()) == 0
    assert ad.queue["sess"] == ["r1", "r2"]  # the next approval in the session was not answered by it
    assert ad._server.done[-1]["choice"] == "timeout"
    assert ad._approval_cards == {}


def test_without_a_request_id_hermes_0_22_resolves_oldest_first(ad):
    async def go():
        req = (await ad._send_exec_approval_prompt(prompt())).message_id
        return await ad.resolve_approval(DEVICE, req, "deny")

    assert asyncio.run(go()) == 1
    assert ad.calls == [("sess", "deny", None)]
    assert ad.queue["sess"] == ["r2"]


def test_without_a_request_id_a_newer_card_waits_for_the_older_one(ad, adapter_mod):  # noqa: F811
    """Hermes 0.22 answers the session's oldest request whichever card was tapped: the newer card must not."""
    async def go():
        first = (await ad._send_exec_approval_prompt(prompt())).message_id
        second = (await ad._send_exec_approval_prompt(prompt())).message_id
        with pytest.raises(adapter_mod.RequestError) as e:
            await ad.resolve_approval(DEVICE, second, "deny")
        assert e.value.code == "bad_request" and ad.calls == []
        assert await ad.resolve_approval(DEVICE, first, "once") == 1
        assert await ad.resolve_approval(DEVICE, second, "deny") == 1

    asyncio.run(go())
    assert ad.calls == [("sess", "once", None), ("sess", "deny", None)]
