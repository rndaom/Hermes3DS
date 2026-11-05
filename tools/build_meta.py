"""Build the app's HOME Menu art: app/meta/icon.png (48x48), banner.png (256x128) and banner.wav.

The icon goes into the SMDH of both the .3dsx (Homebrew Launcher) and the .cia (HOME Menu); the banner
picture and sound are what the HOME Menu shows and plays on the top screen when Hermes is selected (CIA only).

The look ("Marble"): the Met's marble statue of Hermes as blue dot-matrix on Nous blue, the way Nous Research
renders classical sculpture, with a pixel wordmark and the caduceus mark. Everything is drawn pixel by pixel in
colours that survive the hardware: bannertool stores the banner as RGBA4444 (so every channel is a multiple of
0x11 here) and the SMDH icon is RGB565. No fonts are used.

    python tools/build_meta.py                      writes app/meta/icon.png, banner.png, banner.wav
    python tools/build_meta.py --mark-c             print the 9x9 caduceus coverage table for app/source/ui.c
    python tools/build_meta.py --marble-from JPG    rebuild tools/art/hermes-marble.png from the Met's photo

The PNGs and the WAV are committed, so a normal build doesn't need this script. The PNGs need Pillow
(`pip install pillow`); the WAV needs only the stdlib.

Source photo: "Marble statue of Hermes", Roman, 1st or 2nd century CE, The Metropolitan Museum of Art 56.234.15,
https://www.metmuseum.org/art/collection/search/254925, image DP253642.jpg (Open Access, CC0).
tools/art/hermes-marble.png is the head-and-shoulders crop of it as luminance plus a statue mask.
"""
import argparse
import math
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "meta"
MARBLE = ROOT / "tools" / "art" / "hermes-marble.png"
MARBLE_BOX = (1000, 200, 1900, 1100)  # crop of DP253642.jpg (2882x3842) kept in hermes-marble.png, at half size

# RGBA4444-exact colours (each channel a multiple of 0x11). The app's #0000f2 blue lands on #0000ff on the banner.
BLUE = (0, 0, 255)
NAVY = (0, 0, 136)
LIGHT = (136, 136, 255)
WHITE = (255, 255, 255)

# The Hermes caduceus at 9x9 for the app header and the assistant avatar ('#' full, '+' 150/255).
MARK = ["....#....",
        "##+###+##",
        "+#######+",
        "...###...",
        "..#.#.#..",
        "...###...",
        "..#.#.#..",
        "...###...",
        "....#...."]
MARK_COVER = {"#": 255, "+": 150, ".": 0}

# 5-px caps for the small labels.
SMALL = {
    "A": [".#.", "#.#", "###", "#.#", "#.#"],
    "D": ["##.", "#.#", "#.#", "#.#", "##."],
    "E": ["###", "#..", "##.", "#..", "###"],
    "G": [".##.", "#...", "#.##", "#..#", ".##."],
    "N": ["#..#", "##.#", "#.##", "#..#", "#..#"],
    "O": [".##.", "#..#", "#..#", "#..#", ".##."],
    "R": ["##.", "#.#", "##.", "#.#", "#.#"],
    "S": [".##", "#..", ".#.", "..#", "##."],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "U": ["#.#", "#.#", "#.#", "#.#", "###"],
    "Y": ["#.#", "#.#", ".#.", ".#.", ".#."],
    "3": ["##.", "..#", ".#.", "..#", "##."],
    " ": ["..", "..", "..", "..", ".."],
}

