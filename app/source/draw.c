// Chat and pairing screens, following design/mockups (Chat, Connect); the other tabs are in tabs.c.
// The header/footer bands are drawn after the transcript so they cover its overflow.
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "app.h"
#include "audio.h"
#include "blob.h"
#include "camera.h"
#include "codec.h"
#include "image.h"
#include "tabs.h"
#include "text.h"
#include "ui.h"
#include "upload.h"
#include "viewer.h"
#include "voice.h"

#define USER_MAX_W 262
#define USER_ATT_W 220  // the user's photo / voice cards, right-aligned
#define TEXT_X 33   // assistant text column (after the 15 px badge + gap)

static const char *const VERBS[] = {"thinking", "pondering", "reasoning", "cogitating", "formulating",
                                    "mulling it over"};
void draw_init(void) { ui_init(); }

void draw_exit(void) {}

// --- top screen: chat -------------------------------------------------------

typedef struct {
    bool head;  // draws the "HERMES · time" row
    float gap, head_h, body_h, x, w, scale;
} Block;

static bool is_agent(const Msg *m) { return m && (m->role == R_ASSISTANT || m->role == R_TOOL); }

static void block_of(int i, Block *b)
{
    const Msg *m = app.chat.m[i], *prev = i > 0 ? app.chat.m[i - 1] : NULL;
    memset(b, 0, sizeof *b);
    b->gap = (m->role == R_TOOL && prev && prev->role == R_TOOL) ? 1 : (prev ? 8 : 0);
    switch (m->role) {
    case R_USER:
        b->scale = S_BODY;
        b->w = USER_MAX_W - 14;
        b->head_h = 12;
        break;
    case R_ASSISTANT:
    case R_TOOL:
        b->head = !is_agent(prev);
        b->head_h = b->head ? 18 : 0;
        b->x = TEXT_X;
        b->w = TOP_W - TEXT_X - PAD_X;
        b->scale = m->role == R_TOOL ? S_TOOL : S_BODY;
        if (b->head && prev) b->gap = 9;
        break;
    case R_SYSTEM:
        b->x = TEXT_X;
        b->w = TOP_W - TEXT_X - PAD_X;
        b->scale = S_SMALL;
        break;
    case R_PROMPT:
        b->x = PAD_X + 12;
        b->w = TOP_W - PAD_X * 2 - 20;
        b->scale = S_BODY;
        break;
    }
}

static void relayout(Msg *m, const Block *b)
{
    if (m->role == R_TOOL) {  // one line, truncated when drawn
        m->h = 11;
        return;
    }
    if (!m->dirty && m->layout_w == b->w) return;
    if (!m->tl.segs) {
        if (m->role == R_ASSISTANT && m->spans) text_segs_from_spans(&m->tl, m->spans, m->len);
        else text_segs_plain(&m->tl, m->len, 0);
    }
    text_layout(&m->tl, m->text, m->len, b->w, b->scale);
    m->layout_w = b->w;
    m->dirty = false;
    m->h = m->tl.height + (m->role == R_USER ? 11 : m->role == R_PROMPT ? 12 : 0);
}

// --- attachments (protocol.md §4.7) ---------------------------------------------

#define ATT_GAP 6
#define THUMB_W 120
#define THUMB_H 90

static const char *js(const json_t *o, const char *k)
{
    return json_string_value(json_object_get(o, k)) ?: "";
}

static bool is_image(const json_t *a)
{
    return !strcmp(js(a, "kind"), "image");
}

// The chat card's thumbnail size (from tw/th, within THUMB_W x THUMB_H).
static void thumb_size(const json_t *a, float *w, float *h)
{
    json_int_t tw = json_integer_value(json_object_get(a, "tw")), th = json_integer_value(json_object_get(a, "th"));
    if (tw <= 0 || th <= 0) tw = THUMB_W, th = THUMB_H * 3 / 4;
    float s = (float)THUMB_W / (float)tw < (float)THUMB_H / (float)th ? (float)THUMB_W / (float)tw
                                                                       : (float)THUMB_H / (float)th;
    if (s > 1) s = 1;
    *w = floorf((float)tw * s);
    *h = floorf((float)th * s);
    if (*w < 24) *w = 24;
    if (*h < 24) *h = 24;
}

static float attach_card_h(const json_t *a)
{
    float w, h;
    if (!is_image(a)) return 28;
    thumb_size(a, &w, &h);
    return h + 2;  // + hard shadow
}

static float attach_height(const Msg *m)
{
    float h = 0;
    for (size_t i = 0; i < json_array_size(m->attach); i++) h += ATT_GAP + attach_card_h(json_array_get(m->attach, i));
    return h;
}

static void draw_attach(const json_t *a, float x, float y, float w)
{
    const char *name = js(a, "name");
    char size[24], sub[96], ext[8];
    size_text(json_number_value(json_object_get(a, "size")), size, sizeof size);
    ext_label(name, ext, sizeof ext);
    float tx;
    if (is_image(a)) {
        float tw, th;
        thumb_size(a, &tw, &th);
        rect(x + 2, y + 2, tw, th, COL_BLUE);
        rect(x, y, tw, th, COL_PAPER_2);
        BlobRef r;
        BlobState st = BLOB_FAILED;
        const Picture *p = blob_ref_parse(json_object_get(a, "thumb"), &r) ? image_get(&r, &st) : NULL;
        if (p) image_draw_cover(p, x, y, tw, th);
        else if (st == BLOB_LOADING) braille(x + tw / 2 - 4, y + th / 2 - 5, 10, COL_BLUE);
        else text_draw("no preview", x + 4, y + th / 2 - 4, S_TOOL, COL_TX_3);
        frame_rect(x, y, tw, th, COL_INK);
        tx = x + tw + 10;
        snprintf(sub, sizeof sub, "Image · %s · %s", ext, size);
    } else if (audio_playable(a)) {
        AudioStatus s;
        bool cur = audio_status(a, &s);
        audio_tile(x, y + 1, 26, cur, s.playing);
        tx = x + 34;
        char pos[16], dur[16];
        audio_time_text(s.dur, dur, sizeof dur);
        if (cur) {  // a progress bar under the name, the clock after it
            audio_time_text(s.pos, pos, sizeof pos);
            snprintf(sub, sizeof sub, "%s / %s%s", pos, dur, s.buffering ? " · loading" : "");
            float tw = text_width(sub, strlen(sub), S_TOOL), bw = x + w - tx - tw - 8;
            if (bw > 30) {
                progress_bar(tx, y + 19, bw, 3, s.dur > 0 ? s.pos / s.dur : 0, s.loaded);
                text_draw(sub, tx + bw + 8, y + 15, S_TOOL, COL_TX_3);
                text_draw_fit(name, tx, y + 2, S_SMALL, COL_INK, x + w - tx);
                return;
            }
        } else {
            snprintf(sub, sizeof sub, "Audio · %s · %s%s", dur, size,
                     a == app_chat_audio() ? " · " GLYPH_Y " plays" : "");
        }
    } else {
        file_tile(x, y + 1, 26, name);
        tx = x + 34;
        snprintf(sub, sizeof sub, "%s · %s%s", ext, size, json_object_get(a, "text") ? " · readable in Files" : "");
    }
    text_draw_fit(name, tx, y + 2, S_SMALL, COL_INK, x + w - tx);
    text_draw_fit(sub, tx, y + 15, S_TOOL, COL_TX_3, x + w - tx);
}

