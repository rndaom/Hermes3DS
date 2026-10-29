"""Markdown → plain text + style spans for the 3DS (protocol.md §4.2).

Spans are [start, end, style] (or [start, end, "link", url]) over UTF-8 byte offsets of the output text.
Emoji are replaced (emoji.py) as each piece is appended, so offsets are measured on the final text.
Deliberately small: covers what Hermes replies actually use. Unknown syntax passes through as text.
"""

from __future__ import annotations

import re

from .emoji import emoji_for_3ds

_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
MAX_COLS = 16  # the 3DS lays out at most this many columns
_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_body>.+?)(?P=code)"
    r"|\*\*(?P<b1>.+?)\*\*"
    r"|(?<![\w])__(?P<b2>.+?)__(?![\w])"
    r"|(?<![\w*])\*(?P<i1>[^*\s](?:[^*]*[^*\s])?)\*(?![\w*])"
    r"|(?<![\w])_(?P<i2>[^_\s](?:[^_]*[^_\s])?)_(?![\w])"
    r"|\[(?P<ltext>[^\]\n]{1,500})\]\((?P<lurl>[^)\s]{1,2000})(?:\s+\"[^\"\n]{0,500}\")?\)"  # bounded: no O(n²)
    r"|(?P<url>https?://[^\s<>()]+[^\s<>().,;:!?'\"])"
)


class _Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pos = 0  # byte offset
        self.spans: list[list] = []

    def add(self, text: str) -> tuple[int, int]:
        start = self.pos
        text = emoji_for_3ds(text)
        self.parts.append(text)
        self.pos += len(text.encode("utf-8"))
        return start, self.pos

    def span(self, start: int, end: int, style: str, *extra: str) -> None:
        if end > start:
            self.spans.append([start, end, style, *extra])

    def inline(self, text: str) -> None:
        last = 0
        for m in _INLINE.finditer(text):
            self.add(text[last:m.start()])
            last = m.end()
            g = m.groupdict()
            if g["code"]:
                self.span(*self.add(g["code_body"]), "code")
            elif g["b1"] is not None or g["b2"] is not None:
                start = self.pos
                self.inline(g["b1"] if g["b1"] is not None else g["b2"])
                self.span(start, self.pos, "b")
            elif g["i1"] is not None or g["i2"] is not None:
                start = self.pos
                self.inline(g["i1"] if g["i1"] is not None else g["i2"])
                self.span(start, self.pos, "i")
            elif g["ltext"] is not None:
                start = self.pos
                self.inline(g["ltext"])
                self.span(start, self.pos, "link", g["lurl"])
            elif g["url"]:
                self.span(*self.add(g["url"]), "link", g["url"])
        self.add(text[last:])


