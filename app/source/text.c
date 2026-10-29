#include "text.h"

#include "emoji.h"
#include "perf.h"

#include <3ds.h>
#include <jansson.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define FRAME_GLYPHS 16384

static FINF_s *finf;
static C2D_TextBuf frame_buf;
static float adv_ascii[128];  // advance at scale 1.0

void text_init(void)
{
    fontEnsureMapped();
    finf = fontGetInfo(NULL);
    frame_buf = C2D_TextBufNew(FRAME_GLYPHS);
    emoji_init();
    for (int i = 0; i < 128; i++) {
        int gi = fontGlyphIndexFromCodePoint(NULL, i < 32 ? ' ' : (u32)i);
        adv_ascii[i] = fontGetCharWidthInfo(NULL, gi)->charWidth;
    }
}

static void cache_reset(void);
static void cache_free(void);
static bool cache_full;

void text_exit(void)
{
    C2D_TextBufDelete(frame_buf);
    cache_free();
    emoji_exit();
}

void text_frame_begin(void)
{
    C2D_TextBufClear(frame_buf);
    if (cache_full) {  // the previous frame is done with the old glyphs
        cache_reset();
        perf_glyph_resets++;
    }
    emoji_frame_begin();
}

static float advance(uint32_t cp)
{
    if (cp < 128) return adv_ascii[cp];
    return fontGetCharWidthInfo(NULL, fontGlyphIndexFromCodePoint(NULL, cp))->charWidth;
}

// Decode one UTF-8 code point; never returns a length below 1, so callers always make progress.
static int next_cp(const char *s, uint32_t avail, uint32_t *cp)
{
    unsigned char c = (unsigned char)s[0];
    int need = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
    if (need == 0 || (uint32_t)need > avail) {
        *cp = 0xFFFD;
        return 1;
    }
    ssize_t n = decode_utf8(cp, (const uint8_t *)s);
    if (n < 1) {
        *cp = 0xFFFD;
        return 1;
    }
    return (int)n;
}

// Emoji sprites: a square a little under the line height, with 1 px of space on each side.
static int emoji_px(float scale) { return (int)floorf(finf->lineFeed * scale * 0.94f + 0.5f); }

// Advance in whole pixels at `scale`. Layout and drawing both use this, so measured and drawn widths agree.
static float adv_px(uint32_t cp, float scale)
{
    if (emoji_is(cp)) return (float)(emoji_px(scale) + 2);
    return floorf(advance(cp) * scale + 0.5f);
}

float text_line_height(float scale) { return finf->lineFeed * scale; }

float text_scale_for(uint16_t mask, float base)
{
    if (mask & ST_H1) return base * 1.32f;
    if (mask & ST_H2) return base * 1.18f;
    if (mask & ST_H3) return base * 1.08f;
    if (mask & ST_CODE) return base * 0.94f;
    return base;
}

float text_width(const char *s, size_t len, float scale)
{
    float w = 0;
    uint32_t cp;
    for (size_t i = 0; i < len;) {
        i += (size_t)next_cp(s + i, (uint32_t)(len - i), &cp);
        w += adv_px(cp, scale);
    }
    return w;
}

// --- style segments ---------------------------------------------------------

static uint16_t style_bit(const char *s)
{
    static const struct { const char *name; uint16_t bit; } map[] = {
        {"b", ST_B},       {"i", ST_I},     {"code", ST_CODE}, {"link", ST_LINK}, {"h1", ST_H1},     {"h2", ST_H2},
        {"h3", ST_H3},     {"dim", ST_DIM}, {"add", ST_ADD},   {"del", ST_DEL},   {"quote", ST_QUOTE},
        {"table", ST_TABLE}, {"th", ST_TH},
    };
    for (size_t i = 0; i < sizeof map / sizeof map[0]; i++)
        if (!strcmp(s, map[i].name)) return map[i].bit;
    return 0;  // unknown styles (e.g. future syntax colors) render plain
}

void text_segs_plain(TextLayout *tl, uint32_t len, uint16_t mask)
{
    free(tl->segs);
    tl->segs = malloc(sizeof(Seg));
    tl->nsegs = tl->segs ? 1 : 0;  // out of memory: text_layout lays out nothing
    if (tl->segs) tl->segs[0] = (Seg){0, mask};
}