static void draw_attachments(const Msg *m, float x, float y, float w, float top, float bottom)
{
    for (size_t i = 0; i < json_array_size(m->attach); i++) {
        const json_t *a = json_array_get(m->attach, i);
        float h = attach_card_h(a);
        y += ATT_GAP;
        if (y + h >= top && y <= bottom) draw_attach(a, x, y, w);
        y += h;
    }
}

// Lines that are (partly) revealed: all of them unless the message is still being typed out.
static int visible_lines(const Msg *m)
{
    if (m->shown >= m->len) return m->tl.nlines;
    int n = 1;
    while (n < m->tl.nlines && m->tl.lines[n].start < m->shown) n++;
    return n;
}

static float visible_height(const Msg *m)
{
    if (m->shown >= m->len) return m->tl.height;
    float h = 0;
    for (int i = 0, n = visible_lines(m); i < n; i++) h += m->tl.lines[i].height;
    return h;
}

static void draw_lines(const Msg *m, float x, float y, float base, const TextColors *c, float clip_top,
                       float clip_bottom)
{
    for (int i = 0, n = visible_lines(m); i < n; i++) {
        const Line *ln = &m->tl.lines[i];
        if (y + ln->height >= clip_top && y <= clip_bottom)
            text_draw_line(&m->tl, i, m->text, x, y, base, c, m->shown);
        y += ln->height;
    }
}

// Caret after the last revealed character; solid while text is arriving, blinking while waiting.
static void draw_caret(const Msg *m, float x, float y, float base)
{
    bool typing_out = m->shown < m->len;
    if (!typing_out && (osGetTime() / 530) % 2) return;
    int li = visible_lines(m) - 1;
    const Line *ln = &m->tl.lines[li];
    float ly = y;
    for (int i = 0; i < li; i++) ly += m->tl.lines[i].height;
    float cx = text_line_x(&m->tl, li, m->text, base, m->shown);
    rect(floorf(x + cx + 1.5f), ly + 2, 4, ln->height - 4, COL_BLUE);
}

static void draw_message(int i, const Block *b, float y, float top, float bottom)
{
    const Msg *m = app.chat.m[i];
    char tbuf[24], eyebrow[48];
    clock_text(m->local_time, tbuf, sizeof tbuf);
    switch (m->role) {
    case R_USER: {
        float bw = m->tl.width + 14;
        if (bw < 24) bw = 24;
        float bx = TOP_W - PAD_X - bw;
        float up = m->upload_id[0] ? upload_progress(m->upload_id) : -1;
        if (m->failed) snprintf(eyebrow, sizeof eyebrow, "YOU · NOT SENT");
        else if (up >= 0) snprintf(eyebrow, sizeof eyebrow, "YOU · UPLOADING %d%%", (int)(up * 100));
        else if (m->upload_id[0]) snprintf(eyebrow, sizeof eyebrow, "YOU · WAITING");
        else
            snprintf(eyebrow, sizeof eyebrow, "YOU%s%s", m->pending ? " · SENDING" : tbuf[0] ? " · " : "",
                     m->pending ? "" : tbuf);
        float ew = text_width(eyebrow, strlen(eyebrow), S_EYE) + strlen(eyebrow) * 0.8f;
        spaced(eyebrow, TOP_W - PAD_X - ew, y, S_EYE, m->failed ? COL_BAD : COL_TX_3, 0.8f);
        float ay = y + b->head_h;
        if (m->len) {
            rect(bx, ay, bw, m->tl.height + 11, COL_BLUE);
            draw_lines(m, bx + 7, ay + 5, b->scale, &USER_COLORS, top, bottom);
            ay += m->tl.height + 11;
        }
        if (m->attach) draw_attachments(m, TOP_W - PAD_X - USER_ATT_W, ay, USER_ATT_W, top, bottom);
        break;
    }
    case R_ASSISTANT:
    case R_TOOL: {
        float ty = y;
        if (b->head) {
            frame_rect(PAD_X, y, 15, 15, COL_LINE_B);
            wing_mark(PAD_X + 3, y + 3, COL_BLUE);
            float w = spaced("HERMES", TEXT_X, y + 4, S_EYE, COL_BLUE, 1.6f);
            if (tbuf[0]) text_draw(tbuf, TEXT_X + w + 5, y + 3.5f, S_EYE * 1.1f, COL_TX_3);
            ty += b->head_h;
        }
        if (m->role == R_TOOL) {
            rect(b->x, ty, b->w, 11, COL_SCR_2);
            for (float dy = 1; dy < 10; dy += 2) rect(b->x + 5, ty + dy, 1, 1, COL_TX_3);  // the ┊ rail
            float right = b->x + b->w - 5;
            if (m->running) {
                braille(right - 6, ty + 1.5f, 9, COL_BLUE);
                right -= 12;
            } else {
                char ms[16];
                if (m->ms) {
                    snprintf(ms, sizeof ms, "%.1fs", m->ms / 1000.0f);
                    float w = text_width(ms, strlen(ms), S_TOOL);
                    text_draw(ms, right - w, ty + 0.5f, S_TOOL, COL_TX_3);
                    right -= w + 5;
                }
                check_mark(right - 7, ty + 2, 7, COL_OK);
                right -= 12;
            }
            text_draw_fit(m->text, b->x + 11, ty + 0.5f, S_TOOL, COL_TX_2, right - (b->x + 11));
        } else {
            if (m->len) draw_lines(m, b->x, ty, b->scale, &BODY_COLORS, top, bottom);
            if (!m->final || m->shown < m->len) draw_caret(m, b->x, ty, b->scale);
            if (m->attach) draw_attachments(m, b->x, ty + (m->len ? visible_height(m) : 0), b->w, top, bottom);
        }
        break;
    }
    case R_SYSTEM:
        draw_lines(m, b->x, y, b->scale, &DIM_COLORS, top, bottom);
        if (m->attach) draw_attachments(m, b->x, y + (m->len ? visible_height(m) : 0), b->w, top, bottom);
        break;
    case R_PROMPT: {
        float h = m->tl.height + 12;
        rect(PAD_X, y, TOP_W - PAD_X * 2, h, m->resolved ? COL_PAPER_2 : COL_ACCENT);
        rect(PAD_X, y, 5, h, COL_INK);
        draw_lines(m, b->x, y + 6, b->scale, &BODY_COLORS, top, bottom);
        break;
    }
    }
}