BAYER8 = [[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26], [12, 44, 4, 36, 14, 46, 6, 38],
          [60, 28, 52, 20, 62, 30, 54, 22], [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
          [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]]


# --- bitmaps: a canvas is a list of rows of RGB tuples; a bitmap is a list of rows of bools ---------------

def canvas(w, h, rgb):
    return [[rgb] * w for _ in range(h)]


def stamp(img, bm, x, y, rgb):
    for j, row in enumerate(bm):
        for i, on in enumerate(row):
            if on and 0 <= y + j < len(img) and 0 <= x + i < len(img[0]):
                img[y + j][x + i] = rgb


def small_text(text, track=1):
    rows = [[] for _ in range(5)]
    for k, ch in enumerate(text):
        glyph = SMALL[ch]
        for j in range(5):
            rows[j] += [c == "#" for c in glyph[j]] + ([False] * track if k < len(text) - 1 else [])
    return rows


def display_glyph(ch, w, h, s):
    """Tall compressed caps (after the Rules Compressed headlines on the Hermes site), s px strokes."""
    g = [[False] * w for _ in range(h)]

    def fill(y0, y1, x0, x1):
        for y in range(max(0, y0), min(h, y1)):
            for x in range(max(0, x0), min(w, x1)):
                g[y][x] = True

    def chamfer(corners, k=2):
        for c in corners:
            for d in range(k):
                for e in range(k - d):
                    g[d if c[0] == "t" else h - 1 - d][e if c[1] == "l" else w - 1 - e] = False

    mid = (h - s) // 2
    if ch == "H":
        fill(0, h, 0, s); fill(0, h, w - s, w); fill(mid, mid + s, 0, w)
    elif ch == "E":
        fill(0, h, 0, s); fill(0, s, 0, w); fill(h - s, h, 0, w); fill(mid, mid + s, 0, w - 2)
    elif ch == "R":
        fill(0, h, 0, s); fill(0, s, 0, w - 1); fill(1, mid + s - 1, w - s, w)
        fill(s - 1, s + 1, w - s - 1, w - s); fill(mid, mid + s, 0, w - 1); fill(mid, h, w - s, w)
        g[0][w - 1] = False
        chamfer(["tr"])  # (only the bowl's top corner: the leg runs to the baseline)
        g[mid + s - 1][w - 1] = False
    elif ch == "M":
        c0 = (w - s) // 2
        fill(0, h, 0, s); fill(0, h, w - s, w); fill(0, int(h * 0.55), c0, c0 + s); fill(0, s, 0, w)
    elif ch == "S":
        fill(0, s, 0, w); fill(h - s, h, 0, w); fill(mid, mid + s, 0, w)
        fill(0, mid + s, 0, s); fill(mid, h, w - s, w)
        fill(s, s + max(2, s), w - s, w); fill(h - s - max(2, s), h - s, 0, s)  # terminals
        chamfer(["tl", "tr", "bl", "br"])
    return g


def display_word(text, w=10, h=30, s=3, gap=3):
    rows = [[] for _ in range(h)]
    for k, ch in enumerate(text):
        glyph = display_glyph(ch, w + 4 if ch == "M" else w, h, s)
        for j in range(h):
            rows[j] += glyph[j] + ([False] * gap if k < len(text) - 1 else [])
    return rows


def save_png(img, path):
    from PIL import Image
    data = bytes(c for row in img for p in row for c in p)
    Image.frombytes("RGB", (len(img[0]), len(img)), data).save(path, optimize=True)


# --- the marble ---------------------------------------------------------------------------------------

def marble_tone(box, size, lo, hi, gamma=1.0):
    """Crop hermes-marble.png, scale to size x size, sharpen, map luminance lo..hi to 0..1. Returns (tone, mask)."""
    from PIL import Image, ImageFilter
    la = Image.open(MARBLE).crop(box)
    lum = la.getchannel("L").resize((size, size), Image.LANCZOS)
    lum = lum.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=0))
    mask = la.getchannel("A").resize((size, size), Image.LANCZOS)
    L, M = lum.tobytes(), mask.tobytes()
    tone = [[min(1.0, max(0.0, (L[y * size + x] - lo) / (hi - lo))) ** gamma for x in range(size)] for y in range(size)]
    return tone, [[M[y * size + x] / 255 for x in range(size)] for y in range(size)]


