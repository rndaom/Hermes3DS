import importlib

from test_fetch import adapter_mod  # noqa: F401 - fixture

CFG = {
    "model": {"default": "grok-4.7", "provider": "xai-oauth"},
    "providers": {"bonsai": {"name": "Bonsai", "default_model": "bonsai-27b", "models": {"bonsai-27b": {}}}},
    "fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.6-terra"}],
}


def picks(out):
    return {(p["provider"], m["id"]): m["current"] for p in out["items"] for m in p["models"]}


def test_models_come_from_config_with_the_default_current(adapter_mod):  # noqa: F811
    compat = importlib.import_module("plugin.compat")
    out = compat._model_options(CFG, {})
    assert (out["model"], out["provider"]) == ("grok-4.7", "xai-oauth")
    assert picks(out) == {("xai-oauth", "grok-4.7"): True, ("bonsai", "bonsai-27b"): False,
                          ("openai-codex", "gpt-5.6-terra"): False}


def test_the_configured_default_stays_pickable_after_a_model_switch(adapter_mod):  # noqa: F811
    compat = importlib.import_module("plugin.compat")
    out = compat._model_options(CFG, {"model": "bonsai-27b", "provider": "bonsai"})
    assert (out["model"], out["provider"]) == ("bonsai-27b", "bonsai")
    assert picks(out) == {("bonsai", "bonsai-27b"): True, ("xai-oauth", "grok-4.7"): False,
                          ("openai-codex", "gpt-5.6-terra"): False}