def _cells(line: str) -> list[str]:
    """Cells of a GFM table row: outer pipes optional, `\\|` is a literal pipe, pipes inside code don't split."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, tick, i = [], [], 0, 0
    while i < len(s):
        c = s[i]
        if c == "\\" and s.startswith("|", i + 1):
            cur.append("|")
            i += 2
            continue
        if c == "`":
            j = i
            while j < len(s) and s[j] == "`":
                j += 1
            run = j - i
            tick = run if not tick else (0 if run == tick else tick)
            cur.append(s[i:j])
            i = j
            continue
        if c == "|" and not tick:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur))
    return [c.strip().replace("\t", " ") for c in cells]


def _table_at(lines: list[str], i: int) -> int:
    """Number of lines of the GFM table starting at lines[i] (header, delimiter, rows), or 0."""
    if i + 1 >= len(lines) or "|" not in lines[i] or "|" not in lines[i + 1]:
        return 0  # both pipes required, so "Title" over "---" stays a paragraph and a rule
    if not _TABLE_DELIM.match(lines[i + 1]) or len(_cells(lines[i])) != len(_cells(lines[i + 1])):
        return 0
    n = 2
    while i + n < len(lines) and "|" in lines[i + n] and lines[i + n].strip() and not _FENCE.match(lines[i + n]):
        n += 1
    return n


def _table(b: _Builder, rows: list[list[str]]) -> None:
    """A table as rows of tab-separated cells: span `table` over the block, `th` over the header row."""
    ncols = min(len(rows[0]), MAX_COLS)
    start = b.pos
    for r, row in enumerate(rows):
        if r:
            b.add("\n")
        row_start = b.pos
        cells = (row + [""] * ncols)[:ncols]
        for k, cell in enumerate(cells):
            if k:
                b.add("\t")
            b.inline(cell)
        if r == 0:
            b.span(row_start, b.pos, "th")
    b.span(start, b.pos, "table")


def markdown_to_rich(md: str) -> tuple[str, list[list]]:
    b = _Builder()
    lines = md.replace("\r\n", "\n").split("\n")
    in_code = False
    fence = ""
    lang = ""
    started = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        fm = _FENCE.match(line)
        if fm and (not in_code or fm.group(1) == fence):
            if not in_code:
                in_code, fence, lang = True, fm.group(1), fm.group(2).lower()
            else:
                in_code = False
            continue  # fence lines produce no output
        if started:
            b.add("\n")
        started = True
        if in_code:
            start, end = b.add(line)
            style = "code"
            if lang in ("diff", "patch"):
                if line.startswith("+") and not line.startswith("+++"):
                    style = "add"
                elif line.startswith("-") and not line.startswith("---"):
                    style = "del"
                elif line.startswith("@@"):
                    style = "dim"
            b.span(start, end, style)
            continue
        n = _table_at(lines, i - 1)
        if n:
            block = lines[i - 1:i - 1 + n]
            _table(b, [_cells(block[0])] + [_cells(row) for row in block[2:]])
            i += n - 1
            continue
        if _HR.match(line):
            b.add("────────")
            continue
        hm = _HEADING.match(line)
        if hm:
            start = b.pos
            b.inline(hm.group(2))
            b.span(start, b.pos, f"h{min(len(hm.group(1)), 3)}")
            continue
        qm = _QUOTE.match(line)
        if qm:
            start = b.pos
            b.inline(qm.group(1))
            b.span(start, b.pos, "quote")
            continue
        lm = _BULLET.match(line)
        if lm:
            b.add(lm.group(1) + "• ")
            b.inline(lm.group(2))
            continue
        b.inline(line)
    return "".join(b.parts), _merge(b.spans)


# --- streaming -------------------------------------------------------------------------------
# While a reply streams, its last line is unfinished: "Run **all" has no closing ** yet. Rendered as is,
# the raw ** would show and then vanish once the closer arrives. stream_to_rich closes what the last line
# has opened so the formatting shows while the text is typed out, and holds back markers that have nothing
# after them yet. Earlier lines are complete; they render exactly as markdown_to_rich renders them.

_TRAIL_MARKS = re.compile(r"[*_`\[]+$")  # openers (or the start of a closer) with nothing after them yet
_PARTIAL_LINE_START = re.compile(r"^\s*(?:#{1,6}|[-*+>]|`{1,2}|~{1,2})$")  # "#", "-", "``" before their text


def _is_word(c: str) -> bool:
    return c.isalnum() or c == "_"


def _closers(line: str) -> str:
    """Markers that close what `line` leaves open, innermost first (mirrors _INLINE's rules)."""
    stack: list[str] = []  # "**", "__", "*", "_", a backtick run, "[" (link text) or "](" (link url)
    url_len = 0
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        top = stack[-1] if stack else ""
        if top == "](":  # inside a link url: only its end matters
            if c == ")" or c.isspace():
                stack.pop()
            else:
                url_len += 1
            i += 1
            continue
        if c == "`":
            j = i
            while j < n and line[j] == "`":
                j += 1
            k = line.find(line[i:j], j + 1)  # code body is never empty
            if k < 0:
                stack.append(line[i:j])
                break  # the rest of the line is code
            i = k + (j - i)
            continue
        if c in "*_":
            j = i
            while j < n and line[j] == c:
                j += 1
            prev, nxt = line[i - 1] if i else " ", line[j] if j < n else " "
            can_close = not prev.isspace() and (c == "*" or not _is_word(nxt))
            can_open = not nxt.isspace() and (c == "*" or not _is_word(prev))
            run = j - i
            while run > 0:
                top = stack[-1] if stack else ""
                if can_close and top in (c, c * 2) and run >= len(top):
                    stack.pop()
                    run -= len(top)
                elif can_open and (run >= 2 or not (c == "*" and (_is_word(prev) or prev == "*"))):
                    tok = c * 2 if run >= 2 else c
                    stack.append(tok)
                    run -= len(tok)
                else:
                    break
            i = j
            continue
        if c == "[":
            stack.append("[")
        elif c == "]" and top == "[":
            stack.pop()
            if line.startswith("](", i):
                stack.append("](")
                url_len = 0
                i += 1
        i += 1
    out = []
    for tok in reversed(stack):
        if tok == "](":
            out.append(")" if url_len else "#)")  # placeholder url; the real one arrives with the text
        elif tok != "[":  # unfinished link text stays literal until its "](" arrives
            out.append(tok)
    return "".join(out)


def _ends_in_code(lines: list[str]) -> bool:
    in_code, fence = False, ""
    for line in lines:
        fm = _FENCE.match(line)
        if fm and (not in_code or fm.group(1) == fence):
            in_code, fence = not in_code, fm.group(1)
    return in_code


def _hold_table_tail(lines: list[str]) -> bool:
    """Blank out table text that would show as raw pipes for now and reflow once it is complete: an
    unfinished row (the last line), and a header row whose delimiter row hasn't arrived. True if it did."""
    last, held = lines[-1], False
    if "|" in last and (last.lstrip().startswith("|") or (len(lines) > 1 and "|" in lines[-2])):
        lines[-1], held = "", True
    if (len(lines) >= 2 and not lines[-1] and lines[-2].lstrip().startswith("|")
            and (len(lines) < 3 or "|" not in lines[-3])):
        lines.pop()
        lines[-1], held = "", True
    return held


def stream_to_rich(md: str) -> tuple[str, list[list]]:
    """markdown_to_rich for a reply that is still streaming (see the comment above)."""
    lines = md.replace("\r\n", "\n").split("\n")
    last = lines[-1]
    if _ends_in_code(lines[:-1]) or _FENCE.match(last):
        return markdown_to_rich(md)  # code lines carry no inline markup
    if _hold_table_tail(lines):
        return markdown_to_rich("\n".join(lines))
    if _PARTIAL_LINE_START.match(last):
        lines[-1] = ""
        return markdown_to_rich("\n".join(lines))
    held = _TRAIL_MARKS.sub("", last)
    lines[-1] = held
    plain = markdown_to_rich("\n".join(lines))
    close = _closers(held)
    if not close:
        return plain
    body = held.rstrip()
    lines[-1] = body + close + held[len(body):]  # before trailing spaces: "*it *" is not italic
    text, spans = markdown_to_rich("\n".join(lines))
    # Every closer must have been consumed as markup; if one shows up as text, the line doesn't parse the
    # way the stack predicted (e.g. "**a *b***"), so show it unformatted until it is finished.
    if text.endswith(tuple(set(close))):
        return plain
    return text, spans


def _merge(spans: list[list]) -> list[list]:
    """Merge adjacent same-style spans (consecutive code lines become one block)."""
    out: list[list] = []
    for s in spans:
        if out and len(s) == 3 and len(out[-1]) == 3 and out[-1][2] == s[2] and s[0] - out[-1][1] <= 1:
            out[-1][1] = s[1]
        else:
            out.append(s)
    return out