static void draw_status_line(float y)
{
    rect(0, y, TOP_W, TOP_STATUS_H, COL_SCR);
    rect(0, y, TOP_W, 1, COL_RULE);
    char buf[160];
    if (app.chat.typing) {
        braille(PAD_X, y + 3, 10, COL_BLUE);
        u64 el = (osGetTime() - app.chat.typing_since) / 1000;
        const char *verb = VERBS[(el / 3) % (sizeof VERBS / sizeof VERBS[0])];
        snprintf(buf, sizeof buf, "%s...", verb);
        float w = text_draw(buf, PAD_X + 13, y + 3, S_SMALL, COL_TX_2);
        snprintf(buf, sizeof buf, "%llus", (unsigned long long)el);
        text_draw(buf, PAD_X + 13 + w + 8, y + 3.5f, S_TOOL, COL_TX_3);
        return;
    }
    switch (app.net) {
    case NET_CONNECTING:
        braille(PAD_X, y + 3, 10, COL_BLUE);
        snprintf(buf, sizeof buf, "Connecting to %s...", app.server.name[0] ? app.server.name : app.server.host);
        break;
    case NET_WAITING: {
        long left = (long)((s64)(app.retry_at - osGetTime()) / 1000) + 1;
        if (left < 1) left = 1;
        snprintf(buf, sizeof buf, "%s. Retrying in %ld s.", app.net_detail, left);
        break;
    }
    case NET_REFUSED: snprintf(buf, sizeof buf, "Not connected: the PC refused this 3DS."); break;
    default: snprintf(buf, sizeof buf, "Offline."); break;
    }
    text_draw_fit(buf, PAD_X + (app.net == NET_CONNECTING ? 13 : 0), y + 3, S_SMALL, COL_TX_2, TOP_W - 30);
}

static void draw_footer(void)
{
    float y = footer_begin(), x = 7;
    if (app.screen == SCR_PAIR) {
        if (app.pstep == PAIR_LIST) x += hint(GLYPH_A, "Choose", x, y, COL_ON), x += hint(GLYPH_Y, "Scan again", x, y, COL_ON);
        else if (app.pstep == PAIR_CONFIRM) x += hint(GLYPH_A, "Pair", x, y, COL_ON), x += hint(GLYPH_B, "Back", x, y, COL_ON);
        else if (app.pstep == PAIR_ERROR) x += hint(GLYPH_A, "Try again", x, y, COL_ON);
        if (app.have_server && app.pstep != PAIR_CONFIRM) hint(GLYPH_B, "Back to chat", x, y, COL_ON);
        return;
    }
    if (app.chat.approval) {
        x += hint(GLYPH_A, "Allow once", x, y, COL_ON);
        x += hint(GLYPH_B, "Deny", x, y, COL_ON);
    } else if (app.chat.clarify && !clar_count()) {
        x += hint(GLYPH_A, "Answer", x, y, COL_ON);
    } else if (app.chat.clarify) {
        x += hint(GLYPH_A, clar_multi() ? "Tick" : "Pick", x, y, COL_ON);
        if (clar_multi()) x += hint(GLYPH_X, "Send", x, y, COL_ON);
        x += hint(GLYPH_Y, "Type", x, y, COL_ON);
    } else {
        x += hint(GLYPH_A, "Write", x, y, COL_ON);
        if (app.chat.typing) x += hint(GLYPH_B, "Stop", x, y, COL_ON);
        if (files_count()) x += hint(GLYPH_X, "Files", x, y, COL_ON);
        const json_t *au = app_chat_audio();
        AudioStatus s;
        if (au) x += hint(GLYPH_Y, audio_status(au, &s) && s.playing ? "Pause, hold: talk" : "Play, hold: talk", x, y,
                          COL_ON);
        else x += hint(GLYPH_Y, "Hold to talk", x, y, COL_ON);
    }
    if (app.chat.approval || app.chat.clarify) {
        const char *r = "Light stays on until you answer";
        float w = text_width(r, strlen(r), S_HINT);
        text_draw(r, TOP_W - 7 - w, y, S_HINT, COL_ON);
        rect(TOP_W - 7 - w - 9, y + 2, 5, 5, COL_ACCENT);
        return;
    }
    const char *r = "SELECT Pair";
    float w = text_width(r, strlen(r), S_HINT);
    if (x < TOP_W - 7 - w - 4) text_draw(r, TOP_W - 7 - w, y, S_HINT, COL_ON_3);
    if (x < TOP_W - 7 - w - 64) hint(GLYPH_DPAD, "Scroll", TOP_W - 7 - w - 60, y, COL_ON_3);
}

// "m:ss" since the pending approval / clarify arrived.
static void waiting_text(char *out, size_t cap)
{
    u64 s = (osGetTime() - app.chat.prompt_since) / 1000;
    snprintf(out, cap, "waiting %llu:%02llu", (unsigned long long)(s / 60), (unsigned long long)(s % 60));
}

