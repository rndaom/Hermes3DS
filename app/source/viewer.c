#include "viewer.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "app.h"
#include "audio.h"
#include "blob.h"
#include "image.h"
#include "log.h"
#include "store.h"
#include "ui.h"

#define MAX_FILES 64
#define FILES_DIR STORE_DIR "/files"

// Layout (top screen per the mockup: picture box left, details right).
#define BOX_X 12
#define BOX_Y 22
#define BOX_S 197
#define INFO_X 224
#define INFO_W 164
// Reader (text previews): full width between the header and the footer, scrollbar on the right.
#define READ_X 12
#define READ_W (TOP_W - 2 * READ_X - 8)
#define READ_TOP (TOP_HEADER_H + 6)
#define READ_BOTTOM (SCR_H - TOP_FOOTER_H - 4)
#define PREVIEW_BYTES 1500  // of the text laid out for the small preview in the box
// Bottom screen.
#define LIST_Y 52
#define ROW_H 33
#define ROWS 4
#define BTN_Y (SCR_H - 8 - 24)
#define SEEK_S 10.0f  // D-pad left/right on audio

static const float ZOOMS[] = {0.5f, 1, 2, 4};  // pixel scales after FIT (only those bigger than FIT)
#define NZOOMS (int)(sizeof ZOOMS / sizeof ZOOMS[0])

typedef struct {
    const Msg *m;
    const json_t *a;
} File;

static struct {
    bool open;
    const json_t *sel_a;  // the selected attachment (only compared, never read: it may have been freed)
    char sel_mid[32];
    int sel_att;
    int zoom;        // 0 = FIT, else ZOOMS[zoom - 1]
    float cx, cy;    // picture point in the middle of the box
    int list_first;
    uint32_t open_req;
    char saving[33];  // id of the file blob being saved, to report when it finishes
    bool reading;     // the selected file's text fills the top screen
} v;

// The selected file's text preview (protocol.md §4.7 `text`), parsed once its blob arrives. The full
// layout is only built when the reader opens; the box shows the first lines.
static struct {
    char id[33];
    BlobState st;
    char *text;
    uint32_t len;
    json_t *spans;
    bool more;
    TextLayout box, full;
    bool laid_out;
    float scroll;
} rd;

static File files[MAX_FILES];
static int nfiles;

static const char *js(const json_t *o, const char *k)
{
    return json_string_value(json_object_get(o, k)) ?: "";
}

static void collect(void)
{
    nfiles = 0;
    for (int i = 0; i < app.chat->n; i++) {
        const Msg *m = app.chat->m[i];
        for (size_t k = 0; k < json_array_size(m->attach) && nfiles < MAX_FILES; k++)
            files[nfiles++] = (File){m, json_array_get(m->attach, k)};
    }
}

int files_count(void)
{
    collect();
    return nfiles;
}

// Index of the selected file in files[] (the selection follows its message as the transcript changes).
static int sel_index(void)
{
    for (int i = 0; i < nfiles; i++)  // your own photos and voice messages have no mid: match the attachment
        if (files[i].a == v.sel_a) return i;
    for (int i = 0; i < nfiles && v.sel_mid[0]; i++)  // a message whose attachments were replaced by an edit
        if (!strcmp(files[i].m->mid, v.sel_mid) && json_array_get(files[i].m->attach, (size_t)v.sel_att) == files[i].a)
            return i;
    return nfiles ? nfiles - 1 : -1;
}

static void reader_free(void)
{
    free(rd.text);
    json_decref(rd.spans);
    text_layout_free(&rd.box);
    text_layout_free(&rd.full);
    memset(&rd, 0, sizeof rd);
}

static bool has_text(const json_t *a, BlobRef *r)
{
    return blob_ref_parse(json_object_get(a, "text"), r);
}

