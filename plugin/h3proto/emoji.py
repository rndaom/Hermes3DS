"""Emoji → colour sprites or short readable text for the 3DS (protocol.md §6).

The 3DS system font has no emoji: they draw as tofu boxes. The app bundles the Twemoji set as sprites
(tools/build_emoji.py), and every outgoing string is passed through `emoji_for_3ds`, which does all the
sequence matching (ZWJ, skin tones, flags, keycaps) and replaces each emoji the set has with one private-use
code point, BASE + sprite index, so the app needs no emoji parsing. Emoji the set lacks become `:name:`
tokens via `emoji_to_text` (a curated table for common ones, the Unicode character name for the rest), which
also drops the invisible joiners/selectors that build emoji sequences.

An app that does not announce the matching set (hello cap "emoji:<set id>") gets every sprite turned back
into its `:name:` text at send time (`downgrade_message`, span offsets included).

What the font can draw is approximated without the font itself: its character map uses 16-bit code
points, so nothing above U+FFFF exists, and among the symbol blocks emoji live in, it holds what Shift-JIS
(cp932) holds (☆★○●◎◆□■△▲▽▼♪♀♂←↑→↓ and the like), which are left alone unless U+FE0F asks for the emoji
form. Dependency-free: stdlib only.
"""

from __future__ import annotations

import bisect
import functools
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

