#include "camera.h"

#include <citro2d.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <turbojpeg.h>

#include "app.h"
#include "blob.h"
#include "log.h"
#include "sfx.h"
#include "ui.h"
#include "upload.h"

#define W 640  // VGA: the 3DS cameras' largest size, ~0.3 MP
#define H 480
#define FRAME_BYTES (W * H * 2)  // YUYV 4:2:2, the camera's native output
#define PW 320  // the preview: the picture halved, in a 512x256 RGB565 texture
#define PH 240
#define THUMB_W 120
#define THUMB_H 90
#define JPEG_Q 88
#define STALL_MS 1500  // no frame for this long: restart the capture
#define CAST_MAX 40    // the most the colour correction shifts U or V (of 255)

typedef enum { C_PREVIEW, C_REVIEW, C_ENCODE } CState;

static struct {
    bool open, cam_ok, tex_ok, have_frame, prep_drawn;
    CState st;
    u32 select;      // SELECT_OUT1 or SELECT_IN1
    uint8_t *buf[2]; // receiving into buf[rx]; buf[!rx] holds the last whole frame
    int du, dv;      // colour correction applied to U and V (most of the picture's average cast)
    int mu, mv;      // the last preview frame's average cast, before correction (logged)
    int rx;
    Handle ev;
    Handle err_ev;   // the camera's buffer-error event (a frame was lost): restart the capture
    u32 xfer;
    u64 last_frame;
    int frames;
    C3D_Tex tex;
    Tex3DS_SubTexture sub;
    char caption[1024];
} cm;

static inline uint32_t morton8(int x, int y)
{
    return (uint32_t)((x & 1) | ((y & 1) << 1) | ((x & 2) << 1) | ((y & 2) << 2) | ((x & 4) << 2) | ((y & 4) << 3));
}

static const Rect SHUTTER = {110, 60, 100, 100};
static const Rect SWITCH = {10, 196, 145, 34}, BACK = {165, 196, 145, 34};
static const Rect SEND = {10, 148, 300, 40}, CAPTION = {10, 196, 145, 34}, RETAKE = {165, 196, 145, 34};

static void arm(void)
{
    if (cm.ev) return;
    CAMU_SetReceiving(&cm.ev, cm.buf[cm.rx], PORT_CAM1, FRAME_BYTES, (s16)cm.xfer);
}

static void stop_capture(void)
{
    if (!cm.cam_ok) return;
    CAMU_StopCapture(PORT_CAM1);
    if (cm.ev) {
        svcCloseHandle(cm.ev);
        cm.ev = 0;
    }
}

static void start_capture(void)
{
    if (!cm.cam_ok) return;
    stop_capture();
    CAMU_Activate(cm.select);
    CAMU_ClearBuffer(PORT_CAM1);
    CAMU_StartCapture(PORT_CAM1);
    if (!cm.err_ev) CAMU_GetBufferErrorInterruptEvent(&cm.err_ev, PORT_CAM1);
    cm.last_frame = osGetTime();
    arm();
}

static bool cam_setup(void)
{
    Result rc = camInit();
    if (R_FAILED(rc)) {
        log_printf("camera: camInit failed %08lX", (unsigned long)rc);
        return false;
    }
    u32 both = SELECT_IN1_OUT1;
    if (R_FAILED(rc = CAMU_SetSize(both, SIZE_VGA, CONTEXT_A)) ||
        R_FAILED(rc = CAMU_SetOutputFormat(both, OUTPUT_YUV_422, CONTEXT_A)) ||
        // 15 fps in good light, 10 in a dim room: a little longer exposure, still short enough to hold by hand.
        R_FAILED(rc = CAMU_SetFrameRate(both, FRAME_RATE_15_TO_10)) || R_FAILED(rc = CAMU_SetNoiseFilter(both, true)) ||
        R_FAILED(rc = CAMU_SetAutoExposure(both, true)) ||
        R_FAILED(rc = CAMU_SetWhiteBalance(both, WHITE_BALANCE_AUTO)) ||
        R_FAILED(rc = CAMU_SetAutoWhiteBalance(both, true)) ||
        R_FAILED(rc = CAMU_SetTrimming(PORT_CAM1, false)) || R_FAILED(rc = CAMU_GetMaxBytes(&cm.xfer, W, H)) ||
        R_FAILED(rc = CAMU_SetTransferBytes(PORT_CAM1, cm.xfer, W, H))) {
        log_printf("camera: setup failed %08lX", (unsigned long)rc);
        camExit();
        return false;
    }
    return true;
}