// The file's text preview: READY once parsed, LOADING while its blob downloads, NONE if it has none.
static BlobState reader_load(const json_t *a)
{
    BlobRef r;
    if (!has_text(a, &r)) return BLOB_NONE;
    if (strcmp(rd.id, r.id)) {
        reader_free();
        snprintf(rd.id, sizeof rd.id, "%s", r.id);
        rd.st = BLOB_LOADING;
    }
    if (rd.st != BLOB_LOADING) return rd.st;
    const uint8_t *data;
    size_t len;
    BlobState st = blob_get(&r, &data, &len);
    if (st == BLOB_LOADING) return st;
    rd.st = BLOB_FAILED;
    if (st == BLOB_READY) {
        json_t *doc = json_depth_ok((const char *)data, len) ? json_loadb((const char *)data, len, 0, NULL) : NULL;
        const char *text = json_string_value(json_object_get(doc, "text"));
        size_t n = json_string_length(json_object_get(doc, "text"));
        if (text && n && (rd.text = malloc(n + 1))) {
            memcpy(rd.text, text, n + 1);
            rd.len = (uint32_t)n;
            rd.spans = json_incref(json_object_get(doc, "spans"));
            rd.more = json_is_true(json_object_get(doc, "more"));
            // The box preview: the first PREVIEW_BYTES, cut at a character boundary.
            uint32_t pl = rd.len < PREVIEW_BYTES ? rd.len : PREVIEW_BYTES;
            while (pl < rd.len && ((unsigned char)rd.text[pl] & 0xC0) == 0x80) pl--;
            text_segs_from_spans(&rd.box, rd.spans, pl);
            text_layout(&rd.box, rd.text, pl, BOX_S - 16, S_SMALL);
            rd.st = BLOB_READY;
        }
        json_decref(doc);
    }
    blob_release(r.id);  // parsed (or unusable): the bytes stay in the SD cache only
    app.dirty = true;
    return rd.st;
}

static void reader_layout(void)
{
    if (rd.laid_out || rd.st != BLOB_READY) return;
    u64 t0 = osGetTime();
    text_segs_from_spans(&rd.full, rd.spans, rd.len);
    text_layout(&rd.full, rd.text, rd.len, READ_W, S_BODY);
    rd.laid_out = true;
    log_printf("reader: %lu bytes, %d lines laid out in %llu ms", (unsigned long)rd.len, rd.full.nlines,
               (unsigned long long)(osGetTime() - t0));
}

static float reader_content_h(void)
{
    return rd.full.height + (rd.more ? 34 : 8);
}

static float reader_max_scroll(void)
{
    float m = reader_content_h() - (READ_BOTTOM - READ_TOP);
    return m > 0 ? m : 0;
}

static void reader_scroll(float dy)
{
    float s = rd.scroll + dy, m = reader_max_scroll();
    s = s < 0 ? 0 : s > m ? m : s;
    if (s != rd.scroll) app.dirty = true;
    rd.scroll = s;
}

static void select_file(int i)
{
    if (i < 0 || i >= nfiles) return;
    v.sel_a = files[i].a;
    snprintf(v.sel_mid, sizeof v.sel_mid, "%s", files[i].m->mid);
    v.sel_att = 0;
    for (size_t k = 0; k < json_array_size(files[i].m->attach); k++)
        if (json_array_get(files[i].m->attach, k) == files[i].a) v.sel_att = (int)k;
    v.zoom = 0;
    v.cx = v.cy = -1;  // centred once the picture's size is known
    if (i < v.list_first) v.list_first = i;
    if (i >= v.list_first + ROWS) v.list_first = i - ROWS + 1;
    app.dirty = true;
}

void viewer_open(int index)
{
    collect();
    if (!nfiles) {
        app_toast("No files in this chat yet");
        return;
    }
    v.open = true;
    v.list_first = 0;
    select_file(index < 0 || index >= nfiles ? nfiles - 1 : index);
}

bool viewer_is_open(void) { return v.open; }

void viewer_close(void)
{
    v.open = false;
    v.reading = false;
    reader_free();
    app.dirty = true;
}

static bool viewable(const json_t *a, BlobRef *view)
{
    return !strcmp(js(a, "kind"), "image") && blob_ref_parse(json_object_get(a, "view"), view);
}

// --- actions ------------------------------------------------------------------

// "name.png" safe for the SD card, not clashing with a file already there: "name (2).png".
static void save_path(const char *name, char *out, size_t cap)
{
    char clean[96];
    size_t n = 0;
    for (const char *p = name; *p && n + 1 < sizeof clean; p++)
        clean[n++] = strchr("/\\:*?\"<>|", *p) || (unsigned char)*p < 32 ? '_' : *p;
    clean[n] = 0;
    if (!n || clean[0] == '.') snprintf(clean, sizeof clean, "file%s", n ? name : "");
    const char *dot = strrchr(clean, '.');
    int stem = dot && dot != clean ? (int)(dot - clean) : (int)strlen(clean);
    struct stat st;
    for (int k = 1; k < 100; k++) {
        if (k == 1) snprintf(out, cap, FILES_DIR "/%s", clean);
        else snprintf(out, cap, FILES_DIR "/%.*s (%d)%s", stem, clean, k, clean + stem);
        if (stat(out, &st) != 0) return;
    }
}

