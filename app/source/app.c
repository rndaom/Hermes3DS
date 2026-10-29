#include "app.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio.h"
#include "blob.h"
#include "camera.h"
#include "codec.h"
#include "log.h"
#include "sfx.h"
#include "store.h"
#include "tabs.h"
#include "ui.h"
#include "upload.h"
#include "viewer.h"
#include "voice.h"

App app;

const char *const CHIP_CMDS[N_CHIPS] = {"/new", "/retry", "/undo", "/status", "/help"};

static const char *jstr(const json_t *o, const char *k)
{
    const char *s = json_string_value(json_object_get(o, k));
    return s ? s : "";
}

void app_toast(const char *msg)
{
    snprintf(app.toast, sizeof app.toast, "%s", msg);
    app.toast_until = osGetTime() + 2500;
}

static uint32_t next_id(void) { return ++app.next_id; }

uint32_t app_request(const char *t, json_t *fields)
{
    uint32_t id = next_id();
    json_t *m = fields ? fields : json_object();
    json_object_set_new(m, "t", json_string(t));
    json_object_set_new(m, "id", json_integer(id));
    net_send(m);
    return id;
}

// --- pairing ----------------------------------------------------------------

static bool parse_here(const DiscoveryReply *r, ServerInfo *out)
{
    json_t *j = json_depth_ok(r->json, strlen(r->json)) ? json_loads(r->json, 0, NULL) : NULL;
    bool ok = j && !strcmp(jstr(j, "t"), "here") && b64u_decode(jstr(j, "key"), out->key, sizeof out->key) == 32;
    if (ok) {
        json_int_t port = json_integer_value(json_object_get(j, "port"));
        snprintf(out->host, sizeof out->host, "%s", r->addr);
        snprintf(out->name, sizeof out->name, "%s", jstr(j, "name"));
        out->port = (uint16_t)(port > 0 && port < 65536 ? port : 7333);
    }
    json_decref(j);
    return ok;
}

static void start_scan(void)
{
    app.screen = SCR_PAIR;
    app.pstep = PAIR_SCAN;
    app.nfound = 0;
    app.sel = 0;
    app.manual_lookup = false;
    net_discover(NULL);
}

bool app_keyboard(const char *hint, const char *initial, char *out, size_t cap, int maxlen, bool multiline,
                     const char *ok_label)
{
    SwkbdState kb;
    // maxlen counts UTF-16 units; one unit can take 3 UTF-8 bytes.
    if ((size_t)maxlen * 3 + 1 > cap) maxlen = (int)((cap - 1) / 3);
    swkbdInit(&kb, SWKBD_TYPE_NORMAL, 2, maxlen);
    swkbdSetHintText(&kb, hint);
    if (initial && initial[0]) swkbdSetInitialText(&kb, initial);
    swkbdSetButton(&kb, SWKBD_BUTTON_LEFT, "Cancel", false);
    swkbdSetButton(&kb, SWKBD_BUTTON_RIGHT, ok_label, true);
    swkbdSetValidation(&kb, SWKBD_NOTEMPTY_NOTBLANK, 0, 0);
    if (multiline) swkbdSetFeatures(&kb, SWKBD_MULTILINE);
    out[0] = 0;
    SwkbdButton b = swkbdInputText(&kb, out, cap);
    app.dirty = true;
    if (b != SWKBD_BUTTON_RIGHT) return false;
    // The validation above is advisory on emulators (Azahar returns OK with nothing typed).
    for (const char *p = out; *p; p++)
        if (*p != ' ' && *p != '\n' && *p != '\t') return true;
    return false;
}

static void ask_code(void)
{
    char code[32];
    if (!app_keyboard("8-character code from: hermes 3ds pair", NULL, code, sizeof code, 12, false, "OK")) return;
    uint8_t tok[8];
    int n = b32_decode(code, tok, sizeof tok);
    if (n != 5) {
        snprintf(app.pair_msg, sizeof app.pair_msg, "That code isn't valid. It has 8 letters and digits (A-Z, 2-7).");
        app.pstep = PAIR_ERROR;
        return;
    }
    memcpy(app.token, tok, 5);
    app.token_len = 5;
    app.pstep = PAIR_CONFIRM;
}