// Span edges, swept in order: the style mask between two edges is the set of spans covering it. Sorting
// keeps this O(n log n), so long documents (the reader) with thousands of spans lay out quickly.
typedef struct {
    uint32_t pos;
    int8_t delta;  // +1 span starts, -1 span ends
    uint8_t bit;   // style bit index
} Edge;

static int edge_cmp(const void *a, const void *b)
{
    uint32_t x = ((const Edge *)a)->pos, y = ((const Edge *)b)->pos;
    return (x > y) - (x < y);
}

void text_segs_from_spans(TextLayout *tl, const json_t *spans, uint32_t len)
{
    size_t n = json_is_array(spans) ? json_array_size(spans) : 0;
    Edge *ev = n ? malloc(2 * n * sizeof *ev) : NULL;
    if (!ev) {
        text_segs_plain(tl, len, 0);
        return;
    }
    int ne = 0;
    for (size_t i = 0; i < n; i++) {
        const json_t *sp = json_array_get(spans, i);
        json_int_t a = json_integer_value(json_array_get(sp, 0)), b = json_integer_value(json_array_get(sp, 1));
        const char *name = json_string_value(json_array_get(sp, 2));
        if (a < 0) a = 0;
        if (b > (json_int_t)len) b = len;
        uint16_t bit = (name && a < b) ? style_bit(name) : 0;
        if (!bit) continue;
        uint8_t idx = (uint8_t)__builtin_ctz(bit);
        ev[ne++] = (Edge){(uint32_t)a, 1, idx};
        ev[ne++] = (Edge){(uint32_t)b, -1, idx};
    }
    qsort(ev, (size_t)ne, sizeof *ev, edge_cmp);
    Seg *segs = malloc((size_t)(ne + 1) * sizeof(Seg));
    if (!segs) {  // out of memory: unstyled
        free(ev);
        text_segs_plain(tl, len, 0);
        return;
    }
    free(tl->segs);
    tl->segs = segs;
    tl->nsegs = 0;
    int count[16] = {0}, i = 0;
    for (uint32_t pos = 0;;) {
        while (i < ne && ev[i].pos <= pos) {
            count[ev[i].bit] += ev[i].delta;
            i++;
        }
        if (pos >= len && len > 0) break;
        uint16_t m = 0;
        for (int k = 0; k < 16; k++)
            if (count[k] > 0) m |= (uint16_t)(1u << k);
        if (!tl->nsegs || tl->segs[tl->nsegs - 1].mask != m) tl->segs[tl->nsegs++] = (Seg){pos, m};  // merge equal
        if (i >= ne) break;
        pos = ev[i].pos;
    }
    if (tl->nsegs == 0) tl->segs[tl->nsegs++] = (Seg){0, 0};
    free(ev);
}

// Index of the segment holding byte `pos` (segs start at 0, sorted by start).
static int seg_at(const TextLayout *tl, uint32_t pos)
{
    int lo = 0, hi = tl->nsegs - 1;
    while (lo < hi) {
        int mid = (lo + hi + 1) / 2;
        if (tl->segs[mid].start <= pos) lo = mid;
        else hi = mid - 1;
    }
    return lo;
}

// --- wrapping ---------------------------------------------------------------

// Width of bytes [a, b) with each run at its style's scale.
static float range_width(const TextLayout *tl, const char *text, uint32_t a, uint32_t b, float base)
{
    float w = 0;
    for (int i = seg_at(tl, a), first = i; i < tl->nsegs && (i == first || tl->segs[i].start < b); i++) {
        uint32_t s0 = tl->segs[i].start, s1 = i + 1 < tl->nsegs ? tl->segs[i + 1].start : b;
        if (s0 < a) s0 = a;
        if (s1 > b) s1 = b;
        if (s1 > s0) w += text_width(text + s0, s1 - s0, text_scale_for(tl->segs[i].mask, base));
    }
    return w;
}

#define GAP_PX 4  // LF_GAP: space above a table record