# Curated names. Keys may be ZWJ sequences (matched longest first, after selectors and skin tones are
# removed from both sides, before joiners are dropped); values are inserted verbatim. Arrows map to arrows the font has.
_TABLE: dict[str, str] = {
    # status / marks
    "✅": ":check:", "✔": ":check:", "☑": ":check:", "✓": ":check:", "🗸": ":check:",
    "❌": ":x:", "✖": ":x:", "❎": ":x:", "✗": ":x:", "✘": ":x:",
    "⚠": ":warning:", "🚫": ":no_entry:", "⛔": ":no_entry:", "🛑": ":stop:",
    "❗": ":!:", "❕": ":!:", "❓": ":?:", "❔": ":?:", "‼": ":!!:", "⁉": ":!?:",
    "ℹ": ":info:", "💡": ":bulb:", "🔥": ":fire:", "⭐": ":star:", "🌟": ":star:", "✨": ":sparkles:",
    "💯": ":100:", "🆕": ":new:", "🆗": ":ok:", "🆙": ":up:", "🔄": ":repeat:", "🔁": ":repeat:",
    "🔃": ":repeat:", "⏳": ":hourglass:", "⌛": ":hourglass:", "⏰": ":alarm:", "⏱": ":stopwatch:",
    "▶": ":play:", "⏸": ":pause:", "⏹": ":stop:", "⏩": ":fast_forward:", "⏪": ":rewind:",
    "🔴": ":red_circle:", "🟠": ":orange_circle:", "🟡": ":yellow_circle:", "🟢": ":green_circle:",
    "🔵": ":blue_circle:", "🟣": ":purple_circle:", "⚪": ":white_circle:", "⚫": ":black_circle:",
    "🟥": ":red_square:", "🟧": ":orange_square:", "🟨": ":yellow_square:", "🟩": ":green_square:",
    "🟦": ":blue_square:", "⬛": ":black_square:", "⬜": ":white_square:",
    # arrows the font can draw (←↑→↓ are in it)
    "➡": "→", "⬅": "←", "⬆": "↑", "⬇": "↓", "↔": "<->", "➜": "→", "➔": "→", "➞": "→",
    "↩": ":return:", "↪": ":forward:", "🔙": ":back:", "🔜": ":soon:",
    # work and tools
    "🚀": ":rocket:", "📌": ":pin:", "📍": ":pin:", "📎": ":paperclip:", "🔗": ":link:",
    "📝": ":memo:", "📄": ":page:", "📃": ":page:", "📁": ":folder:", "📂": ":folder:",
    "🗂": ":folders:", "📦": ":package:", "📊": ":chart:", "📈": ":chart_up:", "📉": ":chart_down:",
    "📋": ":clipboard:", "📅": ":calendar:", "📆": ":calendar:", "🗓": ":calendar:",
    "🔍": ":search:", "🔎": ":search:", "🔒": ":lock:", "🔓": ":unlock:", "🔐": ":lock:", "🔑": ":key:",
    "🛠": ":tools:", "🔧": ":wrench:", "🔨": ":hammer:", "⚙": ":gear:", "🐛": ":bug:", "🧪": ":test:",
    "🧠": ":brain:", "🤖": ":robot:", "💻": ":computer:", "🖥": ":desktop:", "📱": ":phone:",
    "⌨": ":keyboard:", "🖱": ":mouse:", "🌐": ":globe:", "🌍": ":earth:", "🌎": ":earth:", "🌏": ":earth:",
    "💾": ":disk:", "🗑": ":trash:", "📧": ":email:", "✉": ":email:", "📨": ":email:", "📬": ":mailbox:",
    "💬": ":speech:", "🗨": ":speech:", "💭": ":thought:", "📢": ":loudspeaker:", "📣": ":megaphone:",
    "🔔": ":bell:", "🔕": ":no_bell:", "🎯": ":target:", "🏁": ":finish:", "🚩": ":flag:",
    "🏆": ":trophy:", "🥇": ":1st:", "🥈": ":2nd:", "🥉": ":3rd:", "💰": ":money:", "💵": ":money:",
    "🛒": ":cart:", "🎮": ":game:", "🕹": ":joystick:", "🎵": ":music:", "🎶": ":music:",
    "📷": ":camera:", "📸": ":camera:", "🖼": ":picture:", "🎨": ":art:", "📚": ":books:", "📖": ":book:",
    "✏": ":pencil:", "✍": ":writing:", "🖊": ":pen:", "🏷": ":label:", "🧩": ":puzzle:", "🧹": ":broom:",
    "🎉": ":tada:", "🎊": ":confetti:", "🎁": ":gift:", "☕": ":coffee:", "🍕": ":pizza:",
    "☀": ":sun:", "☁": ":cloud:", "🌙": ":moon:", "⚡": ":zap:", "❄": ":snowflake:", "🌈": ":rainbow:",
    # hands and people
    "👍": ":+1:", "👎": ":-1:", "👏": ":clap:", "🙏": ":pray:", "👋": ":wave:", "👀": ":eyes:",
    "💪": ":muscle:", "🤝": ":handshake:", "✌": ":v:", "👌": ":ok_hand:", "🙌": ":raised_hands:",
    "👉": ":point_right:", "👈": ":point_left:", "👆": ":point_up:", "☝": ":point_up:",
    "👇": ":point_down:", "🤞": ":fingers_crossed:", "🤷": ":shrug:", "🤦": ":facepalm:",
    # faces
    "😀": ":grin:", "😁": ":grin:", "😃": ":smiley:", "😄": ":smile:", "😆": ":laughing:",
    "😅": ":sweat_smile:", "😂": ":joy:", "🤣": ":rofl:", "😊": ":blush:", "🙂": ":slight_smile:",
    "☺": ":relaxed:", "😉": ":wink:", "😍": ":heart_eyes:", "🥰": ":smiling_hearts:", "😘": ":kiss:",
    "😎": ":sunglasses:", "🤓": ":nerd:", "🤔": ":thinking:", "🧐": ":monocle:", "😐": ":neutral:",
    "😑": ":expressionless:", "🙄": ":eye_roll:", "😏": ":smirk:", "😕": ":confused:", "🙁": ":frown:",
    "☹": ":frown:", "😟": ":worried:", "😢": ":cry:", "😭": ":sob:", "😮": ":open_mouth:",
    "😲": ":astonished:", "😳": ":flushed:", "😱": ":scream:", "😨": ":fearful:", "😰": ":anxious:",
    "😡": ":rage:", "😠": ":angry:", "😤": ":huff:", "😴": ":sleeping:", "🥱": ":yawn:",
    "🥳": ":party:", "😬": ":grimace:", "🙃": ":upside_down:", "😇": ":innocent:", "🤯": ":mind_blown:",
    "🥲": ":smile_tear:", "😵": ":dizzy:", "🤗": ":hug:", "🤫": ":shush:", "🤐": ":zipper_mouth:",
    "😶": ":no_mouth:", "🫡": ":salute:", "🤩": ":star_struck:", "😋": ":yum:", "😜": ":wink_tongue:",
    "😛": ":tongue:", "🤪": ":zany:", "💀": ":skull:", "👻": ":ghost:", "💩": ":poop:", "👽": ":alien:",
    # hearts
    "❤": ":heart:", "♥": ":heart:", "💔": ":broken_heart:", "🧡": ":orange_heart:", "💛": ":yellow_heart:",
    "💚": ":green_heart:", "💙": ":blue_heart:", "💜": ":purple_heart:", "🖤": ":black_heart:",
    "🤍": ":white_heart:", "💖": ":sparkling_heart:", "💕": ":hearts:",
    # common sequences (matched before ZWJ/selectors are dropped)
    "❤️‍🔥": ":heart_on_fire:", "🏳️‍🌈": ":rainbow_flag:", "🏳️‍⚧️": ":transgender_flag:",
    "🏴‍☠️": ":pirate_flag:", "👨‍💻": ":technologist:", "👩‍💻": ":technologist:", "🧑‍💻": ":technologist:",
    "😶‍🌫️": ":face_in_clouds:", "😮‍💨": ":exhale:", "🐕‍🦺": ":service_dog:",
}