static void pick_server(int i)
{
    if (i < app.nfound) {
        app.cand = app.found_info[i];
        ask_code();
        return;
    }
    char addr[64];
    const char *initial = app.have_server ? app.server.host : "192.168.";
    if (!app_keyboard("The PC's IP address", initial, addr, sizeof addr, 40, false, "Look up")) return;
    app.manual_lookup = true;
    app.pstep = PAIR_SCAN;
    net_discover(addr);
}

static void pair_input(u32 down)
{
    switch (app.pstep) {
    case PAIR_SCAN:
        if ((down & KEY_B) && app.have_server) app.screen = SCR_CHAT;
        break;
    case PAIR_LIST: {
        int items = app.nfound + 1;  // + "enter an address"
        if (down & (KEY_DOWN | KEY_CPAD_DOWN)) app.sel = (app.sel + 1) % items;
        if (down & (KEY_UP | KEY_CPAD_UP)) app.sel = (app.sel + items - 1) % items;
        if (down & KEY_A) pick_server(app.sel);
        if (down & KEY_Y) start_scan();
        if ((down & KEY_B) && app.have_server) app.screen = SCR_CHAT;
        if (down & KEY_TOUCH) {
            touchPosition t;
            hidTouchRead(&t);
            // rows as drawn by draw.c: 34 px each from y 38
            int row = ((int)t.py - 38) / 34;
            if (row >= 0 && row < items && t.py >= 38) {
                app.sel = row;
                pick_server(row);
            }
        }
        break;
    }
    case PAIR_CONFIRM:
        if (down & KEY_A) {
            app.pstep = PAIR_WORKING;
            net_pair(&app.cand, app.token, app.token_len);
            noise_wipe(app.token, sizeof app.token);
        }
        if (down & KEY_B) app.pstep = PAIR_LIST;
        break;
    case PAIR_WORKING: break;
    case PAIR_ERROR:
        if (down & KEY_A) start_scan();
        if ((down & KEY_B) && app.have_server) app.screen = SCR_CHAT;
        break;
    }
}

// --- chat actions -----------------------------------------------------------

void app_send_text(const char *text)
{
    uint32_t id = next_id();
    json_t *m = json_pack("{s:s, s:I, s:s}", "t", "send", "id", (json_int_t)id, "text", text);
    if (!m) {
        app_toast("Couldn't send that text");
        return;
    }
    net_send(m);
    chat_add_user(&app.chat, text, id);
    app.tab = TAB_CHAT;  // sent from another tab: show the reply
    app.scroll = 0;      // jump to the newest message
    sfx_play(SFX_SENT);
}

// --- photos and voice messages: upload first, then the send (protocol.md §4.4.1, §5.1) ---------------------

#define MEDIA_MAX 6  // waiting or uploading at once

static struct {
    uint32_t local;  // 0: free
    json_t *fields;
    char *text;
} media[MEDIA_MAX];

static void media_done(uint32_t local, bool ok, const char *why)
{
    int k = 0;
    while (k < MEDIA_MAX && media[k].local != local) k++;
    if (k == MEDIA_MAX) return;
    Msg *m = chat_find_local(&app.chat, local);  // NULL when it scrolled out or the session changed
    json_t *f = media[k].fields;
    if (ok) {
        if (media[k].text && media[k].text[0]) json_object_set_new(f, "text", json_string(media[k].text));
        uint32_t req = app_request("send", f);
        if (m) {
            m->req_id = req;
            m->upload_id[0] = 0;
        }
        sfx_play(SFX_SENT);
    } else {
        json_decref(f);
        if (m) {
            m->pending = false;
            m->failed = true;
            m->upload_id[0] = 0;
            m->dirty = true;
        }
        chat_notice(&app.chat, "That didn't reach the PC: %s", why && why[0] ? why : "the upload failed");
        sfx_play(SFX_ERROR);
    }
    free(media[k].text);
    media[k].local = 0;
    media[k].fields = NULL;
    media[k].text = NULL;
    app.chat.version++;
    app.dirty = true;
}

