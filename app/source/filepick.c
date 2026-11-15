#include "filepick.h"

#include <dirent.h>
#include <jansson.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "app.h"
#include "filelist.h"
#include "log.h"
#include "sfx.h"
#include "text.h"
#include "ui.h"
#include "upload.h"

#define MAX_ENTRIES 500           // a folder with more shows the first 500 by the SD card's order
#define ROWS 6
#define ROW_H 30
#define LIST_Y 26
#define READ_STEP (192 * 1024)    // bytes read per loop pass
#define DEFAULT_MAX (8u << 20)    // the limit to assume while the PC hasn't said (offline)

typedef enum { P_BROWSE, P_CONFIRM, P_READ } State;

static struct {
    bool open;
    State st;
    char dir[600];
    FileEntry *e;
    int n, sel, top;
    bool cut, unreadable;  // more entries than MAX_ENTRIES; the folder couldn't be opened
    char caption[1024];
    char path[900], name[256];
    FILE *f;
    uint8_t *data;
    size_t len, got;
} pk;

static char last_dir[600] = FILE_ROOT;  // where the picker was last time, while the app runs
static bool supported;

static const Rect SEND = {10, 148, 300, 40}, NOTE = {10, 196, 145, 34}, BACK = {165, 196, 145, 34};

void filepick_set_supported(bool yes) { supported = yes; }
bool filepick_is_open(void) { return pk.open; }
bool filepick_animating(void) { return pk.open && pk.st == P_READ; }

static uint32_t limit(void) { return upload_max() ? upload_max() : DEFAULT_MAX; }

static uint32_t entry_size(FileEntry *e)
{
    if (e->dir) return 0;
    if (e->size == FILE_SIZE_UNKNOWN) {  // looked up when first shown: a stat per file up front is slow on an SD card
        char p[900];
        struct stat st;
        e->size = 0;
        if (filelist_join(p, sizeof p, pk.dir, e->name) && !stat(p, &st))
            e->size = st.st_size >= FILE_SIZE_UNKNOWN ? FILE_SIZE_UNKNOWN - 1 : (uint32_t)st.st_size;
    }
    return e->size;
}

// Lists pk.dir; `focus` (may be NULL) is the entry to select.
static void load_dir(const char *focus)
{
    pk.n = pk.sel = pk.top = 0;
    pk.cut = pk.unreadable = false;
    if (!pk.e) pk.e = malloc(MAX_ENTRIES * sizeof *pk.e);
    DIR *d = pk.e ? opendir(pk.dir) : NULL;
    if (!d) {
        pk.unreadable = true;
        return;
    }
    struct dirent *de;
    while ((de = readdir(d))) {
        if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..")) continue;
        if (strlen(de->d_name) >= sizeof pk.e[0].name) continue;
        if (pk.n == MAX_ENTRIES) {
            pk.cut = true;
            break;
        }
        FileEntry *e = &pk.e[pk.n++];
        snprintf(e->name, sizeof e->name, "%s", de->d_name);
        e->dir = de->d_type == DT_DIR;
        e->size = FILE_SIZE_UNKNOWN;
    }
    closedir(d);
    qsort(pk.e, pk.n, sizeof *pk.e, filelist_cmp);
    if (focus)
        for (int i = 0; i < pk.n; i++)
            if (!strcmp(pk.e[i].name, focus)) pk.sel = i;
    snprintf(last_dir, sizeof last_dir, "%s", pk.dir);
}

static void stop_read(void)
{
    if (pk.f) fclose(pk.f);
    free(pk.data);
    pk.f = NULL;
    pk.data = NULL;
    pk.len = pk.got = 0;
}

void filepick_open(void)
{
    if (pk.open) return;
    if (app.net == NET_ONLINE && !supported) {
        app_toast("Update the Hermes 3DS plugin on the PC to send files");
        return;
    }
    memset(&pk, 0, sizeof pk);
    pk.open = true;
    snprintf(pk.dir, sizeof pk.dir, "%s", last_dir);
    load_dir(NULL);
    if (pk.unreadable && strcmp(pk.dir, FILE_ROOT)) {  // the folder from last time is gone
        snprintf(pk.dir, sizeof pk.dir, "%s", FILE_ROOT);
        load_dir(NULL);
    }
    app.dirty = true;
}