static void save_to_sd(const File *f)
{
    BlobRef r;
    if (!blob_ref_parse(json_object_get(f->a, "file"), &r)) {
        app_toast("Too big to send to the 3DS; use Open on PC");
        return;
    }
    float prog;
    BlobState st = blob_save_state(r.id, &prog);
    if (st == BLOB_LOADING) return;
    if (st == BLOB_READY) {
        app_toast("Already saved to the SD card");
        return;
    }
    mkdir(STORE_DIR, 0777);
    mkdir(FILES_DIR, 0777);
    char path[160];
    save_path(js(f->a, "name"), path, sizeof path);
    if (blob_save(&r, path) == BLOB_FAILED) app_toast("Couldn't save to the SD card");
    else snprintf(v.saving, sizeof v.saving, "%s", r.id);
}

static void open_on_pc(const File *f)
{
    BlobRef r;
    if (!blob_ref_parse(json_object_get(f->a, "file"), &r)) {
        app_toast("The PC no longer offers this file");
        return;
    }
    if (app.net != NET_ONLINE) {
        app_toast("Not connected to the PC");
        return;
    }
    v.open_req = app_request("blob.open", json_pack("{s:s}", "blob", r.id));
    app_toast("Opening it on the PC...");
}

static void ask_about(const File *f)
{
    char buf[160];
    snprintf(buf, sizeof buf, "About %s: ", js(f->a, "name"));
    viewer_close();
    app_compose(buf);
}

bool viewer_handle(const json_t *msg)
{
    json_t *idv = json_object_get(msg, "id");
    if (!v.open_req || !json_is_integer(idv) || (uint32_t)json_integer_value(idv) != v.open_req) return false;
    v.open_req = 0;
    const char *t = js(msg, "t");
    if (!strcmp(t, "blob.open_r")) {
        app_toast("Opened on the PC");
    } else {
        char buf[96];
        snprintf(buf, sizeof buf, "The PC couldn't open it: %s", js(msg, "msg")[0] ? js(msg, "msg") : js(msg, "code"));
        app_toast(buf);
    }
    return true;
}

// Report a finished save once.
static void check_save(void)
{
    if (!v.saving[0]) return;
    BlobState st = blob_save_state(v.saving, NULL);
    if (st == BLOB_READY) app_toast("Saved to the SD card: /3ds/hermes/files");
    else if (st == BLOB_FAILED) app_toast("Couldn't save it to the SD card");
    if (st != BLOB_LOADING) v.saving[0] = 0;
}

bool viewer_animating(void)
{
    return v.open && blob_busy();
}

// --- zoom and pan -------------------------------------------------------------

static float fit_scale(const Picture *p)
{
    float s = (float)BOX_S / p->w < (float)BOX_S / p->h ? (float)BOX_S / p->w : (float)BOX_S / p->h;
    return s > 1 ? 1 : s;
}

static float cur_scale(const Picture *p)
{
    float fit = fit_scale(p);
    if (v.zoom == 0) return fit;
    return ZOOMS[v.zoom - 1];
}

// Next zoom step after the current one, or 0 (FIT) when there is none.
static int next_zoom(const Picture *p)
{
    float fit = fit_scale(p);
    for (int z = v.zoom + 1; z <= NZOOMS; z++)
        if (ZOOMS[z - 1] > fit * 1.05f) return z;
    return 0;
}

static int zoom_steps(const Picture *p, int *pos)
{
    float fit = fit_scale(p);
    int n = 1;
    *pos = 0;
    for (int z = 1; z <= NZOOMS; z++)
        if (ZOOMS[z - 1] > fit * 1.05f) {
            if (z == v.zoom) *pos = n;
            n++;
        }
    return n;
}

static void clamp_center(const Picture *p, float scale)
{
    float half = BOX_S / 2.0f / scale;
    if (v.cx < 0) v.cx = p->w / 2.0f, v.cy = p->h / 2.0f;
    if (p->w * scale <= BOX_S) v.cx = p->w / 2.0f;
    else v.cx = v.cx < half ? half : v.cx > p->w - half ? p->w - half : v.cx;
    if (p->h * scale <= BOX_S) v.cy = p->h / 2.0f;
    else v.cy = v.cy < half ? half : v.cy > p->h - half ? p->h - half : v.cy;
}

static const Picture *current_picture(const File *f, BlobState *st)
{
    BlobRef view;
    *st = BLOB_NONE;
    if (!viewable(f->a, &view)) return NULL;
    return image_get(&view, st);
}

// --- input --------------------------------------------------------------------

static void bottom_rects(Rect *back, Rect btn[3])
{
    *back = (Rect){0, 0, 110, 22};
    float w = (BOT_W - 20 - 10) / 3.0f;
    for (int i = 0; i < 3; i++) btn[i] = (Rect){10 + i * (w + 5), BTN_Y, w, 24};
}