// --- approval (design/mockups: Approval) --------------------------------------

#define APPR_CARD_H 112

// Pinned above the footer while Hermes waits; the transcript scrolls above it.
static void draw_approval_card(float y)
{
    json_t *a = app.chat.approval;
    const char *cmd = json_string_value(json_object_get(a, "command"));
    const char *desc = json_string_value(json_object_get(a, "description"));
    float x = 8, w = TOP_W - 16, h = APPR_CARD_H;
    char buf[64];
    rect(x + 3, y + 3, w, h, COL_BLUE);
    rect(x, y, w, h, COL_PAPER);
    // Yellow band: label, a run of slashes, and the wait time.
    rect(x, y, w, 14, COL_ACCENT);
    float lx = x + 6 + spaced("APPROVAL NEEDED", x + 6, y + 3.5f, S_EYE, COL_INK, 1.6f) + 6;
    waiting_text(buf, sizeof buf);
    float ww = text_width(buf, strlen(buf), S_TOOL);
    text_draw(buf, x + w - 6 - ww, y + 2, S_TOOL, COL_INK);
    for (float sx = lx; sx < x + w - 14 - ww; sx += 5) text_draw("/", sx, y + 1.5f, S_TOOL, COL_INK_2);
    frame_rect(x, y, w, h, COL_INK);
    text_draw("Hermes wants to run a command", x + 8, y + 19, S_BIG * 0.55f, COL_INK);
    // The command on blue, as a terminal line.
    float by = y + 42;
    rect(x + 8, by, w - 16, 34, COL_BLUE);
    float px = x + 14 + text_draw("$", x + 14, by + 4, S_SMALL, COL_ON_3) + 5;
    text_block(cmd ? cmd : "", px, by + 4, x + w - 14 - px, S_SMALL, &USER_COLORS, 2, NULL);
    if (desc && desc[0]) {
        rect(x + 8, by + 43, 11, 11, COL_BAD);
        text_draw("!", x + 12, by + 43, S_SMALL, COL_ON);
        float fx = x + 24 + text_draw("Flagged:", x + 24, by + 43, S_SMALL, COL_BAD) + 5;
        text_draw_fit(desc, fx, by + 43, S_SMALL, COL_BAD, x + w - 8 - fx);
    }
}

static void draw_chat_top(void)
{
    bool card = app.chat.approval != NULL;
    bool status = !card && (app.chat.typing || app.net != NET_ONLINE);
    float top = TOP_HEADER_H, bottom = SCR_H - TOP_FOOTER_H - (status ? TOP_STATUS_H : 0);
    if (card) bottom = SCR_H - TOP_FOOTER_H - APPR_CARD_H - 8;
    app.view_h = bottom - top - 8;

    // Layout pass: positions are cheap to recompute; wrapping is cached per message.
    static Block blocks[CHAT_MAX];
    float total = 0;
    for (int i = 0; i < app.chat.n; i++) {
        block_of(i, &blocks[i]);
        Msg *m = app.chat.m[i];
        relayout(m, &blocks[i]);
        if (m->role == R_ASSISTANT || m->role == R_SYSTEM)
            m->h = (m->len || !m->attach ? visible_height(m) : 0) + (m->attach ? attach_height(m) : 0);
        else if (m->role == R_USER && m->attach)
            m->h = (m->len ? m->tl.height + 11 : 0) + attach_height(m);
        total += blocks[i].gap + blocks[i].head_h + m->h;
    }
    // Keep the view anchored when content grows while scrolled up.
    if (app.scroll > 0 && total > app.content_h) app.scroll += total - app.content_h;
    app.content_h = total;
    float max = total - app.view_h;
    if (app.scroll > max) app.scroll = max > 0 ? max : 0;

    float y = top + 8 - (total > app.view_h ? total - app.view_h - app.scroll : 0);
    for (int i = 0; i < app.chat.n; i++) {
        const Block *b = &blocks[i];
        float h = b->gap + b->head_h + app.chat.m[i]->h;  // as measured in the layout pass
        if (y + h >= top && y <= bottom) draw_message(i, b, y + b->gap, top, bottom);
        y += h;
    }
    if (app.chat.n == 0) {
        text_draw("Say hello. Press " GLYPH_A " to write.", TEXT_X, top + 12, S_BODY, COL_TX_3);
    }

    // Scroll indicator when not at the bottom.
    if (app.scroll > 1 && total > app.view_h) {
        float track = bottom - top - 4, frac = app.view_h / total, h = track * frac;
        if (h < 10) h = 10;
        float pos = (1.0f - (app.scroll + app.view_h) / total) * track;
        rect(TOP_W - 4, top + 2 + pos, 2, h, COL_LINE_B);
    }

    char title[128];
    snprintf(title, sizeof title, "%s",
             app.chat.title[0] ? app.chat.title : app.server.name[0] ? app.server.name : "Hermes");
    if (card) {
        rect(0, bottom, TOP_W, SCR_H - bottom, COL_PAPER);
        draw_approval_card(bottom + 2);
    }
    draw_header(title);
    if (status) draw_status_line(bottom);
    draw_footer();
}

// --- bottom screen: chat ----------------------------------------------------

const char *const APPR_CHOICE[APPR_N] = {"once", "session", "always", "deny"};

// Which choices the request offers (all four when it doesn't say).
static bool appr_offered(int i)
{
    json_t *acts = json_object_get(app.chat.approval, "actions");
    if (!json_array_size(acts)) return true;
    for (size_t k = 0; k < json_array_size(acts); k++)
        if (!strcmp(json_string_value(json_array_get(json_array_get(acts, k), 1)) ?: "", APPR_CHOICE[i])) return true;
    return false;
}

// The pending clarify's question, choice count, choice i and whether several may be picked.
static const char *clar_question(void)
{
    return json_string_value(json_object_get(app.chat.clarify, "question")) ?: "";
}

int clar_count(void)
{
    return (int)json_array_size(json_object_get(app.chat.clarify, "choices"));
}

const char *clar_choice(int i)
{
    return json_string_value(json_array_get(json_object_get(app.chat.clarify, "choices"), (size_t)i)) ?: "";
}

