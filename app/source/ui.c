#include "ui.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "codec.h"
#include "image.h"

float S_BODY, S_SMALL, S_EYE, S_TOOL, S_TITLE, S_BIG, S_HINT;

const TextColors BODY_COLORS = {
    .ink = COL_INK, .dim = COL_TX_3, .code_ink = COL_BLUE, .code_bg = COL_BLUE_SOFT,
    .link = COL_BLUE, .add = COL_OK, .del = COL_BAD, .quote_bar = COL_LINE_B,
};
const TextColors USER_COLORS = {
    .ink = COL_ON, .dim = COL_ON_2, .code_ink = COL_ON, .code_bg = C32(0x30, 0x30, 0xf5, 0xff),
    .link = COL_ON, .add = COL_ON, .del = COL_ON, .quote_bar = COL_ON_3,
};
const TextColors DIM_COLORS = {
    .ink = COL_TX_3, .dim = COL_TX_3, .code_ink = COL_TX_2, .code_bg = COL_SCR_2,
    .link = COL_BLUE, .add = COL_OK, .del = COL_BAD, .quote_bar = COL_RULE,
};

// ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ as braille dot bits (dot1=1, dot2=2, dot3=4, dot4=8, dot5=16, dot6=32)
static const uint8_t BRAILLE[10] = {0x0B, 0x19, 0x39, 0x38, 0x3C, 0x34, 0x26, 0x27, 0x07, 0x0F};

void ui_init(void)
{
    float lf = text_line_height(1.0f);
    S_BODY = 12.5f / lf;
    S_SMALL = 10.5f / lf;
    S_TOOL = 9.5f / lf;
    S_EYE = 8.0f / lf;
    S_HINT = 9.0f / lf;
    S_TITLE = 10.0f / lf;
    S_BIG = 30.0f / lf;
}

// --- primitives -------------------------------------------------------------

void rect(float x, float y, float w, float h, u32 c) { C2D_DrawRectSolid(x, y, Z, w, h, c); }

// A filled circle as a triangle fan (C2D_DrawCircleSolid's shader path doesn't survive the text renderer's
// texenv setup and draws a shaded square).
void disc(float cx, float cy, float r, u32 c)
{
    int n = r < 6 ? 12 : r < 20 ? 20 : 36;
    float px = cx + r, py = cy;
    for (int i = 1; i <= n; i++) {
        float a = 6.2831853f * (float)i / (float)n, qx = cx + r * cosf(a), qy = cy + r * sinf(a);
        C2D_DrawTriangle(cx, cy, c, px, py, c, qx, qy, c, Z);
        px = qx;
        py = qy;
    }
}

void frame_rect(float x, float y, float w, float h, u32 c)
{
    rect(x, y, w, 1, c);
    rect(x, y + h - 1, w, 1, c);
    rect(x, y, 1, h, c);
    rect(x + w - 1, y, 1, h, c);
}

// Braille spinner cell drawn as dots (the system font has no braille).
void braille(float x, float y, float cell, u32 color)
{
    int frame = (int)((osGetTime() / 80) % 10);
    uint8_t bits = BRAILLE[frame];
    float d = floorf(cell / 5.0f);
    if (d < 1) d = 1;
    float step = d + floorf(d * 0.6f + 0.5f);
    static const int col[6] = {0, 0, 0, 1, 1, 1}, row[6] = {0, 1, 2, 0, 1, 2};
    for (int i = 0; i < 6; i++)
        if (bits & (1 << i)) rect(x + col[i] * step, y + row[i] * step + 1, d, d, color);
}

void check_mark(float x, float y, float s, u32 color)
{
    C2D_DrawLine(x, y + s * 0.55f, color, x + s * 0.38f, y + s * 0.9f, color, 1.2f, Z);
    C2D_DrawLine(x + s * 0.38f, y + s * 0.9f, color, x + s, y + s * 0.1f, color, 1.2f, Z);
}