void camera_open(void)
{
    if (cm.open) return;
    if (app.net == NET_ONLINE && !upload_max()) {
        app_toast("Update the Hermes 3DS plugin on the PC to send photos");
        return;
    }
    memset(&cm, 0, sizeof cm);
    cm.buf[0] = malloc(FRAME_BYTES);
    cm.buf[1] = malloc(FRAME_BYTES);
    cm.tex_ok = C3D_TexInit(&cm.tex, 512, 256, GPU_RGB565);
    if (!cm.buf[0] || !cm.buf[1] || !cm.tex_ok) {
        app_toast("Not enough memory for the camera");
        camera_close();
        return;
    }
    C3D_TexSetFilter(&cm.tex, GPU_LINEAR, GPU_LINEAR);
    C3D_TexSetWrap(&cm.tex, GPU_CLAMP_TO_EDGE, GPU_CLAMP_TO_EDGE);
    memset(cm.tex.data, 0, cm.tex.size);
    cm.sub = (Tex3DS_SubTexture){.width = PW, .height = PH, .left = 0, .top = 1.0f, .right = PW / 512.0f,
                                 .bottom = 1.0f - PH / 256.0f};
    cm.open = true;
    cm.select = SELECT_OUT1;
    cm.st = C_PREVIEW;
    u64 t0 = osGetTime();
    cm.cam_ok = cam_setup();
    if (!cm.cam_ok) {
        app_toast("The camera didn't start");
        camera_close();
        return;
    }
    start_capture();
    log_printf("camera: open (%llu ms)", (unsigned long long)(osGetTime() - t0));
    app.dirty = true;
}

bool camera_is_open(void) { return cm.open; }

void camera_close(void)
{
    stop_capture();
    if (cm.err_ev) svcCloseHandle(cm.err_ev);
    if (cm.cam_ok) {
        CAMU_Activate(SELECT_NONE);
        camExit();
    }
    if (cm.tex_ok) C3D_TexDelete(&cm.tex);
    free(cm.buf[0]);
    free(cm.buf[1]);
    memset(&cm, 0, sizeof cm);
    app.dirty = true;
}

static inline int clamp255(int v) { return v < 0 ? 0 : v > 255 ? 255 : v; }

// The correction for a pixel of brightness y: full from y 48 up, fading to none at black, so dark areas stay
// neutral instead of taking on the opposite tint.
static inline int shade(int d, int y) { return y >= 48 ? d : d * y / 48; }

// One pixel, full-range BT.601 (what the camera and JPEG use); u, v centred on 0.
static inline void yuv_rgb(int y, int u, int v, int *r, int *g, int *b)
{
    *r = clamp255(y + ((359 * v) >> 8));
    *g = clamp255(y - ((88 * u + 183 * v) >> 8));
    *b = clamp255(y + ((454 * u) >> 8));
}

// Halve a YUYV frame into the preview texture (2x2 average), tiled for the GPU, with the colour correction.
// Also measures the frame's average colour cast and eases the correction towards cancelling part of it:
// the 3DS cameras' white balance drifts green under screens and indoor light.
static void to_texture(const uint8_t *src, bool measure)
{
    u16 *dst = cm.tex.data;
    int32_t su = 0, sv = 0, n = 0;
    for (int y = 0; y < PH; y++) {
        const uint8_t *r0 = src + (size_t)(y * 2) * W * 2, *r1 = r0 + W * 2;
        uint32_t row = (uint32_t)((y >> 3) * (512 >> 3)) * 64;
        for (int x = 0; x < PW; x++) {
            const uint8_t *a = r0 + x * 4, *b = r1 + x * 4;  // Y0 U Y1 V, two rows
            int yy = (a[0] + a[2] + b[0] + b[2] + 2) >> 2;
            int u = ((a[1] + b[1] + 1) >> 1) - 128, v = ((a[3] + b[3] + 1) >> 1) - 128;
            if (yy > 40 && yy < 235) {  // dark pixels' colour is mostly noise, bright ones are clipped
                su += u;
                sv += v;
                n++;
            }
            int rr, gg, bb;
            yuv_rgb(yy, u + shade(cm.du, yy), v + shade(cm.dv, yy), &rr, &gg, &bb);
            dst[row + (uint32_t)(x >> 3) * 64 + morton8(x & 7, y & 7)] = (u16)((rr >> 3) << 11 | (gg >> 2) << 5 | bb >> 3);
        }
    }
    C3D_TexFlush(&cm.tex);
    if (!measure || n < PW * PH / 50) return;  // too little to judge (a nearly black frame): keep what we have
    int mu = su / n, mv = sv / n;
    cm.mu = mu;
    cm.mv = mv;
    // Cancel most of it: the 3DS cameras tint the whole picture green (U and V both ~-20..-35 under screens and
    // room light), which hides what Hermes is meant to see. A scene that really is one colour keeps a little.
    int tu = -mu * 17 / 20, tv = -mv * 17 / 20;
    int full = PW * PH / 4;  // with less than a quarter of the frame in mid-tones, trust the measure less
    if (n < full) {
        tu = tu * n / full;
        tv = tv * n / full;
    }
    tu = tu < -CAST_MAX ? -CAST_MAX : tu > CAST_MAX ? CAST_MAX : tu;
    tv = tv < -CAST_MAX ? -CAST_MAX : tv > CAST_MAX ? CAST_MAX : tv;
    cm.du += (tu - cm.du) / 4;  // eased over a few frames so the preview doesn't flicker
    cm.dv += (tv - cm.dv) / 4;
}