bool clar_multi(void)
{
    return json_is_true(json_object_get(app.chat.clarify, "multi")) && clar_count() > 0;
}

#define CLAR_Q_Y 45
#define CLAR_ROW_H 26
#define CLAR_ROW_STEP 30

static void clarify_layout(BottomLayout *l)
{
    int n = clar_count();
    l->clar_q_h = text_block(clar_question(), 11, CLAR_Q_Y, BOT_W - 22, S_BODY, NULL, n ? 2 : 4, NULL);
    float y = CLAR_Q_Y + l->clar_q_h + 8;
    if (!n) {  // open-ended: one big "type" button
        l->clar_type = (Rect){8, y + 4, BOT_W - 16, 40};
        return;
    }
    const float bot = SCR_H - 8 - 30;
    int fit = (int)((bot - 6 - y + (CLAR_ROW_STEP - CLAR_ROW_H)) / CLAR_ROW_STEP);
    int rows = n < fit ? n : fit;
    if (rows > CLAR_ROWS) rows = CLAR_ROWS;
    if (rows < 1) rows = 1;
    int first = app.chat.clar_cur - rows + 1;
    if (first > n - rows) first = n - rows;
    if (first < 0) first = 0;
    l->clar_rows = rows;
    l->clar_first = first;
    for (int r = 0; r < rows; r++) l->clar[r] = (Rect){8, y + r * CLAR_ROW_STEP, BOT_W - 16, CLAR_ROW_H};
    if (clar_multi()) {
        float w = (BOT_W - 22) / 2.0f;
        l->clar_type = (Rect){8, bot, w, 30};
        l->clar_send = (Rect){8 + w + 6, bot, w, 30};
    } else {
        l->clar_type = (Rect){8, bot, BOT_W - 16, 30};
    }
}

void bottom_layout(BottomLayout *l)
{
    memset(l, 0, sizeof *l);
    for (int i = 0; i < 5; i++) l->tabs[i] = (Rect){i * 64.0f, 0, 64, TABS_H};
    if (app.chat.approval) {
        const Rect full[APPR_N] = {
            {8, 76, BOT_W - 16, 40}, {8, 122, (BOT_W - 22) / 2.0f, 40},
            {8 + (BOT_W - 22) / 2.0f + 6, 122, (BOT_W - 22) / 2.0f, 40}, {8, 168, BOT_W - 16, 36}};
        bool both = appr_offered(APPR_SESSION) && appr_offered(APPR_ALWAYS);
        for (int i = 0; i < APPR_N; i++)
            if (appr_offered(i)) l->appr[i] = full[i];
        if (!both) {  // a lone middle choice takes the whole row
            int i = appr_offered(APPR_SESSION) ? APPR_SESSION : APPR_ALWAYS;
            if (appr_offered(i)) l->appr[i] = (Rect){8, 122, BOT_W - 16, 40};
        }
    } else if (app.chat.clarify) {
        clarify_layout(l);
    }
    float y = TABS_H + 7;
    // (A pending approval or clarify replaces this whole screen with its own panel.)
    l->strip_kind = app.net == NET_REFUSED ? STRIP_REFUSED : app.chat.typing ? STRIP_WORKING : STRIP_NONE;
    if (l->strip_kind != STRIP_NONE) {
        l->strip = (Rect){8, y, BOT_W - 16, 34};
        l->strip_btn = (Rect){BOT_W - 8 - 6 - 58, y + 6, 58, 22};
        y += 34 + 6;
    }
    l->input = (Rect){8, y, BOT_W - 16, 32};
    y += 32 + 8;
    l->talk = (Rect){8, y, (BOT_W - 22) / 2.0f, 38};
    l->photo = (Rect){8 + (BOT_W - 22) / 2.0f + 6, y, (BOT_W - 22) / 2.0f, 38};
    y += 38 + 6;
    float chips_y = SCR_H - 8 - 15;
    l->panel = (Rect){8, y, BOT_W - 16, chips_y - 6 - y};
    if (files_count()) l->files_btn = (Rect){l->panel.x + l->panel.w - 6 - 74, l->panel.y + 5, 74, 18};
    float x = 8;
    for (int i = 0; i < N_CHIPS; i++) {
        float w = text_width(CHIP_CMDS[i], strlen(CHIP_CMDS[i]), S_SMALL) + 12;
        l->chips[i] = (Rect){x, chips_y, w, 15};
        x += w + 4;
    }
}

static void draw_approval_bottom(const BottomLayout *l)
{
    static const char *const TITLE[APPR_N] = {"Allow once", "This session", "Always", "Deny"};
    static const char *const SUB[APPR_N] = {"Just this command, right now", "Until you start a new one",
                                            "For commands like this", "Hermes will find another way"};
    char buf[48];
    spaced("APPROVE", 12, 34, S_EYE, COL_TX_3, 1.6f);
    waiting_text(buf, sizeof buf);
    float w = text_width(buf, strlen(buf), S_TOOL);
    text_draw(buf, BOT_W - 12 - w, 33, S_TOOL, COL_TX_3);
    text_draw("Run this command?", 11, 45, S_BIG * 0.7f, COL_INK);
    for (int i = 0; i < APPR_N; i++) {
        const Rect *r = &l->appr[i];
        if (r->w <= 0) continue;
        bool primary = i == APPR_ONCE, deny = i == APPR_DENY;
        u32 bg = primary ? COL_BLUE : COL_PAPER, fg = primary ? COL_ON : deny ? COL_BAD : COL_INK;
        u32 fg2 = primary ? COL_ON_2 : deny ? COL_BAD : COL_TX_3;
        rect(r->x, r->y, r->w, r->h, bg);
        frame_rect(r->x, r->y, r->w, r->h, primary ? COL_BLUE : deny ? COL_BAD : COL_RULE);
        float ty = r->y + (r->h - 26) / 2;
        text_draw_fit(TITLE[i], r->x + 10, ty, S_BODY, fg, r->w - 34);
        text_draw_fit(SUB[i], r->x + 10, ty + 15, S_TOOL, fg2, r->w - 34);
        const char *glyph = primary ? GLYPH_A : deny ? GLYPH_B : ">";
        text_draw(glyph, r->x + r->w - 18, r->y + (r->h - 14) / 2, S_HINT * 1.2f, fg);
    }
    text_draw("Change how often Hermes asks: More > /approvals", 11, SCR_H - 22, S_TOOL, COL_TX_3);
}