static Line *push_line(TextLayout *tl, uint32_t start, uint32_t end, float width, float base, const Line *tmpl)
{
    if (tl->nlines == tl->cap) {
        int cap = tl->cap ? tl->cap * 2 : 8;
        Line *lines = realloc(tl->lines, (size_t)cap * sizeof(Line));
        if (!lines) return NULL;  // out of memory: the text is cut short here
        tl->lines = lines;
        tl->cap = cap;
    }
    // Line box from the largest style in the range (headings make taller lines).
    float maxsc = 0;
    uint16_t mor = 0;
    for (int i = seg_at(tl, start), first = i; i < tl->nsegs && (i == first || tl->segs[i].start < end); i++) {
        float sc = text_scale_for(tl->segs[i].mask, base);
        if (sc > maxsc) maxsc = sc;
        mor |= tl->segs[i].mask;
    }
    if (maxsc == 0 || (tmpl->kind == LINE_CELL && maxsc < base)) maxsc = base;  // labels are at base
    Line *ln = &tl->lines[tl->nlines++];
    *ln = *tmpl;
    ln->start = start;
    ln->end = end;
    ln->width = tmpl->x0 + width;
    ln->height = finf->lineFeed * maxsc + ((mor & ST_HEADING) ? 3 : 0);
    ln->ascent = finf->ascent * maxsc + ((mor & ST_HEADING) ? 2 : 0);
    if (tmpl->kind == LINE_ROW && (mor & ST_TH)) ln->height += 3;  // room for the rule under the header
    if (tmpl->flags & LF_GAP) {
        ln->height += GAP_PX;
        ln->ascent += GAP_PX;
    }
    ln->mask_or = mor;
    if (ln->width > tl->width) tl->width = ln->width;
    tl->height += ln->height;
    return ln;
}

// Word-wrap [a, b) (no '\n'): the first line starts at `first->x0`, later ones at `rest->x0`, both ending at
// max_w. Lines take the rest of their fields from the template.
static void wrap_range(TextLayout *tl, const char *text, uint32_t a, uint32_t b, float max_w, float base,
                       const Line *first, const Line *rest)
{
    const Line *tmpl = first;
    float avail = max_w - first->x0;
    uint32_t pos = a, line_start = a, brk = 0;
    float x = 0, x_brk = 0, x_before_space = 0;
    int si = seg_at(tl, a);
    while (pos < b) {
        while (si + 1 < tl->nsegs && tl->segs[si + 1].start <= pos) si++;
        uint32_t cp;
        int n = next_cp(text + pos, b - pos, &cp);
        float w = adv_px(cp, text_scale_for(tl->segs[si].mask, base));
        if (x + w > avail && pos > line_start) {
            if (brk > line_start) {
                push_line(tl, line_start, brk - 1, x_before_space, base, tmpl);  // drop the space we broke at
                line_start = brk;
                x -= x_brk;
            } else {  // one long word: break inside it
                push_line(tl, line_start, pos, x, base, tmpl);
                line_start = pos;
                x = 0;
            }
            brk = 0;
            tmpl = rest;
            avail = max_w - rest->x0;
        }
        if (cp == ' ') {
            x_before_space = x;
            brk = pos + 1;
            x_brk = x + w;
        }
        x += w;
        pos += (uint32_t)n;
    }
    push_line(tl, line_start, b, x, base, tmpl);  // last line (also gives an empty text one line for the caret)
}

static uint32_t find_byte(const char *text, uint32_t a, uint32_t b, char ch)
{
    const char *p = memchr(text + a, ch, b - a);
    return p ? (uint32_t)(p - text) : b;
}

// Cell k of the row [a, b): its byte range. False when the row has fewer cells.
static bool cell_range(const char *text, uint32_t a, uint32_t b, int k, uint32_t *s, uint32_t *e)
{
    for (int i = 0; i < k; i++) {
        a = find_byte(text, a, b, '\t');
        if (a >= b) return false;
        a++;
    }
    *s = a;
    *e = find_byte(text, a, b, '\t');
    return true;
}

