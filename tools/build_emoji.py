"""Build the 3DS emoji sprite set from Twemoji (graphics CC-BY 4.0, https://github.com/jdecked/twemoji).

    python tools/build_emoji.py                      download Twemoji TWEMOJI_VERSION and build
    python tools/build_emoji.py --src <dir>/72x72    build from already extracted 72x72 PNGs

Writes
  app/romfs/emoji.bin             sprites for the app (format below), read lazily one sprite at a time
  app/romfs/emoji-credits.txt     attribution shipped next to the graphics
  plugin/h3proto/emoji_set.json   the ordered sequence list the plugin matches against

Sprite i is sent as code point BASE + i (Supplementary Private Use Area-A, protocol.md §6). Both sides
must use the same list, so its hash is the set id: the app announces it in hello caps as "emoji:<id>" and
the plugin only sends sprites to a matching app.

emoji.bin, little-endian:
  "H3EM"  u16 format=1  u16 master size M  u32 count  u32 base code point  char[8] set id
  u32 offset[count + 1]          sprite i = bytes [offset[i], offset[i+1]), from the start of the file
  zlib(M x M x RGBA8)            premultiplied alpha, rows top to bottom
Needs Pillow (dev only: `.venv/Scripts/python -m pip install pillow`).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import struct
import sys
import tarfile
import tempfile
import urllib.request
import zlib
from pathlib import Path

from PIL import Image

TWEMOJI_VERSION = "17.0.3"
TARBALL = "https://codeload.github.com/jdecked/twemoji/tar.gz/refs/tags/v{v}"
BASE = 0xF0000
MASTER = 32  # px; the app area-filters this down to the line height (8-17 px on screen)

ROOT = Path(__file__).resolve().parent.parent
OUT_BIN = ROOT / "app" / "romfs" / "emoji.bin"
OUT_CREDITS = ROOT / "app" / "romfs" / "emoji-credits.txt"
OUT_SET = ROOT / "plugin" / "h3proto" / "emoji_set.json"

CREDITS = f"""Emoji graphics: Twemoji {TWEMOJI_VERSION}
Copyright 2019 Twitter, Inc and other contributors; 2024 onward jdecked and other contributors.
Licensed under CC-BY 4.0: https://creativecommons.org/licenses/by/4.0/
Source: https://github.com/jdecked/twemoji
Changes: rasterised PNGs scaled down to {MASTER} px and repacked for the Nintendo 3DS.
"""


def download(version: str, dest: Path) -> Path:
    """Fetch the release tarball and extract only assets/72x72/*.png (nothing else is trusted or needed)."""
    url = TARBALL.format(v=version)
    print(f"downloading {url}")
    with urllib.request.urlopen(url) as r:
        data = r.read()
    out = dest / "72x72"
    out.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/")
            if len(parts) == 4 and parts[1:3] == ["assets", "72x72"] and parts[3].endswith(".png") and m.isfile():
                name = parts[3]
                if "/" in name or "\\" in name or name.startswith("."):
                    continue
                (out / name).write_bytes(tar.extractfile(m).read())
    return out


def seq_of(name: str) -> tuple[int, ...]:
    return tuple(int(p, 16) for p in name.removesuffix(".png").split("-"))


def sprite(path: Path) -> bytes:
    im = Image.open(path).convert("RGBA")
    im = im.convert("RGBa").resize((MASTER, MASTER), Image.Resampling.BOX)  # box filter on premultiplied
    return im.tobytes()  # RGBa: premultiplied RGBA8, rows top to bottom


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, help="directory of Twemoji 72x72 PNGs (default: download)")
    ap.add_argument("--version", default=TWEMOJI_VERSION)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        src = args.src or download(args.version, Path(tmp))
        files = sorted(src.glob("*.png"), key=lambda p: seq_of(p.name))
        if not files:
            print(f"no PNGs in {src}", file=sys.stderr)
            return 1
        # The plugin matches with U+FE0F removed; make sure that keeps sequences unique.
        seen: dict[tuple[int, ...], str] = {}
        for p in files:
            key = tuple(c for c in seq_of(p.name) if c != 0xFE0F)
            if key in seen:
                print(f"duplicate after dropping FE0F: {seen[key]} and {p.name}", file=sys.stderr)
                return 1
            seen[key] = p.name
        seqs = ["-".join(f"{c:x}" for c in seq_of(p.name)) for p in files]
        set_id = hashlib.sha256("\n".join(seqs).encode()).hexdigest()[:8]
        blobs = [zlib.compress(sprite(p), 9) for p in files]

    n = len(blobs)
    head = struct.pack("<4sHHII8s", b"H3EM", 1, MASTER, n, BASE, set_id.encode())
    off = len(head) + 4 * (n + 1)
    table = []
    for b in blobs:
        table.append(off)
        off += len(b)
    table.append(off)
    OUT_BIN.parent.mkdir(parents=True, exist_ok=True)
    OUT_BIN.write_bytes(head + struct.pack(f"<{n + 1}I", *table) + b"".join(blobs))
    OUT_CREDITS.write_text(CREDITS, encoding="utf-8", newline="\n")
    OUT_SET.write_text(json.dumps({"twemoji": args.version, "set": set_id, "base": BASE, "seqs": seqs},
                                  separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    print(f"{n} sprites, set {set_id}, {OUT_BIN.stat().st_size / 1e6:.2f} MB -> {OUT_BIN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
