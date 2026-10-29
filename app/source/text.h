// Styled text on the system font: metrics, word wrap into lines, and drawing styled runs (protocol.md §4.2).
#pragma once

#include <citro2d.h>
#include <jansson.h>
#include <stdbool.h>
#include <stdint.h>

// Span styles as a bitmask.
enum {
    ST_B = 1 << 0,
    ST_I = 1 << 1,
    ST_CODE = 1 << 2,
    ST_LINK = 1 << 3,
    ST_H1 = 1 << 4,
    ST_H2 = 1 << 5,
    ST_H3 = 1 << 6,
    ST_DIM = 1 << 7,
    ST_ADD = 1 << 8,
    ST_DEL = 1 << 9,
    ST_QUOTE = 1 << 10,
    ST_TABLE = 1 << 11,  // a table block: rows split by '\n', cells by '\t'
    ST_TH = 1 << 12,     // its header row
};
#define ST_HEADING (ST_H1 | ST_H2 | ST_H3)
#define TABLE_MAX_COLS 16

// Style segments: [start, next.start) has `mask`. Built once per text change.
typedef struct {
    uint32_t start;
    uint16_t mask;
} Seg;

// Tables (ST_TABLE) lay out as a grid of aligned columns when they fit the width (one LINE_ROW per row),
// otherwise as records: per row a bold title line (the first cell) and "Header: value" lines (LINE_CELL).
enum { LINE_TEXT, LINE_ROW, LINE_CELL };
enum { LF_BOLD = 1, LF_GAP = 2 };

typedef struct {
    uint32_t start, end;        // byte range in the text (no '\n'); a LINE_ROW holds its '\t's
    float width;                // drawn width at the base scale(s), from the line's left edge
    float height;               // line box height
    float ascent;               // baseline offset from the line top
    float x0;                   // pen offset of `start` (indent)
    float lab_x;                // LINE_CELL: where its label (a header cell, lab_start..lab_end) is drawn
    uint32_t lab_start, lab_end;
    int16_t table;              // LINE_ROW: index into TextLayout.tables
    uint8_t kind, flags;
    uint16_t mask_or;           // union of styles in the line
} Line;

typedef struct {
    int ncols;
    float col_x[TABLE_MAX_COLS];
} TableCols;

typedef struct {
    Seg *segs;
    int nsegs;
    Line *lines;
    int nlines, cap;
    TableCols *tables;
    int ntables, tcap;
    float width;   // widest line
    float height;  // sum of line heights
} TextLayout;

void text_init(void);
void text_exit(void);
void text_frame_begin(void);  // clears the per-frame glyph buffer

float text_line_height(float scale);
float text_width(const char *s, size_t len, float scale);
float text_scale_for(uint16_t mask, float base);

// Build style segments from spans [[start,end,style],...] (a jansson array, may be NULL) over `len` bytes.
void text_segs_from_spans(TextLayout *tl, const json_t *spans, uint32_t len);
void text_segs_plain(TextLayout *tl, uint32_t len, uint16_t mask);
// Word-wrap `text` to `max_w` at base scale. Keeps segs; replaces lines.
void text_layout(TextLayout *tl, const char *text, uint32_t len, float max_w, float base);
void text_layout_free(TextLayout *tl);

typedef struct {
    u32 ink, dim, code_ink, code_bg, link, add, del, quote_bar;
} TextColors;

// Draw one laid-out line with its top-left at (x, y), only the bytes before `limit` (streaming reveal).
void text_draw_line(const TextLayout *tl, int line, const char *text, float x, float y, float base,
                    const TextColors *c, uint32_t limit);
// Pen offset of byte `pos` within a laid-out line, with each run at its style's scale.
float text_line_x(const TextLayout *tl, int line, const char *text, float base, uint32_t pos);
// Plain single-style string, top-left at (x, y); returns the drawn width.
float text_draw(const char *s, float x, float y, float scale, u32 color);
// Same, but truncated with "..." to fit max_w.
float text_draw_fit(const char *s, float x, float y, float scale, u32 color, float max_w);