def bayer(tone, mask, inks, bg, ox=0, oy=0):
    """Ordered dither between neighbouring inks (dark to light); outside the statue is bg."""
    n = len(inks) - 1
    out = []
    for y, row in enumerate(tone):
        line = []
        for x, t in enumerate(row):
            v = t * n
            lo = min(n - 1, int(v))
            k = lo + ((v - lo) > BAYER8[(y + oy) % 8][(x + ox) % 8] / 64 + 1 / 128)
            line.append(inks[k] if mask[y][x] >= 0.5 else bg)
        out.append(line)
    return out


def atkinson(tone, mask, inks, bg):
    """Atkinson error diffusion onto evenly spaced levels (crisper than Bayer at icon size)."""
    n = len(inks) - 1
    h, w = len(tone), len(tone[0])
    v = [row[:] for row in tone]
    out = canvas(w, h, bg)
    for y in range(h):
        for x in range(w):
            k = min(n, max(0, int(v[y][x] * n + 0.5)))
            e = (v[y][x] - k / n) / 8
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                if 0 <= x + dx < w and y + dy < h:
                    v[y + dy][x + dx] += e
            if mask[y][x] >= 0.5:
                out[y][x] = inks[k]
    return out


def icon():
    tone, mask = marble_tone((110, 50, 390, 330), 48, 70, 220)
    return atkinson(tone, mask, [NAVY, BLUE, LIGHT, WHITE], BLUE)


def banner():
    size = 168
    tone, mask = marble_tone((0, 0, 450, 450), size, 62, 222, 1.05)
    for y in range(size):  # let the bust sink into the ground and the left edge
        for x in range(size):
            fade = min(1.0, max(0.0, (150 - y) / 40)) * min(1.0, max(0.0, (x - 4) / 30))
            tone[y][x] *= 0.25 + 0.75 * fade
            mask[y][x] *= min(1.0, max(0.0, (158 - y) / 12))
    head = bayer(tone, mask, [NAVY, BLUE, LIGHT, WHITE], BLUE, ox=8, oy=14)
    img = canvas(256, 128, BLUE)
    for y in range(128):
        img[y][:158] = head[y + 14][10:168]
    for y in range(70, 73):  # one glitch slice through the head
        img[y][3:153] = img[y][0:150]

    tx = 156
    stamp(img, [[MARK_COVER[c] > 128 for c in r] for r in MARK], tx, 29, LIGHT)
    stamp(img, small_text("AGENT", 2), tx + 13, 31, LIGHT)
    word = display_word("HERMES")
    stamp(img, word, tx, 42, WHITE)
    img[77][tx:tx + len(word[0])] = [LIGHT] * len(word[0])
    stamp(img, small_text("ON YOUR 3DS", 2), tx, 82, WHITE)
    for x, y, dx, dy in ((3, 3, 1, 1), (252, 3, -1, 1), (3, 124, 1, -1), (252, 124, -1, -1)):  # plate corners
        for k in range(4):
            img[y][x + dx * k] = img[y + dy * k][x] = LIGHT
    return img


