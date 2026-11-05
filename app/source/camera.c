#include "camera.h"

#include <citro2d.h>
#include <malloc.h>
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
#include "yuv.h"

#define W 640  // VGA: the 3DS cameras' largest size, ~0.3 MP
#define H 480
#define FRAME_BYTES (W * H * 2)  // YUYV 4:2:2, the camera's native output
#define NBUF 3  // one being received, the newest whole frame, the one on screen (or the photo)
#define PW 320  // the preview: the picture halved, in a 512x256 RGB565 texture
#define PH 240
#define THUMB_W 120
#define THUMB_H 90
#define JPEG_Q 88
#define STALL_NS (1500 * 1000000LL)        // no frame for this long: restart the capture
#define STALLS_REACTIVATE 2                // that many in a row: turn the camera on again
#define FROZEN_MS 1000                     // no new frame for this long: the shutter waits
#define LOG_EACH 5                         // buffer errors and stalls logged one by one up to this, then counted
#define REOPEN_WAIT_NS (500 * 1000000ULL)  // reopened while the last camera is still turning off
#define EXIT_WAIT_NS (2000 * 1000000ULL)
#define CAP_STACK (32 * 1024)
#define ENC_STACK (64 * 1024)

typedef enum { C_PREVIEW, C_REVIEW, C_ENCODE } CState;

// The capture thread does every camera call, so none of them (CAMU_Activate takes 100-170 ms on an Old 3DS)
// holds up the UI, and it re-arms the receive the moment a frame lands: the camera buffers only a few lines.
// It outlives a close by a moment while it turns the camera off, so its state is apart from the screen's;
// reap() frees it once the thread has ended.
static struct {
    Thread th;
    Handle wake;  // to the thread: a new request
    uint8_t *buf[NBUF];
    Handle err_ev;  // the thread's own: the camera's buffer-error event (it overflowed: the frame in flight is torn)
    u32 xfer;       // the thread's own: the transfer unit
    LightLock lock;  // guards the rest
    // Requests (from the UI, and from APT for the HOME Menu and sleep).
    u32 want;         // the camera wanted on
    bool run, quit;   // capturing (the preview) or paused (a photo in review); the thread to end
    bool away;        // in the HOME Menu or asleep: paused
    bool reactivate;  // back from there: turn the camera on again
    bool shutter;     // play the shutter sound
    // Results.
    bool failed;   // the camera couldn't be set up or turned on: the UI closes it
    YuvConv conv;  // the camera's YUV (CAMU_GetSuitableY2rStandardCoefficient), set before the first gen
    int ready;     // the newest whole frame the UI hasn't taken, or -1
    u32 ready_sel; // the camera it came from
    int held;      // the UI's frame (on screen, or the photo): never received into while held; -1 none
    u32 gen;       // bumped each time a camera is turned on: its exposure and white balance start over
    u32 frames, errors, stalls;
} cap;

static struct {
    bool open, tex_ok, settled, have_frame, enc_ok;
    CState st;
    u32 select;        // SELECT_OUT1 or SELECT_IN1
    YuvConv conv;
    YuvSettle settle;  // the warm-up after the camera starts
    YuvStats shown;    // the frame on screen (logged with the photo)
    u32 seen_gen;
    u64 warm_t0, last_frame;
    C3D_Tex tex;
    Tex3DS_SubTexture sub;
    char caption[1024];
    aptHookCookie apt;
    Thread enc;  // the JPEG encoder, below the UI's priority so "Preparing the photo" keeps moving
    unsigned char *jpg, *tjpg;
    unsigned long jlen, tlen;
    u64 enc_ms;
} cm;

static const Rect SHUTTER = {110, 60, 100, 100};
static const Rect SWITCH = {10, 196, 145, 34}, BACK = {165, 196, 145, 34};
static const Rect SEND = {10, 148, 300, 40}, CAPTION = {10, 196, 145, 34}, RETAKE = {165, 196, 145, 34};

