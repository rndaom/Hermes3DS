"""Reader previews for non-image attachments (docs/protocol.md §4.7 `text`).

Text and code files are read directly; documents (PDF, Word, PowerPoint, Excel, OpenDocument, RTF, EPUB)
go through anydoc (`firecrawl-anydoc`, bundled with Hermes for read_file) into Markdown. Either way the 3DS
gets one small JSON blob: rich text + spans (§4.2), cut at TEXT_LIMIT bytes.
"""

from __future__ import annotations

import codecs
import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Union

from .emoji import emoji_for_3ds
from .richtext import markdown_to_rich

TEXT_LIMIT = 64 * 1024        # UTF-8 bytes of source text kept for the reader
DOC_MAX = 32 * 1024 * 1024    # documents bigger than this aren't converted
SNIFF_MAX = 1024 * 1024       # unknown file types are only tried as text up to this size
TAB = "    "
MIME = "application/json"

MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
PLAIN_EXTS = {".txt", ".text", ".log", ".csv", ".tsv", ".rst", ".adoc", ".org", ".tex", ".srt", ".vtt", ".nfo",
              ".diff", ".patch"}
CODE_EXTS = {".py", ".pyw", ".pyi", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".java", ".kt", ".kts",
             ".go", ".rs", ".swift", ".m", ".mm", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
             ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".jl", ".scala", ".clj", ".hs", ".ml", ".ex", ".exs", ".erl",
             ".dart", ".zig", ".nim", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".sql",
             ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".html", ".htm",
             ".css", ".scss", ".less", ".svg", ".gradle", ".cmake", ".mk", ".make", ".dockerfile", ".proto",
             ".graphql", ".s", ".asm", ".pica", ".glsl", ".hlsl", ".v", ".sv", ".vhd"}
CODE_NAMES = {"makefile", "dockerfile", "cmakelists.txt", "gemfile", "rakefile", "procfile", "justfile",
              ".gitignore", ".gitattributes", ".editorconfig", ".bashrc", ".zshrc", ".profile"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".docm", ".odt", ".rtf", ".epub", ".ppt", ".pps", ".pot", ".pptx", ".pptm",
            ".ppsx", ".ppsm", ".odp", ".xls", ".xlsx", ".xlsm", ".xlsb", ".ods"}

Source = Union[Path, bytes]

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def kind_of(name: str, mime: str) -> Optional[str]:
    """'markdown', 'plain', 'code' or 'doc' for a file the reader can show; 'sniff' when only its content
    can tell; None for media and other binary types."""
    base = os.path.basename(name).lower()
    ext = os.path.splitext(base)[1]
    if ext in DOC_EXTS:
        return "doc"
    if ext in MARKDOWN_EXTS or mime == "text/markdown":
        return "markdown"
    if base in CODE_NAMES or ext in CODE_EXTS:
        return "code"
    if ext in PLAIN_EXTS or mime.startswith("text/"):
        return "plain"
    if mime.startswith(("image/", "audio/", "video/", "font/", "model/")):
        return None
    if mime in ("application/octet-stream", "") and not ext:
        return "sniff"
    return None


def _read_head(src: Source, n: int) -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src[:n])
    with open(src, "rb") as f:
        return f.read(n)


