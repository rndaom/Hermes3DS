from h3proto import emoji
from h3proto.emoji import CAP, downgrade_message, draws_sprites, emoji_for_3ds, emoji_to_text, sprites_to_text
from h3proto.richtext import markdown_to_rich


def sprite(seq):
    """The sprite code point for an emoji sequence (with or without U+FE0F)."""
    return chr(emoji.BASE + emoji._SPRITES[seq.replace("️", "")])


def is_sprite(ch):
    return emoji._is_sprite(ord(ch))


def text_form(text, spans):
    """What an app without the sprite set receives."""
    d = downgrade_message({"text": text, "spans": spans})
    return d["text"], d["spans"]


def sub(text, span):
    return text.encode("utf-8")[span[0]:span[1]].decode("utf-8")


# --- text form (apps without the sprite set, and emoji the set lacks) ---------------------------------------

def test_curated():
    assert emoji_to_text("📌 Pinned ✅ done ❌ failed 🚀 shipped 👍") == \
        ":pin: Pinned :check: done :x: failed :rocket: shipped :+1:"


def test_selectors_joiners_and_modifiers_dropped():
    assert emoji_to_text("⚠️ careful") == ":warning: careful"  # VS16
    assert emoji_to_text("❤︎") == ":heart:"  # VS15
    assert emoji_to_text("👍🏽") == ":+1:"  # skin tone
    assert emoji_to_text("👨‍💻 and 🧑🏿‍💻") == ":technologist: and :technologist:"
    assert emoji_to_text("👩‍🚀") == ":woman::rocket:"  # unknown ZWJ sequence: joiner dropped
    assert "‍" not in emoji_to_text("a‍b")
    assert emoji_to_text("©️ 2026") == "© 2026"


def test_generic_fallback_uses_unicode_names():
    assert emoji_to_text("🦩") == ":flamingo:"
    assert emoji_to_text("🫠") == ":melting_face:"
    assert emoji_to_text("⛵") == ":sailboat:"  # BMP emoji the font lacks


def test_flags_keycaps_and_math_letters():
    assert emoji_to_text("🇯🇵 🇺🇸") == ":flag_jp: :flag_us:"
    assert emoji_to_text("1️⃣ #⃣") == "[1] [#]"
    assert emoji_to_text("𝐁old") == "Bold"  # astral non-emoji folds via NFKC


def test_font_characters_kept():
    s = "☆★○●◎◆□■△▲▽▼♪♀♂←↑→↓ • — ─ ünïcode 日本語 ™ ©"
    assert emoji_to_text(s) == s
    assert emoji_to_text("➡ next ⬅") == "→ next ←"


def test_nothing_left_above_bmp_and_idempotent():
    s = "Done 🎉🦄🧬 ok 🏴󠁧󠁢󠁳󠁣󠁴󠁿 x"
    out = emoji_to_text(s)
    assert all(ord(c) <= 0xFFFF for c in out)
    assert emoji_to_text(out) == out


def test_spans_stay_correct_after_replacement():
    rich, rspans = markdown_to_rich("📌 **✅ all 🚀** in `x 👍` see [docs 📄](https://e.io/🚀)")
    assert rich == f"{sprite('📌')} {sprite('✅')} all {sprite('🚀')} in x {sprite('👍')} see docs {sprite('📄')}"
    assert [(sub(rich, s), s[2]) for s in rspans] == [
        (f"{sprite('✅')} all {sprite('🚀')}", "b"), (f"x {sprite('👍')}", "code"), (f"docs {sprite('📄')}", "link")]
    text, spans = text_form(rich, rspans)
    assert text == ":pin: :check: all :rocket: in x :+1: see docs :page:"
    got = [(sub(text, s), s[2]) for s in spans]
    assert got == [(":check: all :rocket:", "b"), ("x :+1:", "code"), ("docs :page:", "link")]
    assert spans[-1][3] == "https://e.io/🚀"  # URLs are not rewritten