// --- capture thread ---------------------------------------------------------------------

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
        R_FAILED(rc = CAMU_SetTrimming(PORT_CAM1, false)) || R_FAILED(rc = CAMU_GetMaxBytes(&cap.xfer, W, H)) ||
        R_FAILED(rc = CAMU_SetTransferBytes(PORT_CAM1, cap.xfer, W, H)) ||
        R_FAILED(rc = CAMU_GetBufferErrorInterruptEvent(&cap.err_ev, PORT_CAM1))) {
        log_printf("camera: setup failed %08lX", (unsigned long)rc);
        camExit();
        return false;
    }
    // Which YUV the camera gives (range and matrix), so the preview and the JPEG take it as it is.
    Y2RU_StandardCoefficient std = COEFFICIENT_ITU_R_BT_601;
    if (R_FAILED(rc = CAMU_GetSuitableY2rStandardCoefficient(&std))) {
        log_printf("camera: GetSuitableY2rStandardCoefficient failed %08lX", (unsigned long)rc);
        std = COEFFICIENT_ITU_R_BT_601;
    }
    YuvConv conv;
    yuv_conv_init(&conv, (YuvStd)std);
    LightLock_Lock(&cap.lock);
    cap.conv = conv;
    LightLock_Unlock(&cap.lock);
    log_printf("camera: YUV %s (%d), transfer unit %lu", yuv_std_name(conv.std), (int)std, (unsigned long)cap.xfer);
    return true;
}

// The buffer to receive into next: not the newest whole frame, not the UI's. Under the lock.
static int free_buf(void)
{
    for (int i = 0; i < NBUF; i++)
        if (i != cap.ready && i != cap.held) return i;
    return 0;
}

static void cap_stop(Handle *done)
{
    CAMU_StopCapture(PORT_CAM1);
    bool busy = true;  // the frame in flight may finish first: wait for it (a little over a frame at 10 fps)
    for (int i = 0; i < 30 && R_SUCCEEDED(CAMU_IsBusy(&busy, PORT_CAM1)) && busy; i++) svcSleepThread(5 * 1000000LL);
    if (*done) {
        svcCloseHandle(*done);
        *done = 0;
    }
}

static void arm(Handle *done, int rx)
{
    Result rc = CAMU_SetReceiving(done, cap.buf[rx], PORT_CAM1, FRAME_BYTES, (s16)cap.xfer);
    if (R_FAILED(rc)) {
        log_printf("camera: SetReceiving failed %08lX", (unsigned long)rc);
        *done = 0;  // the stall timeout restarts it
    }
}

// (Re)start the capture into buffer rx. The camera itself is left as it is (no CAMU_Activate, which restarts
// its auto exposure and white balance from scratch). Armed before started, so the data always has a home.
static void cap_start(Handle *done, int rx)
{
    cap_stop(done);
    CAMU_ClearBuffer(PORT_CAM1);
    svcClearEvent(cap.err_ev);  // an error from before (paused, turning on) isn't this capture's
    arm(done, rx);
    if (*done) CAMU_StartCapture(PORT_CAM1);
}

static void count(u32 *n, const char *what)
{
    LightLock_Lock(&cap.lock);
    u32 k = ++*n;
    u32 frames = cap.frames;
    LightLock_Unlock(&cap.lock);
    if (k <= LOG_EACH) log_printf("camera: %s after %lu frames, restarting the capture", what, (unsigned long)frames);
}