// Reader keys: D-pad / Circle Pad scroll, D-pad left/right a page, L/R the previous/next file (staying in
// the reader when it has text too: it opens once that text has arrived), B back to the file.
static void reader_input(u32 down, u32 held)
{
    float page = READ_BOTTOM - READ_TOP - 24;
    if (down & KEY_B) {
        v.reading = false;
        app.dirty = true;
        return;
    }
    if (down & (KEY_L | KEY_R)) {
        select_file(sel_index() + ((down & KEY_L) ? -1 : 1));  // a new file's text starts at the top
        return;
    }
    if (held & KEY_DUP) reader_scroll(-5);
    if (held & KEY_DDOWN) reader_scroll(5);
    if (down & KEY_DLEFT) reader_scroll(-page);
    if (down & KEY_DRIGHT) reader_scroll(page);
    circlePosition cp;
    hidCircleRead(&cp);
    if (abs(cp.dy) > 15) reader_scroll(-cp.dy / 12.0f);
}

void viewer_input(u32 down, u32 held, const touchPosition *touch)
{
    collect();
    int sel = sel_index();
    if (sel < 0) {
        viewer_close();
        return;
    }
    const File *f = &files[sel];
    if (v.reading) {
        reader_input(down, held);
        f = &files[sel_index()];
    } else {
        if (down & KEY_B) {
            viewer_close();
            return;
        }
        if (down & (KEY_L | KEY_DUP)) select_file(sel - 1);
        if (down & (KEY_R | KEY_DDOWN)) select_file(sel + 1);
        f = &files[sel_index()];
        BlobState ts = reader_load(f->a);
        if ((down & KEY_A) && (ts == BLOB_READY || ts == BLOB_LOADING)) {  // opens when the text is in
            v.reading = true;
            rd.scroll = 0;
            app.dirty = true;
        }
        if (audio_playable(f->a)) {
            AudioStatus s;
            bool cur = audio_status(f->a, &s);
            if (down & KEY_A) audio_toggle(f->a);
            if (cur && (down & KEY_DLEFT)) audio_seek(-SEEK_S);
            if (cur && (down & KEY_DRIGHT)) audio_seek(SEEK_S);
        }
    }
    if (down & KEY_X) save_to_sd(f);
    if (down & KEY_Y) open_on_pc(f);
    BlobState st;
    const Picture *p = current_picture(f, &st);
    if (p && !v.reading && (down & KEY_A)) {
        v.zoom = next_zoom(p);
        if (v.zoom == 0) v.cx = v.cy = -1;  // back to FIT: recentre
    }
    if (p && !v.reading && v.zoom) {
        circlePosition cp;
        hidCircleRead(&cp);
        float s = cur_scale(p);
        if (abs(cp.dx) > 15 || abs(cp.dy) > 15) {
            v.cx += cp.dx / 30.0f / s;
            v.cy -= cp.dy / 30.0f / s;
            app.dirty = true;
        }
        clamp_center(p, s);
    }
    if (touch) {
        Rect back, btn[3];
        bottom_rects(&back, btn);
        int x = touch->px, y = touch->py;
        if (rect_hit(&back, x, y)) {
            viewer_close();
            return;
        }
        for (int r = 0; r < ROWS; r++) {
            Rect row = {10, LIST_Y + r * ROW_H, BOT_W - 20, ROW_H};
            if (v.list_first + r < nfiles && rect_hit(&row, x, y)) select_file(v.list_first + r);
        }
        if (rect_hit(&btn[0], x, y)) save_to_sd(f);
        if (rect_hit(&btn[1], x, y)) open_on_pc(f);
        if (rect_hit(&btn[2], x, y)) ask_about(f);
    }
}

// --- drawing ------------------------------------------------------------------

// The first lines of a text preview, like a page, with "A Read it all" under them.
static void draw_box_text(void)
{
    float mid = BOX_Y + BOX_S / 2.0f;
    if (rd.st != BLOB_READY) {
        braille(BOX_X + BOX_S / 2.0f - 5, mid - 14, 12, COL_BLUE);
        const char *t = "Loading the text";
        text_draw(t, BOX_X + (BOX_S - text_width(t, strlen(t), S_TOOL)) / 2, mid + 4, S_TOOL, COL_TX_3);
        return;
    }
    rect(BOX_X, BOX_Y, BOX_S, BOX_S, COL_PAPER);
    float y = BOX_Y + 7, bottom = BOX_Y + BOX_S - 22;
    for (int i = 0; i < rd.box.nlines && y + rd.box.lines[i].height <= bottom; i++) {
        text_draw_line(&rd.box, i, rd.text, BOX_X + 8, y, S_SMALL, &BODY_COLORS, UINT32_MAX);
        y += rd.box.lines[i].height;
    }
    rect(BOX_X + 1, bottom + 2, BOX_S - 2, 1, COL_RULE);
    float x = BOX_X + 8, ty = bottom + 7;
    x += text_draw(GLYPH_A, x, ty - 1, S_SMALL, COL_BLUE) + 3;
    spaced("READ IT ALL", x, ty + 1, S_EYE, COL_BLUE, 1.6f);
}