static void draw_clarify_bottom(const BottomLayout *l)
{
    int n = clar_count();
    bool multi = clar_multi();
    char buf[48];
    float ex = spaced(!n ? "QUESTION" : multi ? "PICK ANY" : "PICK ONE", 12, 34, S_EYE, COL_TX_3, 1.6f);
    if (n > l->clar_rows) {
        snprintf(buf, sizeof buf, "%d of %d", app.chat.clar_cur + 1, n);
        text_draw(buf, 12 + ex + 6, 33, S_TOOL, COL_TX_3);
    }
    waiting_text(buf, sizeof buf);
    text_draw(buf, BOT_W - 12 - text_width(buf, strlen(buf), S_TOOL), 33, S_TOOL, COL_TX_3);
    text_block(clar_question(), 11, CLAR_Q_Y, BOT_W - 22, S_BODY, &BODY_COLORS, n ? 2 : 4, NULL);
    if (!n) {
        button(&l->clar_type, "Type your answer", GLYPH_A, COL_BLUE, COL_ON);
        return;
    }
    for (int r = 0; r < l->clar_rows; r++) {
        const Rect *b = &l->clar[r];
        int i = l->clar_first + r;
        bool cur = i == app.chat.clar_cur, on = multi && (app.chat.clar_sel >> i & 1);
        u32 fg = cur ? COL_ON : COL_INK;
        rect(b->x, b->y, b->w, b->h, cur ? COL_BLUE : COL_PAPER);
        frame_rect(b->x, b->y, b->w, b->h, cur ? COL_BLUE : COL_RULE);
        float tx = b->x + 10;
        if (multi) {
            float bx = b->x + 8, by = b->y + (b->h - 12) / 2;
            rect(bx, by, 12, 12, on ? (cur ? COL_ON : COL_BLUE) : (cur ? COL_BLUE : COL_PAPER));
            frame_rect(bx, by, 12, 12, cur ? COL_ON : COL_TX_3);
            if (on) check_mark(bx + 2, by + 2, 8, cur ? COL_BLUE : COL_ON);
            tx = bx + 20;
        } else {
            snprintf(buf, sizeof buf, "%d", i + 1);
            text_draw(buf, tx, b->y + (b->h - 12) / 2, S_SMALL, cur ? COL_ON_2 : COL_TX_3);
            tx += 14;
        }
        text_draw_fit(clar_choice(i), tx, b->y + (b->h - 13) / 2, S_BODY, fg, b->x + b->w - 24 - tx);
        if (cur) text_draw(GLYPH_A, b->x + b->w - 18, b->y + (b->h - 14) / 2, S_HINT * 1.2f, fg);
    }
    button(&l->clar_type, multi ? "Type instead" : "Type an answer", GLYPH_Y, COL_PAPER, COL_INK);
    if (multi) {
        int k = 0;
        for (int i = 0; i < n && i < 32; i++) k += app.chat.clar_sel >> i & 1;
        snprintf(buf, sizeof buf, k ? "Send %d" : "Send", k);
        button(&l->clar_send, buf, GLYPH_X, k ? COL_BLUE : COL_PAPER_2, k ? COL_ON : COL_TX_3);
    }
}

