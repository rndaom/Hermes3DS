#include "voice.h"

#include <malloc.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adpcm.h"
#include "app.h"
#include "audio.h"
#include "blob.h"
#include "log.h"
#include "sfx.h"
#include "ui.h"
#include "upload.h"

#define MIC_BUF 0x30000  // the mic's shared ring: ~6 s of PCM16 at 16360 Hz
#define RATE 16360
#define BLOCK 1024
#define LIMIT_S 120      // the design's 2:00; sent automatically there
#define TAP_MS 300       // a release before this is a tap of Y, not a recording
#define MIN_MS 700       // shorter recordings are dropped ("hold to talk")
#define SILENT_MS 1500   // no samples for this long (lid closed, mic gone): stop
#define COLS 44          // the level meter: one column per COL_MS, newest on the right
#define ROWS 9
#define COL_MS 68
#define OUT_CAP ((size_t)(RATE * (LIMIT_S + 2) / adpcm_block_samples(BLOCK) + 2) * BLOCK)

typedef enum { V_IDLE, V_ARMED, V_RECORDING } VState;

static struct {
    VState st;
    bool touch;         // started by a finger on the Talk tile (else Y)
    bool mic_ok, mic_tried;
    u8 *micbuf;
    u32 datasize, pos;  // the ring's data size and our read position (bytes)
    u64 t0, last_rx;    // press time, last time samples came in
    uint8_t *out;
    AdpcmEnc enc;
    float level;        // now, 0..1
    float cols[COLS];   // the meter's history
    int ncols;
    u64 col_t;
    float col_peak;
    double sum_sq;      // for the column being collected
    u32 sum_n;
} vo;

static bool mic_ready(void)
{
    if (vo.mic_tried) return vo.mic_ok;
    vo.mic_tried = true;
    vo.micbuf = memalign(0x1000, MIC_BUF);
    if (!vo.micbuf) return false;
    Result rc = micInit(vo.micbuf, MIC_BUF);
    if (R_FAILED(rc)) {
        log_printf("voice: micInit failed %08lX", (unsigned long)rc);
        free(vo.micbuf);
        vo.micbuf = NULL;
        return false;
    }
    vo.datasize = micGetSampleDataSize();
    u8 gain = 0;
    MICU_GetGain(&gain);
    log_printf("voice: mic ready, %lu-byte ring, gain %u", (unsigned long)vo.datasize, gain);
    vo.mic_ok = true;
    return true;
}

static void stop_mic(void)
{
    if (vo.mic_ok) MICU_StopSampling();
}

static void reset(void)
{
    stop_mic();
    free(vo.out);
    vo.out = NULL;
    vo.st = V_IDLE;
    app.dirty = true;
}

bool voice_press(bool touch, bool quiet)
{
    if (vo.st != V_IDLE) return true;
    if (app.net == NET_ONLINE && !upload_max()) {
        if (!quiet) app_toast("Update the Hermes 3DS plugin on the PC for voice messages");
        return false;
    }
    if (!mic_ready()) {
        if (!quiet) app_toast("The microphone isn't available");
        return false;
    }
    if (!(vo.out = malloc(OUT_CAP))) {
        app_toast("Not enough memory to record");
        return false;
    }
    adpcm_enc_init(&vo.enc, vo.out, OUT_CAP, BLOCK);
    // The ring's last word is the mic's write offset; it keeps the previous recording's end (or junk) until new
    // samples land, so start both from 0.
    memset(vo.micbuf, 0, MIC_BUF);
    Result rc = MICU_StartSampling(MICU_ENCODING_PCM16_SIGNED, MICU_SAMPLE_RATE_16360, 0, vo.datasize, true);
    if (R_FAILED(rc)) {
        log_printf("voice: StartSampling failed %08lX", (unsigned long)rc);
        free(vo.out);
        vo.out = NULL;
        app_toast("The microphone didn't start");
        return false;
    }
    vo.pos = 0;
    vo.st = V_ARMED;
    vo.touch = touch;
    vo.t0 = vo.last_rx = osGetTime();
    vo.level = 0;
    vo.ncols = 0;
    vo.col_t = vo.t0;
    vo.col_peak = 0;
    vo.sum_sq = 0;
    vo.sum_n = 0;
    return true;
}

bool voice_active(void) { return vo.st != V_IDLE; }
bool voice_recording(void) { return vo.st == V_RECORDING; }