static void take(void)
{
    if (!cm.have_frame) return;
    stop_capture();
    CAMU_PlayShutterSound(SHUTTER_SOUND_TYPE_NORMAL);
    to_texture(cm.buf[!cm.rx], false);  // exactly the frame that is kept, as it will be sent
    cm.st = C_REVIEW;
    app.dirty = true;
}

static void retake(void)
{
    cm.st = C_PREVIEW;
    cm.have_frame = false;
    start_capture();
    app.dirty = true;
}

static void switch_camera(void)
{
    cm.select = cm.select == SELECT_OUT1 ? SELECT_IN1 : SELECT_OUT1;
    cm.have_frame = false;
    start_capture();
    app.dirty = true;
}

static void edit_caption(void)
{
    char buf[sizeof cm.caption];
    if (app_keyboard("A caption or a question about the photo", cm.caption, buf, sizeof buf, 300, true, "OK"))
        snprintf(cm.caption, sizeof cm.caption, "%s", buf);
    else if (!buf[0])
        cm.caption[0] = 0;  // cleared and confirmed (an empty caption is fine)
}

// The photo for the PC (JPEG) and the chat card's thumbnail, both cached under their ids, then the send.
static void encode_and_send(void)
{
    u64 t0 = osGetTime();
    const uint8_t *frame = cm.buf[!cm.rx];
    // Straight from the camera's YUV to JPEG (no RGB round trip): planes Y (W x H), U and V (W/2 x H).
    uint8_t *yp = malloc((size_t)W * H * 2), *up = yp ? yp + W * H : NULL, *vp = up ? up + W * H / 2 : NULL;
    uint8_t *thumb = malloc(THUMB_W * THUMB_H * 3);
    tjhandle tj = tjInitCompress();
    unsigned char *jpg = NULL, *tjpg = NULL;
    unsigned long jlen = 0, tlen = 0;
    bool ok = yp && thumb && tj;
    if (ok) {
        for (size_t i = 0; i < (size_t)W * H / 2; i++) {
            const uint8_t *q = frame + i * 4;
            yp[i * 2] = q[0];
            yp[i * 2 + 1] = q[2];
            int y2 = (q[0] + q[2]) >> 1;
            up[i] = (uint8_t)clamp255(q[1] + shade(cm.du, y2));
            vp[i] = (uint8_t)clamp255(q[3] + shade(cm.dv, y2));
        }
        // Thumbnail: each pixel the mean of its area, then to RGB.
        for (int y = 0; y < THUMB_H; y++)
            for (int x = 0; x < THUMB_W; x++) {
                int x0 = x * W / THUMB_W, x1 = (x + 1) * W / THUMB_W, y0 = y * H / THUMB_H, y1 = (y + 1) * H / THUMB_H;
                u32 sy = 0, su = 0, sv = 0, n = 0;
                for (int yy = y0; yy < y1; yy++)
                    for (int xx = x0; xx < x1; xx++, n++) {
                        sy += yp[(size_t)yy * W + xx];
                        su += up[(size_t)yy * (W / 2) + xx / 2];
                        sv += vp[(size_t)yy * (W / 2) + xx / 2];
                    }
                int r, g, b;
                yuv_rgb((int)(sy / n), (int)(su / n) - 128, (int)(sv / n) - 128, &r, &g, &b);
                uint8_t *t = thumb + (y * THUMB_W + x) * 3;
                t[0] = (uint8_t)r;
                t[1] = (uint8_t)g;
                t[2] = (uint8_t)b;
            }
        const unsigned char *planes[3] = {yp, up, vp};
        ok = tjCompressFromYUVPlanes(tj, planes, W, NULL, H, TJSAMP_422, &jpg, &jlen, JPEG_Q, TJFLAG_FASTDCT) == 0 &&
             tjCompress2(tj, thumb, THUMB_W, 0, THUMB_H, TJPF_RGB, &tjpg, &tlen, TJSAMP_420, 75, TJFLAG_FASTDCT) == 0;
        if (!ok) log_printf("camera: jpeg: %s", tjGetErrorStr2(tj));
    }
    free(yp);
    free(thumb);
    if (tj) tjDestroy(tj);
    char id[UPLOAD_ID_LEN], tid[UPLOAD_ID_LEN + 2];
    upload_new_id(id);
    snprintf(tid, sizeof tid, "%st", id);  // the thumbnail stays on the 3DS
    uint8_t *data = ok ? malloc(jlen) : NULL;
    if (data) {
        memcpy(data, jpg, jlen);
        blob_cache_put(id, data, jlen);
        blob_cache_put(tid, tjpg, tlen);
    }
    tjFree(jpg);
    tjFree(tjpg);
    if (!data) {
        app_toast("Couldn't prepare the photo");
        sfx_play(SFX_ERROR);
        cm.st = C_REVIEW;
        return;
    }
    log_printf("camera: photo %lu bytes, thumb %lu, in %llu ms, cast u%+d v%+d, corrected u%+d v%+d",
               jlen, tlen, (unsigned long long)(osGetTime() - t0), cm.mu, cm.mv, cm.du, cm.dv);
    json_t *attach = json_pack("{s:s, s:s, s:I, s:{s:s, s:I, s:s}, s:i, s:i, s:{s:s, s:I, s:s}, s:i, s:i,"
                               " s:{s:s, s:I, s:s}}",
                               "kind", "image", "name", "Photo.jpg", "size", (json_int_t)jlen, "view", "id", id,
                               "size", (json_int_t)jlen, "mime", "image/jpeg", "w", W, "h", H, "thumb", "id", tid,
                               "size", (json_int_t)tlen, "mime", "image/jpeg", "tw", THUMB_W, "th", THUMB_H, "file",
                               "id", id, "size", (json_int_t)jlen, "mime", "image/jpeg");
    json_t *fields = json_pack("{s:[{s:s}]}", "attach", "blob", id);
    char caption[sizeof cm.caption];
    snprintf(caption, sizeof caption, "%s", cm.caption);
    camera_close();
    app_send_media(id, data, jlen, "image/jpeg", "photo.jpg", attach, fields, caption);
}