void app_send_media(const char *id, uint8_t *data, size_t len, const char *mime, const char *name, json_t *attach,
                    json_t *fields, const char *text)
{
    int k = 0;
    while (k < MEDIA_MAX && media[k].local) k++;
    if (k == MEDIA_MAX || !attach || !fields) {
        free(data);
        json_decref(attach);
        json_decref(fields);
        app_toast(k == MEDIA_MAX ? "Still sending the others; try again in a moment" : "Couldn't send that");
        return;
    }
    uint32_t local = next_id();
    Msg *m = chat_add_user(&app.chat, text ? text : "", 0);
    m->local_id = local;
    m->attach = json_pack("[o]", attach);
    snprintf(m->upload_id, sizeof m->upload_id, "%s", id);
    media[k].local = local;
    media[k].fields = fields;
    media[k].text = strdup(text ? text : "");
    if (!media[k].text) media[k].text = calloc(1, 1);
    app.tab = TAB_CHAT;
    app.scroll = 0;
    if (!upload_start(id, data, len, mime, name, media_done, local))
        media_done(local, false, upload_max() && len > upload_max() ? "too large for the PC" : "too many uploads waiting");
}

void app_set_sounds(bool on)
{
    app.settings.sounds = on;
    sfx_set_muted(!on);
    if (!store_save_settings(&app.settings)) app_toast("Couldn't save to the SD card");
    else app_toast(on ? "Sounds on" : "Sounds off");
    if (on) sfx_play(SFX_SENT);
}

// Hermes' voice mode for this chat: /voice on answers voice messages with speech too (as audio attachments,
// played here as they arrive); off keeps replies as text.
void app_set_speak(bool on)
{
    app.settings.speak_replies = on;
    if (!store_save_settings(&app.settings)) app_toast("Couldn't save to the SD card");
    if (app.net != NET_ONLINE) {
        app_toast(on ? "Spoken replies on (tells Hermes when connected)" : "Spoken replies off");
        return;
    }
    app_request("send", json_pack("{s:s}", "text", on ? "/voice on" : "/voice off"));
    app_toast(on ? "Spoken replies on" : "Spoken replies off");
}

// A new spoken reply (an audio attachment flagged voice, protocol.md §4.7): play it when that is wanted.
static void autoplay(const json_t *msg)
{
    static char last[8][40];
    static int next;
    if (!app.settings.speak_replies || voice_active() || camera_is_open()) return;
    json_t *attach = json_object_get(msg, "attach");
    for (size_t i = 0; i < json_array_size(attach); i++) {
        const json_t *a = json_array_get(attach, i);
        if (!json_is_true(json_object_get(a, "voice")) || !audio_playable(a)) continue;
        const char *id = jstr(json_object_get(a, "audio"), "id");
        bool seen = false;
        for (int k = 0; k < 8; k++) seen |= !strcmp(last[k], id);
        if (seen) continue;  // a replayed message
        snprintf(last[next], sizeof last[next], "%s", id);
        next = (next + 1) % 8;
        AudioStatus st;
        if (!audio_status(a, &st) || !st.playing) audio_toggle(a);
        return;
    }
}

void app_compose(const char *initial)
{
    char buf[3072];
    bool ok = app_keyboard(app.chat.typing ? "Queues until this step ends" : "Write to Hermes",
                           initial ? initial : app.draft, buf, sizeof buf, 1000, true, "Send");
    if (ok) {
        app_send_text(buf);
        app.draft[0] = 0;
    } else {
        snprintf(app.draft, sizeof app.draft, "%s", buf);  // keep what was typed
    }
}