static void layout_table(TextLayout *tl, const char *text, uint32_t a, uint32_t end, float max_w, float base)
{
    const Line plain = {.table = -1};
    uint32_t head_end = find_byte(text, a, end, '\n');
    int ncols = 1;
    for (uint32_t p = a; (p = find_byte(text, p, head_end, '\t')) < head_end; p++) ncols++;
    if (ncols > TABLE_MAX_COLS) ncols = TABLE_MAX_COLS;
    if (ncols == 1 || head_end >= end) {  // one column, or only a header: ordinary lines
        for (uint32_t r = a;; r++) {
            uint32_t re = find_byte(text, r, end, '\n');
            wrap_range(tl, text, r, re, max_w, base, &plain, &plain);
            if ((r = re) >= end) return;
        }
    }

    float colw[TABLE_MAX_COLS] = {0};
    for (uint32_t r = a;; r++) {
        uint32_t re = find_byte(text, r, end, '\n'), s, e;
        for (int k = 0; k < ncols && cell_range(text, r, re, k, &s, &e); k++) {
            float w = range_width(tl, text, s, e, base);
            if (w > colw[k]) colw[k] = w;
        }
        if ((r = re) >= end) break;
    }
    float gap = floorf(finf->lineFeed * base * 0.8f + 0.5f), total = -gap;
    for (int k = 0; k < ncols; k++) total += colw[k] + gap;

    if (total <= max_w) {  // grid
        if (tl->ntables == tl->tcap) {
            tl->tcap = tl->tcap ? tl->tcap * 2 : 2;
            tl->tables = realloc(tl->tables, (size_t)tl->tcap * sizeof(TableCols));
        }
        TableCols *tc = &tl->tables[tl->ntables];
        tc->ncols = ncols;
        for (int k = 0, x = 0; k < ncols; x += (int)(colw[k] + gap), k++) tc->col_x[k] = (float)x;
        const Line row = {.kind = LINE_ROW, .table = (int16_t)tl->ntables++};
        for (uint32_t r = a;; r++) {
            uint32_t re = find_byte(text, r, end, '\n');
            push_line(tl, r, re, total, base, &row);
            if ((r = re) >= end) return;
        }
    }

    // Records: the header row becomes the labels.
    const float indent = floorf(finf->lineFeed * base * 0.6f + 0.5f);
    float colon = text_width(": ", 2, base);
    bool first_rec = true;
    for (uint32_t r = head_end + 1; r <= end; r++) {
        uint32_t re = find_byte(text, r, end, '\n'), s, e, ls, le;
        uint8_t gap_flag = first_rec ? 0 : LF_GAP;
        first_rec = false;
        for (int k = 0; k < ncols && cell_range(text, r, re, k, &s, &e); k++) {
            if (s == e) continue;  // empty cell
            if (k == 0) {
                const Line title = {.table = -1, .flags = (uint8_t)(LF_BOLD | gap_flag)};
                const Line title_rest = {.table = -1, .flags = LF_BOLD};
                wrap_range(tl, text, s, e, max_w, base, &title, &title_rest);
            } else {
                if (!cell_range(text, a, head_end, k, &ls, &le)) ls = le = a;
                float labw = ls < le ? text_width(text + ls, le - ls, base) + colon : 0;
                Line cell = {.kind = LINE_CELL, .table = -1, .flags = gap_flag, .lab_x = indent,
                             .lab_start = ls, .lab_end = le, .x0 = indent + labw};
                const Line rest = {.kind = LINE_CELL, .table = -1, .x0 = indent * 2};
                if (cell.x0 > max_w * 0.5f) {  // long label: on a line of its own, the value below it
                    push_line(tl, s, s, 0, base, &cell);
                    wrap_range(tl, text, s, e, max_w, base, &rest, &rest);
                } else {
                    wrap_range(tl, text, s, e, max_w, base, &cell, &rest);
                }
            }
            gap_flag = 0;
        }
        if ((r = re) >= end) break;
    }
}

// End of the table block that starts at `pos` (its last byte + 1, before the '\n' that follows it).
static uint32_t table_end(const TextLayout *tl, uint32_t pos, uint32_t len)
{
    int i = seg_at(tl, pos);
    while (i + 1 < tl->nsegs && (tl->segs[i + 1].mask & ST_TABLE)) i++;
    uint32_t e = i + 1 < tl->nsegs ? tl->segs[i + 1].start : len;
    return e > len ? len : e;
}

static void layout(TextLayout *tl, const char *text, uint32_t len, float max_w, float base);

void text_layout(TextLayout *tl, const char *text, uint32_t len, float max_w, float base)
{
    u64 t = perf_now();
    layout(tl, text, len, max_w, base);
    perf_add(&perf_layout, t);
}

static void layout(TextLayout *tl, const char *text, uint32_t len, float max_w, float base)
{
    tl->nlines = 0;
    tl->ntables = 0;
    tl->width = tl->height = 0;
    if (!tl->segs) text_segs_plain(tl, len, 0);
    if (!tl->segs) return;  // out of memory

    const Line plain = {.table = -1};
    for (uint32_t pos = 0;;) {  // one paragraph (or table) per pass
        uint32_t eol;
        if (pos < len && (tl->segs[seg_at(tl, pos)].mask & ST_TABLE)) {
            eol = table_end(tl, pos, len);
            layout_table(tl, text, pos, eol, max_w, base);
            if (eol >= len) return;
            if (text[eol] != '\n') {  // span ended mid-line: carry on with the rest of it
                pos = eol;
                continue;
            }
        } else {
            eol = find_byte(text, pos, len, '\n');
            wrap_range(tl, text, pos, eol, max_w, base, &plain, &plain);
            if (eol >= len) return;
        }
        pos = eol + 1;  // a text ending in '\n' gets an empty last line from the next pass
    }
}

