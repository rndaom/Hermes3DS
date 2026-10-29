"""Build the app's HOME Menu art: app/meta/icon.png (48x48), banner.png (256x128) and banner.wav.

The icon goes into the SMDH of both the .3dsx (Homebrew Launcher) and the .cia (HOME Menu); the banner
picture and sound are what the HOME Menu shows and plays on the top screen when Hermes is selected (CIA only).

    python tools/build_meta.py              writes app/meta/*.svg, *.png, banner.wav
    python tools/build_meta.py --variant b  the white-on-blue icon instead
    python tools/build_meta.py --wing-c 9   print the wing as a 9x9 coverage table for app/source/ui.c

The SVGs and PNGs are committed, so a normal build doesn't need this script. The PNGs need CairoSVG
(`pip install cairosvg`; on Windows it also needs the Cairo DLL, e.g. from GTK). The WAV needs only the stdlib.
"""
import argparse
import math
import struct
import wave
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "meta"

BLUE = "#0000f2"  # Nous electric blue: structure
INK = "#0d0d24"
PAPER = "#ffffff"
# Hermes wing, same path as design/mockups/lib.mjs wing() (24x24 box).
WING = ("M2 19.5C5.6 11 12.6 5.2 22.6 3c-1.9 2.6-3.8 4.2-6 5.3 2.1-.1 3.9.1 5.5.6-2.6 2.5-5.6 3.9-8.7 4.5 "
        "1.8.4 3.4 1 4.7 1.8C13.3 18.1 7.9 20 2 19.5z")

# ANSI Shadow "HERMES", as on the Connect screen (design/mockups/lib.mjs SHADOW).
SHADOW = {
    "H": ["██╗  ██╗", "██║  ██║", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "E": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
    "R": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "S": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
}


def icon_svg(variant):
    bg, fg = (PAPER, BLUE) if variant == "a" else (BLUE, PAPER)
    frame = f'<rect x="0.5" y="0.5" width="47" height="47" fill="none" stroke="{BLUE}"/>' if variant == "a" else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">\n'
            f'<rect width="48" height="48" fill="{bg}"/>{frame}\n'
            f'<g transform="translate(3 4.5) scale(1.7)"><path d="{WING}" fill="{fg}"/></g>\n</svg>\n')


def logotype(x0, y0, cw=4, lh=7):
    """Draw the block letters as pixel-aligned rects: █ fills a cell, the box lines are 1 px strokes."""
    rows = ["".join(SHADOW[c][r] for c in "HERMES") for r in range(6)]
    out = []
    rect = lambda x, y, w, h: out.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}"/>')
    mid = cw // 2 - 1  # column of a vertical stroke
    for i, row in enumerate(rows):
        for j, ch in enumerate(row):
            x, y = x0 + j * cw, y0 + i * lh
            if ch == "█":
                rect(x, y, cw, lh)
            elif ch == "═":
                rect(x, y + 3, cw, 1)
            elif ch == "║":
                rect(x + mid, y, 1, lh)
            elif ch == "╗":
                rect(x, y + 3, mid + 1, 1); rect(x + mid, y + 3, 1, lh - 3)
            elif ch == "╔":
                rect(x + mid, y + 3, cw - mid, 1); rect(x + mid, y + 3, 1, lh - 3)
            elif ch == "╝":
                rect(x, y + 3, mid + 1, 1); rect(x + mid, y, 1, 4)
            elif ch == "╚":
                rect(x + mid, y + 3, cw - mid, 1); rect(x + mid, y, 1, 4)
    return len(rows[0]) * cw, "".join(out)


def banner_svg():
    w, _ = logotype(0, 0)
    _, letters = logotype((256 - w) // 2, 36)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="128" viewBox="0 0 256 128">\n'
            f'<rect width="256" height="128" fill="{PAPER}"/>\n'
            f'<rect width="256" height="18" fill="{BLUE}"/>\n'
            f'<g transform="translate(8 3) scale(0.5)"><path d="{WING}" fill="{PAPER}"/></g>\n'
            f'<text x="24" y="12.5" font-family="DejaVu Sans, Arial, sans-serif" font-weight="bold" font-size="7" '
            f'letter-spacing="1.6" fill="{PAPER}">HERMES 3DS</text>\n'
            f'<g fill="{BLUE}" shape-rendering="crispEdges">{letters}</g>\n'
            f'<text x="128" y="95" text-anchor="middle" font-family="DejaVu Sans, Arial, sans-serif" font-size="10" '
            f'fill="{INK}">Hermes Agent on your 3DS</text>\n'
            f'<rect y="114" width="256" height="14" fill="{BLUE}"/>\n</svg>\n')


def banner_wav(path, rate=32000):
    """A soft rising A-major arpeggio in the same voice as the app's sounds (app/source/sfx.c)."""
    notes = [(880.0, 0.0), (1108.7, 0.11), (1318.5, 0.22), (1760.0, 0.33)]  # A5 C#6 E6 A6, start times (s)
    n = int(rate * 1.4)
    buf = [0.0] * n
    for f, start in notes:
        s0 = int(start * rate)
        for i in range(n - s0):
            t = i / rate
            env = min(1.0, i / 60) * math.exp(-3.2 * t)
            ph = 2 * math.pi * f * t
            buf[s0 + i] += (math.sin(ph) + 0.18 * math.sin(2 * ph)) * env * 0.3
    fade = int(0.05 * rate)
    for i in range(fade):
        buf[n - 1 - i] *= i / fade
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 30000)) for v in buf))


def wing_c(n):
    """The wing as an n x n coverage table (0..255), the way ui.c's wing_mark() draws it."""
    import io
    import cairosvg
    from PIL import Image
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{n}" height="{n}" viewBox="0 0 24 24"><path d="{WING}"/></svg>'
    im = Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg.encode()))).convert("RGBA")
    rows = [", ".join(f"{im.getpixel((x, y))[3]:3d}" for x in range(n)) for y in range(n)]
    print(f"static const uint8_t WING{n}[{n} * {n}] = {{")
    print("".join(f"    {r},\n" for r in rows), end="")
    print("};")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant", choices="ab", default="a", help="a: blue wing on white (default), b: white on blue")
    ap.add_argument("--wing-c", type=int, metavar="SIZE", help="print the wing coverage table for ui.c and stop")
    args = ap.parse_args()
    if args.wing_c:
        wing_c(args.wing_c)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon.svg").write_text(icon_svg(args.variant), encoding="utf-8")
    (OUT / "banner.svg").write_text(banner_svg(), encoding="utf-8")
    banner_wav(OUT / "banner.wav")
    try:
        import cairosvg
    except ImportError:
        print("cairosvg not installed: wrote the SVGs and banner.wav, left the PNGs as they were")
        return
    cairosvg.svg2png(url=str(OUT / "icon.svg"), write_to=str(OUT / "icon.png"), output_width=48, output_height=48)
    cairosvg.svg2png(url=str(OUT / "banner.svg"), write_to=str(OUT / "banner.png"), output_width=256, output_height=128)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