static void stop_turn(void)
{
    chat_reveal_now(&app.chat, true);
    app_request("stop", NULL);
    app_toast("Stopping...");
}

static void approve(const char *choice)
{
    if (!app.chat.approval) return;
    app_request("approve", json_pack("{s:s, s:s}", "req", jstr(app.chat.approval, "req"), "choice", choice));
}

static void answer_typed(void)
{
    json_t *cl = app.chat.clarify;
    if (!cl) return;
    int nch = clar_count();
    char buf[2432];
    const char *hint = !nch          ? "Your answer"
                       : clar_multi() ? "Numbers like 1,3, or your own answer"
                                      : "A number from the list, or your own answer";
    if (!app_keyboard(hint, NULL, buf, sizeof buf, 800, true, "Answer")) return;
    char *end;
    long v = strtol(buf, &end, 10);
    json_t *f = (nch && !clar_multi() && end != buf && *end == 0 && v >= 1 && v <= nch)
                    ? json_pack("{s:s, s:i}", "cid", jstr(cl, "cid"), "choice", (int)(v - 1))
                    : json_pack("{s:s, s:s}", "cid", jstr(cl, "cid"), "text", buf);
    if (f) app_request("answer", f);
}

// Multi-select: send the ticked choices.
static void answer_ticked(void)
{
    json_t *cl = app.chat.clarify;
    if (!cl || !clar_multi()) return;
    json_t *picks = json_array();
    for (int i = 0; i < clar_count() && i < 32; i++)
        if (app.chat.clar_sel >> i & 1) json_array_append_new(picks, json_integer(i));
    if (!json_array_size(picks)) {
        json_decref(picks);
        app_toast("Tick at least one, or type an answer");
        return;
    }
    app_request("answer", json_pack("{s:s, s:o}", "cid", jstr(cl, "cid"), "choices", picks));
}

// A on (or a tap of) choice i: answers a single-choice question, ticks a multi-select one.
static void clar_pick(int i)
{
    json_t *cl = app.chat.clarify;
    if (!cl || i < 0 || i >= clar_count()) return;
    app.chat.clar_cur = i;
    if (clar_multi()) {
        if (i < 32) app.chat.clar_sel ^= 1u << i;
        return;
    }
    app_request("answer", json_pack("{s:s, s:i}", "cid", jstr(cl, "cid"), "choice", i));
}

// A question panel ignores A, X, Y and touches for its first PROMPT_ARM_MS: its rows lie where the input box
// and the Talk / Photo tiles were, and A is otherwise "type a message", so a tap or press meant for those, made
// just as the panel appeared, must not answer it (allow a command, pick a choice). B stays immediate: it was
// already the stop key, and on an approval it can only deny.
#define PROMPT_ARM_MS 700

static bool prompt_armed(void) { return osGetTime() - app.chat.prompt_since >= PROMPT_ARM_MS; }

static void primary(void)
{
    if ((app.chat.approval || app.chat.clarify) && !prompt_armed()) return;
    if (app.chat.approval) approve("once");
    else if (app.chat.clarify) clar_count() ? clar_pick(app.chat.clar_cur) : answer_typed();
    else if (app.net == NET_REFUSED) start_scan();
    else app_compose(NULL);
}

static void back(void)
{
    if (app.chat.approval) approve("deny");
    else if (app.chat.typing) stop_turn();
    else chat_reveal_now(&app.chat, false);  // skip the rest of a reply that is still typing out
}