void text_layout_free(TextLayout *tl)
{
    free(tl->segs);
    free(tl->lines);
    free(tl->tables);
    memset(tl, 0, sizeof *tl);
}

// --- glyph cache --------------------------------------------------------------
// The system font is ~30 px tall. Scaled down to 8-13 px by the GPU (no mipmaps) it is point-sampled, so
// thin strokes drop out on the real screen. Instead each (size, glyph) is area-filtered once on the CPU
// into an A8 atlas and drawn 1:1 at whole pixels: still the system font, but crisp.

#define PAGE_W 256
#define PAGE_H 256
#define MAX_PAGES 8       // 64 KB each, allocated as needed
#define HASH_SIZE 4096    // power of two
#define MAX_GLYPHS 3000   // keeps the table under ~75% full
#define GLYPH_GAMMA 0.72f // < 1 darkens partial coverage so thin stems stay solid

typedef struct {
    uint16_t skey, glyph;  // skey 0 = empty slot
    uint8_t page, w, h;
    int8_t ox, oy;         // top-left offset from (pen, baseline)
    uint16_t x, y;         // position in the page
} GlyphEnt;

typedef struct {
    C3D_Tex tex;
    int shelf_x, shelf_y, shelf_h;
} Page;

static GlyphEnt cache[HASH_SIZE];
static int ncached;
static Page pages[MAX_PAGES];
static int npages, cur_page;

static void cache_reset(void)
{
    memset(cache, 0, sizeof cache);
    ncached = 0;
    for (int i = 0; i < npages; i++) pages[i].shelf_x = pages[i].shelf_y = pages[i].shelf_h = 0;
    cur_page = 0;
    cache_full = false;
}

static void cache_free(void)
{
    for (int i = 0; i < npages; i++) C3D_TexDelete(&pages[i].tex);
    npages = 0;
    cache_reset();
}

static inline uint32_t morton8(int x, int y)
{
    return (uint32_t)((x & 1) | ((y & 1) << 1) | ((x & 2) << 1) | ((y & 2) << 2) | ((x & 4) << 2) | ((y & 4) << 3));
}

// Texel index in a tiled (8x8 Morton) texture `w` texels wide; row 0 is the top of the image (v = 1).
static inline uint32_t tex_index(int x, int y, int w)
{
    return (uint32_t)(((y >> 3) * (w >> 3) + (x >> 3)) * 64) + morton8(x & 7, y & 7);
}

static float sheet_alpha(const uint8_t *sheet, int fmt, int w, int x, int y)
{
    uint32_t i = tex_index(x, y, w);
    if (fmt == GPU_A8) return sheet[i] / 255.0f;
    uint8_t b = sheet[i >> 1];  // GPU_A4: two texels per byte, the first in the low nibble
    return ((i & 1) ? b >> 4 : b & 15) / 15.0f;
}

// Room for a w x h box plus a 1 px gutter. False when the atlas is full.
static bool atlas_alloc(int w, int h, int *page, int *x, int *y)
{
    for (;;) {
        if (cur_page >= npages) {
            if (npages == MAX_PAGES) return false;
            Page *np = &pages[npages];
            if (!C3D_TexInit(&np->tex, PAGE_W, PAGE_H, GPU_A8)) return false;
            C3D_TexSetFilter(&np->tex, GPU_NEAREST, GPU_NEAREST);
            memset(np->tex.data, 0, np->tex.size);
            C3D_TexFlush(&np->tex);
            np->shelf_x = np->shelf_y = np->shelf_h = 0;
            npages++;
        }
        Page *p = &pages[cur_page];
        if (p->shelf_x + w + 1 > PAGE_W || h + 1 > p->shelf_h) {  // open a new shelf
            p->shelf_y += p->shelf_h;
            p->shelf_x = 0;
            p->shelf_h = h + 1;
            if (p->shelf_y + p->shelf_h > PAGE_H) {
                cur_page++;
                continue;
            }
        }
        *page = cur_page;
        *x = p->shelf_x;
        *y = p->shelf_y;
        p->shelf_x += w + 1;
        return true;
    }
}