// Play triangle or pause bars filling an s x s square.
void play_icon(float x, float y, float s, bool pause, u32 color)
{
    if (pause) {
        rect(x + s * 0.18f, y, s * 0.24f, s, color);
        rect(x + s * 0.58f, y, s * 0.24f, s, color);
    } else {
        C2D_DrawTriangle(x + s * 0.12f, y, color, x + s * 0.12f, y + s, color, x + s * 0.95f, y + s / 2, color, Z);
    }
}

// Audio tile: like file_tile, with a play/pause icon (blue when it is the current track).
void audio_tile(float x, float y, float s, bool current, bool playing)
{
    rect(x, y, s, s, current ? COL_BLUE : COL_PAPER);
    frame_rect(x, y, s, s, current ? COL_BLUE : COL_INK);
    float i = s * 0.42f;
    play_icon(x + (s - i) / 2 + (playing ? 0 : s * 0.03f), y + (s - i) / 2, i, playing, current ? COL_ON : COL_BLUE);
}

// Progress bar: track, downloaded part (light) and played part (blue).
void progress_bar(float x, float y, float w, float h, float played, float loaded)
{
    played = played < 0 ? 0 : played > 1 ? 1 : played;
    loaded = loaded < played ? played : loaded > 1 ? 1 : loaded;
    rect(x, y, w, h, COL_RULE);
    rect(x, y, w * loaded, h, COL_LINE_B);
    rect(x, y, w * played, h, COL_BLUE);
}

// Letter-spaced caps for eyebrows (Rules Expanded in the mockups).
// Bytes in the character at p (a broken sequence counts its lead byte and up to 3 more).
static int char_len(const char *p)
{
    int n = 1;
    while (n < 4 && ((unsigned char)p[n] & 0xC0) == 0x80) n++;
    return n;
}

float spaced(const char *s, float x, float y, float scale, u32 color, float spacing)
{
    char ch[8];
    float x0 = x;
    for (const char *p = s; *p;) {
        int n = char_len(p);
        memcpy(ch, p, (size_t)n);
        ch[n] = 0;
        x += text_draw(ch, x, y, scale, color) + spacing;
        p += n;
    }
    return x - x0;
}

float spaced_width(const char *s, float scale, float spacing)
{
    float w = 0;
    for (const char *p = s; *p;) {
        int n = char_len(p);
        w += text_width(p, (size_t)n, scale) + spacing;
        p += n;
    }
    return w;
}

float spaced_fit(const char *s, float x, float y, float scale, u32 color, float spacing, float max_w)
{
    if (spaced_width(s, scale, spacing) <= max_w) return spaced(s, x, y, scale, color, spacing);
    char ch[8];
    float ell = text_width("...", 3, scale), w = 0;
    for (const char *p = s; *p;) {
        int n = char_len(p);
        float cw = text_width(p, (size_t)n, scale) + spacing;
        if (w + cw + ell > max_w) break;
        memcpy(ch, p, (size_t)n);
        ch[n] = 0;
        text_draw(ch, x + w, y, scale, color);
        w += cw;
        p += n;
    }
    return w + text_draw("...", x + w, y, scale, color);
}

float hint(const char *glyph, const char *label, float x, float y, u32 color)
{
    float w = text_draw(glyph, x, y - 1, S_HINT * 1.15f, color);
    w += 3;
    w += text_draw(label, x + w, y, S_HINT, color);
    return w + 10;
}

void button(const Rect *r, const char *label, const char *glyph, u32 bg, u32 fg)
{
    rect(r->x, r->y, r->w, r->h, bg);
    rect(r->x, r->y + r->h - 1, r->w, 1, C32(0, 0, 0, 0x40));
    rect(r->x + r->w - 1, r->y, 1, r->h, C32(0, 0, 0, 0x40));
    float w = text_width(label, strlen(label), S_SMALL) + (glyph ? 13 : 0);
    float x = r->x + (r->w - w) / 2;
    if (glyph) x += text_draw(glyph, x, r->y + (r->h - 14) / 2, S_HINT * 1.15f, fg) + 3;
    text_draw(label, x, r->y + (r->h - 12) / 2, S_SMALL, fg);
}

