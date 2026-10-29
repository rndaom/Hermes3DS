from h3proto.richtext import markdown_to_rich, stream_to_rich


def sub(text, span):
    return text.encode("utf-8")[span[0]:span[1]].decode("utf-8")


def test_inline_styles():
    text, spans = markdown_to_rich("Run **all** tests in `src/` now, *really* — see [docs](https://x.io/a)")
    assert text == "Run all tests in src/ now, really — see docs"
    got = {(sub(text, s), s[2]) for s in spans}
    assert got == {("all", "b"), ("src/", "code"), ("really", "i"), ("docs", "link")}
    assert [s for s in spans if s[2] == "link"][0][3] == "https://x.io/a"


def test_snake_case_not_italic_and_utf8_offsets():
    text, spans = markdown_to_rich("ünïcode my_var_name and **bø**")
    assert text == "ünïcode my_var_name and bø"
    assert len(spans) == 1 and sub(text, spans[0]) == "bø"


def test_blocks():
    md = "# Title\n\n- one\n- two\n> quoted\n```python\nx = 1\ny = 2\n```\nafter"
    text, spans = markdown_to_rich(md)
    assert text == "Title\n\n• one\n• two\nquoted\nx = 1\ny = 2\nafter"
    styles = {(sub(text, s), s[2]) for s in spans}
    assert ("Title", "h1") in styles and ("quoted", "quote") in styles
    assert ("x = 1\ny = 2", "code") in styles


def test_diff_block():
    text, spans = markdown_to_rich("```diff\n@@ -1 +1 @@\n-old\n+new\n```")
    assert text == "@@ -1 +1 @@\n-old\n+new"
    assert [(sub(text, s), s[2]) for s in spans] == [("@@ -1 +1 @@", "dim"), ("-old", "del"), ("+new", "add")]


def test_unclosed_fence_while_streaming():
    text, spans = markdown_to_rich("Here:\n```sh\nls -la")
    assert text == "Here:\nls -la"
    assert [(sub(text, s), s[2]) for s in spans] == [("ls -la", "code")]


def styled(md):
    text, spans = stream_to_rich(md)
    return text, [(sub(text, s), s[2]) for s in spans]


def test_stream_formats_the_unfinished_line():
    assert styled("Run **all te") == ("Run all te", [("all te", "b")])
    assert styled("so *rea") == ("so rea", [("rea", "i")])
    assert styled("in `src/") == ("in src/", [("src/", "code")])
    assert styled("see [docs](https://x") == ("see docs", [("docs", "link")])
    assert styled("## Head") == ("Head", [("Head", "h2")])
    assert styled("a\n- **b") == ("a\n• b", [("b", "b")])
    assert styled("*it ") == ("it ", [("it", "i")])  # closed before the trailing space


def test_stream_holds_back_bare_markers():
    assert styled("Run **") == ("Run ", [])
    assert styled("Run **all*") == ("Run all", [("all", "b")])  # half a closer
    assert styled("x\n#") == ("x\n", [])
    assert styled("x\n-") == ("x\n", [])
    assert styled("x\n``") == ("x\n", [])
    assert styled("2 * 3") == ("2 * 3", [])
    assert styled("my_var_") == ("my_var", [])


def test_stream_leaves_code_and_unparseable_lines_alone():
    assert styled("```py\nx = **1") == ("x = **1", [("x = **1", "code")])
    assert styled("```py\nx = 1\n```") == ("x = 1", [("x = 1", "code")])  # Hermes closes open fences
    assert styled("**a *b") == ("**a *b", [])  # closing would not parse the same way: stays plain


def test_stream_prefixes_grow_and_end_like_the_final_render():
    md = "Run **all tests** in `src/` now, *really*: see [docs](https://x.io/a) and my_var. 2 * 3"
    prev, rewrites = "", []
    for i in range(1, len(md) + 1):
        text, _ = stream_to_rich(md[:i])
        if not text.startswith(prev):
            rewrites.append((prev, text))
        prev = text
    # The only rewrite: link text shows literally until its "](" arrives.
    assert len(rewrites) == 1 and rewrites[0][0].endswith("[docs]") and rewrites[0][1].endswith(" docs")
    assert stream_to_rich(md) == markdown_to_rich(md)


def test_table_rows_and_spans():
    md = r"Sizes:" "\n\n| Name | Size |\n|:-----|-----:|\n| **a.py** | 2 KB |\n" r"| b \| c | `x|y` |" "\n| d |\n\nDone"
    text, spans = markdown_to_rich(md)
    assert text == "Sizes:\n\nName\tSize\na.py\t2 KB\nb | c\tx|y\nd\t\n\nDone"
    got = [(sub(text, s), s[2]) for s in spans]
    assert ("Name\tSize\na.py\t2 KB\nb | c\tx|y\nd\t", "table") in got
    assert ("Name\tSize", "th") in got and ("a.py", "b") in got and ("x|y", "code") in got


def test_not_tables():
    assert markdown_to_rich("Title\n---")[0] == "Title\n────────"  # no pipes: paragraph + rule
    assert markdown_to_rich("a | b\nc | d")[0] == "a | b\nc | d"  # no delimiter row
    assert markdown_to_rich("| a | b |\n|---|\n| 1 | 2 |")[1] == []  # delimiter has the wrong cell count


def test_stream_holds_unfinished_table_rows():
    md = "Here:\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\nEnd"
    final = markdown_to_rich(md)[0]
    for i in range(1, len(md) + 1):
        text, _ = stream_to_rich(md[:i])
        assert "|" not in text, md[:i]  # raw table syntax never shows
        assert final.startswith(text.rstrip("\n")) or text.rstrip("\n") in final
    assert stream_to_rich(md) == markdown_to_rich(md)