void filepick_close(void)
{
    if (!pk.open) return;
    stop_read();
    free(pk.e);
    memset(&pk, 0, sizeof pk);
    app.dirty = true;
}

static void go_up(void)
{
    char child[256];
    if (!filelist_up(pk.dir, child, sizeof child)) {
        filepick_close();
        return;
    }
    load_dir(child);
}

// Why the selected file can't go, or NULL when it can.
static const char *trouble(FileEntry *e)
{
    static char buf[64];
    uint32_t size = entry_size(e);
    if (!size) return "This file is empty";
    if (size > limit()) {
        char lim[24];
        size_text(limit(), lim, sizeof lim);
        snprintf(buf, sizeof buf, "Too large to send (the limit is %s)", lim);
        return buf;
    }
    return NULL;
}

static void choose(void)
{
    if (pk.sel >= pk.n) return;
    FileEntry *e = &pk.e[pk.sel];
    if (e->dir) {
        char next[sizeof pk.dir];
        if (!filelist_join(next, sizeof next, pk.dir, e->name)) {
            app_toast("That folder's path is too long");
            return;
        }
        snprintf(pk.dir, sizeof pk.dir, "%s", next);
        load_dir(NULL);
        return;
    }
    const char *why = trouble(e);
    if (why || !filelist_join(pk.path, sizeof pk.path, pk.dir, e->name)) {
        app_toast(why ? why : "That file's path is too long");
        sfx_play(SFX_ERROR);
        return;
    }
    snprintf(pk.name, sizeof pk.name, "%s", e->name);
    pk.caption[0] = 0;
    pk.st = P_CONFIRM;
}

static void edit_note(void)
{
    char buf[sizeof pk.caption];
    if (app_keyboard("A note or a question about the file", pk.caption, buf, sizeof buf, 300, true, "OK"))
        snprintf(pk.caption, sizeof pk.caption, "%s", buf);
    else if (pk.caption[0])
        pk.caption[0] = 0;  // cleared and confirmed (no note is fine)
}

static void start_read(void)
{
    struct stat st;
    if (stat(pk.path, &st) || st.st_size <= 0 || (uint64_t)st.st_size > limit()) {
        app_toast("That file changed or can't be read");
        sfx_play(SFX_ERROR);
        pk.st = P_BROWSE;
        load_dir(pk.name);
        return;
    }
    pk.len = (size_t)st.st_size;
    pk.got = 0;
    pk.data = malloc(pk.len);
    pk.f = pk.data ? fopen(pk.path, "rb") : NULL;
    if (!pk.f) {
        stop_read();
        app_toast(pk.data ? "Couldn't open that file" : "Not enough memory for that file");
        sfx_play(SFX_ERROR);
        return;
    }
    setvbuf(pk.f, NULL, _IONBF, 0);  // the reads are large already
    pk.st = P_READ;
}

static void send_file(void)
{
    char id[UPLOAD_ID_LEN], caption[sizeof pk.caption], name[sizeof pk.name];
    upload_new_id(id);
    const char *mime = filelist_mime(pk.name);
    uint8_t *data = pk.data;
    size_t len = pk.len;
    pk.data = NULL;  // app_send_media owns it now
    // NULL when the name isn't valid UTF-8; app_send_media then says it couldn't send.
    json_t *attach = json_pack("{s:s, s:s, s:I, s:s, s:{s:s, s:I, s:s}}", "kind", "file", "name", pk.name, "size",
                               (json_int_t)len, "mime", mime, "file", "id", id, "size", (json_int_t)len, "mime", mime);
    json_t *fields = json_pack("{s:[{s:s}]}", "attach", "blob", id);
    snprintf(caption, sizeof caption, "%s", pk.caption);
    snprintf(name, sizeof name, "%s", pk.name);
    log_printf("picker: sending %s, %lu bytes, %s", name, (unsigned long)len, mime);
    filepick_close();
    app_send_media(id, data, len, mime, name, attach, fields, caption);
}