void size_text(double bytes, char *out, size_t cap)
{
    if (bytes < 1000) snprintf(out, cap, "%.0f B", bytes);
    else if (bytes < 1000 * 1000) snprintf(out, cap, "%.0f KB", bytes / 1024);
    else snprintf(out, cap, "%.1f MB", bytes / (1024 * 1024));
}

void ext_label(const char *name, char *out, size_t cap)
{
    const char *dot = strrchr(name, '.');
    size_t n = 0;
    if (dot && dot[1])
        for (const char *p = dot + 1; *p && n < 4 && n + 1 < cap; p++)
            out[n++] = (char)(*p >= 'a' && *p <= 'z' ? *p - 32 : *p);
    if (!n) snprintf(out, cap, "FILE");
    else out[n] = 0;
}

void file_tile(float x, float y, float s, const char *name)
{
    char ext[8];
    ext_label(name, ext, sizeof ext);
    rect(x, y, s, s, COL_PAPER);
    frame_rect(x, y, s, s, COL_INK);
    float sc = S_TOOL * (s >= 40 ? 1.6f : 1.0f);
    float w = text_width(ext, strlen(ext), sc);
    text_draw(ext, x + (s - w) / 2, y + s / 2 - 5 * sc / S_TOOL, sc, COL_BLUE);
}

// --- times ------------------------------------------------------------------

void clock_text(time_t t, char *out, size_t cap)
{
    if (!t) {
        out[0] = 0;
        return;
    }
    struct tm *tm = gmtime(&t);  // the 3DS RTC holds local time, so no zone conversion
    int h = tm->tm_hour % 12;
    snprintf(out, cap, "%d:%02d %s", h ? h : 12, tm->tm_min, tm->tm_hour < 12 ? "AM" : "PM");
}

void when_text(double unix_t, char *out, size_t cap)
{
    static const char *const MON[12] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    if (unix_t <= 0) {
        out[0] = 0;
        return;
    }
    time_t now = time(NULL), t = (time_t)unix_t + app.tz_off;
    if (now - t < 90 && now - t > -90) {
        snprintf(out, cap, "now");
        return;
    }
    struct tm a = *gmtime(&t), b = *gmtime(&now);
    long day_t = (long)(t / 86400), day_now = (long)(now / 86400);
    if (day_t == day_now) clock_text(t, out, cap);
    else if (day_t == day_now - 1) snprintf(out, cap, "Yesterday");
    else if (a.tm_year == b.tm_year) snprintf(out, cap, "%s %d", MON[a.tm_mon], a.tm_mday);
    else snprintf(out, cap, "%s %d, %d", MON[a.tm_mon], a.tm_mday, a.tm_year + 1900);
}

// --- text -------------------------------------------------------------------

float text_block(const char *s, float x, float y, float w, float scale, const TextColors *c, int max_lines,
                 float *out_w)
{
    TextLayout tl = {0};
    uint32_t len = (uint32_t)strlen(s);
    text_segs_plain(&tl, len, 0);
    text_layout(&tl, s, len, w, scale);
    float y0 = y, widest = 0;
    for (int i = 0; i < tl.nlines && i < max_lines; i++) {
        if (c) text_draw_line(&tl, i, s, x, y, scale, c, UINT32_MAX);
        if (tl.lines[i].width > widest) widest = tl.lines[i].width;
        y += tl.lines[i].height;
    }
    text_layout_free(&tl);
    if (out_w) *out_w = widest;
    return y - y0;
}

