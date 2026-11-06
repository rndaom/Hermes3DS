"""Bot pictures (protocol.md §4.8): Bot Mode's look for a profile, drawn as a small PNG for the roster."""

import asyncio
import io
import time

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from h3proto import avatar  # noqa: E402
from h3proto.blobs import BlobStore  # noqa: E402
from test_fetch import adapter_mod  # noqa: E402,F401 - fixture


def _png(data):
    im = Image.open(io.BytesIO(data))
    assert im.format == "PNG"
    return im


def test_defaults_match_bot_mode():
    # Reference values from Bot Mode's JS: h = (h * 31 + charCode) >>> 0; SHAPES[h % 7]; hsl(h % 360 68% 58%)
    assert [(n, avatar._hash(n), avatar.default_shape(n)) for n in ("research", "writer", "planner", "ops-bot")] == [
        ("research", 3944071579, "circle"), ("writer", 3515393139, "drop"), ("planner", 3801080274, "cloud"),
        ("ops-bot", 3042768940, "squircle")]
    assert avatar.default_color("research") == avatar.parse_color("hsl(139, 68%, 58%)")


@pytest.mark.parametrize("text,rgb", [
    ("#3366ff", (0x33, 0x66, 0xFF)), ("#abc", (0xAA, 0xBB, 0xCC)), ("hsl(0 100% 50%)", (255, 0, 0)),
    ("hsl(0, 100%, 50%)", (255, 0, 0)), ("rgb(1 2 3 / 0.5)", (1, 2, 3)), ("rebeccapurple", (102, 51, 153)),
    ("nope", None), ("", None), (12, None), ("#" + "f" * 80, None)])
def test_colors(text, rgb):
    assert avatar.parse_color(text) == rgb


def test_look_uses_bot_mode_settings_and_falls_back(tmp_path):
    spec = avatar.look("research", tmp_path, {"color": "#102030", "shape": "Hexagon"})
    assert (spec["photo"], spec["shape"], spec["color"]) == (None, "hexagon", (16, 32, 48))
    # Generated looks (blobatar, sigil, solids) and junk get the name's classic shape; a bad colour its colour.
    spec = avatar.look("research", tmp_path, {"color": "chartreuse-ish", "shape": "blobatar:7:soft"})
    assert (spec["shape"], spec["color"]) == ("circle", avatar.default_color("research"))
    assert avatar.look("research", tmp_path, None)["shape"] == "circle"


def test_every_shape_draws_with_eyes_that_show():
    for shape in avatar.SHAPES:
        for color in ((20, 20, 40), (240, 230, 120)):
            im = _png(avatar.render({"shape": shape, "color": color})).convert("RGBA")
            assert im.size == (avatar.SIZE, avatar.SIZE)
            assert im.getpixel((0, 0))[3] == 0  # see-through around the shape
            px = [p for p in (im.getpixel((x, y)) for x in range(im.width) for y in range(im.height)) if p[3] == 255]
            assert any(abs(p[0] - color[0]) < 3 and abs(p[1] - color[1]) < 3 for p in px)
            eye = (250, 250, 250) if color[0] < 100 else (24, 24, 32)
            assert any(max(abs(a - b) for a, b in zip(p[:3], eye)) < 12 for p in px), (shape, color)


def test_photo_wins_and_bad_photos_fall_back(tmp_path):
    (tmp_path / "assets").mkdir()
    Image.new("RGB", (300, 120), (200, 10, 10)).save(tmp_path / "assets" / "avatar.jpg")
    spec = avatar.look("research", tmp_path, {"shape": "pill"})
    assert spec["photo"].endswith("avatar.jpg") and spec["stamp"]
    im = _png(avatar.render(spec)).convert("RGB")
    assert im.size == (avatar.SIZE, avatar.SIZE) and im.getpixel((18, 18))[0] > 150  # cover-cropped, opaque
    (tmp_path / "assets" / "avatar.png").write_bytes(b"not a picture")  # .png is looked for first
    spec = avatar.look("research", tmp_path, {"shape": "pill"})
    assert spec["photo"].endswith("avatar.png")
    assert _png(avatar.render(spec)).convert("RGBA").getpixel((0, 0))[3] == 0  # drew the pill instead
    (tmp_path / "assets" / "avatar.png").write_bytes(b"x" * (avatar.PHOTO_MAX + 1))  # Bot Mode never writes these
    assert avatar.look("research", tmp_path, None)["photo"].endswith("avatar.jpg")


def test_roster_items_carry_a_cached_avatar_blob(adapter_mod, tmp_path, monkeypatch):
    class A:
        _blobs = BlobStore(tmp_path / "blobs")

    b = adapter_mod._Backend(A())
    item = lambda: {"id": "research", "_look": avatar.look("research", tmp_path, {"shape": "cloud"})}  # noqa: E731
    first = item()
    b._avatar(first)
    assert "_look" not in first and first["avatar"]["mime"] == "image/png"
    assert _png(A._blobs.original_path(first["avatar"]["id"]).read_bytes()).size == (avatar.SIZE, avatar.SIZE)
    again = item()
    b._avatar(again)
    assert again["avatar"] == first["avatar"]  # drawn once per look
    restyled = {"id": "research", "_look": avatar.look("research", tmp_path, {"shape": "drop"})}
    b._avatar(restyled)
    assert restyled["avatar"]["id"] != first["avatar"]["id"]
    real = time.time
    monkeypatch.setattr(adapter_mod.time, "time", lambda: real() + A._blobs.ttl * 0.6)  # its blob is getting old
    late = {"id": "research", "_look": avatar.look("research", tmp_path, {"shape": "drop"})}
    b._avatar(late)
    assert late["avatar"]["id"] != restyled["avatar"]["id"]
    main = {"id": "default", "_look": None}
    b._avatar(main)
    assert main == {"id": "default"}


def test_no_pillow_means_no_avatar(adapter_mod, tmp_path, monkeypatch):
    class A:
        _blobs = BlobStore(tmp_path / "blobs")

    monkeypatch.setattr(adapter_mod.avatar, "render", lambda spec: None)
    it = {"id": "research", "_look": avatar.look("research", tmp_path, None)}
    asyncio.run(asyncio.to_thread(adapter_mod._Backend(A())._avatar, it))
    assert it == {"id": "research"}
