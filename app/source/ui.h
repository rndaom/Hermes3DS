// Drawing primitives and chrome shared by every screen (chat and pairing in draw.c, the other tabs in
// tabs.c). Everything is drawn at z 0.5 in painter's order.
#pragma once

#include <citro2d.h>
#include <time.h>

#include "app.h"
#include "text.h"

#define Z 0.5f
#define TOP_W 400
#define BOT_W 320
#define SCR_H 240
#define PAD_X 11

// Face-button glyphs live in the system font's private-use area.
#define GLYPH_A "\xEE\x80\x80"
#define GLYPH_B "\xEE\x80\x81"
#define GLYPH_X "\xEE\x80\x82"
#define GLYPH_Y "\xEE\x80\x83"
#define GLYPH_L "\xEE\x80\x84"
#define GLYPH_R "\xEE\x80\x85"
#define GLYPH_DPAD "\xEE\x80\x86"

// Text scales for the design's pixel sizes (set by ui_init from the font's line height).
extern float S_BODY, S_SMALL, S_EYE, S_TOOL, S_TITLE, S_BIG, S_HINT;

extern const TextColors BODY_COLORS, USER_COLORS, DIM_COLORS;

void ui_init(void);

void rect(float x, float y, float w, float h, u32 c);
void disc(float cx, float cy, float r, u32 c);  // filled circle
void frame_rect(float x, float y, float w, float h, u32 c);
void hermes_mark(float x, float y, u32 color);  // the Hermes caduceus, 9x9 px
void agent_mark(const Agent *a, float x, float y, u32 color);  // the caduceus, or a bot's initial
bool agent_picture(const Agent *a, float x, float y, float size);  // a bot's picture; false while there is none
void agent_caps(const Agent *a, char *out, size_t cap);        // its title in capitals
void braille(float x, float y, float cell, u32 color);  // spinner cell, `cell` px tall
void check_mark(float x, float y, float s, u32 color);
float spaced(const char *s, float x, float y, float scale, u32 color, float spacing);  // letter-spaced caps
float spaced_width(const char *s, float scale, float spacing);
float spaced_fit(const char *s, float x, float y, float scale, u32 color, float spacing, float max_w);  // "..." past max_w
float hint(const char *glyph, const char *label, float x, float y, u32 color);  // returns the advance
void button(const Rect *r, const char *label, const char *glyph, u32 bg, u32 fg);

// Times: `t` is a Unix time; the 3DS clock holds local time, so app.tz_off converts.
void clock_text(time_t local, char *out, size_t cap);  // "9:41 PM" from a local (3DS clock) time
void when_text(double unix_t, char *out, size_t cap);   // "now", "9:41 PM", "Yesterday", "Oct 18"

// Wrapped plain text, top-left at (x, y), at most max_lines lines; returns the height used.
// With c == NULL it only measures; out_w (may be NULL) gets the widest drawn line.
float text_block(const char *s, float x, float y, float w, float scale, const TextColors *c, int max_lines,
                 float *out_w);

void size_text(double bytes, char *out, size_t cap);           // "900 B", "48 KB", "1.2 MB"
void ext_label(const char *name, char *out, size_t cap);        // "PNG" from "plot.png" (at most 4 chars)
void file_tile(float x, float y, float s, const char *name);    // square tile with the file's extension
void play_icon(float x, float y, float s, bool pause, u32 color);  // triangle or pause bars in an s square
void audio_tile(float x, float y, float s, bool current, bool playing);  // file_tile for audio
void progress_bar(float x, float y, float w, float h, float played, float loaded);  // fractions 0..1

void draw_header(const char *title);  // blue top band with the link state and model chip
float footer_begin(void);             // draws the top screen's blue hint band, returns the text y
void draw_tabs(void);                 // bottom screen tab bar
void draw_toast(void);