// Area-filter glyph `gi` from its font sheet to `scale` into the atlas. False if it can't be cached now.
static bool bake(GlyphEnt *e, int gi, uint16_t skey, float scale)
{
    static float rows[64 * 256];  // source rows x destination columns
    TGLP_s *tg = finf->tglp;
    if (tg->sheetFmt != GPU_A4 && tg->sheetFmt != GPU_A8) return false;
    charWidthInfo_s *cw = fontGetCharWidthInfo(NULL, gi);
    fontGlyphPos_s gp;
    fontCalcGlyphPos(&gp, NULL, gi, 0, 1.0f, 1.0f);
    int sw = cw->glyphWidth, sh = tg->cellHeight, W = tg->sheetWidth, H = tg->sheetHeight;
    int sx0 = (int)floorf(gp.texcoord.left * W + 0.5f);
    float ya = (1.0f - gp.texcoord.top) * H, yb = (1.0f - gp.texcoord.bottom) * H;
    int sy0 = (int)floorf((ya < yb ? ya : yb) + 0.5f);

    // Destination box at whole-pixel offsets from pen/baseline; the fractional phase is baked in.
    float lx = cw->left * scale, ty = -finf->ascent * scale;
    int ox = (int)floorf(lx), oy = (int)floorf(ty);
    float fx = lx - ox, fy = ty - oy;
    int dw = sw ? (int)ceilf(fx + sw * scale) : 0, dh = sw ? (int)ceilf(fy + sh * scale) : 0;
    if (dw > 250 || dh > 250 || sh > 64 || sh * dw > (int)(sizeof rows / sizeof rows[0])) return false;
    if (ox < -128 || ox > 127 || oy < -128) return false;

    int page = 0, px = 0, py = 0;
    if (dw && !atlas_alloc(dw, dh, &page, &px, &py)) return false;
    *e = (GlyphEnt){skey, (uint16_t)gi, (uint8_t)page, (uint8_t)dw, (uint8_t)dh, (int8_t)ox, (int8_t)oy,
                    (uint16_t)px, (uint16_t)py};
    ncached++;
    if (!dw) return true;  // blank glyph

    // Separable box filter. Source texel (i, j) covers [i, i+1) x [j, j+1); destination pixel u covers
    // source [(u - fx) / scale, (u + 1 - fx) / scale), likewise vertically.
    const uint8_t *sheet = tg->sheetData + (size_t)gp.sheetIndex * tg->sheetSize;
    float inv = 1.0f / scale;
    for (int j = 0; j < sh; j++)
        for (int u = 0; u < dw; u++) {
            float a = (u - fx) * inv, b = (u + 1 - fx) * inv, acc = 0;
            int i0 = a < 0 ? 0 : (int)a, i1 = b > sw ? sw : (int)ceilf(b);
            for (int i = i0; i < i1; i++) {
                float lo = a > i ? a : (float)i, hi = b < i + 1 ? b : (float)(i + 1);
                if (hi > lo) acc += (hi - lo) * sheet_alpha(sheet, tg->sheetFmt, W, sx0 + i, sy0 + j);
            }
            rows[j * dw + u] = acc;
        }
    uint8_t *dst = pages[page].tex.data;
    float norm = scale * scale;  // 1 / footprint area
    for (int v = 0; v < dh; v++) {
        float a = (v - fy) * inv, b = (v + 1 - fy) * inv;
        int j0 = a < 0 ? 0 : (int)a, j1 = b > sh ? sh : (int)ceilf(b);
        for (int u = 0; u < dw; u++) {
            float acc = 0;
            for (int j = j0; j < j1; j++) {
                float lo = a > j ? a : (float)j, hi = b < j + 1 ? b : (float)(j + 1);
                if (hi > lo) acc += (hi - lo) * rows[j * dw + u];
            }
            float c = acc * norm;
            c = c <= 0 ? 0 : c >= 1 ? 1 : powf(c, GLYPH_GAMMA);
            dst[tex_index(px + u, py + v, PAGE_W)] = (uint8_t)(c * 255.0f + 0.5f);
        }
    }
    // Flush the tile rows written so the GPU reads them this frame.
    int t0 = py >> 3, t1 = (py + dh - 1) >> 3;
    GSPGPU_FlushDataCache(dst + t0 * 8 * PAGE_W, (u32)((t1 - t0 + 1) * 8 * PAGE_W));
    return true;
}