void camera_input(u32 down, u32 held, const touchPosition *t)
{
    if (!cm.open) return;
    int x = t ? t->px : -1, y = t ? t->py : -1;
    if (cm.st == C_PREVIEW) {
        if ((down & KEY_A) || (t && rect_hit(&SHUTTER, x, y))) take();
        else if ((down & KEY_X) || (t && rect_hit(&SWITCH, x, y))) switch_camera();
        else if ((down & KEY_B) || (t && rect_hit(&BACK, x, y))) camera_close();
    } else if (cm.st == C_REVIEW) {
        if ((down & KEY_A) || (t && rect_hit(&SEND, x, y))) {
            cm.st = C_ENCODE;  // drawn as "Preparing..." first, encoded on the next tick
            app.dirty = true;
        } else if ((down & KEY_Y) || (t && rect_hit(&CAPTION, x, y))) {
            edit_caption();
        } else if ((down & KEY_B) || (t && rect_hit(&RETAKE, x, y))) {
            retake();
        }
    }
}

void camera_tick(void)
{
    if (!cm.open) return;
    if (cm.st == C_ENCODE) {
        if (!cm.prep_drawn) {  // let "Preparing the photo" reach the screen before the CPU is busy
            cm.prep_drawn = true;
            return;
        }
        cm.prep_drawn = false;
        encode_and_send();
        return;
    }
    if (cm.st != C_PREVIEW || !cm.cam_ok) return;
    u64 now = osGetTime();
    if (cm.err_ev && svcWaitSynchronization(cm.err_ev, 0) == 0) {
        start_capture();  // the receive wasn't re-armed in time: the frame in flight is torn
        return;
    }
    if (cm.ev && svcWaitSynchronization(cm.ev, 0) == 0) {
        svcCloseHandle(cm.ev);
        cm.ev = 0;
        cm.rx = !cm.rx;  // that buffer is now the last whole frame
        cm.have_frame = true;
        cm.last_frame = now;
        cm.frames++;
        to_texture(cm.buf[!cm.rx], true);
        arm();
        app.dirty = true;
    } else if (now - cm.last_frame > STALL_MS) {
        log_printf("camera: no frame for %d ms, restarting the capture", STALL_MS);
        start_capture();
    }
}

bool camera_animating(void) { return cm.open && cm.st != C_REVIEW; }

