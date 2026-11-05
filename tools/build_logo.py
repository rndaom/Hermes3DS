"""Build the CIA's boot logo: the splash the HOME Menu shows on both screens while Hermes starts.

makerom embeds it with `-logo` (the Makefile's `cia` target runs this script first). The file is what the HOME
Menu reads from the CIA's logo region: an LZ11-compressed DARC of layouts (blyt/*.bclyt), animations
(anim/*.bclan) and textures (timg/*.bclim), with an HMAC-SHA256 of the DARC appended before compression. The
compressed file has to fit in 0x2000 bytes.

The HOME Menu checks that HMAC with a key from its own code, so a logo can only be signed with that key. It is
not in this repository: dump the HOME Menu's .code from your own console (GodMode9) and run --find-key once, which
writes ~/.3ds/logo_hmackey_text (the file name ctr-logobuilder uses). Without the key, `make cia` keeps makerom's
stock "Homebrew" logo.

The structure is the one makerom shipped for years as its "HomebrewLegacy" logo (built by yellows8's
ctr-logobuilder, known to work on retail consoles): per screen one root pane, an "HbRoot0" pane holding the
pictures, the groups G_A_00..G_C_00 and three static scene animations. This script only adds pictures and
materials to it. Every texture is A4 (4-bit alpha), like Nintendo's own logos, and every material is
ctr-logobuilder's "HbMat" (it draws white wherever the texture is opaque); the colour comes from each picture's
vertex colours, as in the stock Homebrew logo. The art is the banner's text block (tools/build_meta.py) at 2x, in
white and light blue on the Hermes blue.

    python tools/build_logo.py                      writes app/build/logo/logo.bin (signed; needs the key)
    python tools/build_logo.py --preview PNG        draws both screens the way the HOME Menu shows them (Pillow)
    python tools/build_logo.py --check FILE         unpacks a logo file, lists it and checks its size and HMAC
    python tools/build_logo.py --find-key CODE      finds the key in the HOME Menu's .code, saves it and stops

Needs only the standard library (the preview needs Pillow).
"""
import argparse
import hashlib
import hmac
import os
import struct
import sys
from pathlib import Path

import build_meta as meta

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "build" / "logo" / "logo.bin"
KEY_FILE = Path.home() / ".3ds" / "logo_hmackey_text"
# SHA-256 of the HOME Menu's 32-byte logo HMAC key (published with PabloMK7's logo tutorial on GBAtemp, 2016):
# lets --find-key recognise the key without this repository holding it.
KEY_SHA256 = "001ecefbf56f3614a21532391a7e303cc62a054f5c7bbbf92a97f9c62443ad15"
LOGO_MAX = 0x2000

BLUE = (0x00, 0x00, 0xF2, 0xFF)  # the app's blue (the banner's is #0000ff only because of RGBA4444)
LIGHT = meta.LIGHT + (0xFF,)
WHITE = meta.WHITE + (0xFF,)


# --- art: coverage bitmaps (rows of 0..255) -------------------------------------------------------------

def blank(w, h):
    return [[0] * w for _ in range(h)]


def put(dst, bm, x, y, scale=1):
    """Draw a bitmap (bools or 0..255) into dst at x, y, each pixel scale x scale."""
    for j, row in enumerate(bm):
        for i, v in enumerate(row):
            v = 255 if v is True else (0 if v is False else v)
            for dy in range(scale):
                for dx in range(scale):
                    yy, xx = y + j * scale + dy, x + i * scale + dx
                    if v and 0 <= yy < len(dst) and 0 <= xx < len(dst[0]):
                        dst[yy][xx] = max(dst[yy][xx], v)


def text_width(text, track):
    return sum(len(meta.SMALL[c][0]) for c in text) + track * (len(text) - 1)


def mark_bitmap():
    return [[meta.MARK_COVER[c] for c in row] for row in meta.MARK]