static void capture_main(void *arg)
{
    bool ok = cam_setup();
    if (!ok) {
        LightLock_Lock(&cap.lock);
        cap.failed = true;
        LightLock_Unlock(&cap.lock);
        return;
    }
    u32 active = SELECT_NONE;
    bool running = false, dead = false;
    Handle done = 0;
    int rx = 0, streak = 0;  // streak: stalls in a row
    for (;;) {
        LightLock_Lock(&cap.lock);
        u32 want = cap.want;
        bool run = cap.run && !cap.away && !dead, quit = cap.quit, shutter = cap.shutter;
        bool again = cap.reactivate && !cap.away;
        cap.shutter = false;
        if (again) cap.reactivate = false;
        LightLock_Unlock(&cap.lock);
        if (quit) break;
        if (shutter) CAMU_PlayShutterSound(SHUTTER_SOUND_TYPE_NORMAL);
        if (again || streak >= STALLS_REACTIVATE) {
            if (streak >= STALLS_REACTIVATE) log_printf("camera: still no frames, turning the camera on again");
            active = SELECT_NONE;  // (it is on: this only forces the CAMU_Activate below)
            streak = 0;
        }
        if (want != active && !dead) {
            cap_stop(&done);
            running = false;
            u64 t0 = osGetTime();
            Result rc = CAMU_Activate(want);
            log_printf("camera: %s camera on in %llu ms%s", want == SELECT_OUT1 ? "outer" : "inner",
                       (unsigned long long)(osGetTime() - t0), R_FAILED(rc) ? " FAILED" : "");
            active = want;
            dead = R_FAILED(rc);
            if (dead) run = false;
            LightLock_Lock(&cap.lock);
            cap.gen++;
            cap.ready = -1;
            cap.failed = dead;
            LightLock_Unlock(&cap.lock);
        }
        if (run && !running) {
            cap_start(&done, rx);  // the same buffer as before: an unfinished receive into it can't land elsewhere
        } else if (!run && running) {
            cap_stop(&done);
        }
        running = run;
        Handle hs[3];
        int n = 0, i_err = -1, i_done = -1;
        hs[n++] = cap.wake;
        if (running) {
            i_err = n;
            hs[n++] = cap.err_ev;
        }
        if (running && done) {
            i_done = n;
            hs[n++] = done;
        }
        s32 idx = -1;
        // Camera events always with a timeout; with the capture paused, only a request can come.
        Result rc = svcWaitSynchronizationN(&idx, hs, n, false, running ? STALL_NS : -1LL);
        if (R_DESCRIPTION(rc) == RD_TIMEOUT) {
            count(&cap.stalls, "no frame for 1.5 s");
            if (++streak < STALLS_REACTIVATE) cap_start(&done, rx);
        } else if (R_FAILED(rc)) {
            log_printf("camera: wait failed %08lX", (unsigned long)rc);
            svcSleepThread(100 * 1000000LL);
        } else if (idx == i_err) {
            svcClearEvent(cap.err_ev);  // (a one-shot event already is; harmless if so)
            count(&cap.errors, "buffer error");
            cap_start(&done, rx);  // the frame in flight is torn: receive it again from the top
        } else if (idx == i_done) {
            svcCloseHandle(done);
            done = 0;
            streak = 0;
            LightLock_Lock(&cap.lock);
            cap.ready = rx;
            cap.ready_sel = active;
            cap.frames++;
            rx = free_buf();
            LightLock_Unlock(&cap.lock);
            arm(&done, rx);  // at once: the next frame is already on its way
        }
    }
    cap_stop(&done);
    if (active != SELECT_NONE) CAMU_Activate(SELECT_NONE);
    svcCloseHandle(cap.err_ev);
    camExit();
}

// Free a closed camera's thread and buffers once the thread has ended. True when nothing is left.
static bool reap(u64 wait_ns)
{
    if (!cap.th) return true;
    if (threadJoin(cap.th, wait_ns) != 0) return false;
    log_printf("camera: off: %lu frames, %lu buffer errors, %lu stalls", (unsigned long)cap.frames,
               (unsigned long)cap.errors, (unsigned long)cap.stalls);
    threadFree(cap.th);
    svcCloseHandle(cap.wake);
    for (int i = 0; i < NBUF; i++) free(cap.buf[i]);
    memset(&cap, 0, sizeof cap);
    return true;
}