static GlyphEnt *glyph_get(int gi, float scale)
{
    uint16_t skey = (uint16_t)(scale * 4096.0f + 0.5f);
    if (skey == 0) skey = 1;
    uint32_t h = ((uint32_t)skey * 2654435761u ^ (uint32_t)gi * 40503u) & (HASH_SIZE - 1);
    for (;;) {
        GlyphEnt *e = &cache[h];
        if (e->skey == skey && e->glyph == gi) return e;
        if (e->skey == 0) {
            if (ncached >= MAX_GLYPHS) break;
            u64 t = perf_now();
            bool ok = bake(e, gi, skey, scale);
            perf_add(&perf_glyph, t);
            if (ok) return e;
            break;
        }
        h = (h + 1) & (HASH_SIZE - 1);
    }
    cache_full = true;  // cleared at the next frame start
    return NULL;
}

// Fallback while the atlas is full for this frame: let the GPU scale the sheet glyph.
static void draw_glyph_gpu(uint32_t cp, float x, float baseline, float scale, u32 color)
{
    char tmp[8] = {0};
    encode_utf8((uint8_t *)tmp, cp);
    C2D_Text t;
    C2D_TextFontParse(&t, NULL, frame_buf, tmp);
    C2D_DrawText(&t, C2D_AtBaseline | C2D_WithColor, x, baseline, 0.5f, scale, scale, color);
}

// --- drawing ----------------------------------------------------------------

static float draw_run(const char *s, size_t len, float x, float baseline, float scale, u32 color, bool bold)
{
    baseline = floorf(baseline + 0.5f);  // whole pixels: cached glyphs map 1:1 to screen pixels
    float pen = floorf(x + 0.5f), x0 = pen;
    C2D_ImageTint tint;
    C2D_PlainImageTint(&tint, color, 1.0f);
    uint32_t cp;
    for (size_t i = 0; i < len;) {
        i += (size_t)next_cp(s + i, (uint32_t)(len - i), &cp);
        if (cp < 32) cp = ' ';  // tabs/CR: same width as the layout assumed
        if (emoji_is(cp)) {  // centred in this scale's line box, which sits on the baseline
            int px = emoji_px(scale);
            float top = baseline - finf->ascent * scale + (finf->lineFeed * scale - px) * 0.5f;
            emoji_draw(cp, pen + 1, floorf(top + 0.5f), px);
        } else if (cp != ' ') {
            int gi = fontGlyphIndexFromCodePoint(NULL, cp);
            GlyphEnt *e = glyph_get(gi, scale);
            if (e && e->w) {
                Tex3DS_SubTexture sub = {
                    .width = e->w,
                    .height = e->h,
                    .left = e->x / (float)PAGE_W,
                    .top = 1.0f - e->y / (float)PAGE_H,
                    .right = (e->x + e->w) / (float)PAGE_W,
                    .bottom = 1.0f - (e->y + e->h) / (float)PAGE_H,
                };
                C2D_Image img = {&pages[e->page].tex, &sub};
                C2D_DrawImageAt(img, pen + e->ox, baseline + e->oy, 0.5f, &tint, 1.0f, 1.0f);
                if (bold) C2D_DrawImageAt(img, pen + e->ox + 1, baseline + e->oy, 0.5f, &tint, 1.0f, 1.0f);
            } else if (!e) {
                draw_glyph_gpu(cp, pen, baseline, scale, color);
                if (bold) draw_glyph_gpu(cp, pen + 1, baseline, scale, color);
            }
        }
        pen += adv_px(cp, scale);
    }
    return pen - x0;
}

