"""Bot pictures for the 3DS roster (docs/protocol.md §4.8): the avatar Bot Mode shows for a profile, as a small PNG.

Bot Mode keeps a profile's look in two places. A photo, when one was set, is `<profile>/assets/avatar.png`
(or `.jpg`, `.webp`). Otherwise `profile.yaml` `ui_meta.hermes-bots` names a `color` and a `shape`; a profile
nobody styled gets a shape and colour picked from its name. The classic shapes (circle, squircle, pill,
triangle, hexagon, cloud, drop) are drawn here with Bot Mode's two eyes; its generated looks (blobatars,
sigils, solids) come from a drawing library this side doesn't have, so those bots get their name's classic
shape in their colour instead.

Needs Pillow (the gateway's environment has it); without it `render` returns None and the 3DS draws the
initial it always did.
"""

from __future__ import annotations

import colorsys
import io
import re
from pathlib import Path
from typing import Any

SIZE = 36            # pixels a side: drawn 1:1 on the top screen, at half size in the Bots list
PHOTO_MAX = 2 << 20  # Bot Mode refuses bigger photos; a bigger file here wasn't put there by it
PHOTO_PIXELS = 16_000_000
PHOTO_NAMES = ("avatar.png", "avatar.jpg", "avatar.webp")
SHAPES = ("circle", "squircle", "pill", "triangle", "hexagon", "cloud", "drop")
_SS = 4  # supersampling for smooth edges


def _hash(name: str) -> int:
    """Bot Mode's string hash: `h = (h * 31 + charCode) >>> 0` over UTF-16 code units."""
    h = 0
    data = name.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h = (h * 31 + (data[i] | data[i + 1] << 8)) & 0xFFFFFFFF
    return h


def default_shape(name: str) -> str:
    return SHAPES[_hash(name) % len(SHAPES)]


def default_color(name: str) -> tuple[int, int, int]:
    """`hsl(hash % 360, 68%, 58%)`, the colour Bot Mode gives a profile nobody coloured."""
    r, g, b = colorsys.hls_to_rgb((_hash(name) % 360) / 360.0, 0.58, 0.68)
    return round(r * 255), round(g * 255), round(b * 255)


def parse_color(value: Any) -> tuple[int, int, int] | None:
    """`#rgb`, `#rrggbb`, `rgb(…)`, `hsl(…)` (commas or CSS4 spaces) or a CSS colour name; None otherwise."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    try:
        from PIL import ImageColor
    except ImportError:
        return None
    s = value.strip()
    m = re.fullmatch(r"(rgba?|hsla?)\(\s*([^)]*)\)", s, re.I)
    if m:  # CSS4 `hsl(210 68% 58% / 0.5)` → `hsl(210, 68%, 58%)`; alpha is dropped
        parts = [p for p in re.split(r"[\s,/]+", m.group(2)) if p][:3]
        s = f"{m.group(1).lower().rstrip('a')}({', '.join(parts)})"
    try:
        c = ImageColor.getrgb(s)
    except (ValueError, TypeError):
        return None
    return int(c[0]), int(c[1]), int(c[2])


def photo_path(home: Path) -> Path | None:
    """The profile's photo, if Bot Mode set one."""
    for n in PHOTO_NAMES:
        p = home / "assets" / n
        try:
            if p.is_file() and 0 < p.stat().st_size <= PHOTO_MAX:
                return p
        except OSError:
            continue
    return None


def look(name: str, home: Path, ui: dict | None) -> dict[str, Any]:
    """What `render` draws for profile `name`: `{photo, shape, color}`; also the cache key's ingredients."""
    ui = ui if isinstance(ui, dict) else {}
    shape = str(ui.get("shape") or "").strip().lower()
    if shape not in SHAPES:
        shape = default_shape(name)
    color = parse_color(ui.get("color")) or default_color(name)
    p = photo_path(home)
    stamp = None
    if p is not None:
        try:
            st = p.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            p = None
    return {"photo": str(p) if p else None, "stamp": stamp, "shape": shape, "color": color}