def test_spans_in_blocks_and_code_fences():
    text, spans = text_form(*markdown_to_rich("# 🚀 Launch\n- ✅ item\n```\nprint('🐍')\n```"))
    assert text == ":rocket: Launch\n• :check: item\nprint(':snake:')"
    assert [(sub(text, s), s[2]) for s in spans] == [(":rocket: Launch", "h1"), ("print(':snake:')", "code")]


# --- sprites --------------------------------------------------------------------------------------------

def test_set_loaded():
    assert emoji.SET_ID and len(emoji._SET_SEQS) > 3000 and CAP == f"emoji:{emoji.SET_ID}"
    assert draws_sprites(["png", CAP])
    assert not draws_sprites(["png", "emoji:00000000"]) and not draws_sprites(None)
    assert emoji.BASE == 0xF0000  # above the font's 16-bit cmap (its button glyphs are U+E000...)


def test_common_and_complex_emoji_become_one_sprite_each():
    for e in ["📌", "✅", "🚀", "👍🏽", "👨‍💻", "🇯🇵", "❤️‍🔥", "🏳️‍🌈", "🧑🏿‍💻", "1️⃣", "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "🫠"]:
        out = emoji_for_3ds(e)
        assert len(out) == 1 and is_sprite(out), e
    assert emoji_for_3ds("👍🏽") != emoji_for_3ds("👍")  # skin tones have their own pictures
    assert emoji_for_3ds("🇯🇵🇺🇸") == sprite("🇯🇵") + sprite("🇺🇸")
    assert emoji_for_3ds("Done 🎉 ok") == f"Done {sprite('🎉')} ok"


def test_font_text_kept_unless_emoji_form_requested():
    s = "☆★○●◎◆□■△▲▽▼♪♀♂←↑→↓ • — ─ ünïcode 日本語 ™ © ®"
    assert emoji_for_3ds(s) == s
    assert emoji_for_3ds("©️ 2026") == sprite("©") + " 2026"  # VS16 asks for the emoji
    assert emoji_for_3ds("⬆️ up") == sprite("⬆") + " up"
    assert emoji_for_3ds("⬆ up") == sprite("⬆") + " up"  # the font has no ⬆: a picture beats a word
    assert emoji_for_3ds("❤︎") == ":heart:"  # VS15 asks for text


def test_missing_from_set_falls_back_to_text():
    assert emoji_for_3ds("🌢 drop") == ":black_droplet: drop"  # a pictograph that is not an emoji
    assert emoji_for_3ds("𝐁old ✓") == "Bold :check:"
    assert emoji_for_3ds("a‍b") == "ab"
    assert emoji_for_3ds("👍🏽‍x") == sprite("👍🏽") + "x"


def test_sprites_idempotent_and_reversible():
    s = "Done 🎉🦄🧬 ok 🏴󠁧󠁢󠁳󠁣󠁴󠁿 x 👨‍💻 1️⃣ ✅ ★"
    out = emoji_for_3ds(s)
    assert emoji_for_3ds(out) == out
    assert emoji_to_text(out) == out  # the text pass keeps already converted sprites
    assert sprites_to_text(out) == emoji_to_text(s)
    assert all(ord(c) <= 0xFFFF for c in sprites_to_text(out))


def test_downgrade_walks_whole_bodies():
    rich, spans = markdown_to_rich("**✅ ok** 🚀")
    body = {"t": "welcome", "chat": {"history": [{"text": rich, "spans": spans, "title": "🚀 x"}]},
            "list": [emoji_for_3ds("📌")], "n": 3, "spans": "not a list"}
    out = downgrade_message(body)
    item = out["chat"]["history"][0]
    assert item["text"] == ":check: ok :rocket:" and sub(item["text"], item["spans"][0]) == ":check: ok"
    assert item["title"] == "🚀 x"  # raw emoji (never sent by the plugin) are not touched
    assert out["list"] == [":pin:"] and out["n"] == 3 and out["spans"] == "not a list"
    assert body["chat"]["history"][0]["text"] == rich  # the original is not modified