def _decode(data: bytes, cut: bool) -> Optional[str]:
    """UTF-8 (BOM allowed) or UTF-16 with a BOM; None for binary. `cut`: `data` may end mid-character."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        if cut:
            data = data[:len(data) & ~1]
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:8192]:
        return None
    dec = codecs.getincrementaldecoder("utf-8-sig")("strict")
    try:
        return dec.decode(data, final=not cut)  # an incomplete last character stays in the decoder
    except UnicodeDecodeError:
        try:  # not UTF-8: Windows text files are often cp1252
            return data.decode("cp1252")
        except UnicodeDecodeError:
            return None


def _clip(text: str, limit: int, cut: bool = False) -> tuple[str, bool]:
    """At most `limit` UTF-8 bytes, ending at a line break when there is one in the last quarter.
    `cut`: `text` is already the head of something longer (it is trimmed to a line break too)."""
    raw = text.encode("utf-8")
    if len(raw) <= limit and not cut:
        return text, False
    head = raw[:limit].decode("utf-8", errors="ignore")
    nl = head.rfind("\n")
    if nl > len(head) * 3 // 4:
        head = head[:nl]
    return head, True


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", TAB)
    return _CONTROL.sub("", text).rstrip()


_FENCE = re.compile(r"^\s*(```|~~~)")
_NO_JOIN = re.compile(r"^\s*(#{1,6}\s|\||<|([-*_])(\s*\2){2,}\s*$)|^( {4}|\t)")  # heading, table, html, rule, code
_STARTS_BLOCK = re.compile(r"^\s*([-*+]\s|\d+[.)]\s|>)")  # list item, quote: begins its own line


def _unwrap(md: str) -> str:
    """Join Markdown's soft line breaks: a paragraph (or list item / quote) wrapped over several source
    lines becomes one line, so the 3DS wraps it to the screen instead of keeping the source's breaks.
    Code blocks, tables, headings and hard breaks (two trailing spaces, backslash) stay as they are."""
    out: list[str] = []
    in_code = False
    for line in md.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
            out.append(line)
            continue
        prev = out[-1] if out else ""
        if (not in_code and line.strip() and prev.strip() and not _NO_JOIN.match(line)
                and not _STARTS_BLOCK.match(line) and not _NO_JOIN.match(prev) and not _FENCE.match(prev)
                and not prev.endswith(("  ", "\\"))):
            out[-1] = prev.rstrip() + " " + line.strip()
        else:
            out.append(line)
    return "\n".join(out)


def _doc_markdown(src: Source, name: str) -> Optional[str]:
    try:
        import anydoc
    except ImportError:
        return None
    try:
        if isinstance(src, (bytes, bytearray)):
            fmt = anydoc.format_from_bytes(src) or anydoc.format_from_extension(os.path.splitext(name)[1])
            return anydoc.to_markdown_bytes(bytes(src), fmt)
        return anydoc.to_markdown(str(src))
    except Exception:  # scanned (needs OCR), encrypted, malformed, unsupported, resource limits, I/O
        return None


def build_preview(src: Source, name: str, mime: str, size: int) -> Optional[dict[str, Any]]:
    """The reader document `{text, spans?, more?}` for a file, or None when it has no text to show.

    `text`/`spans` follow §4.2 (emoji already replaced); `more` is true when the source was cut.
    Blocking (disk, document conversion): run it in a thread.
    """
    kind = kind_of(name, mime)
    if kind is None or (kind == "sniff" and size > SNIFF_MAX):
        return None
    more = False
    if kind == "doc":
        if size > DOC_MAX:
            return None
        md = _doc_markdown(src, name)
        if md is None:
            return None
        md, more = _clip(_unwrap(_tidy(md)), TEXT_LIMIT)
        text, spans = markdown_to_rich(md)
    else:
        head = _read_head(src, TEXT_LIMIT + 4)
        cut = size > len(head) or len(head) > TEXT_LIMIT
        decoded = _decode(head, cut)
        if decoded is None:
            return None
        body = _tidy(decoded)
        if kind == "markdown":
            body = _unwrap(body)
        body, more = _clip(body, TEXT_LIMIT, cut)
        if kind == "markdown":
            text, spans = markdown_to_rich(body)
        else:  # plain text and code as they are (a whole file in `code` style boxes every line: too busy)
            text, spans = emoji_for_3ds(body), []
    if not text.strip():
        return None
    doc: dict[str, Any] = {"text": text}
    if spans:
        doc["spans"] = spans
    if more:
        doc["more"] = True
    return doc


def encode(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