static void chat_touch(int x, int y)
{
    BottomLayout l;
    bottom_layout(&l);
    if ((app.chat.approval || app.chat.clarify) && !prompt_armed()) return;
    if (app.chat.approval) {  // the approval panel owns the screen
        for (int i = 0; i < APPR_N; i++)
            if (l.appr[i].w > 0 && rect_hit(&l.appr[i], x, y)) approve(APPR_CHOICE[i]);
        return;
    }
    if (app.chat.clarify) {  // so does the clarify panel
        for (int r = 0; r < l.clar_rows; r++)
            if (rect_hit(&l.clar[r], x, y)) clar_pick(l.clar_first + r);
        if (rect_hit(&l.clar_type, x, y)) answer_typed();
        if (l.clar_send.w > 0 && rect_hit(&l.clar_send, x, y)) answer_ticked();
        return;
    }
    if (l.strip_kind != STRIP_NONE && rect_hit(&l.strip_btn, x, y)) {
        if (l.strip_kind == STRIP_WORKING) stop_turn();
        else if (l.strip_kind == STRIP_REFUSED) start_scan();
        return;
    }
    if (l.files_btn.w > 0 && rect_hit(&l.files_btn, x, y)) {
        viewer_open(-1);
        return;
    }
    if (rect_hit(&l.input, x, y)) {
        app_compose(NULL);
        return;
    }
    if (rect_hit(&l.talk, x, y)) {
        voice_press(true, false);  // records while the finger stays on the screen
        return;
    }
    if (rect_hit(&l.photo, x, y)) {
        camera_open();
        return;
    }
    for (int i = 0; i < N_CHIPS; i++)
        if (rect_hit(&l.chips[i], x, y)) {
            if (app.net != NET_ONLINE) app_toast("Not connected; it will send when the PC is back");
            app_send_text(CHIP_CMDS[i]);
            return;
        }
}

const json_t *app_chat_audio(void)
{
    const json_t *newest = NULL;
    AudioStatus s;
    for (int i = 0; i < app.chat.n; i++)
        for (size_t k = 0; k < json_array_size(app.chat.m[i]->attach); k++) {
            const json_t *a = json_array_get(app.chat.m[i]->attach, k);
            if (!audio_playable(a)) continue;
            if (audio_status(a, &s)) return a;
            newest = a;
        }
    return newest;
}

static void chat_input(u32 down, u32 held, const touchPosition *touch)
{
    if (down & KEY_A) primary();
    if (down & KEY_B) back();
    if (touch) chat_touch(touch->px, touch->py);
    if (app.chat.clarify && !app.chat.approval && clar_count()) {  // the D-pad moves through the choices
        int n = clar_count();
        if (down & KEY_DUP) app.chat.clar_cur = (app.chat.clar_cur + n - 1) % n;
        if (down & KEY_DDOWN) app.chat.clar_cur = (app.chat.clar_cur + 1) % n;
        if ((down & KEY_X) && prompt_armed()) answer_ticked();
        if ((down & KEY_Y) && prompt_armed()) answer_typed();
        down &= ~(u32)(KEY_DUP | KEY_DDOWN);
        held &= ~(u32)(KEY_DUP | KEY_DDOWN);
    } else if (app.chat.clarify && (down & KEY_Y)) {
        if (prompt_armed()) answer_typed();
    } else if (!app.chat.approval && !app.chat.clarify && (down & KEY_X)) {
        viewer_open(-1);
    } else if (!app.chat.approval && !app.chat.clarify && (down & KEY_Y)) {
        // A hold talks; a tap plays (talk_input). Without voice messages (older plugin, no mic) Y just plays.
        if (!voice_press(false, app_chat_audio() != NULL) && app_chat_audio()) audio_toggle(app_chat_audio());
    }
    // Scrolling: D-pad steps, Circle Pad proportional. scroll = pixels above the bottom.
    float d = 0;
    if (down & KEY_DUP) d += 36;
    if (down & KEY_DDOWN) d -= 36;
    if (held & (KEY_DUP | KEY_DDOWN)) {
        static u64 since;
        if (down & (KEY_DUP | KEY_DDOWN)) since = app.now;
        else if (app.now - since > 350) d += (held & KEY_DUP) ? 6 : -6;  // auto-repeat
    }
    circlePosition cp;
    hidCircleRead(&cp);
    if (cp.dy > 18 || cp.dy < -18) d += (float)cp.dy / 12.0f;
    if (d != 0) {
        float max = app.content_h - app.view_h;
        app.scroll += d;
        if (app.scroll > max) app.scroll = max;
        if (app.scroll < 0) app.scroll = 0;
        app.dirty = true;
    }
}

// While Y (or a finger on Talk) is down. The first ~0.3 s it may still be a tap: Y's tap plays or pauses the
// chat's audio. Past that, the recording screens own the input until it's sent or cancelled.
static void talk_input(u32 down, u32 held, u32 up)
{
    if (voice_recording()) {
        voice_input(down, held, up);
        return;
    }
    bool by_y = held & KEY_Y || up & KEY_Y;
    bool lifted = by_y ? (up & KEY_Y) || !(held & KEY_Y) : !(held & KEY_TOUCH);
    if (!lifted) return;
    if (voice_release() && by_y) {
        const json_t *au = app_chat_audio();
        if (au) audio_toggle(au);
        else app_toast("Hold " GLYPH_Y " and talk");
    }
}

void app_input(void)
{
    hidScanInput();
    u32 down = hidKeysDown(), held = hidKeysHeld(), up = hidKeysUp();
    if (down & KEY_START) {
        app.quit = true;
        return;
    }
    if (down) app.dirty = true;
    if (app.screen == SCR_PAIR) {
        pair_input(down);
        return;
    }
    if (voice_active()) {
        talk_input(down, held, up);
        return;
    }
    if (camera_is_open()) {  // full screen, both screens
        touchPosition t;
        if (down & KEY_TOUCH) hidTouchRead(&t);
        camera_input(down, held, (down & KEY_TOUCH) ? &t : NULL);
        return;
    }
    if (viewer_is_open()) {  // full-screen over the Chat tab, no tab bar
        touchPosition t;
        if (down & KEY_TOUCH) hidTouchRead(&t);
        viewer_input(down, held, (down & KEY_TOUCH) ? &t : NULL);
        return;
    }
    if (down & KEY_SELECT) {
        start_scan();
        return;
    }
    if (down & (KEY_L | KEY_R)) {
        tabs_switch((Tab)((app.tab + ((down & KEY_R) ? 1 : TAB_COUNT - 1)) % TAB_COUNT));
        return;
    }
    touchPosition t, *touch = NULL;
    if (down & KEY_TOUCH) {
        hidTouchRead(&t);
        if (t.py < TABS_H) {
            tabs_switch((Tab)(t.px / 64 < TAB_COUNT ? t.px / 64 : TAB_COUNT - 1));
            return;
        }
        touch = &t;
    }
    if (app.tab == TAB_CHAT) chat_input(down, held, touch);
    else tabs_input(down, held, touch);
}

// The session changed (/new, /resume, the Sessions tab): show its history instead of the old transcript.
static void session_changed(const char *sid)
{
    chat_clear_transcript(&app.chat);
    app.scroll = 0;
    app.session_switch = true;
    app.chat.history_loaded = false;
    app_request("sessions.messages", json_pack("{s:s, s:i}", "sid", sid, "limit", 20));
}

// --- network events ---------------------------------------------------------

void app_handle_event(NetEvent *ev)
{
    app.dirty = true;
    switch (ev->kind) {
    case NET_EV_BLOB:
        blob_on_chunk(ev);
        break;
    case NET_EV_STATE:
        app.net = ev->state;
        blob_online(ev->state == NET_ONLINE);
        upload_online(ev->state == NET_ONLINE);
        app.retry_s = ev->retry_s;
        app.retry_at = osGetTime() + (u64)ev->retry_s * 1000;
        snprintf(app.net_detail, sizeof app.net_detail, "%s", ev->detail);
        if (ev->state == NET_REFUSED) {
            if (!strcmp(ev->detail, "not_paired"))
                chat_notice(&app.chat, "%s doesn't know this 3DS any more. Press SELECT to pair again.",
                            app.server.name[0] ? app.server.name : "The PC");
            else chat_notice(&app.chat, "The PC refused the connection: %s", ev->detail);
        }
        if (ev->state != NET_ONLINE && app.chat.typing) app.chat.typing = false;  // re-sent after welcome if still on
        break;
    case NET_EV_WELCOME: {
        if (memcmp(ev->server.key, app.server.key, 32)) break;  // a late welcome from the PC before a re-pair
        app.handshake_ms = ev->handshake_ms;
        app.route = ev->route;
        app.tls = ev->tls;
        json_int_t up_max = json_integer_value(json_object_get(json_object_get(ev->json, "upload"), "max"));
        upload_set_max(up_max > 0 && up_max < 0x7fffffff ? (uint32_t)up_max : 0);
        const char *name = jstr(ev->json, "server");
        if (name[0] && strcmp(name, app.server.name)) {
            snprintf(app.server.name, sizeof app.server.name, "%s", name);
            store_save_server(&app.server);
        }
        // Hermes' voice mode for this chat (plugins that report it): bring it in line with "Spoken replies".
        json_t *speaks = json_object_get(json_object_get(ev->json, "chat"), "voice");
        if (json_is_boolean(speaks) && json_is_true(speaks) != app.settings.speak_replies)
            app_request("send", json_pack("{s:s}", "text", app.settings.speak_replies ? "/voice on" : "/voice off"));
        json_t *sess = json_object_get(json_object_get(ev->json, "chat"), "session");
        const char *sid = jstr(sess, "id");
        if (sid[0]) snprintf(app.chat.session_id, sizeof app.chat.session_id, "%s", sid);
        if (!app.chat.history_loaded && sid[0])
            app_request("sessions.messages", json_pack("{s:s, s:i}", "sid", sid, "limit", 20));
        app_request("status", NULL);
        tabs_online();
        break;
    }
    case NET_EV_JSON: {
        if (upload_on_json(ev->json)) break;                             // photos and voice going up
        if (blob_on_json(ev->json) || viewer_handle(ev->json)) break;  // blob transfers, Open on PC
        if (tabs_handle(ev->json)) break;                                // replies to the other tabs' requests
        const char *t = jstr(ev->json, "t");
        bool asking = app.chat.approval || app.chat.clarify;
        if (!strcmp(t, "status_r")) {
            double now = json_number_value(json_object_get(ev->json, "now"));
            if (now > 0) app.tz_off = lround(((double)time(NULL) - now) / 900.0) * 900;  // to 15 min
            const char *sid = jstr(json_object_get(ev->json, "session"), "id");
            if (sid[0] && app.chat.session_id[0] && strcmp(sid, app.chat.session_id)) session_changed(sid);
        }
        if (!strcmp(t, "sessions.messages_r")) {
            chat_load_history(&app.chat, json_object_get(ev->json, "items"), app.tz_off);
            if (app.session_switch) {
                app.session_switch = false;
                const char *name = app.chat.title[0] ? app.chat.title : app.switch_name;
                if (!app.chat.n) chat_notice(&app.chat, "New session started.");
                else if (name[0]) chat_notice(&app.chat, "Now in \"%s\".", name);
                else chat_notice(&app.chat, "Now in an earlier session.");
                app.switch_name[0] = 0;
            }
        } else {
            chat_handle(&app.chat, ev->json);
            if (!strcmp(t, "msg")) autoplay(ev->json);
        }
        if (app.chat.replied) {
            app.chat.replied = false;
            if (!voice_active()) sfx_play(SFX_REPLY);  // not into the mic
        }
        if (!asking && (app.chat.approval || app.chat.clarify)) {  // Hermes is waiting: show the question
            viewer_close();
            camera_close();
            if (app.tab != TAB_CHAT) tabs_switch(TAB_CHAT);
            sfx_play(SFX_ATTENTION);
        }
        if (app.chat.want_status) {
            app.chat.want_status = false;
            app_request("status", NULL);
        }
        break;
    }
    case NET_EV_DISCOVERED:
        if (app.screen != SCR_PAIR) break;
        if (app.manual_lookup) {
            app.manual_lookup = false;
            if (ev->nfound > 0 && parse_here(&ev->found[0], &app.cand)) {
                app.pstep = PAIR_LIST;  // what shows behind the keyboard and if it is cancelled
                ask_code();
            } else {
                snprintf(app.pair_msg, sizeof app.pair_msg,
                         "No Hermes answered at that address. Is the gateway running, and is UDP 7334 allowed "
                         "through the PC's firewall?");
                app.pstep = PAIR_ERROR;
            }
            break;
        }
        app.nfound = 0;
        for (int i = 0; i < ev->nfound; i++)
            if (parse_here(&ev->found[i], &app.found_info[app.nfound])) app.found[app.nfound++] = ev->found[i];
        app.sel = 0;
        app.pstep = PAIR_LIST;
        break;
    case NET_EV_PAIRED:
        app.server = ev->server;
        app.have_server = true;
        if (!store_save_server(&app.server)) app_toast("Couldn't save to the SD card");
        chat_clear(&app.chat);
        app.chat.history_loaded = false;
        chat_notice(&app.chat, "Paired with %s.", app.server.name);
        app.screen = SCR_CHAT;
        net_connect(&app.server);
        break;
    case NET_EV_PAIR_FAILED:
        snprintf(app.pair_msg, sizeof app.pair_msg, "%s", ev->detail);
        app.pstep = PAIR_ERROR;
        break;
    case NET_EV_MOVED:
        snprintf(ev->server.name, sizeof ev->server.name, "%s", app.server.name);
        app.server = ev->server;
        store_save_server(&app.server);
        break;
    case NET_EV_AWAY:
        if (memcmp(ev->server.key, app.server.key, 32)) break;
        memcpy(app.server.away_host, ev->server.away_host, sizeof app.server.away_host);
        app.server.away_port = ev->server.away_port;
        store_save_server(&app.server);
        break;
    }
}