_KEYCAP = "⃣"
_KEYCAP_BASES = set("0123456789#*")


def _modifier(cp: int) -> bool:
    """Variation selectors and skin-tone modifiers: they only restyle the previous character."""
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or 0x1F3FB <= cp <= 0x1F3FF


def _invisible(cp: int) -> bool:
    """Zero-width joiner and tag characters (subdivision flags): dropped after sequence matching."""
    return cp == 0x200D or 0xE0020 <= cp <= 0xE007F


def _strip_modifiers(s: str) -> str:
    return "".join(c for c in s if not _modifier(ord(c)))


_SEQS: dict[str, list[str]] = {}
for _k, _v in list(_TABLE.items()):
    if len(_k) > 1:
        del _TABLE[_k]
        _TABLE[_strip_modifiers(_k)] = _v
for _k in sorted((k for k in _TABLE if len(k) > 1), key=len, reverse=True):
    _SEQS.setdefault(_k[0], []).append(_k)


def _regional(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


# BMP blocks that hold emoji. Inside them, a character the font has (cp932-encodable) is kept.
_BMP_RANGES = ((0x203C, 0x203C), (0x2049, 0x2049), (0x2139, 0x2139), (0x2194, 0x21FF), (0x2300, 0x23FF),
               (0x24C2, 0x24C2), (0x25A0, 0x25FF), (0x2600, 0x27BF), (0x2900, 0x297F), (0x2B00, 0x2BFF),
               (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299))
# Astral blocks that are pictographs; anything else above U+FFFF is tried with NFKC first (𝐀 → A).
_ASTRAL_EMOJI = ((0x1F000, 0x1FAFF),)


def _in(cp: int, ranges) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def _font_has(ch: str) -> bool:
    try:
        ch.encode("cp932")
        return True
    except UnicodeEncodeError:
        return False


def _name(ch: str) -> str:
    name = unicodedata.name(ch, "")
    if not name:
        return f":U+{ord(ch):04X}:"
    return ":" + name.lower().replace(" ", "_").replace("-", "_") + ":"


@functools.lru_cache(maxsize=4096)
def _replace(ch: str) -> str | None:
    """Replacement for one code point, or None to keep it."""
    if ch in _TABLE:
        return _TABLE[ch]
    cp = ord(ch)
    if cp > 0xFFFF:
        if not _in(cp, _ASTRAL_EMOJI):
            folded = unicodedata.normalize("NFKC", ch)
            if folded != ch and all(ord(c) <= 0xFFFF for c in folded):
                return folded
        return _name(ch)
    if _in(cp, _BMP_RANGES) and not _font_has(ch):
        return _name(ch)
    return None


def emoji_to_text(s: str) -> str:
    """Replace emoji with `:name:` tokens and drop emoji joiners/selectors. Idempotent."""
    if s.isascii():
        return s
    s = _strip_modifiers(s)
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        seqs = _SEQS.get(ch)
        if seqs:
            hit = next((q for q in seqs if s.startswith(q, i)), None)
            if hit:
                out.append(_TABLE[hit])
                i += len(hit)
                continue
        cp = ord(ch)
        if ch in _KEYCAP_BASES:  # keycap: 1 (FE0F) 20E3 → [1]
            if i + 1 < n and s[i + 1] == _KEYCAP:
                out.append(f"[{ch}]")
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        if _invisible(cp) or ch == _KEYCAP:
            i += 1
            continue
        if cp < 0x203C or _is_sprite(cp):  # sprites are already converted
            out.append(ch)
            i += 1
            continue
        if _regional(cp):  # a pair is a flag: 🇺🇸 → :flag_us:
            a = chr(cp - 0x1F1E6 + ord("a"))
            if i + 1 < n and _regional(ord(s[i + 1])):
                out.append(f":flag_{a}{chr(ord(s[i + 1]) - 0x1F1E6 + ord('a'))}:")
                i += 2
            else:
                out.append(a.upper())
                i += 1
            continue
        rep = _replace(ch)
        out.append(ch if rep is None else rep)
        i += 1
    return "".join(out)


# --- sprites ---------------------------------------------------------------------------------------------

_SET_FILE = Path(__file__).with_name("emoji_set.json")
_VS = {"️": 1, "︎": -1}  # emoji / text presentation of the previous character


def _load_set() -> tuple[str | None, int, list[str]]:
    try:
        data = json.loads(_SET_FILE.read_text(encoding="utf-8"))
        seqs = ["".join(chr(int(p, 16)) for p in s.split("-")) for s in data["seqs"]]
        return str(data["set"]), int(data["base"]), seqs
    except (OSError, ValueError, KeyError, TypeError):
        return None, 0xF0000, []  # no set: everything stays text


SET_ID, BASE, _SET_SEQS = _load_set()
CAP = f"emoji:{SET_ID}" if SET_ID else None  # hello cap of an app that draws this set
_SPRITES: dict[str, int] = {s.replace("️", ""): i for i, s in enumerate(_SET_SEQS)}
_SPRITE_SEQS: dict[str, list[str]] = {}
for _k in sorted((k for k in _SPRITES if len(k) > 1), key=len, reverse=True):
    _SPRITE_SEQS.setdefault(_k[0], []).append(_k)
_SPRITE_RE = re.compile(f"[{chr(BASE)}-{chr(BASE + len(_SET_SEQS) - 1)}]") if _SET_SEQS else None


def _is_sprite(cp: int) -> bool:
    return BASE <= cp < BASE + len(_SET_SEQS)


def draws_sprites(caps: Any) -> bool:
    """True when a hello `caps` list says the app draws this sprite set."""
    return CAP is not None and isinstance(caps, list) and CAP in caps


def _single_ok(ch: str, pres: int) -> bool:
    """A lone code point becomes a sprite unless it is text the font can draw (and no U+FE0F asks otherwise)."""
    if pres:
        return pres > 0
    cp = ord(ch)
    return cp > 0xFFFF or (_in(cp, _BMP_RANGES) and not _font_has(ch))


def emoji_for_3ds(s: str) -> str:
    """Replace emoji with sprite code points where the set has them, else as `emoji_to_text`. Idempotent."""
    if s.isascii() or not _SPRITES:
        return emoji_to_text(s)
    chars: list[str] = []
    pres: list[int] = []
    for ch in s:
        v = _VS.get(ch)
        if v is None:
            chars.append(ch)
            pres.append(0)
        elif pres:
            pres[-1] = v
    t = "".join(chars)
    out: list[str] = []
    i, n, plain = 0, len(t), 0
    while i < n:
        ch = t[i]
        hit = next((q for q in _SPRITE_SEQS.get(ch, ()) if t.startswith(q, i)), None)
        if hit is None and ch in _SPRITES and _single_ok(ch, pres[i]):
            hit = ch
        if hit is None:
            i += 1
            continue
        out.append(emoji_to_text(t[plain:i]))
        out.append(chr(BASE + _SPRITES[hit]))
        i += len(hit)
        while i < n and _modifier(ord(t[i])):  # a skin tone the set has no picture for
            i += 1
        plain = i
    out.append(emoji_to_text(t[plain:]))
    return "".join(out)


@functools.lru_cache(maxsize=1024)
def _sprite_text(ch: str) -> str:
    return emoji_to_text(_SET_SEQS[ord(ch) - BASE])


def sprites_to_text(s: str) -> str:
    """Sprite code points → `:name:` text, for an app without the sprite set."""
    if _SPRITE_RE is None or s.isascii():
        return s
    return _SPRITE_RE.sub(lambda m: _sprite_text(m.group()), s)


def _downgrade_rich(text: str, spans: list) -> tuple[str, list]:
    # Byte offsets move by the size change of every sprite before them; spans never cut a sprite.
    at: list[int] = []     # old byte offset just after each sprite
    shift: list[int] = []  # cumulative size change up to and including it
    parts: list[str] = []
    pos = last = total = 0
    for m in _SPRITE_RE.finditer(text):
        pos += len(text[last:m.start()].encode("utf-8")) + 4
        rep = _sprite_text(m.group())
        total += len(rep.encode("utf-8")) - 4
        parts += [text[last:m.start()], rep]
        at.append(pos)
        shift.append(total)
        last = m.end()
    if not at:
        return text, spans
    parts.append(text[last:])

    def move(o: Any) -> Any:
        if not isinstance(o, int):
            return o
        k = bisect.bisect_right(at, o)
        return o + (shift[k - 1] if k else 0)

    out = [[move(sp[0]), move(sp[1]), *sp[2:]] if isinstance(sp, list) and len(sp) >= 2 else sp for sp in spans]
    return "".join(parts), out


def downgrade_message(msg: Any) -> Any:
    """Copy of an outgoing JSON body with every sprite as `:name:` text and span offsets adjusted."""
    if _SPRITE_RE is None:
        return msg
    if isinstance(msg, str):
        return sprites_to_text(msg)
    if isinstance(msg, list):
        return [downgrade_message(v) for v in msg]
    if not isinstance(msg, dict):
        return msg
    out = {k: (v if k == "spans" else downgrade_message(v)) for k, v in msg.items()}
    text, spans = msg.get("text"), msg.get("spans")
    if isinstance(text, str) and isinstance(spans, list):
        out["text"], out["spans"] = _downgrade_rich(text, spans)
    return out