// Called from APT (any thread): pause in the HOME Menu and asleep, and start the camera over after.
static void on_apt(APT_HookType hook, void *param)
{
    bool away = hook == APTHOOK_ONSUSPEND || hook == APTHOOK_ONSLEEP;
    if (!away && hook != APTHOOK_ONRESTORE && hook != APTHOOK_ONWAKEUP) return;
    LightLock_Lock(&cap.lock);
    cap.away = away;
    if (!away) cap.reactivate = true;
    LightLock_Unlock(&cap.lock);
    svcSignalEvent(cap.wake);
}

// --- open, close, requests --------------------------------------------------------------

static void request(bool run, bool shutter)
{
    LightLock_Lock(&cap.lock);
    cap.want = cm.select;
    cap.run = run;
    cap.shutter = shutter;
    LightLock_Unlock(&cap.lock);
    svcSignalEvent(cap.wake);
}

// Hide the preview and the shutter until the camera's exposure and white balance have settled again.
static void warm_reset(void)
{
    memset(&cm.settle, 0, sizeof cm.settle);
    cm.settled = cm.have_frame = false;
    cm.warm_t0 = osGetTime();
}

void camera_open(void)
{
    if (cm.open) return;
    if (app.net == NET_ONLINE && !upload_max()) {
        app_toast("Update the Hermes 3DS plugin on the PC to send photos");
        return;
    }
    if (!reap(REOPEN_WAIT_NS)) {
        app_toast("The camera is still turning off. Try again in a moment");
        return;
    }
    memset(&cm, 0, sizeof cm);
    bool mem = true;
    // Whole cache lines (FRAME_BYTES is a multiple of 32): no heap data shares a line the camera's DMA writes.
    for (int i = 0; i < NBUF; i++) mem = (cap.buf[i] = memalign(32, FRAME_BYTES)) && mem;
    cm.tex_ok = C3D_TexInit(&cm.tex, 512, 256, GPU_RGB565);
    if (mem && cm.tex_ok) {
        LightLock_Init(&cap.lock);
        cap.ready = cap.held = -1;
        cap.want = cm.select = SELECT_OUT1;
        cap.run = true;
        if (R_SUCCEEDED(svcCreateEvent(&cap.wake, RESET_ONESHOT))) {
            s32 prio = 0x30;
            svcGetThreadPriority(&prio, CUR_THREAD_HANDLE);
            // Same core as the UI, just above it: it mostly waits, and must re-arm the moment a frame lands.
            cap.th = threadCreate(capture_main, NULL, CAP_STACK, prio > 0x18 ? prio - 1 : prio, -2, false);
        }
    }
    if (!cap.th) {
        app_toast(mem && cm.tex_ok ? "The camera didn't start" : "Not enough memory for the camera");
        if (cap.wake) svcCloseHandle(cap.wake);
        for (int i = 0; i < NBUF; i++) free(cap.buf[i]);
        memset(&cap, 0, sizeof cap);
        if (cm.tex_ok) C3D_TexDelete(&cm.tex);
        memset(&cm, 0, sizeof cm);
        return;
    }
    C3D_TexSetFilter(&cm.tex, GPU_LINEAR, GPU_LINEAR);
    C3D_TexSetWrap(&cm.tex, GPU_CLAMP_TO_EDGE, GPU_CLAMP_TO_EDGE);
    memset(cm.tex.data, 0, cm.tex.size);
    cm.sub = (Tex3DS_SubTexture){.width = PW, .height = PH, .left = 0, .top = 1.0f, .right = PW / 512.0f,
                                 .bottom = 1.0f - PH / 256.0f};
    cm.open = true;
    cm.st = C_PREVIEW;
    aptHook(&cm.apt, on_apt, NULL);
    warm_reset();
    log_printf("camera: open");
    app.dirty = true;
}

bool camera_is_open(void) { return cm.open; }