def marble_from(jpg):
    """Rebuild tools/art/hermes-marble.png from the Met's DP253642.jpg: luminance + a mask of the (warm) marble."""
    from PIL import Image, ImageChops, ImageFilter
    im = Image.open(jpg).convert("RGB").crop(MARBLE_BOX)
    r, _, b = im.split()
    mask = ImageChops.subtract(r, b).point(lambda v: 255 if v > 12 else 0)  # the backdrop is neutral grey
    mask = mask.filter(ImageFilter.MedianFilter(9)).filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    half = (im.width // 2, im.height // 2)
    out = Image.merge("LA", (im.convert("L").resize(half, Image.LANCZOS), mask.resize(half, Image.LANCZOS)))
    MARBLE.parent.mkdir(parents=True, exist_ok=True)
    out.save(MARBLE, optimize=True)
    print(f"wrote {MARBLE}")


# --- the jingle ---------------------------------------------------------------------------------------

def banner_wav(path, rate=44100):
    """A plucked lyre (Hermes made the first one): D4 A4 F#5 rising, then E5 stepping home to D5 over a soft
    rolled D-major strum (A3 D4 F#4). 0 / 115 / 230 / 390 / 568 ms, 1.7 s. Low and dark (300 Hz high-pass,
    2.4 kHz low-pass, soft fingertip) for the small speakers, stops ringing after ~0.6 s, peaks at -1.1 dBFS
    after 3.1 dB of limiting, and ends on an exact zero.

    Written as 16-bit stereo (the same in both channels) at 44.1 kHz, the format of the homebrew banner sounds
    known to play (FBI, ftpd, Anemone3DS): bannertool packs the WAV as-is, and every mono 32 kHz jingle we made
    played on the HOME Menu as the same high ringing, whatever it contained. The HOME Menu also refuses banner
    sounds of 3 s or more."""

    def hz(m):
        return 440.0 * 2.0 ** ((m - 69) / 12.0)

    def biquad(x, kind, f, q=0.7071):
        w = 2.0 * math.pi * f / rate
        cw, al = math.cos(w), math.sin(w) / (2.0 * q)
        b = {"hp": ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2),
             "lp": ((1 - cw) / 2, 1 - cw, (1 - cw) / 2),
             "bp": (al, 0.0, -al)}[kind]
        a0 = 1.0 + al
        b0, b1, b2, a1, a2 = b[0] / a0, b[1] / a0, b[2] / a0, -2.0 * cw / a0, (1.0 - al) / a0
        y, x1, x2, y1, y2 = [0.0] * len(x), 0.0, 0.0, 0.0, 0.0
        for i, v in enumerate(x):
            o = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, v, y1, o
            y[i] = o
        return y

    def finish(buf, peak_db=-1.1, fade=0.35, limit_db=3.1):
        # 4th-order Butterworth high-pass at 300 Hz and 2nd-order low-pass at 2.4 kHz, raised-cosine fade in
        # (3 ms) and out (to an exact zero), a gentle look-ahead limiter that shaves limit_db off the loudest
        # transients (1.5 ms look-ahead, 60 ms release), then normalise the peak to peak_db.
        buf = biquad(biquad(biquad(buf, "hp", 300.0, 0.5412), "hp", 300.0, 1.3066), "lp", 2400.0)
        n, fi, fo = len(buf), int(0.003 * rate), int(fade * rate)
        for i in range(fi):
            buf[i] *= 0.5 - 0.5 * math.cos(math.pi * i / fi)
        for i in range(fo):
            buf[n - fo + i] *= 0.5 + 0.5 * math.cos(math.pi * i / (fo - 1))
        if limit_db > 0.0:
            thr = max(abs(v) for v in buf) * 10.0 ** (-limit_db / 20.0)
            la, h, rel = int(0.0015 * rate), int(0.0015 * rate) // 2, math.exp(-1.0 / (0.06 * rate))
            want = [min(1.0, thr / abs(v)) if v else 1.0 for v in buf]
            gmin, q = [1.0] * n, []  # sliding minimum over +-la (a monotonic queue)
            head = 0
            for j in range(n + la):
                if j < n:
                    while len(q) > head and want[q[-1]] >= want[j]:
                        q.pop()
                    q.append(j)
                i = j - la
                if i >= 0:
                    while q[head] < i - la:
                        head += 1
                    gmin[i] = want[q[head]]
            pad = [1.0] * h + gmin + [1.0] * h  # smooth it with a box of +-h (h <= la keeps every peak covered)
            acc, g = sum(pad[: 2 * h + 1]), 1.0
            for i in range(n):
                g = min(acc / (2 * h + 1), g + (1.0 - g) * (1.0 - rel))
                if i + 2 * h + 1 < len(pad):
                    acc += pad[i + 2 * h + 1] - pad[i]
                buf[i] *= g
        g = 10.0 ** (peak_db / 20.0) * 32767.0 / max(abs(v) for v in buf)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(struct.pack("<%dh" % (2 * n), *(s for v in buf for s in (int(round(v * g)),) * 2)))

    def pluck(bus, t0, m, amp, ring):
        # A finger-plucked gut string as a sum of decaying partials: pluck-position comb, a soft fingertip
        # (steep roll-off), higher partials dying faster, slight stiffness, and two slightly detuned
        # "polarisations" on the low partials for a living two-stage decay. ring scales the decay time.
        f, s0 = hz(m), int(round(t0 * rate))
        t1 = ring * 2.3 * (440.0 / f) ** 0.6
        pos = 0.16 + 0.012 * (m % 6)
        n = len(bus) - s0
        note = [0.0] * n
        for k in range(1, 40):
            fk = f * k * math.sqrt(1.0 + 1e-4 * k * k)
            if fk > 6000.0:
                break
            a = amp * math.sin(k * math.pi * pos) / k / (1.0 + (fk / 1500.0) ** 2)
            t60 = t1 / (1.0 + 0.45 * (k - 1) + (fk / 2000.0) ** 2)
            for share, tm, cents in (((0.62, 0.75, -0.4), (0.38, 1.35, 0.4)) if k <= 6 else ((1.0, 1.0, 0.0),)):
                r = 10.0 ** (-3.0 / (t60 * tm * rate))
                w = 2.0 * math.pi * fk * 2.0 ** (cents / 1200.0) / rate
                c, r2 = 2.0 * r * math.cos(w), r * r
                y1, y2 = 0.0, -a * share * math.sin(w) / r
                for i in range(1, min(n, int(1.4 * t60 * tm * rate))):
                    y = c * y1 - r2 * y2
                    note[i] += y
                    y2, y1 = y1, y
        att = int(0.0015 * rate)
        for i in range(att):
            note[i] *= 0.5 - 0.5 * math.cos(math.pi * i / att)
        for i in range(n):
            bus[s0 + i] += note[i]

    def soundbox(bus):
        # Two broad wooden body resonances, low enough to warm the strings without adding any ring.
        out = bus[:]
        for f, q, g in ((330.0, 1.5, 0.30), (700.0, 2.0, 0.22)):
            for i, v in enumerate(biquad(bus, "bp", f, q)):
                out[i] += g * v
        return out

    # A whole number of 32-frame blocks, so the second channel's samples start 0x20-aligned in the CWAV like the
    # first's (bannertool puts each channel's samples one after the other).
    bus = [0.0] * ((int(1.70 * rate) + 31) // 32 * 32)
    for t0, m, amp, ring in (
        (0.000, 62, 0.80, 0.30),  # D4
        (0.115, 69, 0.80, 0.30),  # A4
        (0.230, 78, 0.85, 0.30),  # F#5
        (0.390, 76, 0.85, 0.22),  # E5
        (0.520, 57, 0.40, 0.45),  # A3  \
        (0.536, 62, 0.40, 0.45),  # D4   } soft rolled strum under the landing
        (0.552, 66, 0.38, 0.45),  # F#4 /
        (0.568, 74, 1.00, 0.45),  # D5, home
    ):
        pluck(bus, t0, m, amp, ring)
    finish(soundbox(bus))


def mark_c():
    """The caduceus as a 9x9 coverage table (0..255), the way ui.c's hermes_mark() draws it."""
    print("static const uint8_t MARK9[9 * 9] = {")
    for row in MARK:
        print("    " + ", ".join(f"{MARK_COVER[c]:3d}" for c in row) + ",")
    print("};")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mark-c", action="store_true", help="print the 9x9 caduceus coverage table for ui.c and stop")
    ap.add_argument("--marble-from", metavar="JPG", help="rebuild tools/art/hermes-marble.png from DP253642.jpg and stop")
    args = ap.parse_args()
    if args.mark_c:
        mark_c()
        return
    if args.marble_from:
        marble_from(args.marble_from)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    banner_wav(OUT / "banner.wav")
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow not installed: wrote banner.wav, left the PNGs as they were")
        return
    save_png(icon(), OUT / "icon.png")
    save_png(banner(), OUT / "banner.png")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