def screens():
    """Both screens as {width, textures: [(file, bitmap)], pics: [(pane, texture, left, top, w, h, rgba)]}.
    Pixel positions are top-left based; the first picture covers the screen."""
    fill = ("hermes_fill.bclim", [[255] * 16 for _ in range(16)])

    def corners(w, h, inset=6, arm=8, k=2):
        out = []
        for n, (x, y) in enumerate(((inset, inset), (w - inset - arm, inset),
                                    (inset, h - inset - k), (w - inset - arm, h - inset - k))):
            out.append((f"HbCornerH{n}", 0, x, y, arm, k, LIGHT))
        for n, (x, y) in enumerate(((inset, inset), (w - inset - k, inset),
                                    (inset, h - inset - arm), (w - inset - k, h - inset - arm))):
            out.append((f"HbCornerV{n}", 0, x, y, k, arm, LIGHT))
        return out

    # Top: the banner's text block (mark, AGENT, HERMES, rule, ON YOUR 3DS) at 2x, centred.
    word = meta.display_word("HERMES")
    bw, bh = len(word[0]) * 2, 58 * 2
    bx, by = (400 - bw) // 2, (240 - bh) // 2
    light = blank(128, 32)
    put(light, mark_bitmap(), 0, 0, 2)
    put(light, meta.small_text("AGENT", 2), 26, 4, 2)
    white = blank(256, 128)  # from the block's row 26 (the top of HERMES)
    put(white, word, 0, 0, 2)
    put(white, meta.small_text("ON YOUR 3DS", 2), 0, 80, 2)
    top = {
        "width": 400,
        "textures": [fill, ("hermes_top_light.bclim", light), ("hermes_top.bclim", white)],
        "pics": [("HbFill", 0, -24, -24, 448, 288, BLUE),
                 ("HbLight", 1, bx, by, 128, 32, LIGHT),
                 ("HbMat", 2, bx, by + 26, 256, 128, WHITE),
                 ("HbRule", 0, bx, by + 96, bw, 2, LIGHT)] + corners(400, 240),
    }

    # Bottom: two lines of small caps, the way the README banner signs off.
    lines = [("UNOFFICIAL COMMUNITY PROJECT", LIGHT), ("FREE AND OPEN-SOURCE 3DS HOMEBREW", WHITE)]
    texs, pics = [fill], [("HbFill", 0, -24, -24, 368, 288, BLUE)]
    for n, (line, rgba) in enumerate(lines):
        tw = text_width(line, 2)
        bm = blank(256, 8)
        put(bm, meta.small_text(line, 2), 0, 0)
        texs.append((f"hermes_bottom{n}.bclim", bm))
        pics.append(("HbMat" if n else "HbLine0", n + 1, (320 - tw) // 2, 112 + 12 * n, 256, 8, rgba))
    bottom = {"width": 320, "textures": texs, "pics": pics + corners(320, 240)}
    return {"U": top, "D": bottom}


# --- BCLIM (A4) ----------------------------------------------------------------------------------------

def bclim_a4(bm):
    """A4 texture: 8x8 tiles left to right, top to bottom, Morton order inside a tile, low nibble first."""
    h, w = len(bm), len(bm[0])
    assert w >= 8 and h >= 8 and w & (w - 1) == 0 and h & (h - 1) == 0, (w, h)
    vals = [0] * (w * h)
    for y in range(h):
        for x in range(w):
            m = 0
            for i in range(3):
                m |= ((x >> i) & 1) << (2 * i) | ((y >> i) & 1) << (2 * i + 1)
            vals[((y // 8) * (w // 8) + x // 8) * 64 + m] = (bm[y][x] * 15 + 127) // 255
    data = bytes(vals[i] | vals[i + 1] << 4 for i in range(0, len(vals), 2))
    footer = struct.pack("<4sHHIII", b"CLIM", 0xFEFF, 0x14, 0x02020000, len(data) + 0x28, 1)
    footer += struct.pack("<4sIHHII", b"imag", 0x10, w, h, 13, len(data))
    return data + footer


# --- BCLYT / BCLAN (after ctr-logobuilder) ---------------------------------------------------------------

class Sections:
    def __init__(self, magic):
        self.magic, self.body, self.count = magic, b"", 0

    def add(self, magic, payload=b""):
        self.body += struct.pack("<4sI", magic, 8 + len(payload)) + payload
        self.count += 1

    def bytes(self):
        return struct.pack("<4sHHIII", self.magic, 0xFEFF, 0x14, 0x02020000, 0x14 + len(self.body),
                           self.count) + self.body


def name(s, n):
    assert len(s) < n, s
    return s.encode().ljust(n, b"\0")


def pane(vis, origin, pname, x, y, z, w, h):
    return (bytes([vis, origin, 0xFF, 0]) + name(pname, 0x18)
            + struct.pack("<10f", x, y, z, 0, 0, 0, 1, 1, w, h))


def material(mname, tex):
    """ctr-logobuilder's HbMat: one texture, clamped, identity SRT, draws white where the texture is opaque."""
    return (name(mname, 0x14) + struct.pack("<7I", 0x00FFFFFF, *[0xFFFFFFFF] * 6)
            + struct.pack("<IHBB5fI", 0x15, tex, 4, 4, 0, 0, 0, 1, 1, 0))


def group(gname, refs):
    return name(gname, 0x10) + struct.pack("<I", len(refs)) + b"".join(name(r, 0x10) for r in refs)


def clyt(screen):
    """screen: {width, textures: [(file, ...)], materials: [(name, texture)] (default: one per texture),
    pics: [(pane, material, x, y, z, w, h, rgba, origin)] in layout units (centre origin, y up)}."""
    w = float(screen["width"])
    s = Sections(b"CLYT")
    s.add(b"lyt1", struct.pack("<I2f", 1, w, 240.0))
    names = b"".join(t[0].encode() + b"\0" for t in screen["textures"])
    offs, o = [], 4 * len(screen["textures"])
    for t in screen["textures"]:
        offs.append(o)
        o += len(t[0]) + 1
    txl = struct.pack("<I%dI" % len(offs), len(offs), *offs) + names
    s.add(b"txl1", txl + b"\0" * (-len(txl) % 4))
    mats = screen["materials"]
    head = 8 + 4 + 4 * len(mats)
    s.add(b"mat1", struct.pack("<I%dI" % len(mats), len(mats), *[head + 0x50 * i for i in range(len(mats))])
          + b"".join(material(m, t) for m, t in mats))
    s.add(b"pan1", pane(1, 4, "HbRootPane", 0, 0, 0, w, 240.0))
    s.add(b"pas1")
    s.add(b"pan1", pane(3, 4, "HbRoot0", 0, 0, 0, 40.0, 40.0))
    s.add(b"pas1")
    for pname, mat, x, y, z, pw, ph, rgba, origin in screen["pics"]:
        # vertex colours (TL, TR, BL, BR), the material, one set of texture coordinates covering the texture
        s.add(b"pic1", pane(1, origin, pname, x, y, z, pw, ph) + bytes(rgba) * 4
              + struct.pack("<HH8f", mat, 1, 0, 0, 1, 0, 0, 1, 1, 1))
    s.add(b"pae1")
    s.add(b"pae1")
    s.add(b"grp1", group("HbRootGrp", []))
    s.add(b"grs1")
    s.add(b"grp1", group("G_A_00", ["HbRoot0", "HbMat"]))
    s.add(b"grp1", group("G_B_00", ["HbMat"]))
    s.add(b"grp1", group("G_C_00", ["HbRoot0"]))
    s.add(b"gre1")
    return s.bytes()


def clan(scene, grp):
    """A static scene animation (no keys) on the G_x_00 group, as ctr-logobuilder writes it."""
    s = Sections(b"CLAN")
    s.add(b"pat1", struct.pack("<IIIiI", 0x10000, 0x1C, 0x28, -15, 1) + name(scene, 0xC) + name(grp, 0x14))
    s.add(b"pai1", struct.pack("<IHHIII", 15, 0, 2, 0x14, 0x1C, 0x40)
          + name("HbMat", 0x14) + struct.pack("<I", 0) + name("HbRoot0", 0x14) + struct.pack("<I", 0))
    return s.bytes()


# --- DARC ----------------------------------------------------------------------------------------------

def darc(files):
    """files: [(path, data)] with paths like 'blyt/x.bclyt', grouped by directory, in the order to store them."""
    dirs = []
    for path, _ in files:
        d = path.split("/")[0]
        if d not in dirs:
            dirs.append(d)
    entries, names = [], b""

    def add_name(s):
        nonlocal names
        o = len(names)
        names += s.encode("utf-16-le") + b"\0\0"
        return o

    total = 2 + len(dirs) + len(files)
    entries.append([add_name("") | 1 << 24, 0, total])
    entries.append([add_name(".") | 1 << 24, 0, total])
    placed = []
    for d in dirs:
        de = [add_name(d) | 1 << 24, 1, 0]
        entries.append(de)
        for path, data in files:
            if path.split("/")[0] == d:
                fe = [add_name(path.split("/", 1)[1]), 0, len(data)]
                entries.append(fe)
                placed.append((fe, data, path.endswith(".bclim")))
        de[2] = len(entries)
    tablelen = 12 * len(entries) + len(names)
    o, blobs = 0x1C + tablelen, []
    for fe, data, tex in placed:
        o += -o % (0x80 if tex else 0x20)
        fe[1] = o
        blobs.append((o, data))
        o += len(data)
    out = bytearray(o)
    first = placed[0][0][1]  # the table's length runs to the first file, as darctool writes it
    struct.pack_into("<4sHHIIIII", out, 0, b"darc", 0xFEFF, 0x1C, 0x01000000, o, 0x1C, first - 0x1C, first)
    table = b"".join(struct.pack("<3I", *e) for e in entries) + names
    out[0x1C:0x1C + len(table)] = table
    for off, data in blobs:
        out[off:off + len(data)] = data
    return bytes(out)


def darc_list(d):
    magic, _, _, _, size, tab, _, _ = struct.unpack_from("<4sHHIIIII", d, 0)
    assert magic == b"darc" and size <= len(d)
    n = struct.unpack_from("<3I", d, tab)[2]
    names = tab + 12 * n
    out, ends = [], []
    for k in range(n):
        no, off, sz = struct.unpack_from("<3I", d, tab + 12 * k)
        e = d.index(b"\0\0", names + (no & 0xFFFFFF))
        while (e - names - (no & 0xFFFFFF)) % 2:
            e = d.index(b"\0\0", e + 1)
        nm = d[names + (no & 0xFFFFFF):e].decode("utf-16-le")
        while ends and k >= ends[-1][0]:
            ends.pop()
        if no >> 24:
            ends.append((sz, nm))
        else:
            out.append(("/".join(x[1] for x in ends[2:]) + "/" + nm, off, sz))
    return out, size


# --- LZ11 ----------------------------------------------------------------------------------------------

def lz11_compress(src):
    out = bytearray([0x11]) + struct.pack("<I", len(src))[:3]
    heads, i, n = {}, 0, len(src)
    while i < n:
        flag_at = len(out)
        out.append(0)
        for bit in range(8):
            if i >= n:
                break
            best, bdisp = 0, 0
            if i + 3 <= n:
                for j in reversed(heads.get(src[i:i + 3], [])[-256:]):
                    if i - j > 0x1000:
                        break
                    k, lim = 3, min(n - i, 0x10110)
                    while k < lim and src[j + k] == src[i + k]:
                        k += 1
                    if k > best:
                        best, bdisp = k, i - j
                        if k == lim:
                            break
            if best >= 3:
                d = bdisp - 1
                if best <= 0x10:
                    out += bytes([(best - 1) << 4 | d >> 8, d & 0xFF])
                elif best <= 0x110:
                    m = best - 0x11
                    out += bytes([m >> 4, (m & 0xF) << 4 | d >> 8, d & 0xFF])
                else:
                    m = best - 0x111
                    out += bytes([0x10 | m >> 12, (m >> 4) & 0xFF, (m & 0xF) << 4 | d >> 8, d & 0xFF])
                out[flag_at] |= 0x80 >> bit
                step = best
            else:
                out.append(src[i])
                step = 1
            for p in range(i, min(i + step, n - 2)):
                heads.setdefault(src[p:p + 3], []).append(p)
            i += step
    return bytes(out)


def lz11_decompress(d):
    assert d[0] == 0x11, "not LZ11"
    size, i = d[1] | d[2] << 8 | d[3] << 16, 4
    if size == 0:
        size, i = struct.unpack_from("<I", d, 4)[0], 8
    o = bytearray()
    while len(o) < size:
        flags = d[i]
        i += 1
        for b in range(8):
            if len(o) >= size:
                break
            if flags & 0x80 >> b:
                t = d[i] >> 4
                if t == 0:
                    n, disp, i = ((d[i] & 0xF) << 4 | d[i + 1] >> 4) + 0x11, ((d[i + 1] & 0xF) << 8 | d[i + 2]) + 1, i + 3
                elif t == 1:
                    n = ((d[i] & 0xF) << 12 | d[i + 1] << 4 | d[i + 2] >> 4) + 0x111
                    disp, i = ((d[i + 2] & 0xF) << 8 | d[i + 3]) + 1, i + 4
                else:
                    n, disp, i = t + 1, ((d[i] & 0xF) << 8 | d[i + 1]) + 1, i + 2
                for _ in range(n):
                    o.append(o[-disp])
            else:
                o.append(d[i])
                i += 1
    return bytes(o), i


# --- the key -------------------------------------------------------------------------------------------

def load_key(path):
    try:
        key = bytes.fromhex(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None
    return key if hashlib.sha256(key).hexdigest() == KEY_SHA256 else None


def blz_decompress(d):
    """The reverse LZ77 the ExeFS .code is stored in (in case the dump is still compressed)."""
    top_bottom, extra = struct.unpack_from("<II", d, len(d) - 8)
    top, bottom = top_bottom & 0xFFFFFF, top_bottom >> 24
    out = bytearray(d) + bytearray(extra)
    src, dst, end = len(d) - bottom, len(out), len(d) - top
    while src > end:
        src -= 1
        flags = out[src]
        for _ in range(8):
            if src <= end:
                break
            if flags & 0x80:
                src -= 2
                v = out[src] | out[src + 1] << 8
                n, disp = (v >> 12) + 3, (v & 0xFFF) + 3
                for _ in range(n):
                    dst -= 1
                    out[dst] = out[dst + disp]
            else:
                src -= 1
                dst -= 1
                out[dst] = out[src]
            flags = flags << 1 & 0xFF
    return bytes(out)


def find_key(code_path, key_path):
    data = Path(code_path).read_bytes()
    tries = [data]
    try:
        tries.append(blz_decompress(data))
    except (struct.error, IndexError):
        pass
    for buf in tries:
        sha = hashlib.sha256
        for i in range(len(buf) - 31):
            if sha(buf[i:i + 32]).hexdigest() == KEY_SHA256:
                key_path = Path(key_path)
                key_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(buf[i:i + 32].hex() + "\n")
                print(f"found the logo key at 0x{i:x}{' (after decompressing)' if buf is not data else ''}; "
                      f"wrote {key_path}")
                return 0
    print(f"no logo key in {code_path}: is it the HOME Menu's .code (title 0004003000008F02 for USA)?")
    return 1


# --- build ---------------------------------------------------------------------------------------------

def to_layout(screen):
    """Pixel rectangles (top-left based) to centre-origin, y-up layout units, one material per texture."""
    w = screen["width"]
    pics = [(pn, tex, left + pw / 2 - w / 2, 120 - (top + ph / 2), 0.0, float(pw), float(ph), rgba, 4)
            for pn, tex, left, top, pw, ph, rgba in screen["pics"]]
    mats = [("HbMat" if any(p[0] == "HbMat" and p[1] == t for p in screen["pics"]) else f"HbTex{t}", t)
            for t in range(len(screen["textures"]))]
    return {"width": w, "textures": screen["textures"], "materials": mats, "pics": pics}


def logo_darc(scr):
    files = []
    for s in "DU":
        for scene, grp in (("SceneOutA", "G_A_00"), ("SceneOutB", "G_B_00"), ("SceneOutC", "G_C_00")):
            files.append((f"anim/NintendoLogo_{s}_00_{scene}.bclan", clan(scene, grp)))
    for s in "DU":
        files.append((f"blyt/NintendoLogo_{s}_00.bclyt", clyt(to_layout(scr[s]))))
    seen = {}
    for s in "DU":
        for fname, bm in scr[s]["textures"]:
            seen.setdefault(fname, bm)
    files += [(f"timg/{f}", bclim_a4(bm)) for f, bm in sorted(seen.items())]
    return darc(files)


def pack(d, key):
    z = lz11_compress(d + hmac.new(key, d, hashlib.sha256).digest())
    if len(z) > LOGO_MAX:
        raise SystemExit(f"logo is 0x{len(z):x} bytes compressed, over the 0x{LOGO_MAX:x} limit")
    return z + b"\0" * (LOGO_MAX - len(z))


def check(path, key_path):
    raw = Path(path).read_bytes()
    dec, used = lz11_decompress(raw)
    files, size = darc_list(dec)
    print(f"{path}: {len(raw)} bytes, {used} compressed (limit {LOGO_MAX}), DARC {size} bytes + {len(dec) - size}")
    for p, off, sz in files:
        print(f"  {p:42s} {sz:6d} at 0x{off:x}")
    ok = len(raw) <= LOGO_MAX and used <= LOGO_MAX and len(dec) == size + 32
    key = load_key(key_path)
    if key and ok:
        good = hmac.compare_digest(hmac.new(key, dec[:size], hashlib.sha256).digest(), dec[size:])
        print("  HMAC " + ("good" if good else "WRONG"))
        ok = good
    elif ok:
        print("  HMAC not checked (no key)")
    return 0 if ok else 1


def preview(scr, path):
    from PIL import Image
    img = Image.new("RGB", (400, 480), (0x0D, 0x0D, 0x24))
    for s, ox, oy in (("U", 0, 0), ("D", 40, 240)):
        w = scr[s]["width"]
        px = [[(0, 0, 0)] * w for _ in range(240)]
        for _, tex, left, top, pw, ph, rgba in scr[s]["pics"]:
            bm = scr[s]["textures"][tex][1]
            th, tw = len(bm), len(bm[0])
            for y in range(max(0, top), min(240, top + ph)):
                for x in range(max(0, left), min(w, left + pw)):
                    a = (bm[(y - top) * th // ph][(x - left) * tw // pw] * 15 + 127) // 255 / 15 * rgba[3] / 255
                    px[y][x] = tuple(round(c + (v - c) * a) for c, v in zip(px[y][x], rgba[:3]))
        img.paste(Image.frombytes("RGB", (w, 240), bytes(c for row in px for p in row for c in p)), (ox, oy))
    img.resize((800, 960), Image.NEAREST).save(path)
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", default=str(OUT), help=f"output file (default {OUT.relative_to(ROOT)})")
    ap.add_argument("--key-file", default=str(KEY_FILE), help=f"the HMAC key as 64 hex digits (default {KEY_FILE})")
    ap.add_argument("--preview", metavar="PNG", help="draw both screens into PNG and stop")
    ap.add_argument("--check", metavar="FILE", help="unpack and check a logo file and stop")
    ap.add_argument("--find-key", metavar="CODE", help="find the key in the HOME Menu's .code, save it and stop")
    args = ap.parse_args()
    if args.find_key:
        return find_key(args.find_key, args.key_file)
    if args.check:
        return check(args.check, args.key_file)
    scr = screens()
    if args.preview:
        preview(scr, args.preview)
        return 0
    key = load_key(args.key_file)
    if key is None:
        print(f"no valid logo key in {args.key_file} (see --find-key)", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pack(logo_darc(scr), key))
    if check(out, args.key_file):
        print(f"{out} failed its check", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
