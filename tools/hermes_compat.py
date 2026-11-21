"""Does the plugin still fit a newer Hermes? Checks it against Hermes checkouts without a gateway or a model.

Run it with a Hermes environment's Python (it imports Hermes), from anywhere:

    <hermes venv>/bin/python tools/hermes_compat.py                  # the newest release and upstream main
    <hermes venv>/bin/python tools/hermes_compat.py v0.23.1 main     # these tags or branches
    <hermes venv>/bin/python tools/hermes_compat.py --checkout ~/.hermes/hermes-agent
    <hermes venv>/bin/python tools/hermes_compat.py --save-baseline --checkout <oldest supported Hermes>

For each Hermes it:
  1. imports every Hermes name the plugin imports (found by reading the plugin's source) and the few it reaches by
     attribute (GatewayRunner, SessionDB methods, the tool registry);
  2. checks that every method the adapter overrides still accepts what Hermes' base class declares, and that no
     abstract method is missing;
  3. runs the plugin's own reads (commands, skills, tools, models) in a throwaway Hermes home;
  4. runs tools/hermes_integration.py (approvals, clarify, the offline outbox) against it;
  5. lists what changed against the baseline, tools/hermes_compat_baseline.json (parameters of the names used, taken
     from the oldest supported Hermes), and against the shipped slash-command snapshot. These are for reading, not
     failures: a new optional parameter is normal.

Tags and branches are cloned shallow from GitHub into a temporary folder that is removed afterwards, with the
throwaway Hermes home each check runs in. Nothing is installed (HERMES_DISABLE_LAZY_INSTALLS keeps a checkout from
building itself an environment in that home) and no gateway is started. Exit status 1 when a check of 1 to 4 fails on any Hermes.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
BASELINE = Path(__file__).with_name("hermes_compat_baseline.json")
SNAPSHOT = PLUGIN / "h3proto" / "hermes_commands.json"
REPO = "https://github.com/NousResearch/hermes-agent.git"
HERMES_TOP = {"gateway", "hermes_cli", "hermes_constants", "hermes_state", "hermes_yaml", "plugins", "agent", "tools",
              "toolsets"}
# Reached by attribute, not by import (compat.py: the runner the gateway hands the adapter).
RUNNER_ATTRS = ("served_profile_names", "reconcile_served_profiles", "async_session_store")
# Session answers that moved in Hermes 0.23.3 (compat.py `_resume_target`, `_clean_title`): one of each pair is needed.
EITHER = (("hermes_state.SessionDB.resolve_resume_session_id", "persistence_assembly.open_persistence_reader"),
          ("hermes_state.SessionDB.sanitize_title", "persistence.conversation.sessions.sanitize_title"))
SKIP_COMMANDS = {"start", "topic"}  # as tools/build_commands.py


# --- what the plugin uses, from its source ---------------------------------------------------------

def plugin_imports() -> dict[str, list[str]]:
    """{hermes module: [names]} for every `from <hermes module> import ...` in the plugin (tests left out)."""
    found: dict[str, set[str]] = {}
    for path in sorted(PLUGIN.rglob("*.py")):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                    and node.module.split(".")[0] in HERMES_TOP:
                found.setdefault(node.module, set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in HERMES_TOP:
                        found.setdefault(a.name, set())
    return {m: sorted(n) for m, n in sorted(found.items())}


def sessiondb_calls() -> list[str]:
    src = (PLUGIN / "compat.py").read_text(encoding="utf-8")
    return sorted(set(re.findall(r"\b(?:db|_session_db\(\))\.([a-z_]+)\b", src)))


# --- the probe: runs in its own process, with one Hermes checkout first on sys.path ------------------

def _params(obj) -> str:
    import inspect
    try:
        ps = inspect.signature(obj).parameters.values()
    except (TypeError, ValueError):
        return "?"
    return "(" + ", ".join(("*" if p.kind == p.VAR_POSITIONAL else "**" if p.kind == p.VAR_KEYWORD else "")
                           + p.name + ("=" if p.default is not p.empty else "") for p in ps) + ")"


def _has(dotted: str) -> bool:
    """Whether `module.name` or `module.Class.name` is there in the Hermes on the path."""
    import importlib
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except Exception:
            continue
        try:
            for name in parts[cut:]:
                obj = getattr(obj, name)
        except AttributeError:
            return False
        return True
    return False


def probe(hermes: Path) -> dict:
    import asyncio
    import enum
    import importlib
    import inspect

    # The driver gives this process a throwaway Hermes home (never the real one) and removes it afterwards.
    if not os.environ.get("HERMES_HOME", "").startswith(tempfile.gettempdir()):
        raise SystemExit("--probe is run by the driver, with a throwaway HERMES_HOME")
    sys.path.insert(0, str(hermes))
    sys.path.insert(0, str(ROOT))

    out: dict = {"missing": [], "mismatch": [], "abstract": [], "reads": {}, "names": {}, "commands": [],
                 "python": sys.version.split()[0]}
    for mod, names in plugin_imports().items():
        try:
            m = importlib.import_module(mod)
        except Exception as exc:
            out["missing"].append(f"{mod} ({type(exc).__name__}: {exc})")
            continue
        if str(hermes) not in str(getattr(m, "__file__", "") or ""):
            out["missing"].append(f"{mod} was imported from {getattr(m, '__file__', '?')}, not from this checkout")
            continue
        for n in names:
            if not hasattr(m, n):
                out["missing"].append(f"{mod}.{n}")
                continue
            o = getattr(m, n)
            # an Enum's own signature differs between Python versions and says nothing about Hermes
            plain = not callable(o) or (inspect.isclass(o) and issubclass(o, enum.Enum))
            out["names"][f"{mod}.{n}"] = type(o).__name__ if plain else _params(o)

    try:
        from gateway.run import GatewayRunner
        init = inspect.getsource(GatewayRunner.__init__)
        for a in RUNNER_ATTRS:
            if not (hasattr(GatewayRunner, a) or f"self.{a}" in init):
                out["missing"].append(f"GatewayRunner.{a}")
    except Exception as exc:
        out["missing"].append(f"gateway.run.GatewayRunner ({type(exc).__name__}: {exc})")
    try:
        import hermes_state
        out["missing"] += [f"hermes_state.SessionDB.{n}" for n in sessiondb_calls()
                           if not hasattr(hermes_state.SessionDB, n)]
        for pair in EITHER:
            if not any(_has(dotted) for dotted in pair):
                out["missing"].append(" or ".join(pair))
        from tools.registry import registry
        if not hasattr(registry, "get_available_toolsets"):
            out["missing"].append("tools.registry.registry.get_available_toolsets")
    except Exception as exc:
        out["missing"].append(f"hermes_state / tools.registry ({type(exc).__name__}: {exc})")

    try:
        from gateway.platforms.base import BasePlatformAdapter as Base
        import plugin.adapter as adapter
        for name, fn in vars(adapter.ThreeDSAdapter).items():
            if name.startswith("__") or not callable(fn) or not hasattr(Base, name):
                continue
            try:
                theirs = inspect.signature(getattr(Base, name)).parameters
                ours = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                continue
            if any(p.kind == p.VAR_KEYWORD for p in ours.values()):
                continue
            lost = [n for n, p in theirs.items() if n not in ours and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)]
            if lost:
                out["mismatch"].append(f"{name}: Hermes passes {lost}, the adapter does not take them")
            out["names"][f"BasePlatformAdapter.{name}"] = _params(getattr(Base, name))
        out["abstract"] = sorted(getattr(adapter.ThreeDSAdapter, "__abstractmethods__", ()))
        prompt = getattr(importlib.import_module("gateway.platforms.base"), "ExecApprovalPrompt", None)
        out["names"]["ExecApprovalPrompt fields"] = ", ".join(sorted(getattr(prompt, "__dataclass_fields__", {})))
    except Exception as exc:
        out["mismatch"].append(f"the adapter did not import ({type(exc).__name__}: {exc})")

    try:
        import plugin.compat as compat

        async def reads():
            for name, make in (("commands_list", compat.commands_list), ("skills_list", compat.skills_list),
                               ("tools_list", lambda: compat.tools_list("3ds")), ("models_list", compat.models_list)):
                try:
                    got = await make()
                    out["reads"][name] = f"ok ({len(got.get('items', got)) if isinstance(got, dict) else len(got)})"
                except Exception as exc:
                    out["reads"][name] = f"FAILED {type(exc).__name__}: {exc}"
        asyncio.run(reads())
    except Exception as exc:
        out["reads"]["plugin.compat"] = f"FAILED {type(exc).__name__}: {exc}"

    try:  # the gateway's slash commands, as tools/build_commands.py lists them
        from hermes_cli import commands as c
        gate = getattr(c, "_is_gateway_available", None)
        for cmd in c.COMMAND_REGISTRY:
            if cmd.name in SKIP_COMMANDS or (not gate(cmd) if gate else getattr(cmd, "cli_only", False)):
                continue
            out["commands"].append({"name": cmd.name, "args": getattr(cmd, "args_hint", "") or "",
                                    "aliases": list(getattr(cmd, "aliases", ()) or ())})
    except Exception as exc:
        out["missing"].append(f"hermes_cli.commands ({type(exc).__name__}: {exc})")
    return out


# --- the driver ------------------------------------------------------------------------------------

def newest_release() -> str:
    """The highest vX.Y.Z tag upstream (canary and date tags left out)."""
    res = subprocess.run(["git", "ls-remote", "--tags", "--refs", REPO], capture_output=True, text=True, timeout=60)
    tags = re.findall(r"refs/tags/(v\d{1,3}\.\d+\.\d+)$", res.stdout, flags=re.M)  # not the old v2026.9.24 date tags
    if not tags:
        raise SystemExit(f"could not list Hermes' tags: {res.stderr.strip() or 'no vX.Y.Z tag found'}")
    return max(tags, key=lambda t: tuple(int(x) for x in t[1:].split(".")))


def clone(ref: str, into: Path) -> Path:
    dest = into / re.sub(r"[^A-Za-z0-9._-]", "_", ref)
    res = subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref, REPO, str(dest)],
                         capture_output=True, text=True, timeout=600)
    if res.returncode:
        raise SystemExit(f"could not clone Hermes {ref}: {res.stderr.strip()}")
    return dest


def describe(checkout: Path) -> str:
    res = subprocess.run(["git", "-C", str(checkout), "log", "-1", "--format=%h %cs"], capture_output=True, text=True)
    return res.stdout.strip() or "?"


def _env(home: Path) -> dict:
    """A throwaway Hermes home, and no dependency sync: a Hermes checkout that sees a fresh home otherwise starts
    building itself an environment there (hundreds of MB) and restarts the process."""
    return {**os.environ, "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1"}


def run_probe(python: str, checkout: Path, home: Path) -> dict:
    res = subprocess.run([python, str(Path(__file__).resolve()), "--probe", str(checkout)],
                         capture_output=True, text=True, timeout=600, env=_env(home))
    for line in reversed(res.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    tail = (res.stderr or res.stdout).strip().splitlines()[-3:]
    return {"missing": ["the probe did not run: " + " | ".join(tail)], "mismatch": [], "abstract": [], "reads": {},
            "names": {}, "commands": []}


def run_integration(python: str, checkout: Path, home: Path) -> tuple[bool, str]:
    res = subprocess.run([python, str(ROOT / "tools" / "hermes_integration.py"), str(checkout)],
                         capture_output=True, text=True, timeout=900, env=_env(home))
    lines = res.stdout.strip().splitlines()
    failed = [ln for ln in lines if ln.startswith("FAIL")]
    summary = lines[-1] if lines and "passed" in lines[-1] else "did not finish: " + " | ".join(
        (res.stderr or res.stdout).strip().splitlines()[-2:])
    return res.returncode == 0, summary + ("".join(f"\n      {f}" for f in failed))


def _delta(was: str, now: str) -> str:
    """What changed between two parameter (or field) lists, by name."""
    split = lambda t: [x.strip() for x in t.strip("()").split(",") if x.strip()]
    old, new = split(was), split(now)
    added, gone = [x for x in new if x not in old], [x for x in old if x not in new]
    parts = ([f"added {', '.join(added)}"] if added else []) + ([f"removed {', '.join(gone)}"] if gone else [])
    return "; ".join(parts) or f"was {was}, now {now}"


def report(label: str, checkout: Path, got: dict, integration: tuple[bool, str], baseline: dict | None) -> bool:
    ok = True
    print(f"\n=== Hermes {label} ({describe(checkout)}, Python {got.get('python', '?')})")

    def line(good: bool, text: str, items=()) -> None:
        nonlocal ok
        ok &= good
        print(f"{'[ ok ]' if good else '[FAIL]'} {text}")
        for it in items:
            print(f"       {it}")

    line(not got["missing"], f"Hermes names the plugin uses: {len(got['names'])} found"
         + (f", {len(got['missing'])} missing" if got["missing"] else ""), got["missing"])
    line(not got["mismatch"] and not got["abstract"], "The adapter's methods fit Hermes' base class",
         got["mismatch"] + [f"abstract method not implemented: {a}" for a in got["abstract"]])
    bad = [f"{k}: {v}" for k, v in got["reads"].items() if not v.startswith("ok")]
    line(bool(got["reads"]) and not bad, "The plugin's reads (commands, skills, tools, models)", bad)
    line(integration[0], f"Integration test: {integration[1]}")

    notes: list[str] = []
    if baseline:
        was = baseline.get("names", {})
        for name, now in sorted(got["names"].items()):
            if name in was and was[name] != now:
                notes.append(f"{name}: {_delta(was[name], now)}")
    try:
        shipped = {c["name"]: c for c in json.loads(SNAPSHOT.read_text(encoding="utf-8"))}
        new = {c["name"]: c for c in got["commands"]}
        if new:
            added, gone = [n for n in new if n not in shipped], [n for n in shipped if n not in new]
            if added:
                notes.append(f"slash commands not in the 3DS's list: {', '.join(added)}")
            if gone:
                notes.append(f"slash commands the 3DS lists that are gone: {', '.join(gone)}")
            for n in new:
                if n in shipped and new[n]["args"] != (shipped[n].get("args") or ""):
                    notes.append(f"/{n} takes: {new[n]['args'] or '(nothing)'}   (the 3DS's list says: "
                                 f"{shipped[n].get('args') or '(nothing)'})")
    except (OSError, ValueError, KeyError):
        notes.append("the shipped slash-command snapshot could not be read")
    if notes:
        print(f"[info] Changed since the baseline ({(baseline or {}).get('hermes', 'none saved')}); to read, "
              "not failures:")
        for n in notes:
            print(f"       {n}")
    else:
        print("[info] Nothing the plugin uses changed since the baseline.")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("refs", nargs="*", help="Hermes tags or branches to clone (default: the newest release and main)")
    ap.add_argument("--checkout", action="append", default=[], type=Path, help="a Hermes checkout on disk (repeatable)")
    ap.add_argument("--python", default=sys.executable, help="the Hermes environment's Python (default: this one)")
    ap.add_argument("--save-baseline", action="store_true",
                    help="write tools/hermes_compat_baseline.json from the one --checkout given")
    ap.add_argument("--probe", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.probe:
        print(json.dumps(probe(args.probe.expanduser().resolve())))
        return 0

    if args.save_baseline:
        if len(args.checkout) != 1 or args.refs:
            ap.error("--save-baseline takes exactly one --checkout")
        co = args.checkout[0].expanduser().resolve()
        home = Path(tempfile.mkdtemp(prefix="h3ds-compat-home-"))
        try:
            got = run_probe(args.python, co, home)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        if got["missing"] or got["mismatch"] or got["abstract"]:
            print("not saved: the plugin does not fit this Hermes:", *got["missing"], *got["mismatch"], sep="\n  ")
            return 1
        BASELINE.write_text(json.dumps({"hermes": describe(co), "names": got["names"]}, indent=1, sort_keys=True)
                            + "\n", encoding="utf-8")
        print(f"baseline of {len(got['names'])} names from Hermes {describe(co)} -> {BASELINE}")
        return 0

    baseline = None
    try:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"(no baseline at {BASELINE}: the list of changes is left out)")

    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="h3ds-compat-"))
    try:
        targets = [(str(c), c.expanduser().resolve()) for c in args.checkout]
        refs = args.refs or ([] if args.checkout else [newest_release(), "main"])
        for ref in refs:
            ref = newest_release() if ref == "latest" else ref
            print(f"cloning Hermes {ref} ...", flush=True)
            targets.append((ref, clone(ref, tmp)))
        for n, (label, co) in enumerate(targets):
            home = tmp / f"home{n}"
            home.mkdir()
            got = run_probe(args.python, co, home)
            ok &= report(label, co, got, run_integration(args.python, co, home), baseline)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("The plugin fits every Hermes checked." if ok else "The plugin does NOT fit every Hermes checked."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