def _photo(path: str):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        if im.width * im.height > PHOTO_PIXELS:
            return None
        im.draft("RGB", (SIZE * 4, SIZE * 4))  # JPEG: decode reduced
        im.load()
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGBA")
    return ImageOps.fit(im, (SIZE, SIZE), Image.LANCZOS)


def _shape_mask(shape: str, n: int):
    """An `n`×`n` L-mode mask of `shape`, with the box the eyes sit in: (cx, cy, eye height)."""
    from PIL import Image, ImageDraw
    m = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(m)
    pad = n * 0.06
    lo, hi = pad, n - pad
    w = hi - lo
    eyes = (n / 2, n * 0.47, n * 0.19)
    if shape == "circle":
        d.ellipse((lo, lo, hi, hi), fill=255)
    elif shape == "squircle":
        d.rounded_rectangle((lo, lo, hi, hi), radius=w * 0.3, fill=255)
    elif shape == "pill":
        top, bot = n * 0.22, n * 0.78
        d.rounded_rectangle((lo, top, hi, bot), radius=(bot - top) / 2, fill=255)
        eyes = (n / 2, n / 2, n * 0.15)
    elif shape == "triangle":
        m = _triangle(n, lo, hi)
        eyes = (n / 2, n * 0.64, n * 0.14)
    elif shape == "hexagon":
        import math
        c, rad = n / 2, w / 2
        d.polygon([(c + rad * math.cos(math.radians(30 + 60 * k)), c + rad * math.sin(math.radians(30 + 60 * k)))
                   for k in range(6)], fill=255)
    elif shape == "cloud":
        for x, y, r in ((0.32, 0.56, 0.22), (0.68, 0.56, 0.22), (0.5, 0.42, 0.27), (0.5, 0.64, 0.2)):
            d.ellipse(((x - r) * n, (y - r) * n, (x + r) * n, (y + r) * n), fill=255)
        eyes = (n / 2, n * 0.52, n * 0.15)
    else:  # drop: a circle at the bottom drawn up to a point
        r = w * 0.36
        cx, cy = n / 2, hi - r
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        d.polygon([(cx, lo), (cx - r * 0.94, cy - r * 0.34), (cx + r * 0.94, cy - r * 0.34)], fill=255)
        eyes = (n / 2, cy, n * 0.15)
    return m, eyes


def _triangle(n: int, lo: float, hi: float):
    """A triangle pointing up with softly rounded corners (drawn blurred and re-thresholded)."""
    from PIL import Image, ImageDraw, ImageFilter
    m = Image.new("L", (n, n), 0)
    ImageDraw.Draw(m).polygon([(n / 2, lo), (lo, hi), (hi, hi)], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(n * 0.04))
    return m.point(lambda v: 255 if v > 150 else 0)


def _classic(shape: str, color: tuple[int, int, int]):
    from PIL import Image, ImageDraw
    n = SIZE * _SS
    mask, (ex, ey, eh) = _shape_mask(shape, n)
    body = Image.new("RGBA", (n, n), color + (255,))
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(body, mask=mask)
    lum = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    eye = (250, 250, 250, 255) if lum < 110 else (24, 24, 32, 255)  # light eyes on a dark body
    d = ImageDraw.Draw(out)
    ew, gap = eh * 0.5, eh * 0.62
    for x in (ex - gap, ex + gap):
        d.ellipse((x - ew / 2, ey - eh / 2, x + ew / 2, ey + eh / 2), fill=eye)
    return out.resize((SIZE, SIZE), Image.LANCZOS)


def render(spec: dict[str, Any]) -> bytes | None:
    """The PNG for a `look()`; the photo when it decodes, else the shape. None without Pillow."""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return None
    im = None
    if spec.get("photo"):
        try:
            im = _photo(spec["photo"])
        except Exception:  # unreadable or not a picture: the shape it would have without one
            im = None
    if im is None:
        try:
            im = _classic(spec.get("shape") or "circle", tuple(spec.get("color") or (128, 128, 128)))
        except Exception:
            return None
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()
