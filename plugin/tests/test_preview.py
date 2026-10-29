"""Reader previews for text, code and documents (h3proto/preview.py, protocol.md §4.7 `text`)."""

import codecs
import json
import sys

import pytest

from h3proto import preview
from h3proto.blobs import BlobStore
from h3proto.media import build_attachment, build_attachment_bytes


@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs")


def doc_of(store, att):
    ref = att["text"]
    assert ref["mime"] == "application/json" and store.meta(ref["id"])["role"] == "text"
    raw = b"".join(d for _, d in store.open_chunks(ref["id"]))
    assert len(raw) == ref["size"]
    doc = json.loads(raw)
    n = len(doc["text"].encode("utf-8"))
    for s in doc.get("spans", []):
        assert 0 <= s[0] < s[1] <= n
    return doc


def make_pdf(pages):
    """A minimal text PDF (Helvetica), one list of lines per page; the first line is set larger."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font = 3 + 2 * len(pages)
    for i, lines in enumerate(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode())
        body = "".join(f"BT /F1 {18 if j == 0 else 12} Tf 72 {720 - j * 20} Td ({t}) Tj ET\n"
                       for j, t in enumerate(lines))
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body}endstream".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs):
        offs.append(len(out))
        out += f"{i + 1} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_markdown_file_gets_styled_reader_text(store, tmp_path):
    src = tmp_path / "patch-notes.md"
    src.write_text("# Patch notes\n\n- **Faster** export\n- Fixed `crash`\n", encoding="utf-8")
    att = build_attachment(store, src)
    assert att["kind"] == "file" and att["mime"] == "text/markdown" and "file" in att
    doc = doc_of(store, att)
    assert doc["text"].startswith("Patch notes\n") and "#" not in doc["text"] and "**" not in doc["text"]
    styles = {s[2] for s in doc["spans"]}
    assert {"h1", "b", "code"} <= styles and "more" not in doc


def test_code_file_is_plain_text_with_tabs_and_crlf_tidied(store, tmp_path):
    src = tmp_path / "export.py"
    src.write_bytes(b"def f():\r\n\treturn 1  # **not bold**\r\n")
    doc = doc_of(store, build_attachment(store, src))
    assert doc["text"] == "def f():\n    return 1  # **not bold**"
    assert "spans" not in doc  # not parsed as Markdown, and no per-line code boxes


@pytest.mark.parametrize("name", ["Makefile", "LICENSE", "notes.txt", "data.csv", "run.log"])
def test_plain_and_named_text_files(store, tmp_path, name):
    src = tmp_path / name
    src.write_text("hello\nworld\n", encoding="utf-8")
    doc = doc_of(store, build_attachment(store, src))
    assert doc["text"] == "hello\nworld"


@pytest.mark.parametrize("raw,expect", [
    (codecs.BOM_UTF8 + "café €".encode(), "café €"),
    (codecs.BOM_UTF16_LE + "naïve".encode("utf-16-le"), "naïve"),
    ("naïve – café".encode("cp1252"), "naïve – café"),
])
def test_text_encodings(store, tmp_path, raw, expect):
    src = tmp_path / "t.txt"
    src.write_bytes(raw)
    assert doc_of(store, build_attachment(store, src))["text"] == expect


@pytest.mark.parametrize("name,data", [
    ("blob", b"\x7fELF\x00\x01\x02binary"),
    ("song.mp3", b"ID3\x03\x00\x00\x00"),
    ("clip.mp4", b"\x00\x00\x00\x18ftypmp42"),
    ("archive.zip", b"PK\x03\x04\x00"),
    ("empty.txt", b"   \n\n"),
])
def test_binary_media_and_empty_files_get_no_preview(store, tmp_path, name, data):
    src = tmp_path / name
    src.write_bytes(data)
    att = build_attachment(store, src, try_image=False)
    assert att["kind"] == "file" and "text" not in att


def test_images_get_no_text_preview(store, tmp_path):
    from PIL import Image
    src = tmp_path / "a.png"
    Image.new("RGB", (20, 20)).save(src)
    att = build_attachment(store, src)
    assert att["kind"] == "image" and "text" not in att


def test_long_text_is_cut_at_a_line_and_flagged(store, tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "TEXT_LIMIT", 1000)
    src = tmp_path / "big.txt"
    src.write_text("".join(f"line {i:04d} €€€\n" for i in range(500)), encoding="utf-8")
    doc = doc_of(store, build_attachment(store, src))
    assert doc["more"] is True and len(doc["text"].encode()) <= 1000
    assert doc["text"].endswith("€€€") and doc["text"].count("\n") > 30


def test_cut_inside_a_multibyte_character(store, tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "TEXT_LIMIT", 10)
    src = tmp_path / "x.txt"
    src.write_text("ab€€€€€€€€", encoding="utf-8")  # 3-byte characters straddle the limit
    doc = doc_of(store, build_attachment(store, src))
    assert doc["more"] is True and doc["text"].startswith("ab€") and "�" not in doc["text"]


def test_markdown_from_bytes(store):
    att = build_attachment_bytes(store, b"## Hi\n\nthere", "readme.md")
    doc = doc_of(store, att)
    assert doc["text"] == "Hi\nthere" or doc["text"].startswith("Hi")
    assert any(s[2] == "h2" for s in doc["spans"])


def test_pdf_preview_via_anydoc(store, tmp_path):
    pytest.importorskip("anydoc")
    src = tmp_path / "render-report.pdf"
    src.write_bytes(make_pdf([["Render report", "The export finished in 42 seconds."],
                              ["Details", "Clip 3: buffer overflow."]]))
    att = build_attachment(store, src)
    assert att["kind"] == "file" and att["mime"] == "application/pdf"
    doc = doc_of(store, att)
    assert "Render report" in doc["text"] and "Clip 3: buffer overflow." in doc["text"]
    assert any(s[2] == "h1" for s in doc.get("spans", []))
    att = build_attachment_bytes(store, src.read_bytes(), "r.pdf")
    assert "Render report" in doc_of(store, att)["text"]


def test_unreadable_pdf_has_no_preview(store, tmp_path):
    pytest.importorskip("anydoc")
    src = tmp_path / "broken.pdf"
    src.write_bytes(b"%PDF-1.4\nnot really a pdf")
    att = build_attachment(store, src)
    assert att["kind"] == "file" and "text" not in att


def test_documents_without_anydoc_are_plain_files(store, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "anydoc", None)  # import fails
    src = tmp_path / "r.pdf"
    src.write_bytes(make_pdf([["Hello"]]))
    att = build_attachment(store, src)
    assert att["kind"] == "file" and "text" not in att and "file" in att


def test_oversized_documents_are_not_converted(store, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(preview, "_doc_markdown", lambda *a: calls.append(a) or "# x")
    monkeypatch.setattr(preview, "DOC_MAX", 10)
    src = tmp_path / "r.docx"
    src.write_bytes(b"x" * 11)
    assert "text" not in build_attachment(store, src) and not calls


def test_markdown_soft_breaks_are_joined():
    md = ("# Title\n"
          "A paragraph wrapped\nover three\nsource lines.\n"
          "\n"
          "- item one\n  continues here\n- item two\n"
          "> quoted\n> more quote\n"
          "\n"
          "Hard break  \nkept\\\nkept too\n"
          "| a | b |\n|---|---|\n"
          "```\ncode line\nsecond line\n```\n"
          "after code\n"
          "---\n"
          "    indented code\n    more\n")
    assert preview._unwrap(md).split("\n") == [
        "# Title",
        "A paragraph wrapped over three source lines.",
        "",
        "- item one continues here",
        "- item two",
        "> quoted",
        "> more quote",
        "",
        "Hard break  ",
        "kept\\",
        "kept too",
        "| a | b |",
        "|---|---|",
        "```", "code line", "second line", "```",
        "after code",
        "---",
        "    indented code",
        "    more",
        "",
    ]