void filepick_tick(void)
{
    if (!pk.open || pk.st != P_READ) return;
    size_t want = pk.len - pk.got < READ_STEP ? pk.len - pk.got : READ_STEP;
    size_t got = fread(pk.data + pk.got, 1, want, pk.f);
    pk.got += got;
    app.dirty = true;
    if (got != want) {
        stop_read();
        pk.st = P_CONFIRM;
        app_toast("Couldn't read that file from the SD card");
        sfx_play(SFX_ERROR);
    } else if (pk.got == pk.len) {
        fclose(pk.f);
        pk.f = NULL;
        send_file();
    }
}

// -1 / +1 for up / down, with auto-repeat while held.
static int nav_step(u32 down, u32 held)
{
    static u64 since, last;
    const u32 up = KEY_DUP | KEY_CPAD_UP, dn = KEY_DDOWN | KEY_CPAD_DOWN;
    int d = (down & up) ? -1 : (down & dn) ? 1 : 0;
    u64 now = osGetTime();
    if (d) {
        since = last = now;
        return d;
    }
    if ((held & (up | dn)) && now - since > 350 && now - last > 70) {
        last = now;
        return (held & up) ? -1 : 1;
    }
    return 0;
}

void filepick_input(u32 down, u32 held, const touchPosition *t)
{
    int x = t ? t->px : -1, y = t ? t->py : -1;
    app.dirty = true;
    if (pk.st == P_READ) {
        if (down & KEY_B) {
            stop_read();
            pk.st = P_CONFIRM;
        }
        return;
    }
    if (pk.st == P_CONFIRM) {
        if ((down & KEY_A) || (t && rect_hit(&SEND, x, y))) start_read();
        else if ((down & KEY_Y) || (t && rect_hit(&NOTE, x, y))) edit_note();
        else if ((down & KEY_B) || (t && rect_hit(&BACK, x, y))) pk.st = P_BROWSE;
        return;
    }
    if (down & KEY_B) {
        go_up();
        return;
    }
    if (down & KEY_X) {
        filepick_close();
        return;
    }
    pk.sel += nav_step(down, held);
    if (down & KEY_DLEFT) pk.sel -= ROWS;
    if (down & KEY_DRIGHT) pk.sel += ROWS;
    if (pk.sel >= pk.n) pk.sel = pk.n - 1;
    if (pk.sel < 0) pk.sel = 0;
    if (down & KEY_A) {
        choose();
    } else if (t && y >= LIST_Y && y < LIST_Y + ROWS * ROW_H && x >= 8 && x < BOT_W - 8) {
        int i = pk.top + (y - LIST_Y) / ROW_H;
        if (i < pk.n) {
            if (i == pk.sel) choose();  // a second tap opens it
            else pk.sel = i;
        }
    }
}

// --- drawing ------------------------------------------------------------------

static void folder_icon(float x, float y, float s, u32 c)
{
    rect(x, y + s * 0.18f, s * 0.45f, s * 0.2f, c);  // the tab
    rect(x, y + s * 0.3f, s, s * 0.58f, c);
}

static void detail(const char *name, const char *sub, const char *warn, bool dir)
{
    float y = TOP_HEADER_H + 44;
    if (dir) folder_icon(24, y + 2, 44, COL_BLUE);
    else file_tile(24, y, 48, name);
    float h = text_block(name, 86, y, TOP_W - 86 - 20, S_BODY, &BODY_COLORS, 3, NULL);
    text_draw_fit(sub, 86, y + h + 4, S_SMALL, COL_TX_3, TOP_W - 86 - 20);
    if (warn) text_draw_fit(warn, 86, y + h + 20, S_SMALL, COL_BAD, TOP_W - 86 - 20);
}

