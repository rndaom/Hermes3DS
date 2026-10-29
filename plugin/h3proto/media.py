"""Builds protocol attachments (docs/protocol.md §4.7) from local files or downloaded bytes.

Pillow is imported lazily: without it every attachment degrades to kind `file` (no view/thumb). Audio needs
ffmpeg on PATH (h3proto/audio.py); without it audio files are plain `file` attachments too.
"""

from __future__ import annotations

import io
import mimetypes
import os
from pathlib import Path
from typing import Any, Callable

from . import audio, preview
from .blobs import BlobStore

VIEW_BOX = (800, 800)
THUMB_BOX = (120, 90)
MAX_PIXELS = 32_000_000  # larger non-JPEG sources (JPEG decodes reduced) stay plain files
JPEG_QUALITY = 85
FILE_LIMIT = 16 * 1024 * 1024  # `file` (original, for Save to SD) is omitted above this
MAX_DECODE = 64 * 1024 * 1024  # never hand Pillow a bigger source than this
MAX_AUDIO = 512 * 1024 * 1024  # nor ffmpeg (it streams, but a transcode this long is not worth waiting for)
MAX_NAME = 120


def clean_name(name: str | None, fallback: str = "file") -> str:
    """Last path component, no control characters, at most MAX_NAME characters."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = "".join(c for c in base if c >= " " and c != "\x7f")
    if len(base) > MAX_NAME:
        stem, ext = os.path.splitext(base)
        base = stem[:MAX_NAME - len(ext)] + ext
    return base or fallback


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _flatten(im, size: tuple[int, int]):
    """RGB copy of `im` (alpha flattened onto white) fitting `size`, never upscaled."""
    from PIL import Image
    if im.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").getchannel("A"))
        out = bg
    else:
        out = im.convert("RGB")
    out.thumbnail(size, Image.LANCZOS)
    return out


def _jpeg(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=JPEG_QUALITY, progressive=False, optimize=False)
    return buf.getvalue()


def transcode(src: Any) -> dict[str, Any] | None:
    """Decode an image (path or binary file object) into the viewer + thumbnail pictures.

    Returns `{view, view_mime, w, h, thumb, tw, th, fmt}` (bytes for view/thumb), or None when Pillow is
    missing or cannot read the source. The view fits 800x800 without upscaling, EXIF orientation applied;
    it is JPEG q85 baseline, or PNG when the source has transparency or when PNG comes out smaller (flat
    graphics: plots, screenshots, diagrams). The thumb is a 120x90 JPEG.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(src) as opened:
            fmt = opened.format or ""
            if fmt == "JPEG":  # decode at a reduced scale: the view is at most 800x800 anyway
                opened.draft(opened.mode, (VIEW_BOX[0] * 2, VIEW_BOX[1] * 2))
            if opened.width * opened.height > MAX_PIXELS:  # bound memory before decoding (several copies)
                return None
            opened.load()  # first frame of an animation
            im = ImageOps.exif_transpose(opened)
            if im is opened:
                im = opened.copy()
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        if has_alpha and im.mode not in ("RGBA", "LA"):
            im = im.convert("RGBA")
        if has_alpha:
            view = im.copy()
            view.thumbnail(VIEW_BOX, Image.LANCZOS)
            buf = io.BytesIO()
            view.save(buf, "PNG", optimize=True)
            view_bytes, view_mime = buf.getvalue(), "image/png"
        else:
            view = _flatten(im, VIEW_BOX)
            view_bytes, view_mime = _jpeg(view), "image/jpeg"
            if fmt != "JPEG":  # flat graphics usually compress better losslessly
                buf = io.BytesIO()
                view.save(buf, "PNG", optimize=True)
                if buf.tell() < len(view_bytes):
                    view_bytes, view_mime = buf.getvalue(), "image/png"
        thumb = _flatten(im, THUMB_BOX)
        return {"view": view_bytes, "view_mime": view_mime, "w": view.width, "h": view.height,
                "thumb": _jpeg(thumb), "tw": thumb.width, "th": thumb.height, "fmt": fmt}
    except Exception:  # corrupt/unsupported/decompression bomb: caller falls back to a plain file
        return None


def _assemble(store: BlobStore, name: str, size: int, mime: str, src: Any, try_image: bool, try_audio: bool | None,
              put_original: Callable[[str], dict | None], origin: preview.Source) -> dict[str, Any]:
    """Shared tail of build_attachment / build_attachment_bytes. `origin` is the file's path or bytes."""
    pics = transcode(src) if try_image and size <= MAX_DECODE else None
    if pics is not None and not mime.startswith("image/"):
        from PIL import Image
        mime = Image.MIME.get(pics["fmt"], mime)
    original = put_original(mime) if size <= FILE_LIMIT else None
    sound = None
    if try_audio is None:
        try_audio = audio.is_audio(name, mime)
    if pics is None and size <= MAX_AUDIO and try_audio:
        sound = audio.transcode(origin, name)
    if sound is not None:
        att = {"kind": "audio", "name": name, "size": size, "mime": mime,
               "audio": store.put_bytes(sound["data"], audio.MIME, name, role="audio"),
               "rate": sound["rate"], "block": sound["block"], "ms": sound["ms"]}
        if sound["cut"]:
            att["cut"] = True
        if original:
            att["file"] = original
        return att
    if pics is None:
        att: dict[str, Any] = {"kind": "file", "name": name, "size": size, "mime": mime}
        if original:
            att["file"] = original
        try:
            doc = preview.build_preview(origin, name, mime, size)
        except OSError:
            doc = None
        if doc is not None:
            att["text"] = store.put_bytes(preview.encode(doc), preview.MIME, name, role="text")
        return att
    att = {"kind": "image", "name": name, "size": size,
           "view": store.put_bytes(pics["view"], pics["view_mime"], name, role="view"), "w": pics["w"], "h": pics["h"],
           "thumb": store.put_bytes(pics["thumb"], "image/jpeg", name, role="thumb"), "tw": pics["tw"],
           "th": pics["th"]}
    if original:
        att["file"] = original
    return att


def build_attachment(store: BlobStore, path: str | os.PathLike, name: str | None = None, mime: str | None = None,
                     try_image: bool | None = None, try_audio: bool | None = None) -> dict[str, Any]:
    """Attachment for a local file. Blocking (disk + Pillow): run it in a thread.

    `try_image=None` decodes as an image when the (guessed) mime says image/*; True always tries and falls
    back to kind `file` when Pillow cannot read it; False never does. `try_audio` likewise for kind `audio`
    (None: by mime or extension; True: whenever ffmpeg finds an audio stream, e.g. a voice file).
    """
    p = Path(path)
    name = clean_name(name or p.name)
    mime = mime or guess_mime(name)
    size = p.stat().st_size
    want = mime.startswith("image/") if try_image is None else try_image
    return _assemble(store, name, size, mime, str(p) if want else None, want, try_audio,
                     lambda m: store.put_file(p, m, name), p)


def build_attachment_bytes(store: BlobStore, data: bytes, name: str, mime: str | None = None,
                           try_image: bool | None = None, try_audio: bool | None = None) -> dict[str, Any]:
    """Attachment for in-memory bytes (a downloaded image). Blocking: run it in a thread."""
    name = clean_name(name)
    mime = mime or guess_mime(name)
    want = mime.startswith("image/") if try_image is None else try_image
    return _assemble(store, name, len(data), mime, io.BytesIO(data) if want else None, want, try_audio,
                     lambda m: store.put_bytes(data, m, name), data)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico"}


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS
