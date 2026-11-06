// Hermes 3DS: main loop (pairing, secure connection, chat, approvals, media).
#include <3ds.h>
#include <citro2d.h>
#include <malloc.h>
#include <stdio.h>
#include <string.h>

#include "app.h"
#include "led.h"
#include "log.h"
#include "audio.h"
#include "blob.h"
#include "camera.h"
#include "image.h"
#include "net.h"
#include "perf.h"
#include "speak.h"
#include "store.h"
#include "text.h"
#include "upload.h"
#include "viewer.h"
#include "voice.h"

#ifndef APP_VERSION  // set by the Makefile (VERSION_MAJOR/MINOR/MICRO)
#define APP_VERSION "3ds-dev"
#endif
#define SOC_BUFFER_SIZE 0x100000
#define ANIM_FRAME_MS 80  // spinner frame time; redraws are throttled to this when only animating

static u32 *soc_buffer;

static void on_apt(APT_HookType hook, void *param)
{
    // The Wi-Fi link is gone after sleep: drop the dead socket and reconnect right away.
    if (hook == APTHOOK_ONWAKEUP) net_wake();
}

static void device_identity(NetIdentity *id)
{
    u16 name16[0x1C / 2 + 1] = {0};
    char name[48] = "";
    if (R_SUCCEEDED(CFGU_GetConfigInfoBlk2(0x1C, 0x000A0000, name16))) {
        ssize_t n = utf16_to_utf8((uint8_t *)name, name16, sizeof name - 1);
        name[n > 0 ? n : 0] = 0;
    }
    snprintf(id->device, sizeof id->device, "%s%s", name[0] ? name : "My", name[0] ? "'s 3DS" : " 3DS");
    bool is_new = false;
    APT_CheckNew3DS(&is_new);
    snprintf(id->model, sizeof id->model, "%s", is_new ? "new3ds" : "old3ds");
    snprintf(id->app, sizeof id->app, "%s", APP_VERSION);
}

// What the screen shows, for perf lines in the log.
static const char *where(void)
{
    static const char *const TABS[TAB_COUNT] = {"chat", "sessions", "skills", "jobs", "more"};
    if (app.screen == SCR_PAIR) return "pairing";
    if (voice_recording()) return "recording";
    if (camera_is_open()) return "camera";
    if (viewer_is_open()) return "files";
    return TABS[app.tab];
}

static void fatal_screen(const char *msg)
{
    consoleInit(GFX_TOP, NULL);
    printf("\n  Hermes 3DS\n\n  %s\n\n  Press START to exit.\n", msg);
    while (aptMainLoop()) {
        hidScanInput();
        if (hidKeysDown() & KEY_START) break;
        gspWaitForVBlank();
    }
}

int main(void)
{
    perf_start();
    gfxInitDefault();
    cfguInit();
    psInit();
    store_init();
    log_open();

    soc_buffer = memalign(0x1000, SOC_BUFFER_SIZE);
    if (!soc_buffer || R_FAILED(socInit(soc_buffer, SOC_BUFFER_SIZE))) {
        fatal_screen("Couldn't start networking (soc:u).");
        goto out_early;
    }

    NetIdentity id;
    memset(&id, 0, sizeof id);
    device_identity(&id);
    if (!store_device_key(id.device_priv)) {
        fatal_screen("Couldn't read or create sdmc:/3ds/hermes/device_key.\n  Is the SD card in?");
        goto out_soc;
    }
    log_printf("%s on %s as \"%s\"", APP_VERSION, id.model, id.device);
    snprintf(app.device, sizeof app.device, "%s", id.device);

    perf_step("system+sd+network");
    C3D_Init(C3D_DEFAULT_CMDBUF_SIZE);
    C2D_Init(8192);
    C2D_Prepare();
    C3D_RenderTarget *top = C2D_CreateScreenTarget(GFX_TOP, GFX_LEFT);
    C3D_RenderTarget *bottom = C2D_CreateScreenTarget(GFX_BOTTOM, GFX_LEFT);
    romfsInit();  // emoji sprites (text.c)
    perf_step("gpu");
    text_init();
    draw_init();
    perf_step("font+emoji");

    if (!net_init(&id)) {
        fatal_screen("Couldn't start the network thread.");
        goto out_gfx;
    }
    memset(&id, 0, sizeof id);
    aptHookCookie cookie;
    aptHook(&cookie, on_apt, NULL);
    led_init();
    app_init();
    perf_step("app");
    perf_startup_done();

    u64 last_draw = 0;
    while (aptMainLoop() && !app.quit) {
        u64 t_events = perf_now();
        app_input();
        NetEvent *ev;
        for (int budget = 64; budget > 0 && (ev = net_poll()); budget--) {
            app_handle_event(ev);
            net_event_free(ev);
        }
        u64 t_ticks = perf_now();
        u64 now = osGetTime();
        float dt = app.now ? (now - app.now) / 1000.0f : 0;
        app.now = now;
        // Text being typed out gets every frame (smooth); spinners only need ~12 fps; idle draws nothing.
        bool revealing = false;
        for (int i = 0; i < app.nagents; i++)  // the other agents' replies keep typing out behind
            if (chat_tick(&app.agents[i].chat, dt > 0.1f ? 0.1f : dt) && i == app.cur) revealing = true;
        led_waiting(app_any_waiting());
        blob_tick();
        audio_tick();
        speak_tick();
        voice_tick();
        camera_tick();
        upload_tick();
        app_tick();
        u64 t_draw = perf_now(), t_end = t_draw;
        bool drew = app.dirty || revealing || (app_animating() && now - last_draw >= ANIM_FRAME_MS);
        if (drew) {
            app.dirty = false;
            last_draw = now;
            draw_frame(top, bottom);
            t_end = perf_now();
        }
        perf_loop(t_ticks - t_events, t_draw - t_ticks, t_end - t_draw, drew, where());
        if (!drew) gspWaitForVBlank();
    }

    aptUnhook(&cookie);
    led_exit();
    net_exit();
    app_exit();
    image_exit();
out_gfx:
    text_exit();
    romfsExit();
    C2D_Fini();
    C3D_Fini();
out_soc:
    socExit();
out_early:
    log_printf("exit");
    log_close();
    psExit();
    cfguExit();
    gfxExit();
    return 0;
}