void filepick_draw_top(void)
{
    rect(0, 0, TOP_W, SCR_H, COL_PAPER);
    spaced("SD CARD", 12, TOP_HEADER_H + 10, S_EYE, COL_INK_3, 2.0f);
    text_draw_fit(pk.dir, 12, TOP_HEADER_H + 22, S_SMALL, COL_INK_2, TOP_W - 24);
    rect(12, TOP_HEADER_H + 38, TOP_W - 24, 1, COL_RULE);
    char sub[96], size[24], ext[8];
    if (pk.st != P_BROWSE) {
        size_text(pk.st == P_READ ? pk.len : entry_size(&pk.e[pk.sel]), size, sizeof size);
        ext_label(pk.name, ext, sizeof ext);
        snprintf(sub, sizeof sub, "%s%s%s", ext, ext[0] ? " · " : "", size);
        detail(pk.name, sub, NULL, false);
        if (pk.st == P_READ) {
            float f = pk.len ? (float)pk.got / pk.len : 0;
            text_draw("Reading from the SD card...", 24, 168, S_SMALL, COL_INK_2);
            progress_bar(24, 186, TOP_W - 48, 4, f, f);
        }
    } else if (pk.n) {
        FileEntry *e = &pk.e[pk.sel];
        if (e->dir) {
            detail(e->name, "Folder", NULL, true);
        } else {
            size_text(entry_size(e), size, sizeof size);
            ext_label(e->name, ext, sizeof ext);
            snprintf(sub, sizeof sub, "%s%s%s", ext, ext[0] ? " · " : "", size);
            detail(e->name, sub, trouble(e), false);
        }
    } else {
        text_draw(pk.unreadable ? "This folder can't be opened." : "This folder is empty.", 24, TOP_HEADER_H + 60,
                  S_BODY, COL_TX_3);
    }
    draw_header(pk.st == P_BROWSE ? "Send a file" : "Send this file?");
    float fy = footer_begin(), fx = 7;
    bool root = !strcmp(pk.dir, FILE_ROOT);
    if (pk.st == P_BROWSE) {
        if (pk.n) fx += hint(GLYPH_A, pk.e[pk.sel].dir ? "Open" : "Choose", fx, fy, COL_ON);
        fx += hint(GLYPH_B, root ? "Close" : "Up a folder", fx, fy, COL_ON);
        if (!root) hint(GLYPH_X, "Close", fx, fy, COL_ON);
    } else if (pk.st == P_CONFIRM) {
        fx += hint(GLYPH_A, "Send", fx, fy, COL_ON);
        fx += hint(GLYPH_Y, pk.caption[0] ? "Edit note" : "Note", fx, fy, COL_ON);
        hint(GLYPH_B, "Back", fx, fy, COL_ON);
    } else {
        hint(GLYPH_B, "Cancel", fx, fy, COL_ON);
    }
    char r[96];
    snprintf(r, sizeof r, "%s gets it on the PC", agent_title(app_agent()));
    float rw = text_width(r, strlen(r), S_HINT);
    text_draw(r, TOP_W - 7 - rw, fy, S_HINT, COL_ON_3);
}

static void draw_row(FileEntry *e, const Rect *r)
{
    float tx = r->x + 34, right = r->x + r->w - 10;
    if (e->dir) {
        folder_icon(r->x + 7, r->y + 4, 20, COL_BLUE);
        text_draw(">", right - 6, r->y + 8, S_SMALL, COL_TX_3);
        right -= 14;
    } else {
        file_tile(r->x + 6, r->y + 3, 24, e->name);
        char size[24];
        size_text(entry_size(e), size, sizeof size);
        float w = text_width(size, strlen(size), S_TOOL);
        text_draw(size, right - w, r->y + 10, S_TOOL, e->size > limit() || !e->size ? COL_BAD : COL_TX_3);
        right -= w + 8;
    }
    text_draw_fit(e->name, tx, r->y + 8, S_SMALL, COL_INK, right - tx);
}