static uint32_t rec_ms(void) { return (uint32_t)(vo.enc.samples * 1000 / RATE); }

// Recording over: the bubble, the cached copy for playing it back, the upload and then the `send`.
static void finish(void)
{
    stop_mic();
    uint32_t ms = rec_ms();
    sfx_play(SFX_REC_STOP);
    if (ms < MIN_MS) {
        reset();
        app_toast("Hold " GLYPH_Y " while you talk, then let go");
        return;
    }
    size_t len = vo.enc.len;
    uint8_t *data = realloc(vo.out, len) ?: vo.out;
    vo.out = NULL;
    vo.st = V_IDLE;
    char id[UPLOAD_ID_LEN];
    upload_new_id(id);
    blob_cache_put(id, data, len);  // played back from here (blob_stream finds it) until the transcript comes
    log_printf("voice: %lu ms recorded, %lu bytes", (unsigned long)ms, (unsigned long)len);
    json_t *attach = json_pack("{s:s, s:s, s:I, s:{s:s, s:I, s:s}, s:i, s:i, s:I}", "kind", "audio", "name",
                               "Voice message", "size", (json_int_t)len, "audio", "id", id, "size", (json_int_t)len,
                               "mime", "audio/x-ima-adpcm", "rate", RATE, "block", BLOCK, "ms", (json_int_t)ms);
    json_t *fields = json_pack("{s:{s:s, s:i, s:i, s:I}}", "voice", "blob", id, "rate", RATE, "block", BLOCK, "ms",
                               (json_int_t)ms);
    app_send_media(id, data, len, "audio/x-ima-adpcm", "voice", attach, fields, "");
    app.dirty = true;
}

bool voice_release(void)
{
    if (vo.st == V_ARMED) {  // a tap
        reset();
        return true;
    }
    if (vo.st == V_RECORDING) finish();
    return false;
}

void voice_input(u32 down, u32 held, u32 up)
{
    if (vo.st != V_RECORDING) return;
    if (down & KEY_B) {
        reset();
        app_toast("Voice message cancelled");
        return;
    }
    bool released = vo.touch ? !(held & KEY_TOUCH) : (up & KEY_Y) || !(held & KEY_Y);
    if (released) {
        finish();
        return;
    }
    // With Y held, the finger is free: tap "Speak replies aloud".
    if (!vo.touch && (down & KEY_TOUCH)) {
        touchPosition t;
        hidTouchRead(&t);
        if (t.py >= 178 && t.py < 212) app_set_speak(!app.settings.speak_replies);
    }
}

// Close one meter column per COL_MS: its loudness in dBFS mapped onto 0..1 (-48 dB .. -6 dB).
static void meter(u64 now)
{
    while (now - vo.col_t >= COL_MS) {
        vo.col_t += COL_MS;
        double rms = vo.sum_n ? sqrt(vo.sum_sq / vo.sum_n) : 0;
        float db = rms > 1 ? 20.0f * log10f((float)rms / 32768.0f) : -96.0f;
        float lv = (db + 48.0f) / 42.0f;
        lv = lv < 0 ? 0 : lv > 1 ? 1 : lv;
        if (vo.ncols == COLS) memmove(vo.cols, vo.cols + 1, (COLS - 1) * sizeof vo.cols[0]), vo.ncols--;
        vo.cols[vo.ncols++] = lv;
        vo.level = lv;
        vo.sum_sq = 0;
        vo.sum_n = 0;
        app.dirty = true;
    }
}