// Styled runs of bytes [a, b) (only those before `limit`) from pen x; returns the pen advance.
static float draw_range(const TextLayout *tl, const char *text, uint32_t a, uint32_t b, float x, float y,
                        float lh, float baseline, float base, const TextColors *c, uint32_t limit, bool bold)
{
    float bx = x;
    for (int i = seg_at(tl, a); i < tl->nsegs && tl->segs[i].start < b; i++) {
        uint32_t s0 = tl->segs[i].start, s1 = i + 1 < tl->nsegs ? tl->segs[i + 1].start : b;
        if (s0 < a) s0 = a;
        if (s1 > b) s1 = b;
        if (s1 > limit) s1 = limit;
        if (s1 <= s0) continue;
        uint16_t m = tl->segs[i].mask;
        float sc = text_scale_for(m, base);
        u32 color = (m & ST_CODE) ? c->code_ink
                    : (m & ST_LINK) ? c->link
                    : (m & ST_ADD) ? c->add
                    : (m & ST_DEL) ? c->del
                    : (m & (ST_DIM | ST_QUOTE)) ? c->dim
                    : c->ink;
        if (m & ST_CODE) {
            float w = text_width(text + s0, s1 - s0, sc);
            C2D_DrawRectSolid(bx - 1, y + 1, 0.5f, w + 2, lh - 1, c->code_bg);
        }
        float w = draw_run(text + s0, s1 - s0, bx, baseline, sc, color, bold || (m & (ST_B | ST_HEADING | ST_TH)));
        if (m & ST_LINK) C2D_DrawRectSolid(bx, baseline + 1, 0.5f, w, 1, c->link);
        if (m & ST_DEL) C2D_DrawRectSolid(bx, baseline - finf->ascent * sc * 0.35f, 0.5f, w, 1, c->del);
        bx += w;
    }
    return bx - x;
}

void text_draw_line(const TextLayout *tl, int line, const char *text, float x, float y, float base,
                    const TextColors *c, uint32_t limit)
{
    const Line *ln = &tl->lines[line];
    float top = y + ((ln->flags & LF_GAP) ? GAP_PX : 0), lh = ln->height - (top - y);
    float baseline = y + ln->ascent;
    if (ln->mask_or & ST_QUOTE) C2D_DrawRectSolid(x - 6, top, 0.5f, 2, lh, c->quote_bar);
    if (ln->kind == LINE_ROW) {
        const TableCols *tc = &tl->tables[ln->table];
        bool head = (ln->mask_or & ST_TH) != 0;
        if (head) lh -= 3;
        uint32_t s, e;
        for (int k = 0; k < tc->ncols && cell_range(text, ln->start, ln->end, k, &s, &e) && s < limit; k++)
            draw_range(tl, text, s, e, x + tc->col_x[k], top, lh, baseline, base, c, limit, false);
        if (head && ln->end <= limit) C2D_DrawRectSolid(x, top + lh + 1, 0.5f, ln->width, 1, c->quote_bar);
        return;
    }
    if (ln->kind == LINE_CELL && ln->lab_start < ln->lab_end) {
        float lx = x + ln->lab_x, bl = floorf(baseline + 0.5f);
        lx += draw_run(text + ln->lab_start, ln->lab_end - ln->lab_start, lx, bl, base, c->dim, false);
        draw_run(":", 1, lx, bl, base, c->dim, false);
    }
    draw_range(tl, text, ln->start, ln->end, x + ln->x0, top, lh, baseline, base, c, limit, ln->flags & LF_BOLD);
}

float text_line_x(const TextLayout *tl, int line, const char *text, float base, uint32_t pos)
{
    const Line *ln = &tl->lines[line];
    if (pos > ln->end) pos = ln->end;
    if (ln->kind == LINE_ROW) {  // the cell holding pos
        const TableCols *tc = &tl->tables[ln->table];
        uint32_t s, e;
        for (int k = tc->ncols - 1; k >= 0; k--)
            if (cell_range(text, ln->start, ln->end, k, &s, &e) && s <= pos)
                return tc->col_x[k] + range_width(tl, text, s, pos < e ? pos : e, base);
        return 0;
    }
    return ln->x0 + (pos > ln->start ? range_width(tl, text, ln->start, pos, base) : 0);
}

float text_draw(const char *s, float x, float y, float scale, u32 color)
{
    return draw_run(s, strlen(s), x, y + finf->ascent * scale, scale, color, false);
}

float text_draw_fit(const char *s, float x, float y, float scale, u32 color, float max_w)
{
    size_t len = strlen(s);
    if (text_width(s, len, scale) <= max_w) return text_draw(s, x, y, scale, color);
    float ell = text_width("...", 3, scale), w = 0;
    size_t cut = 0;
    uint32_t cp;
    while (cut < len) {
        int n = next_cp(s + cut, (uint32_t)(len - cut), &cp);
        float cw = adv_px(cp, scale);
        if (w + cw + ell > max_w) break;
        w += cw;
        cut += (size_t)n;
    }
    float baseline = y + finf->ascent * scale;
    draw_run(s, cut, x, baseline, scale, color, false);
    draw_run("...", 3, x + w, baseline, scale, color, false);
    return w + ell;
}