// The Hermes caduceus at 9 px, as coverage per pixel (`python tools/build_meta.py --mark-c`, the same mark as
// on the HOME banner). Drawn pixel by pixel on whole pixels so it stays crisp.
static const uint8_t MARK9[9 * 9] = {
      0,   0,   0,   0, 255,   0,   0,   0,   0,
    255, 255, 150, 255, 255, 255, 150, 255, 255,
    150, 255, 255, 255, 255, 255, 255, 255, 150,
      0,   0,   0, 255, 255, 255,   0,   0,   0,
      0,   0, 255,   0, 255,   0, 255,   0,   0,
      0,   0,   0, 255, 255, 255,   0,   0,   0,
      0,   0, 255,   0, 255,   0, 255,   0,   0,
      0,   0,   0, 255, 255, 255,   0,   0,   0,
      0,   0,   0,   0, 255,   0,   0,   0,   0,
};

void hermes_mark(float x, float y, u32 color)
{
    x = floorf(x + 0.5f);
    y = floorf(y + 0.5f);
    u32 rgb = color & 0x00ffffff, a = color >> 24;
    for (int j = 0; j < 9; j++)
        for (int i = 0; i < 9; i++) {
            u32 c = MARK9[j * 9 + i];
            if (c) rect(x + i, y + j, 1, 1, rgb | ((a * c / 255) << 24));
        }
}

// --- chrome -----------------------------------------------------------------

static void draw_signal(float x, float y, u32 color)
{
    rect(x, y + 5, 2, 2, color);
    rect(x + 3.5f, y + 3, 2, 4, color);
    rect(x + 7, y, 2, 7, color);
}

// A loudspeaker with two sound waves, 9 x 7 px: Spoken replies are on.
static void draw_speaker(float x, float y, u32 color)
{
    rect(x, y + 2, 2, 3, color);
    rect(x + 2, y + 1, 1, 5, color);
    rect(x + 3, y, 1, 7, color);
    rect(x + 5, y + 2, 1, 3, color);
    rect(x + 7, y + 1, 1, 1, color);
    rect(x + 8, y + 2, 1, 3, color);
    rect(x + 7, y + 5, 1, 1, color);
}

void draw_header(const char *title)
{
    rect(0, 0, TOP_W, TOP_HEADER_H, COL_BLUE);
    char who[40];
    if (!agent_picture(app_agent(), 5.5f, 2, 12)) agent_mark(app_agent(), 7, 3.5f, COL_ON);
    agent_caps(app_agent(), who, sizeof who);
    float x = 19;
    x += spaced_fit(who, x, 4.5f, S_EYE, COL_ON, 1.6f, 110) + 5;  // a long name leaves room for the model chip
    rect(x, 4.5f, 1, 7, COL_ON_3);
    x += 6;
    // right side: model chip + link state
    float right = TOP_W - 7;
    const char *link = app.net == NET_ONLINE       ? (app.route == ROUTE_AWAY ? "AWAY" : "LAN")
                       : app.net == NET_CONNECTING ? "..."
                       : app.net == NET_WAITING    ? "RETRY"
                                                   : "OFFLINE";
    float lw = spaced_width(link, S_EYE, 1.0f);
    spaced(link, right - lw, 4.5f, S_EYE, app.net == NET_ONLINE ? COL_ON : COL_ON_2, 1.0f);
    right -= lw + 3;
    if (app.net == NET_ONLINE) {
        draw_signal(right - 9, 4.5f, COL_ON);
        right -= 14;
    }
    if (app.screen == SCR_CHAT && app.settings.speak_replies) {  // Spoken replies on (More > Spoken replies)
        float vw = spaced_width("VOICE", S_EYE, 1.0f);
        spaced("VOICE", right - vw, 4.5f, S_EYE, COL_ON, 1.0f);
        draw_speaker(right - vw - 12, 4.5f, COL_ON);
        right -= vw + 18;
    }
    if (app.screen == SCR_CHAT && app.chat->model[0]) {
        float mw = text_width(app.chat->model, strlen(app.chat->model), S_TOOL);
        if (mw > 110) mw = 110;
        frame_rect(right - mw - 8, 3, mw + 8, 10, COL_ON_3);
        text_draw_fit(app.chat->model, right - mw - 4, 3.5f, S_TOOL, COL_ON, mw);
        right -= mw + 14;
    }
    text_draw_fit(title, x, 3, S_TITLE, COL_ON_2, right - x);
}