// --- drawing --------------------------------------------------------------------------

void camera_draw_top(void)
{
    rect(0, 0, TOP_W, SCR_H, COL_INK);
    float top = TOP_HEADER_H, h = SCR_H - TOP_HEADER_H - TOP_FOOTER_H, w = h * PW / PH;
    float x = (TOP_W - w) / 2;
    if (cm.have_frame || cm.st != C_PREVIEW) {
        C2D_Image img = {&cm.tex, &cm.sub};
        C2D_DrawImageAt(img, x, top, Z, NULL, w / PW, h / PH);
    } else {
        braille(TOP_W / 2 - 5, top + h / 2 - 6, 12, COL_ON);
    }
    const char *which = cm.select == SELECT_OUT1 ? "OUTER" : "INNER";
    spaced(which, 8, top + 6, S_EYE, COL_ON_3, 1.6f);
    spaced("CAMERA", 8, top + 16, S_EYE, COL_ON_3, 1.6f);
    spaced("640x480", TOP_W - 8 - spaced_width("640x480", S_EYE, 1.2f), top + 6, S_EYE, COL_ON_3, 1.2f);
    if (cm.st == C_ENCODE) {
        rect(x, top + h / 2 - 14, w, 28, COL_BLUE);
        braille(x + 10, top + h / 2 - 6, 11, COL_ON);
        text_draw("Preparing the photo...", x + 26, top + h / 2 - 7, S_BODY, COL_ON);
    }
    draw_header(cm.st == C_PREVIEW ? "Photo" : "Send this photo?");
    float fy = footer_begin(), fx = 7;
    if (cm.st == C_PREVIEW) {
        fx += hint(GLYPH_A, "Take", fx, fy, COL_ON);
        fx += hint(GLYPH_X, cm.select == SELECT_OUT1 ? "Inner camera" : "Outer camera", fx, fy, COL_ON);
        hint(GLYPH_B, "Back", fx, fy, COL_ON);
    } else if (cm.st == C_REVIEW) {
        fx += hint(GLYPH_A, "Send", fx, fy, COL_ON);
        fx += hint(GLYPH_Y, cm.caption[0] ? "Edit caption" : "Caption", fx, fy, COL_ON);
        hint(GLYPH_B, "Retake", fx, fy, COL_ON);
    }
    const char *r = "Hermes looks at it on the PC";
    float rw = text_width(r, strlen(r), S_HINT);
    text_draw(r, TOP_W - 7 - rw, fy, S_HINT, COL_ON_3);
}

void camera_draw_bottom(void)
{
    rect(0, 0, BOT_W, SCR_H, COL_PAPER_2);
    spaced("PHOTO", 10, 9, S_EYE, COL_INK_3, 2.0f);
    if (cm.st == C_PREVIEW) {
        const char *s = "Aim, then press " GLYPH_A " or tap the button";
        text_draw(s, (BOT_W - text_width(s, strlen(s), S_SMALL)) / 2, 34, S_SMALL, COL_INK_2);
        float cx = SHUTTER.x + SHUTTER.w / 2, cy = SHUTTER.y + SHUTTER.h / 2;
        disc(cx + 3, cy + 3, 44, COL_BLUE);
        disc(cx, cy, 44, COL_INK);
        disc(cx, cy, 38, COL_PAPER);
        disc(cx, cy, 33, cm.have_frame ? COL_BLUE : COL_RULE);
        button(&SWITCH, cm.select == SELECT_OUT1 ? "Inner camera" : "Outer camera", GLYPH_X, COL_PAPER, COL_INK);
        button(&BACK, "Back", GLYPH_B, COL_PAPER, COL_INK);
    } else {
        text_draw("Caption", 10, 30, S_SMALL, COL_INK_3);
        rect(10, 44, BOT_W - 20, 96, COL_PAPER);
        frame_rect(10, 44, BOT_W - 20, 96, COL_RULE);
        if (cm.caption[0]) text_block(cm.caption, 16, 50, BOT_W - 32, S_SMALL, &BODY_COLORS, 5, NULL);
        else text_draw("None. Hermes gets the photo on its own.", 16, 50, S_SMALL, COL_TX_3);
        bool busy = cm.st == C_ENCODE;
        button(&SEND, busy ? "Preparing..." : "Send to Hermes", busy ? "" : GLYPH_A, COL_BLUE, COL_ON);
        button(&CAPTION, cm.caption[0] ? "Edit caption" : "Add a caption", GLYPH_Y, COL_PAPER, COL_INK);
        button(&RETAKE, "Retake", GLYPH_B, COL_PAPER, COL_INK);
    }
}