void camera_close(void)
{
    if (!cm.open) return;
    aptUnhook(&cm.apt);
    if (cm.enc) {  // closed while encoding (Hermes asked something): it only reads the photo, and ends shortly
        threadJoin(cm.enc, U64_MAX);
        threadFree(cm.enc);
        tjFree(cm.jpg);
        tjFree(cm.tjpg);
    }
    LightLock_Lock(&cap.lock);
    cap.quit = true;
    LightLock_Unlock(&cap.lock);
    svcSignalEvent(cap.wake);  // it turns the camera off on its own time; reap() frees the rest
    if (cm.tex_ok) C3D_TexDelete(&cm.tex);
    memset(&cm, 0, sizeof cm);
    reap(0);
    app.dirty = true;
}

void camera_exit(void)
{
    camera_close();
    if (!reap(EXIT_WAIT_NS)) log_printf("camera: the capture thread didn't end");
}

static void take(void)
{
    if (!cm.have_frame) return;
    request(false, true);  // paused, not off: a retake doesn't start the camera over
    cm.st = C_REVIEW;      // the photo is the frame on screen (cap.held), exactly as it will be sent
    app.dirty = true;
}

static void retake(void)
{
    cm.st = C_PREVIEW;
    warm_reset();  // short: the camera stayed on, so it is steady within a few frames
    request(true, false);
    app.dirty = true;
}

static void switch_camera(void)
{
    cm.select = cm.select == SELECT_OUT1 ? SELECT_IN1 : SELECT_OUT1;
    warm_reset();
    request(true, false);
    app.dirty = true;
}

static void edit_caption(void)
{
    // (The system keyboard holds the app until it closes: the camera is paused meanwhile.)
    char buf[sizeof cm.caption];
    if (app_keyboard("A caption or a question about the photo", cm.caption, buf, sizeof buf, 300, true, "OK"))
        snprintf(cm.caption, sizeof cm.caption, "%s", buf);
    else if (!buf[0])
        cm.caption[0] = 0;  // cleared and confirmed (an empty caption is fine)
}

// The encoder thread: the photo for the PC (JPEG) and the chat card's thumbnail. It reads the held frame,
// which nothing writes while a photo is in review.
static void encode_main(void *arg)
{
    u64 t0 = osGetTime();
    const uint8_t *frame = cap.buf[cap.held];
    // Straight from the camera's YUV to JPEG's (no RGB round trip): planes Y (W x H), Cb and Cr (W/2 x H).
    uint8_t *yp = malloc((size_t)W * H * 2), *up = yp ? yp + W * H : NULL, *vp = up ? up + W * H / 2 : NULL;
    uint8_t *thumb = malloc(THUMB_W * THUMB_H * 3);
    tjhandle tj = tjInitCompress();
    unsigned char *jpg = NULL, *tjpg = NULL;
    unsigned long jlen = 0, tlen = 0;
    bool ok = yp && thumb && tj;
    if (ok) {
        yuv_planes(&cm.conv, frame, W, H, yp, up, vp);
        yuv_thumb(yp, up, vp, W, H, thumb, THUMB_W, THUMB_H);
        const unsigned char *planes[3] = {yp, up, vp};
        ok = tjCompressFromYUVPlanes(tj, planes, W, NULL, H, TJSAMP_422, &jpg, &jlen, JPEG_Q, TJFLAG_FASTDCT) == 0 &&
             tjCompress2(tj, thumb, THUMB_W, 0, THUMB_H, TJPF_RGB, &tjpg, &tlen, TJSAMP_420, 75, TJFLAG_FASTDCT) == 0;
        if (!ok) log_printf("camera: jpeg: %s", tjGetErrorStr2(tj));
    }
    free(yp);
    free(thumb);
    if (tj) tjDestroy(tj);
    cm.jpg = jpg;
    cm.tjpg = tjpg;
    cm.jlen = jlen;
    cm.tlen = tlen;
    cm.enc_ok = ok;
    cm.enc_ms = osGetTime() - t0;
}

static void start_encode(void)
{
    s32 prio = 0x30;
    svcGetThreadPriority(&prio, CUR_THREAD_HANDLE);
    // Below the UI and the network thread: the screen keeps moving, the connection keeps its pace.
    cm.enc = threadCreate(encode_main, NULL, ENC_STACK, prio + 2 < 0x3F ? prio + 2 : 0x3F, -2, false);
    if (!cm.enc) {
        app_toast("Couldn't prepare the photo");
        sfx_play(SFX_ERROR);
        return;
    }
    cm.st = C_ENCODE;
    app.dirty = true;
}