// Audio: a big play/pause button, the progress bar with the clock, and what it is doing.
static void draw_box_audio(const File *f)
{
    AudioStatus s;
    bool cur = audio_status(f->a, &s);
    float bs = 64, bx = BOX_X + (BOX_S - bs) / 2, by = BOX_Y + 44;
    rect(BOX_X, BOX_Y, BOX_S, BOX_S, COL_PAPER);
    rect(bx + 3, by + 3, bs, bs, COL_INK);
    rect(bx, by, bs, bs, cur ? COL_BLUE : COL_PAPER);
    frame_rect(bx, by, bs, bs, COL_INK);
    float is = 26;
    play_icon(bx + (bs - is) / 2 + (s.playing ? 0 : 2), by + (bs - is) / 2, is, s.playing, cur ? COL_ON : COL_BLUE);

    float px = BOX_X + 14, pw = BOX_S - 28, py = by + bs + 30;
    progress_bar(px, py, pw, 4, cur && s.dur > 0 ? s.pos / s.dur : 0, cur ? s.loaded : 0);
    char pos[16], dur[16];
    audio_time_text(s.pos, pos, sizeof pos);
    audio_time_text(s.dur, dur, sizeof dur);
    text_draw(pos, px, py + 8, S_TOOL, COL_TX_3);
    text_draw(dur, px + pw - text_width(dur, strlen(dur), S_TOOL), py + 8, S_TOOL, COL_TX_3);

    char buf[48];
    if (!cur) snprintf(buf, sizeof buf, "A to play");
    else if (s.buffering) snprintf(buf, sizeof buf, "Loading %d%%", (int)(s.loaded * 100));
    else snprintf(buf, sizeof buf, "%s", s.playing ? "Playing" : s.finished ? "Finished" : "Paused");
    text_draw(buf, BOX_X + (BOX_S - text_width(buf, strlen(buf), S_SMALL)) / 2, py + 26, S_SMALL, COL_INK);
    frame_rect(BOX_X, BOX_Y, BOX_S, BOX_S, COL_INK);
}

static void draw_box_picture(const File *f)
{
    rect(BOX_X + 3, BOX_Y + 3, BOX_S, BOX_S, COL_BLUE);
    if (audio_playable(f->a)) {
        draw_box_audio(f);
        return;
    }
    rect(BOX_X, BOX_Y, BOX_S, BOX_S, COL_PAPER_2);
    BlobState st;
    const Picture *p = current_picture(f, &st);
    float mid = BOX_Y + BOX_S / 2.0f;
    if (p) {
        float s = cur_scale(p);
        clamp_center(p, s);
        image_draw_at(p, BOX_X, BOX_Y, BOX_S, BOX_S, s, v.cx, v.cy);
    } else if (st == BLOB_NONE && reader_load(f->a) != BLOB_NONE && rd.st != BLOB_FAILED) {
        draw_box_text();
    } else if (st == BLOB_LOADING) {
        braille(BOX_X + BOX_S / 2.0f - 5, mid - 14, 12, COL_BLUE);
        const char *t = "Loading the picture";
        text_draw(t, BOX_X + (BOX_S - text_width(t, strlen(t), S_TOOL)) / 2, mid + 4, S_TOOL, COL_TX_3);
    } else {
        file_tile(BOX_X + BOX_S / 2.0f - 28, mid - 44, 56, js(f->a, "name"));
        const char *t1 = st == BLOB_FAILED ? "Can't show this picture" : "No preview on the 3DS";
        const char *t2 = "Save it to SD or open it on the PC";
        text_draw(t1, BOX_X + (BOX_S - text_width(t1, strlen(t1), S_SMALL)) / 2, mid + 22, S_SMALL, COL_INK);
        text_draw(t2, BOX_X + (BOX_S - text_width(t2, strlen(t2), S_TOOL)) / 2, mid + 38, S_TOOL, COL_TX_3);
    }
    frame_rect(BOX_X, BOX_Y, BOX_S, BOX_S, COL_INK);
}

