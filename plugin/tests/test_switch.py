"""Skills and toolsets turned on and off from the 3DS (docs/protocol.md §4.6), against a stand-in for the parts of
Hermes that keep those lists."""
import ast
import asyncio
import importlib
import sys
import types

import pytest

from test_fetch import adapter_mod  # noqa: F401 - fixture

OFFERED = [("web", "🔍 Web Search & Scraping", "web_search, web_extract"),
           ("terminal", "💻 Terminal & Processes", "terminal, process"),
           ("browser", "🌐 Browser Automation", "navigate, click"),
           ("stt", "🎙 Speech-to-Text", "a setting, not tools"), ("discord", "Discord", "only on Discord")]
SKILLS = ["hermes-agent", "pdf", "poems", "taxes"]


class FakeHermes:
    """config.yaml and the functions `hermes skills` / `hermes tools` save through."""

    def __init__(self):
        self.cfg: dict = {}
        self.saves = 0
        self.registered = True  # the 3ds platform is known, as inside a gateway

    def platform_tools(self, cfg, platform, include_default_mcp_servers=True):
        saved = (cfg.get("platform_toolsets") or {}).get(platform)
        if isinstance(saved, list):
            on = {str(n) for n in saved}
            if f"hermes-{platform}" in on:  # the catch-all name brings everything back
                on |= {"web", "terminal", "browser"}
        elif self.registered:
            on = {"web", "terminal", "browser", f"hermes-{platform}"}
        else:
            on = {f"hermes-{platform}"}
        return on - set((cfg.get("agent") or {}).get("disabled_toolsets") or [])

    def save_platform_tools(self, cfg, platform, keys):
        cfg.setdefault("platform_toolsets", {})[platform] = sorted(keys)
        self.saves += 1

    def save_disabled_skills(self, cfg, disabled, platform=None):
        assert platform == "3ds"  # the list for every platform is never the 3DS's to write
        cfg.setdefault("skills", {}).setdefault("platform_disabled", {})[platform] = sorted(disabled)
        self.saves += 1


@pytest.fixture
def hermes(adapter_mod, monkeypatch):  # noqa: F811
    compat = importlib.import_module("plugin.compat")
    fake = FakeHermes()

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)

    def parse_list(value):
        if isinstance(value, str):
            return [str(x) for x in ast.literal_eval(value)] if value.strip().startswith("[") else [value]
        return [str(x) for x in value or []]

    module("agent")
    module("agent.skill_utils", ESSENTIAL_SKILLS=frozenset({"hermes-agent"}), parse_config_string_list=parse_list)
    module("tools")
    module("tools.skills_tool", sort_skills=lambda skills: sorted(skills, key=lambda s: s["name"]),
           find_all_skills=lambda skip_disabled=False: [{"name": n, "description": f"About {n}", "category": "x"}
                                                        for n in SKILLS])
    module("hermes_cli")
    module("hermes_cli.skills_config", save_disabled_skills=fake.save_disabled_skills)
    module("hermes_cli.tools_config", _get_effective_configurable_toolsets=lambda: list(OFFERED),
           _toolset_allowed_for_platform=lambda key, platform: key != "discord",
           _get_platform_tools=fake.platform_tools, _save_platform_tools=fake.save_platform_tools,
           _platform_default_toolset=lambda platform: f"hermes-{platform}", _CONFIG_ONLY_TOOLSETS={"stt"})
    module("toolsets", resolve_toolset=lambda key: [f"{key}_a", f"{key}_b"])
    monkeypatch.setattr(compat, "_config", lambda: fake.cfg)
    return compat, fake


def run(coro):
    return asyncio.run(coro)


def refused(compat, coro, code):
    with pytest.raises(compat.RequestError) as exc:
        run(coro)
    assert exc.value.code == code
    return exc.value.msg