// Both JPEGs cached under their ids, then the send.
static void finish_send(void)
{
    threadFree(cm.enc);
    cm.enc = NULL;
    unsigned char *jpg = cm.enc_ok ? cm.jpg : NULL, *tjpg = cm.tjpg;
    unsigned long jlen = cm.jlen, tlen = cm.tlen;
    if (!cm.enc_ok) tjFree(cm.jpg);
    cm.jpg = cm.tjpg = NULL;
    char id[UPLOAD_ID_LEN], tid[UPLOAD_ID_LEN + 2];
    upload_new_id(id);
    snprintf(tid, sizeof tid, "%st", id);  // the thumbnail stays on the 3DS
    uint8_t *data = jpg ? malloc(jlen) : NULL;
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
    LightLock_Lock(&cap.lock);
    u32 frames = cap.frames, errors = cap.errors, stalls = cap.stalls;
    LightLock_Unlock(&cap.lock);
    log_printf("camera: photo %lu bytes, thumb %lu, in %llu ms; Y %d, U %+d, V %+d (%s); %lu frames, %lu buffer "
               "errors, %lu stalls",
               jlen, tlen, (unsigned long long)cm.enc_ms, cm.shown.y, cm.shown.u, cm.shown.v,
               yuv_std_name(cm.conv.std), (unsigned long)frames, (unsigned long)errors, (unsigned long)stalls);
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
        if ((down & KEY_A) || (t && rect_hit(&SEND, x, y))) start_encode();
        else if ((down & KEY_Y) || (t && rect_hit(&CAPTION, x, y))) edit_caption();
        else if ((down & KEY_B) || (t && rect_hit(&RETAKE, x, y))) retake();
    }
}

void camera_tick(void)
{
    if (!cm.open) {
        reap(0);  // a closed camera's thread, once it has turned the camera off
        return;
    }
    if (cm.st == C_ENCODE) {
        if (threadJoin(cm.enc, 0) == 0) finish_send();
        return;
    }
    if (cm.st != C_PREVIEW) return;
    int f = -1;
    LightLock_Lock(&cap.lock);
    u32 gen = cap.gen;
    bool failed = cap.failed;
    if (gen != cm.seen_gen) cm.conv = cap.conv;
    if (cap.ready >= 0 && cap.ready_sel == cm.select) cap.held = f = cap.ready;  // (a switch drops the old camera's)
    cap.ready = -1;
    LightLock_Unlock(&cap.lock);
    if (failed) {
        app_toast("The camera didn't start");
        camera_close();
        return;
    }
    if (gen != cm.seen_gen) {  // a camera was turned on: its exposure and white balance start over
        cm.seen_gen = gen;
        warm_reset();
    }
    if (f < 0) {
        if (cm.have_frame && osGetTime() - cm.last_frame > FROZEN_MS) {  // stalled: no taking a stale picture
            cm.have_frame = false;
            app.dirty = true;
        }
        return;
    }
    cm.last_frame = osGetTime();
    GSPGPU_InvalidateDataCache(cap.buf[f], FRAME_BYTES);  // read what the camera's DMA wrote, not old cache lines
    YuvStats st;
    yuv_preview(&cm.conv, cap.buf[f], W, H, cm.tex.data, 512, &st);
    C3D_TexFlush(&cm.tex);
    cm.shown = st;
    if (!cm.settled && (cm.settled = yuv_settle(&cm.settle, &st)))
        log_printf("camera: steady after %d frames, %llu ms (Y %d, U %+d, V %+d)", cm.settle.frames,
                   (unsigned long long)(osGetTime() - cm.warm_t0), st.y, st.u, st.v);
    cm.have_frame = cm.settled;
    if (cm.have_frame) app.dirty = true;
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