// Small whole-picture map with the visible part outlined, and the zoom readout beside it. The map is
// drawn from the thumbnail when it is loaded (already filtered down; the full picture aliases at 46 px).
static void draw_zoom_info(const Picture *p, const Picture *map, float x, float y)
{
    const float M = 46;
    float s = cur_scale(p), ms = M / p->w < M / p->h ? M / p->w : M / p->h;
    float mw = p->w * ms, mh = p->h * ms, mx = x + (M - mw) / 2, my = y + (M - mh) / 2;
    frame_rect(x, y, M, M, COL_LINE_B);
    image_draw_fit(map ? map : p, mx, my, mw, mh, 1e9f);
    float vw = BOX_S / s * ms, vh = BOX_S / s * ms;
    if (vw > mw) vw = mw;
    if (vh > mh) vh = mh;
    frame_rect(mx + (v.cx * ms - vw / 2), my + (v.cy * ms - vh / 2), vw, vh, COL_BLUE);

    char buf[48];
    float tx = x + M + 8, ty = y + 6;
    const char *tag = v.zoom ? "ZOOM" : "FIT";
    float tw = text_width(tag, strlen(tag), S_TOOL);
    rect(tx, ty - 1, tw + 8, 11, COL_BLUE);
    text_draw(tag, tx + 4, ty, S_TOOL, COL_ON);
    snprintf(buf, sizeof buf, "%.0f%%", s * 100);
    text_draw(buf, tx + tw + 12, ty, S_TOOL, COL_TX_3);
    int pos, n = zoom_steps(p, &pos);
    float bx = tx + 30;
    text_draw("zoom", tx, ty + 14, S_TOOL, COL_TX_3);
    for (int i = 0; i < n; i++) {
        if (i == pos) rect(bx + i * 8, ty + 15, 6, 6, COL_BLUE);
        else frame_rect(bx + i * 8, ty + 15, 6, 6, COL_BLUE);
    }
    text_draw(v.zoom ? "Circle Pad moves it" : "whole picture shown", tx, ty + 28, S_TOOL, COL_TX_3);
}

static void draw_reader(const File *f)
{
    reader_layout();
    float y = READ_TOP - rd.scroll;
    for (int i = 0; i < rd.full.nlines; i++) {
        const Line *ln = &rd.full.lines[i];
        if (y + ln->height >= READ_TOP - 20 && y <= READ_BOTTOM)
            text_draw_line(&rd.full, i, rd.text, READ_X, y, S_BODY, &BODY_COLORS, UINT32_MAX);
        y += ln->height;
        if (y > READ_BOTTOM) break;
    }
    if (rd.more && y + 30 > READ_TOP && y <= READ_BOTTOM) {
        rect(READ_X, y + 8, READ_W, 1, COL_RULE);
        const char *t = "The 3DS got the start of this file. Y opens all of it on the PC.";
        text_draw_fit(t, READ_X, y + 14, S_TOOL, COL_TX_3, READ_W);
    }
    // Scrollbar.
    float track = READ_BOTTOM - READ_TOP, ch = reader_content_h();
    if (ch > track) {
        float th = track * track / ch;
        if (th < 12) th = 12;
        float ty = READ_TOP + (track - th) * (rd.scroll / reader_max_scroll());
        rect(TOP_W - 9, READ_TOP, 3, track, COL_SCR_2);
        rect(TOP_W - 9, ty, 3, th, COL_BLUE);
    }
    draw_header(js(f->a, "name"));
    float hy = footer_begin(), x = 7;
    x += hint(GLYPH_DPAD, "Scroll", x, hy, COL_ON);
    x += hint(GLYPH_B, "Back", x, hy, COL_ON);
    x += hint(GLYPH_X, "Save", x, hy, COL_ON);
    hint(GLYPH_Y, "Open on PC", x, hy, COL_ON);
    char right[32];
    int pct = reader_max_scroll() > 0 ? (int)(rd.scroll * 100 / reader_max_scroll() + 0.5f) : 100;
    snprintf(right, sizeof right, "%d%%%s", pct, nfiles > 1 ? "  L R files" : "");
    text_draw(right, TOP_W - 7 - text_width(right, strlen(right), S_TOOL), hy + 1, S_TOOL, COL_ON_2);
}