static void draw_chat_bottom(void)
{
    BottomLayout l;
    bottom_layout(&l);
    rect(0, 0, BOT_W, SCR_H, COL_PAPER_2);
    draw_tabs();
    if (app.chat.approval) {
        draw_approval_bottom(&l);
        return;
    }
    if (app.chat.clarify) {
        draw_clarify_bottom(&l);
        return;
    }
    char buf[200];

    const Rect *s = &l.strip;
    switch (l.strip_kind) {
    case STRIP_WORKING: {
        rect(s->x, s->y, s->w, s->h, COL_BLUE);
        braille(s->x + 9, s->y + 12, 11, COL_ON);
        text_draw("Hermes is working", s->x + 24, s->y + 5, S_BODY * 0.95f, COL_ON);
        u64 el = (osGetTime() - app.chat.typing_since) / 1000;
        int tools = 0;
        for (int i = app.chat.n - 1; i >= 0 && i >= app.chat.n - 30; i--)
            if (app.chat.m[i]->role == R_TOOL && app.chat.m[i]->running) tools++;
        snprintf(buf, sizeof buf, "%llus%s", (unsigned long long)el, tools ? " · running a tool" : "");
        text_draw(buf, s->x + 24, s->y + 20, S_TOOL, COL_ON_2);
        button(&l.strip_btn, "Stop", GLYPH_B, COL_PAPER, COL_BLUE);
        break;
    }
    case STRIP_REFUSED: {
        rect(s->x, s->y, s->w, s->h, COL_ACCENT);
        rect(s->x, s->y, 14, s->h, COL_INK);
        text_draw("!", s->x + 5, s->y + 10, S_BODY, COL_ACCENT);
        float tw = l.strip_btn.x - (s->x + 20) - 6;
        text_draw_fit("Not paired any more", s->x + 20, s->y + 5, S_BODY * 0.95f, COL_INK, tw);
        text_draw_fit("Pair this 3DS with the PC again", s->x + 20, s->y + 20, S_TOOL, COL_INK, tw);
        button(&l.strip_btn, "Pair", GLYPH_A, COL_INK, COL_ACCENT);
        break;
    }
    case STRIP_NONE: break;
    }

    // Input bar: paper box, ink border, hard blue shadow.
    const Rect *in = &l.input;
    rect(in->x + 2, in->y + 2, in->w, in->h, COL_BLUE);
    rect(in->x, in->y, in->w, in->h, COL_PAPER);
    frame_rect(in->x, in->y, in->w, in->h, COL_INK);
    // keyboard icon: a key grid
    for (int r = 0; r < 3; r++)
        for (int k = 0; k < 4 - (r == 2 ? 2 : 0); k++)
            rect(in->x + 9 + k * 3 + (r == 2 ? 3 : 0), in->y + 11 + r * 3, 2, 2, COL_BLUE);
    const char *ph = app.draft[0] ? app.draft : "Write to Hermes...";
    float pw = text_draw_fit(ph, in->x + 26, in->y + 5, S_BODY * 0.95f, app.draft[0] ? COL_INK : COL_INK_3,
                             in->w - 26 - 24);
    if ((osGetTime() / 530) % 2 == 0 || !app_animating())
        rect(in->x + 27 + pw, in->y + 6, 3, 10, COL_BLUE);
    text_draw(app.chat.typing ? "Queues until this step ends" : "Tap here or press " GLYPH_A, in->x + 26,
              in->y + 19, S_TOOL, COL_INK_3);

    // Quick tiles: hold to talk, take a photo.
    const Rect *tk = &l.talk, *pt = &l.photo;
    rect(tk->x + 2, tk->y + 2, tk->w, tk->h, COL_INK);
    rect(tk->x, tk->y, tk->w, tk->h, COL_BLUE);
    float mx = tk->x + 16, my = tk->y + 19;  // a microphone: capsule and stand
    disc(mx, my - 7, 4, COL_ON);
    rect(mx - 4, my - 7, 8, 7, COL_ON);
    disc(mx, my, 4, COL_ON);
    rect(mx - 1, my + 4, 2, 5, COL_ON);
    rect(mx - 4, my + 9, 8, 2, COL_ON);
    text_draw("Talk", tk->x + 30, tk->y + 5, S_BODY, COL_ON);
    text_draw("Hold " GLYPH_Y " or keep a finger here", tk->x + 30, tk->y + 21, S_TOOL * 0.92f, COL_ON_2);
    rect(pt->x + 2, pt->y + 2, pt->w, pt->h, COL_BLUE);
    rect(pt->x, pt->y, pt->w, pt->h, COL_PAPER);
    frame_rect(pt->x, pt->y, pt->w, pt->h, COL_INK);
    float cx = pt->x + 16, cy = pt->y + 19;  // a camera: body, lens, viewfinder bump
    rect(cx - 9, cy - 6, 18, 13, COL_INK);
    rect(cx - 4, cy - 9, 8, 3, COL_INK);
    disc(cx, cy + 0.5f, 4.5f, COL_PAPER);
    disc(cx, cy + 0.5f, 2.5f, COL_BLUE);
    text_draw("Photo", pt->x + 30, pt->y + 5, S_BODY, COL_INK);
    text_draw("For Hermes to look at", pt->x + 30, pt->y + 21, S_TOOL * 0.92f, COL_TX_3);

    // Connection panel.
    const Rect *p = &l.panel;
    rect(p->x, p->y, p->w, p->h, COL_PAPER);
    frame_rect(p->x, p->y, p->w, p->h, COL_RULE);
    float y = p->y + 6;
    spaced("CONNECTION", p->x + 8, y, S_EYE, COL_TX_3, 1.4f);
    if (l.files_btn.w > 0) {
        snprintf(buf, sizeof buf, "Files · %d", files_count());
        button(&l.files_btn, buf, GLYPH_X, COL_BLUE, COL_ON);
    }
    y += 12;
    snprintf(buf, sizeof buf, "%s", app.server.name[0] ? app.server.name : app.server.host);
    text_draw_fit(buf, p->x + 8, y, S_BODY * 0.95f, COL_INK, p->w - 16 - (l.files_btn.w > 0 ? 80 : 0));
    y += 15;
    char fp[9];
    key_fingerprint(app.server.key, fp);
    snprintf(buf, sizeof buf, "%s:%u%s  ·  PC key %.4s %.4s", app.server.host, app.server.port,
             app.server.away_host[0] ? "  ·  away ready" : "", fp, fp + 4);
    text_draw_fit(buf, p->x + 8, y, S_TOOL, COL_TX_2, p->w - 16);
    y += 13;
    switch (app.net) {
    case NET_ONLINE:
        if (app.route == ROUTE_AWAY)
            snprintf(buf, sizeof buf, "Online away · TLS %lu ms%s · Noise %lu ms", (unsigned long)app.tls.ms,
                     app.tls.resumed ? " (resumed)" : "", (unsigned long)app.handshake_ms);
        else
            snprintf(buf, sizeof buf, "Online · encrypted (Noise IK) · handshake %lu ms",
                     (unsigned long)app.handshake_ms);
        break;
    case NET_CONNECTING: snprintf(buf, sizeof buf, "Connecting..."); break;
    case NET_WAITING: snprintf(buf, sizeof buf, "%s · retrying", app.net_detail); break;
    case NET_REFUSED: snprintf(buf, sizeof buf, "Refused by the PC"); break;
    default: snprintf(buf, sizeof buf, "Offline"); break;
    }
    if (y + 10 < p->y + p->h) {
        rect(p->x + 8, y + 3, 5, 5, app.net == NET_ONLINE ? COL_OK : app.net == NET_REFUSED ? COL_BAD : COL_TX_3);
        text_draw_fit(buf, p->x + 17, y, S_TOOL, COL_TX_2, p->w - 25);
    }

    for (int i = 0; i < N_CHIPS; i++) {
        const Rect *c = &l.chips[i];
        rect(c->x, c->y, c->w, c->h, COL_PAPER);
        frame_rect(c->x, c->y, c->w, c->h, COL_RULE);
        float x = c->x + 6;
        x += text_draw("/", x, c->y + 2, S_SMALL, COL_BLUE);
        text_draw(CHIP_CMDS[i] + 1, x, c->y + 2, S_SMALL, COL_INK_2);
    }
}

// --- pairing screens --------------------------------------------------------