void voice_tick(void)
{
    if (vo.st == V_IDLE) return;
    u64 now = osGetTime();
    // Drain the mic ring (it wraps at datasize) into the encoder.
    u32 end = micGetLastSampleOffset() & ~1u;
    if (end >= vo.datasize) end = 0;
    if (vo.pos >= vo.datasize) vo.pos = 0;
    int16_t tmp[512];
    while (vo.pos != end) {
        u32 stop = end > vo.pos ? end : vo.datasize;
        u32 n = (stop - vo.pos) / 2;
        if (n > 512) n = 512;
        if (!n) {
            vo.pos = 0;
            continue;
        }
        memcpy(tmp, vo.micbuf + vo.pos, n * 2);
        vo.pos += n * 2;
        if (vo.pos >= vo.datasize) vo.pos = 0;
        for (u32 i = 0; i < n; i++) vo.sum_sq += (double)tmp[i] * tmp[i];
        vo.sum_n += n;
        if (adpcm_encode(&vo.enc, tmp, n) < n) break;  // full: the limit below sends it
        vo.last_rx = now;
    }
    meter(now);
    if (vo.st == V_ARMED && now - vo.t0 >= TAP_MS) {
        vo.st = V_RECORDING;
        audio_pause();  // the speaker would talk into the mic
        log_printf("voice: recording (%s)", vo.touch ? "finger" : "Y");
        app.dirty = true;
    }
    if (vo.st == V_RECORDING && (rec_ms() >= LIMIT_S * 1000u || vo.enc.len + BLOCK > vo.enc.cap)) {
        app_toast("2:00 is the limit: sent");
        finish();
        return;
    }
    if (now - vo.last_rx > SILENT_MS) {
        log_printf("voice: the mic stopped delivering; %s", vo.st == V_RECORDING ? "sending what came" : "dropped");
        if (vo.st == V_RECORDING) finish();
        else reset();
    }
}

// --- drawing (design/mockups/screens/voice.mjs) ------------------------------------------

static void clock_str(uint32_t ms, char *out, size_t cap)
{
    unsigned s = ms / 1000;
    snprintf(out, cap, "%u:%02u", s / 60, s % 60);
}

// A microphone: capsule, cradle and stand, `s` px tall, centred on (cx, cy).
static void mic_glyph(float cx, float cy, float s, u32 c)
{
    float w = s * 0.42f, h = s * 0.62f, top = cy - s / 2;
    disc(cx, top + w / 2, w / 2, c);
    rect(cx - w / 2, top + w / 2, w, h - w, c);
    disc(cx, top + h - w / 2, w / 2, c);
    float cw = s * 0.7f, lw = s * 0.09f, cyb = top + h * 0.95f;
    rect(cx - cw / 2, top + h * 0.45f, lw, cyb - (top + h * 0.45f), c);
    rect(cx + cw / 2 - lw, top + h * 0.45f, lw, cyb - (top + h * 0.45f), c);
    rect(cx - cw / 2, cyb, cw, lw, c);
    rect(cx - lw / 2, cyb, lw, top + s - cyb, c);
    rect(cx - s * 0.22f, top + s - lw, s * 0.44f, lw, c);
}

void voice_draw_top(void)
{
    char buf[160], clk[16];
    rect(0, 0, TOP_W, SCR_H, COL_PAPER);
    draw_header("Talk");
    uint32_t ms = rec_ms();
    clock_str(ms, clk, sizeof clk);
    // Eyebrow with a live dot, the big word, the clock against the limit.
    if ((osGetTime() / 500) % 2 == 0) rect(12, 36, 5, 5, COL_BLUE);
    spaced("RECORDING", 21, 35, S_EYE, COL_BLUE, 2.4f);
    text_draw("Listening", 12, 46, S_BIG, COL_INK);
    float lw = text_width(" / 2:00", 7, S_SMALL);
    text_draw(" / 2:00", TOP_W - 12 - lw, 60, S_SMALL, COL_TX_3);
    float cw = text_width(clk, strlen(clk), S_BIG * 0.7f);
    text_draw(clk, TOP_W - 12 - lw - cw, 50, S_BIG * 0.7f, COL_BLUE);
    rect(12, 86, TOP_W - 24, 1, COL_RULE);

    // The level meter: columns of ▄ cells mirrored about the middle row, newest on the right; dots when quiet.
    const float x0 = 12, y0 = 98, pitch_x = (TOP_W - 24) / (float)COLS, pitch_y = 8;
    for (int c = 0; c < COLS; c++) {
        int k = c - (COLS - vo.ncols);  // history index (the meter fills from the right)
        float lv = k >= 0 ? vo.cols[k] : 0;
        int amp = (int)lroundf(lv * (ROWS / 2));
        float cx = x0 + c * pitch_x;
        for (int r = 0; r < ROWS; r++) {
            float y = y0 + r * pitch_y;
            if (abs(r - ROWS / 2) <= amp && (lv > 0.02f || r == ROWS / 2))
                rect(cx + 1, y + 3, pitch_x - 3, 4, COL_BLUE);
            else
                rect(cx + pitch_x / 2 - 1, y + 4, 1, 1, COL_TX_3);
        }
    }
    if (ms >= (LIMIT_S - 10) * 1000u) {
        snprintf(buf, sizeof buf, "Sends by itself at 2:00 (%u s left)", (unsigned)(LIMIT_S - ms / 1000));
        text_draw(buf, 12, 176, S_SMALL, COL_BAD);
    }
    snprintf(buf, sizeof buf, "%s turns this into text when you let go", app.server.name[0] ? app.server.name : "Your PC");
    text_draw_fit(buf, 12, 196, S_SMALL, COL_TX_2, TOP_W - 24);

    float y = footer_begin(), x = 7;
    x += hint(vo.touch ? "" : GLYPH_Y, vo.touch ? "Lift your finger to send" : "Release to send", x, y, COL_ON);
    x += hint(GLYPH_B, "Cancel", x, y, COL_ON);
    snprintf(buf, sizeof buf, "Speech-to-text on %s", app.server.name[0] ? app.server.name : "the PC");
    float w = text_width(buf, strlen(buf), S_HINT);
    text_draw(buf, TOP_W - 7 - w, y, S_HINT, COL_ON_3);
}