void filepick_draw_bottom(void)
{
    rect(0, 0, BOT_W, SCR_H, COL_PAPER_2);
    if (pk.st != P_BROWSE) {
        spaced("SEND A FILE", 10, 9, S_EYE, COL_INK_3, 2.0f);
        text_draw("Note", 10, 30, S_SMALL, COL_INK_3);
        rect(10, 44, BOT_W - 20, 96, COL_PAPER);
        frame_rect(10, 44, BOT_W - 20, 96, COL_RULE);
        char line[96], to[80];
        snprintf(line, sizeof line, "None. %s gets the file on its own.", agent_title(app_agent()));
        snprintf(to, sizeof to, "Send to %s", agent_title(app_agent()));
        if (pk.caption[0]) text_block(pk.caption, 16, 50, BOT_W - 32, S_SMALL, &BODY_COLORS, 5, NULL);
        else text_draw_fit(line, 16, 50, S_SMALL, COL_TX_3, BOT_W - 32);
        bool busy = pk.st == P_READ;
        button(&SEND, busy ? "Reading..." : to, busy ? "" : GLYPH_A, COL_BLUE, COL_ON);
        button(&NOTE, pk.caption[0] ? "Edit note" : "Add a note", GLYPH_Y, COL_PAPER, COL_INK);
        button(&BACK, busy ? "Cancel" : "Back", GLYPH_B, COL_PAPER, COL_INK);
        return;
    }
    char eye[160], caps[96];
    snprintf(caps, sizeof caps, "%s", filelist_title(pk.dir));
    for (char *p = caps; *p; p++)
        if (*p >= 'a' && *p <= 'z') *p -= 32;
    snprintf(eye, sizeof eye, "%s · %d%s", caps, pk.n, pk.cut ? "+" : "");
    spaced_fit(eye, 11, 10, S_EYE, COL_INK_2, 1.4f, BOT_W - 22);
    Rect p = {8, LIST_Y, BOT_W - 16, ROWS * ROW_H};
    rect(p.x, p.y, p.w, p.h, COL_PAPER);
    frame_rect(p.x, p.y, p.w, p.h, COL_RULE);
    if (!pk.n) {
        text_draw(pk.unreadable ? "Can't open this folder" : "Nothing here", p.x + 12, p.y + p.h / 2 - 6, S_SMALL,
                  COL_TX_3);
    } else {
        if (pk.sel < pk.top) pk.top = pk.sel;
        if (pk.sel >= pk.top + ROWS) pk.top = pk.sel - ROWS + 1;
        if (pk.top > pk.n - ROWS) pk.top = pk.n - ROWS;
        if (pk.top < 0) pk.top = 0;
        for (int i = 0; i < ROWS && pk.top + i < pk.n; i++) {
            Rect r = {p.x, p.y + i * ROW_H, p.w, ROW_H};
            if (i) rect(r.x + 1, r.y, r.w - 2, 1, COL_RULE);
            if (pk.top + i == pk.sel) {
                rect(r.x, r.y, r.w, r.h, COL_BLUE_SOFT);
                frame_rect(r.x, r.y, r.w, r.h, COL_BLUE);
                frame_rect(r.x + 1, r.y + 1, r.w - 2, r.h - 2, COL_BLUE);
            }
            draw_row(&pk.e[pk.top + i], &r);
        }
        if (pk.n > ROWS) {
            float track = p.h - 4, h = track * ROWS / pk.n, pos = track * pk.top / pk.n;
            if (h < 8) h = 8;
            rect(p.x + p.w - 4, p.y + 2 + pos, 2, h, COL_LINE_B);
        }
    }
    char lim[24], note[80];
    size_text(limit(), lim, sizeof lim);
    snprintf(note, sizeof note, "Tap twice to open · files up to %s", lim);
    text_draw_fit(note, 11, LIST_Y + ROWS * ROW_H + 9, S_TOOL, COL_TX_3, BOT_W - 22);
}
