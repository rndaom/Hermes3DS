"""Build the 3DS slash-command grid's list from a Hermes Agent checkout.

    python tools/build_commands.py <hermes-agent checkout>    e.g. ~/.hermes/hermes-agent at the oldest supported tag

Writes
  plugin/h3proto/hermes_commands.json   [{name, desc, args, aliases}, ...] in Hermes' registry order

Hermes has no public API for its command catalog yet, so the plugin ships this snapshot instead of reading
`hermes_cli.commands` at runtime (catalog rule 9: public surfaces only). Build it from the oldest Hermes the
plugin supports (plugin.yaml `requires_hermes`) so every command in the grid exists on every supported version.
Run it with that checkout's Python (its venv), since it imports the registry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "plugin" / "h3proto" / "hermes_commands.json"
SKIP = {"start", "topic"}  # Telegram-only: platform start pings and DM topics


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    sys.path.insert(0, str(Path(argv[1]).expanduser().resolve()))
    from hermes_cli import commands as c

    gate = getattr(c, "_is_gateway_available", None)
    items = []
    for cmd in c.COMMAND_REGISTRY:
        if cmd.name in SKIP or (not gate(cmd) if gate else getattr(cmd, "cli_only", False)):
            continue
        desc = cmd.describe() if hasattr(cmd, "describe") else getattr(cmd, "description", "")
        items.append({"name": cmd.name, "desc": desc, "args": getattr(cmd, "args_hint", "") or "",
                      "aliases": list(getattr(cmd, "aliases", ()) or ())})
    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(items)} commands -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