bool app_animating(void)
{
    u64 now = osGetTime();
    if (app.toast_until > now) return true;
    if (app.screen == SCR_CHAT && app.tab != TAB_CHAT && tabs_animating()) return true;
    if (app.screen == SCR_PAIR) return app.pstep == PAIR_SCAN || app.pstep == PAIR_WORKING;
    if (app.chat.approval || app.chat.clarify) return true;  // the "waiting m:ss" clock
    if (blob_busy() || upload_busy()) return true;           // loading spinners, save and upload progress
    if (voice_active() || camera_animating()) return true;   // the level meter, the camera preview
    if (audio_animating()) return true;                      // the play position
    if (app.chat.typing || app.net == NET_CONNECTING || app.net == NET_WAITING) return true;
    for (int i = app.chat.n - 1; i >= 0 && i >= app.chat.n - 8; i--)
        if (!app.chat.m[i]->final || (app.chat.m[i]->role == R_TOOL && app.chat.m[i]->running)) return true;
    return false;
}

// --- lifecycle --------------------------------------------------------------

void app_init(void)
{
    chat_init(&app.chat);
    app.dirty = true;
    store_load_settings(&app.settings);
    sfx_set_muted(!app.settings.sounds);
    app.have_server = store_load_server(&app.server);
    if (app.have_server) {
        app.screen = SCR_CHAT;
        net_connect(&app.server);
    } else {
        start_scan();
    }
}

void app_exit(void)
{
    voice_exit();
    camera_close();
    upload_exit();
    for (int k = 0; k < MEDIA_MAX; k++) {
        json_decref(media[k].fields);
        free(media[k].text);
    }
    sfx_exit();    // before audio_exit: both use the DSP
    audio_exit();  // before blob_exit: it closes its stream
    blob_exit();
    chat_free(&app.chat);
}