void viewer_draw_top(void)
{
    collect();
    int sel = sel_index();
    char title[128];
    snprintf(title, sizeof title, "%s", app.chat->title[0] ? app.chat->title : "Files");
    if (sel < 0) {
        draw_header(title);
        return;
    }
    const File *f = &files[sel];
    check_save();
    BlobState ts = v.reading ? reader_load(f->a) : BLOB_NONE;
    if (ts == BLOB_READY) {
        draw_reader(f);
        return;
    }
    if (ts != BLOB_LOADING) v.reading = false;  // no text here: back to the file (L/R onto a picture)
    draw_box_picture(f);

    // Details column.
    char buf[96], when[24], size[24], ext[8];
    float y = 24;
    bool img = !strcmp(js(f->a, "kind"), "image"), snd = audio_playable(f->a);
    snprintf(buf, sizeof buf, "%s %d OF %d", img ? "IMAGE" : snd ? "AUDIO" : "FILE", sel + 1, nfiles);
    spaced(buf, INFO_X, y, S_EYE, COL_BLUE, 1.6f);
    y += 11;
    clock_text(f->m->local_time, when, sizeof when);
    char who[40];
    agent_caps(app_agent(), who, sizeof who);
    snprintf(buf, sizeof buf, "FROM %s%s%s", f->m->role == R_USER ? "YOU" : who, when[0] ? " · " : "", when);
    spaced(buf, INFO_X, y, S_EYE, COL_TX_3, 1.2f);
    y += 13;
    y += text_block(js(f->a, "name"), INFO_X, y, INFO_W, S_BODY, &BODY_COLORS, 2, NULL) + 2;
    size_text(json_number_value(json_object_get(f->a, "size")), size, sizeof size);
    ext_label(js(f->a, "name"), ext, sizeof ext);
    BlobState st;
    const Picture *p = current_picture(f, &st);
    char dur[16];
    audio_time_text((float)json_integer_value(json_object_get(f->a, "ms")) / 1000.0f, dur, sizeof dur);
    if (p) snprintf(buf, sizeof buf, "%s · %s · %d × %d", ext, size, p->w, p->h);
    else if (snd) snprintf(buf, sizeof buf, "%s · %s · %s", ext, size, dur);
    else snprintf(buf, sizeof buf, "%s · %s", ext, size);
    text_draw_fit(buf, INFO_X, y, S_TOOL, COL_TX_3, INFO_W);
    y += 15;
    if (snd && json_is_true(json_object_get(f->a, "cut"))) {
        text_draw_fit("The 3DS gets the first 15 minutes", INFO_X, y, S_TOOL, COL_TX_3, INFO_W);
        y += 13;
    }
    if (f->m->len) {
        float h = text_block(f->m->text, INFO_X + 7, y, INFO_W - 7, S_TOOL, NULL, 5, NULL);
        rect(INFO_X, y, 2, h, COL_BLUE);
        text_block(f->m->text, INFO_X + 7, y, INFO_W - 7, S_TOOL, &DIM_COLORS, 5, NULL);
        y += h + 8;
    }
    if (p && y < SCR_H - TOP_FOOTER_H - 52) {
        BlobRef tr;
        BlobState ts;
        const Picture *map = blob_ref_parse(json_object_get(f->a, "thumb"), &tr) ? image_get(&tr, &ts) : NULL;
        draw_zoom_info(p, map, INFO_X, SCR_H - TOP_FOOTER_H - 52);
    }

    draw_header(title);
    float hy = footer_begin(), x = 7;
    AudioStatus as;
    bool playing = snd && audio_status(f->a, &as) && as.playing;
    if (p) x += hint(GLYPH_A, next_zoom(p) ? "Zoom in" : "Fit", x, hy, COL_ON);
    else if (snd) x += hint(GLYPH_A, playing ? "Pause" : "Play", x, hy, COL_ON);
    else if (reader_load(f->a) == BLOB_READY) x += hint(GLYPH_A, "Read", x, hy, COL_ON);
    x += hint(GLYPH_B, "Back", x, hy, COL_ON);
    x += hint(GLYPH_X, "Save", x, hy, COL_ON);
    x += hint(GLYPH_Y, "Open on PC", x, hy, COL_ON);
    if (snd && audio_status(f->a, &as)) hint(GLYPH_DPAD, "10 s", x, hy, COL_ON);
    const char *right = nfiles > 1 ? "L R prev/next" : "";
    text_draw(right, TOP_W - 7 - text_width(right, strlen(right), S_TOOL), hy + 1, S_TOOL, COL_ON_2);
}