def test_skills_say_what_this_platform_gets(hermes):
    compat, fake = hermes
    fake.cfg = {"skills": {"disabled": "['taxes']", "platform_disabled": {"3ds": ["poems"], "telegram": ["pdf"]}}}
    rows = {r["name"]: r for r in run(compat.skills_list("3ds"))}
    assert [rows[n]["enabled"] for n in SKILLS] == [True, True, False, False]  # telegram's list is not ours
    assert rows["hermes-agent"]["lock"] == "always" and rows["taxes"]["lock"] == "pc"
    assert "lock" not in rows["pdf"] and "lock" not in rows["poems"]


def test_a_skill_is_switched_for_the_3ds_only(hermes):
    compat, fake = hermes
    fake.cfg = {"skills": {"disabled": ["taxes"], "platform_disabled": {"telegram": ["pdf"]}}}
    assert run(compat.skills_set("3ds", "pdf", False)) == {"name": "pdf", "enabled": False}
    assert fake.cfg["skills"] == {"disabled": ["taxes"], "platform_disabled": {"telegram": ["pdf"], "3ds": ["pdf"]}}
    run(compat.skills_set("3ds", "poems", False))
    assert run(compat.skills_set("3ds", "pdf", True)) == {"name": "pdf", "enabled": True}
    assert fake.cfg["skills"]["platform_disabled"]["3ds"] == ["poems"]
    saves = fake.saves
    run(compat.skills_set("3ds", "pdf", True))  # already on: nothing is written
    assert fake.saves == saves


def test_skills_the_3ds_may_not_switch(hermes):
    compat, fake = hermes
    fake.cfg = {"skills": {"disabled": ["taxes"]}}
    assert "stays on" in refused(compat, compat.skills_set("3ds", "hermes-agent", False), "bad_request")
    assert "hermes skills" in refused(compat, compat.skills_set("3ds", "taxes", True), "bad_request")
    refused(compat, compat.skills_set("3ds", "nope", False), "not_found")
    refused(compat, compat.skills_set("3ds", "pdf", "off"), "bad_request")
    assert fake.saves == 0


def test_tools_list_is_what_hermes_tools_offers_here(hermes):
    compat, fake = hermes
    fake.cfg = {"agent": {"disabled_toolsets": ["browser"]}}
    rows = run(compat.tools_list("3ds"))
    assert [r["toolset"] for r in rows] == ["web", "terminal", "browser"]  # not Discord's, not a setting
    assert rows[0] == {"toolset": "web", "label": "Web Search & Scraping", "desc": "web_search, web_extract",
                       "enabled": True, "tools": ["web_a", "web_b"]}
    assert rows[2]["enabled"] is False and rows[2]["lock"] == "pc"


def test_a_toolset_is_switched_for_the_3ds_only(hermes):
    compat, fake = hermes
    fake.cfg = {"platform_toolsets": {"telegram": ["web"]}}
    assert run(compat.tools_set("3ds", "browser", False)) == {"toolset": "browser", "enabled": False}
    # The first save writes the full choice, without the catch-all that would turn everything back on.
    assert fake.cfg["platform_toolsets"] == {"telegram": ["web"], "3ds": ["terminal", "web"]}
    assert run(compat.tools_set("3ds", "browser", True)) == {"toolset": "browser", "enabled": True}
    assert fake.cfg["platform_toolsets"]["3ds"] == ["browser", "terminal", "web"]
    saves = fake.saves
    run(compat.tools_set("3ds", "web", True))  # already on
    assert fake.saves == saves


def test_tools_the_3ds_may_not_switch(hermes):
    compat, fake = hermes
    fake.cfg = {"agent": {"disabled_toolsets": ["browser"]}}
    assert "hermes tools" in refused(compat, compat.tools_set("3ds", "browser", True), "bad_request")
    refused(compat, compat.tools_set("3ds", "discord", True), "not_found")
    refused(compat, compat.tools_set("3ds", "web", 1), "bad_request")
    assert fake.saves == 0


def test_no_save_when_hermes_cannot_tell_the_chats_tools(hermes):
    """Outside a gateway the 3ds platform resolves to nothing; a save then would leave the chat with no tools."""
    compat, fake = hermes
    fake.registered = False
    refused(compat, compat.tools_set("3ds", "web", False), "unavailable")
    assert fake.saves == 0 and "platform_toolsets" not in fake.cfg