void voice_draw_bottom(void)
{
    char buf[112], clk[16];
    rect(0, 0, BOT_W, SCR_H, COL_PAPER_2);
    spaced("TALK", 10, 9, S_EYE, COL_INK_3, 2.0f);
    clock_str(rec_ms(), clk, sizeof clk);
    snprintf(buf, sizeof buf, "REC %s", clk);
    float w = text_width(buf, strlen(buf), S_TOOL);
    text_draw(buf, BOT_W - 10 - w, 7, S_TOOL, COL_BLUE);
    if ((osGetTime() / 500) % 2 == 0) disc(BOT_W - 16 - w, 12, 2.5f, COL_BLUE);

    // The panel: the big mic button, the instruction, a small level readout.
    const float px = 10, py = 22, pw = BOT_W - 20, ph = 150;
    rect(px + 3, py + 3, pw, ph, COL_BLUE);
    rect(px, py, pw, ph, COL_PAPER_2);
    frame_rect(px, py, pw, ph, COL_INK);
    float cx = BOT_W / 2.0f, cy = py + 56;
    disc(cx, cy, 44, COL_BLUE);
    disc(cx, cy, 43, COL_BLUE_SOFT);
    disc(cx, cy, 37 + 3 * vo.level, COL_BLUE);
    mic_glyph(cx, cy, 30, COL_ON);
    const char *how = vo.touch ? "Keep your finger on the screen" : "Hold " GLYPH_Y " or keep your finger here";
    w = text_width(how, strlen(how), S_SMALL);
    text_draw(how, cx - w / 2, py + 108, S_SMALL, COL_INK);
    float lx = cx - 30;
    text_draw("level", lx, py + 127, S_TOOL, COL_INK_3);
    for (int i = 0; i < 5; i++) {
        float on = vo.level * 5 > i ? 1 : 0;
        float bh = 3 + i * 2.0f;
        rect(lx + 30 + i * 6, py + 136 - bh, 4, bh, on ? COL_BLUE : COL_RULE);
    }

    // "Speak replies aloud" switch.
    const float ry = 180, rh = 32;
    rect(px, ry, pw, rh, COL_PAPER);
    frame_rect(px, ry, pw, rh, COL_RULE);
    text_draw("Speak replies aloud", px + 10, ry + 4, S_SMALL, COL_INK);
    text_draw("Replies play through the 3DS speakers", px + 10, ry + 18, S_TOOL, COL_INK_3);
    bool on = app.settings.speak_replies;
    float sx = px + pw - 36;
    rect(sx, ry + 9, 26, 14, on ? COL_BLUE : COL_RULE);
    rect(on ? sx + 14 : sx + 2, ry + 11, 10, 10, COL_PAPER);

    snprintf(buf, sizeof buf, "Speech-to-text runs on %s", app.server.name[0] ? app.server.name : "the PC");
    text_draw_fit(buf, 10, SCR_H - 16, S_TOOL, COL_INK_3, BOT_W - 20);
}

void voice_exit(void)
{
    reset();
    if (vo.mic_ok) micExit();
    free(vo.micbuf);
    memset(&vo, 0, sizeof vo);
}