static void draw_pair_top(void)
{
    rect(0, 0, TOP_W, SCR_H, COL_PAPER);
    draw_header("Pair with your PC");
    float y = 34;
    float w = text_draw("HERMES", PAD_X + 10, y, S_BIG, COL_BLUE);
    text_draw("HERMES", PAD_X + 11, y, S_BIG, COL_BLUE);
    text_draw("3DS", PAD_X + 10 + w + 8, y + 14, S_SMALL, COL_TX_3);
    y += 38;
    text_draw("The agent on your PC, in your pocket.", PAD_X + 10, y, S_BODY, COL_TX_2);
    y += 28;
    rect(PAD_X + 10, y - 8, TOP_W - 2 * (PAD_X + 10), 1, COL_RULE);
    char buf[200], fp[9];
    float x = PAD_X + 10, maxw = TOP_W - 2 * x;
    switch (app.pstep) {
    case PAIR_SCAN:
        braille(x, y + 2, 11, COL_BLUE);
        text_draw("Looking for Hermes on your network...", x + 14, y, S_BODY, COL_INK);
        break;
    case PAIR_LIST:
        text_draw("1. On the PC, run:", x, y, S_BODY, COL_INK);
        text_draw("hermes 3ds pair", x + 110, y, S_BODY, COL_BLUE);
        text_draw("2. Pick the PC below and type the 8-character code.", x, y + 17, S_BODY, COL_INK);
        text_draw(app.nfound ? "Not listed? Choose \"Enter the PC's address\"."
                             : "No PC answered. Check Wi-Fi, or enter the address.",
                  x, y + 40, S_SMALL, COL_TX_3);
        break;
    case PAIR_CONFIRM:
        key_fingerprint(app.cand.key, fp);
        text_draw("Check that the PC shows this fingerprint:", x, y, S_BODY, COL_INK);
        snprintf(buf, sizeof buf, "%.4s %.4s", fp, fp + 4);
        spaced(buf, x, y + 18, S_BIG * 0.75f, COL_BLUE, 3);
        snprintf(buf, sizeof buf, "%s  ·  %s:%u", app.cand.name, app.cand.host, app.cand.port);
        text_draw_fit(buf, x, y + 50, S_SMALL, COL_TX_2, maxw);
        text_draw("Different? Press " GLYPH_B " and check you picked the right PC.", x, y + 66, S_SMALL, COL_TX_3);
        break;
    case PAIR_WORKING:
        braille(x, y + 2, 11, COL_BLUE);
        snprintf(buf, sizeof buf, "Pairing with %s...", app.cand.name[0] ? app.cand.name : app.cand.host);
        text_draw_fit(buf, x + 14, y, S_BODY, COL_INK, maxw - 14);
        break;
    case PAIR_ERROR: {
        text_draw("Pairing didn't work", x, y, S_BODY, COL_BAD);
        TextLayout tl = {0};
        text_segs_plain(&tl, (uint32_t)strlen(app.pair_msg), 0);
        text_layout(&tl, app.pair_msg, (uint32_t)strlen(app.pair_msg), maxw, S_SMALL);
        float ly = y + 18;
        for (int i = 0; i < tl.nlines; i++) {
            text_draw_line(&tl, i, app.pair_msg, x, ly, S_SMALL, &BODY_COLORS, UINT32_MAX);
            ly += tl.lines[i].height;
        }
        text_layout_free(&tl);
        break;
    }
    }
    draw_footer();
}

static void draw_pair_bottom(void)
{
    rect(0, 0, BOT_W, SCR_H, COL_PAPER_2);
    rect(0, 0, BOT_W, TABS_H, COL_PAPER);
    rect(0, TABS_H - 1, BOT_W, 1, COL_INK);
    spaced("PCS ON THIS NETWORK", 10, 9, S_EYE, COL_INK_2, 1.4f);
    text_draw("Emoji graphics: Twemoji, CC-BY 4.0 (twitter, jdecked & contributors)", 10, SCR_H - 12, S_EYE,
              COL_TX_3);
    if (app.pstep == PAIR_SCAN || app.pstep == PAIR_WORKING) {
        braille(BOT_W / 2 - 4, 110, 12, COL_BLUE);
        return;
    }
    char buf[128], fp[9];
    int items = app.nfound + 1;
    for (int i = 0; i < items; i++) {
        float y = 38 + i * 34.0f;
        if (y + 30 > SCR_H - 14) break;  // above the credits line
        bool sel = i == app.sel && app.pstep == PAIR_LIST;
        Rect r = {8, y, BOT_W - 16, 30};
        rect(r.x, r.y, r.w, r.h, sel ? COL_BLUE : COL_PAPER);
        frame_rect(r.x, r.y, r.w, r.h, sel ? COL_BLUE : COL_RULE);
        u32 fg = sel ? COL_ON : COL_INK, fg2 = sel ? COL_ON_2 : COL_TX_3;
        if (i < app.nfound) {
            const ServerInfo *s = &app.found_info[i];
            text_draw_fit(s->name[0] ? s->name : s->host, r.x + 8, r.y + 3, S_BODY * 0.95f, fg, r.w - 16);
            key_fingerprint(s->key, fp);
            snprintf(buf, sizeof buf, "%s:%u  ·  fingerprint %.4s %.4s", s->host, s->port, fp, fp + 4);
            text_draw_fit(buf, r.x + 8, r.y + 17, S_TOOL, fg2, r.w - 16);
        } else {
            text_draw("Enter the PC's address", r.x + 8, r.y + 3, S_BODY * 0.95f, fg);
            text_draw("If it isn't listed above", r.x + 8, r.y + 17, S_TOOL, fg2);
        }
    }
}

// --- frame ------------------------------------------------------------------

void draw_frame(C3D_RenderTarget *top, C3D_RenderTarget *bottom)
{
    text_frame_begin();
    C3D_FrameBegin(C3D_FRAME_SYNCDRAW);
    C2D_TargetClear(top, COL_PAPER);
    C2D_SceneBegin(top);
    bool tab = app.screen == SCR_CHAT && app.tab != TAB_CHAT;
    bool files = app.screen == SCR_CHAT && viewer_is_open();
    bool cam = app.screen == SCR_CHAT && camera_is_open(), rec = app.screen == SCR_CHAT && voice_recording();
    if (app.screen == SCR_PAIR) draw_pair_top();
    else if (rec) voice_draw_top();
    else if (cam) camera_draw_top();
    else if (files) viewer_draw_top();
    else if (tab) tabs_draw_top();
    else draw_chat_top();
    C2D_TargetClear(bottom, COL_PAPER_2);
    C2D_SceneBegin(bottom);
    if (app.screen == SCR_PAIR) draw_pair_bottom();
    else if (rec) voice_draw_bottom();
    else if (cam) camera_draw_bottom();
    else if (files) viewer_draw_bottom();
    else if (tab) tabs_draw_bottom();
    else draw_chat_bottom();
    draw_toast();
    C3D_FrameEnd(0);
}