void viewer_draw_bottom(void)
{
    collect();
    int sel = sel_index();
    char buf[64];
    Rect back, btn[3];
    bottom_rects(&back, btn);
    text_draw("<", 10, 9, S_SMALL, COL_INK_3);
    spaced("CHAT", 20, 10, S_EYE, COL_INK_3, 1.6f);
    snprintf(buf, sizeof buf, "%d file%s", nfiles, nfiles == 1 ? "" : "s");
    text_draw(buf, BOT_W - 10 - text_width(buf, strlen(buf), S_TOOL), 10, S_TOOL, COL_INK_3);
    text_draw("Files in this chat", 10, 22, S_BIG * 0.7f, COL_INK);

    if (v.list_first > nfiles - ROWS) v.list_first = nfiles > ROWS ? nfiles - ROWS : 0;  // files went away
    int rows = nfiles - v.list_first < ROWS ? nfiles - v.list_first : ROWS;
    rect(10, LIST_Y, BOT_W - 20, ROWS * ROW_H, COL_PAPER);
    frame_rect(10, LIST_Y, BOT_W - 20, ROWS * ROW_H, COL_RULE);
    for (int r = 0; r < rows; r++) {
        int i = v.list_first + r;
        const File *f = &files[i];
        float y = LIST_Y + r * ROW_H;
        bool on = i == sel;
        if (r) rect(10, y, BOT_W - 20, 1, COL_RULE);
        if (on) {
            rect(10, y, BOT_W - 20, ROW_H, COL_BLUE_SOFT);
            frame_rect(10, y, BOT_W - 20, ROW_H, COL_BLUE);
        }
        BlobRef th;
        BlobState st = BLOB_NONE;
        const Picture *p = blob_ref_parse(json_object_get(f->a, "thumb"), &th) ? image_get(&th, &st) : NULL;
        AudioStatus as;
        bool snd = audio_playable(f->a), cur = snd && audio_status(f->a, &as);
        if (p) {
            image_draw_cover(p, 16, y + 4, 25, 25);
            frame_rect(16, y + 4, 25, 25, COL_INK);
        } else if (snd) {
            audio_tile(16, y + 4, 25, cur, cur && as.playing);
        } else {
            file_tile(16, y + 4, 25, js(f->a, "name"));
        }
        char size[24], ext[8], dur[16];
        size_text(json_number_value(json_object_get(f->a, "size")), size, sizeof size);
        ext_label(js(f->a, "name"), ext, sizeof ext);
        float tx = 48, right = BOT_W - 10 - (on ? 50 : 16);
        text_draw_fit(js(f->a, "name"), tx, y + 6, S_SMALL, COL_INK, right - tx);
        BlobRef tr;
        if (snd) {
            audio_time_text((float)json_integer_value(json_object_get(f->a, "ms")) / 1000.0f, dur, sizeof dur);
            snprintf(buf, sizeof buf, "Audio · %s · %s%s", dur, size, cur && as.playing ? " · playing" : "");
        } else {
            snprintf(buf, sizeof buf, "%s · %s%s", !strcmp(js(f->a, "kind"), "image") ? "Image" : ext, size,
                     has_text(f->a, &tr) ? " · opens in the reader" : "");
        }
        text_draw_fit(buf, tx, y + 19, S_TOOL, COL_INK_3, right - tx);
        if (on) spaced("VIEWING", BOT_W - 10 - 46, y + 13, S_EYE, COL_BLUE, 1.2f);
        else text_draw(">", BOT_W - 10 - 11, y + 10, S_SMALL, COL_INK_3);
    }
    if (nfiles > ROWS) {
        snprintf(buf, sizeof buf, "%d-%d of %d  " GLYPH_DPAD, v.list_first + 1, v.list_first + rows, nfiles);
        text_draw(buf, BOT_W - 10 - text_width(buf, strlen(buf), S_TOOL), LIST_Y + ROWS * ROW_H + 3, S_TOOL,
                  COL_INK_3);
    }

    if (sel < 0) return;
    const File *f = &files[sel];
    BlobRef fr;
    bool have = blob_ref_parse(json_object_get(f->a, "file"), &fr);
    float prog = 0;
    BlobState st = have ? blob_save_state(fr.id, &prog) : BLOB_NONE;
    if (st == BLOB_LOADING) snprintf(buf, sizeof buf, "Saving %d%%", (int)(prog * 100));
    else snprintf(buf, sizeof buf, "%s", st == BLOB_READY ? "Saved" : "Save to SD");
    button(&btn[0], buf, st == BLOB_LOADING ? NULL : GLYPH_X, have ? COL_BLUE : COL_PAPER_2, have ? COL_ON : COL_TX_3);
    button(&btn[1], "Open on PC", GLYPH_Y, COL_PAPER, have ? COL_INK : COL_TX_3);
    button(&btn[2], "Ask about it", NULL, COL_PAPER, COL_INK);
}