float footer_begin(void)
{
    rect(0, SCR_H - TOP_FOOTER_H, TOP_W, TOP_FOOTER_H, COL_BLUE);
    return SCR_H - TOP_FOOTER_H + 3;
}

static const char *const TAB_NAMES[TAB_COUNT] = {"Chat", "Bots", "Sessions", "Skills", "Jobs", "More"};

// Another agent replied or waits for you: a dot on the Bots tab (yellow when it waits).
static int bots_badge(void)
{
    int b = 0;
    for (int i = 0; i < app.nagents; i++) {
        if (i == app.cur) continue;
        if (app.agents[i].chat.approval || app.agents[i].chat.clarify) return 2;
        if (app.agents[i].unread) b = 1;
    }
    return b;
}

void draw_tabs(void)
{
    const float W = (float)BOT_W / TAB_COUNT;
    rect(0, 0, BOT_W, TABS_H, COL_PAPER);
    for (int i = 0; i < TAB_COUNT; i++) {
        float x = floorf(i * W), w = floorf((i + 1) * W) - x;
        bool on = i == (int)app.tab;
        if (on) rect(x, 0, w, TABS_H, COL_BLUE);
        else if (i < TAB_COUNT - 1 && i + 1 != (int)app.tab) rect(x + w - 1, 0, 1, TABS_H, COL_RULE);
        float tw = text_width(TAB_NAMES[i], strlen(TAB_NAMES[i]), S_SMALL);
        if (tw > w - 6) tw = w - 6;
        text_draw_fit(TAB_NAMES[i], x + (w - tw) / 2, 7, S_SMALL, on ? COL_ON : COL_INK_2, w - 6);
        int badge = i == TAB_BOTS ? bots_badge() : 0;
        if (badge) {
            rect(x + w - 9, 4, 5, 5, badge == 2 ? COL_ACCENT : on ? COL_ON : COL_BLUE);
            if (badge == 2) frame_rect(x + w - 9, 4, 5, 5, COL_INK);
        }
    }
    rect(0, TABS_H - 1, BOT_W, 1, COL_INK);
}

// The caduceus for the main agent; a bot's initial in a 9 px cell.
void agent_mark(const Agent *a, float x, float y, u32 color)
{
    if (a == app.agents) {
        hermes_mark(x, y, color);
        return;
    }
    char ch[2] = {agent_title(a)[0], 0};
    if (ch[0] >= 'a' && ch[0] <= 'z') ch[0] -= 32;
    if ((unsigned char)ch[0] >= 0x80) ch[0] = '@';  // the font draws ASCII at this size; emoji names fall back
    float w = text_width(ch, 1, S_EYE * 1.25f);
    text_draw(ch, x + (9 - w) / 2, y - 1.5f, S_EYE * 1.25f, color);
}

// A bot's Bot Mode picture filling a size×size box. False when it has none, while it loads, or when it can't
// be shown: callers draw agent_mark instead.
bool agent_picture(const Agent *a, float x, float y, float size)
{
    if (a == app.agents || !a->has_avatar) return false;
    BlobState st;
    const Picture *p = image_get(&a->avatar, &st);
    if (!p) return false;
    image_draw_cover(p, x, y, size, size);
    return true;
}

// The agent's title in capitals for eyebrows ("HERMES", "RESEARCH BOT").
void agent_caps(const Agent *a, char *out, size_t cap)
{
    utf8_copy(out, cap, agent_title(a));
    for (char *p = out; *p; p++)
        if (*p >= 'a' && *p <= 'z') *p -= 32;
}

void draw_toast(void)
{
    if (app.toast_until <= osGetTime()) return;
    float w = text_width(app.toast, strlen(app.toast), S_SMALL) + 16;
    float x = (BOT_W - w) / 2, y = SCR_H - 8 - 15 - 6 - 20;
    rect(x, y, w, 18, COL_INK);
    text_draw(app.toast, x + 8, y + 3, S_SMALL, COL_ON);
}
