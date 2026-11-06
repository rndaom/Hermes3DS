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
#include "speak.h"
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
    utf8_copy(app.toast, sizeof app.toast, msg);  // (a bot's name in it may be cut short)
    app.toast_until = osGetTime() + 2500;
}

static uint32_t next_id(void) { return ++app.next_id; }

// --- agents (protocol.md §4.8) -----------------------------------------------

Agent *app_agent(void) { return &app.agents[app.cur]; }

const char *agent_title(const Agent *a) { return a->title[0] ? a->title : a->id[0] && a != app.agents ? a->id : "Hermes"; }

static bool asks(const Agent *a) { return a->chat.approval || a->chat.clarify; }

bool app_any_waiting(void)
{
    for (int i = 0; i < app.nagents; i++)
        if (asks(&app.agents[i])) return true;
    return false;
}

// Requests about one agent's chat, sessions, skills, jobs or model. The rest (uploads, files, the roster) are
// the 3DS's own.
static bool scoped(const char *t)
{
    static const char *const T[] = {"send", "stop", "status", "approve", "answer", "commands.list", "models.list",
                                    "skills.list", "tools.list"};
    if (!strncmp(t, "sessions.", 9) || !strncmp(t, "jobs.", 5)) return true;
    for (size_t i = 0; i < sizeof T / sizeof *T; i++)
        if (!strcmp(t, T[i])) return true;
    return false;
}

// A bot's requests and sends carry its name; the main agent's carry none (what older plugins expect).
static void tag(json_t *m, const Agent *a)
{
    if (a && a != app.agents && a->id[0]) json_object_set_new(m, "agent", json_string(a->id));
}

// Room for a bot that must be kept (it asks something, you just made it) when every slot is taken: an idle bot
// makes way (its chat loads again from the PC when it is next picked), one the PC no longer lists first. Never
// the chat showing or one asking you something. NULL when there is none.
static Agent *evict(void)
{
    int best = 0, best_score = 0;
    for (int i = app.nagents - 1; i >= 1; i--) {
        const Agent *a = &app.agents[i];
        if (i == app.cur || asks(a)) continue;
        int score = 1 + (a->listed ? 0 : 2) + (a->chat.typing || a->unread ? 0 : 1);
        if (score > best_score) best = i, best_score = score;
    }
    if (!best) return NULL;
    chat_free(&app.agents[best].chat);
    return &app.agents[best];
}

// The agent an event or reply is for: no tag is the main agent. With `add`, a bot not in the roster yet gets a
// place (an event from a bot made on the PC since, or one past the 3DS's AGENT_MAX); with `urgent` too, an idle
// bot makes way when the roster is full. A reply for a bot the roster dropped finds none.
static Agent *agent_for_wire(const char *id, bool add, bool urgent)
{
    if (!id[0] || !strcmp(id, app.agents[0].id)) return &app.agents[0];
    for (int i = 1; i < app.nagents; i++)
        if (!strcmp(app.agents[i].id, id)) return &app.agents[i];
    if (!add) return NULL;
    Agent *a = app.nagents < AGENT_MAX ? &app.agents[app.nagents++] : urgent ? evict() : NULL;
    if (!a) return NULL;
    memset(a, 0, sizeof *a);
    snprintf(a->id, sizeof a->id, "%s", id);
    chat_init(&a->chat);
    app_refresh_agents();  // its title and picture (unless a roster is on its way already)
    return a;
}

Agent *agent_by_id(const char *id)
{
    if (!id[0]) return &app.agents[0];
    for (int i = 1; i < app.nagents; i++)
        if (!strcmp(app.agents[i].id, id)) return &app.agents[i];
    return NULL;
}

bool app_is_current(const json_t *msg)
{
    const char *id = jstr(msg, "agent");
    return app.cur ? !strcmp(id, app_agent()->id) : !id[0] || !strcmp(id, app.agents[0].id);
}

uint32_t app_request_for(const Agent *a, const char *t, json_t *fields)
{
    uint32_t id = next_id();
    json_t *m = fields ? fields : json_object();
    json_object_set_new(m, "t", json_string(t));
    json_object_set_new(m, "id", json_integer(id));
    if (scoped(t)) tag(m, a);
    net_send(m);
    return id;
}

uint32_t app_request(const char *t, json_t *fields) { return app_request_for(app_agent(), t, fields); }

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
    tag(m, app_agent());
    net_send(m);
    chat_add_user(app.chat, text, id);
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
    char agent[68];  // whose chat it was sent in ("" for the main agent)
} media[MEDIA_MAX];

static void media_done(uint32_t local, bool ok, const char *why)
{
    int k = 0;
    while (k < MEDIA_MAX && media[k].local != local) k++;
    if (k == MEDIA_MAX) return;
    Agent *a = agent_by_id(media[k].agent);  // NULL: that bot left the gateway meanwhile
    Chat *c = a ? &a->chat : app.chat;
    Msg *m = a ? chat_find_local(c, local) : NULL;  // NULL when it scrolled out or the session changed
    json_t *f = media[k].fields;
    if (ok && !a) {
        ok = false;
        why = "that bot is no longer on the PC";
    }
    if (ok) {
        if (media[k].text && media[k].text[0]) json_object_set_new(f, "text", json_string(media[k].text));
        uint32_t req = app_request_for(a, "send", f);
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
        chat_notice(c, "That didn't reach the PC: %s", why && why[0] ? why : "the upload failed");
        sfx_play(SFX_ERROR);
    }
    free(media[k].text);
    media[k].local = 0;
    media[k].fields = NULL;
    media[k].text = NULL;
    c->version++;
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
    Msg *m = chat_add_user(app.chat, text ? text : "", 0);
    m->local_id = local;
    m->attach = json_pack("[o]", attach);
    snprintf(m->upload_id, sizeof m->upload_id, "%s", id);
    media[k].local = local;
    media[k].fields = fields;
    snprintf(media[k].agent, sizeof media[k].agent, "%s", app.cur ? app_agent()->id : "");
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
// It is set per chat on the PC: the selected agent's changes now, the others' when they are next opened.
void app_set_speak(bool on)
{
    app.settings.speak_replies = on;
    if (!store_save_settings(&app.settings)) app_toast("Couldn't save to the SD card");
    for (int i = 0; i < app.nagents; i++) app.agents[i].voice_synced = false;
    if (app.net != NET_ONLINE) {
        app_toast(on ? "Spoken replies on (tells Hermes when connected)" : "Spoken replies off");
        return;
    }
    Agent *a = app_agent();
    app_request("send", json_pack("{s:s}", "text", on ? "/voice on" : "/voice off"));
    a->voice_known = a->voice_synced = true;
    a->voice = on;
    app_toast(on ? "Spoken replies on" : "Spoken replies off");
}

// Bring Hermes' voice mode in an agent's chat in line with Spoken replies, when the PC said it differs.
static void sync_voice(Agent *a)
{
    if (app.net != NET_ONLINE || a->voice_synced || !a->voice_known) return;
    a->voice_synced = true;
    if (a->voice == app.settings.speak_replies) return;
    app_request_for(a, "send", json_pack("{s:s}", "text", app.settings.speak_replies ? "/voice on" : "/voice off"));
    a->voice = app.settings.speak_replies;
}

// The first look at an agent on this connection: its status (title, model, session) and so its history.
static void agent_online(Agent *a)
{
    if (app.net != NET_ONLINE) return;
    if (!a->status_asked) {
        a->status_asked = true;
        app_request_for(a, "status", NULL);
    }
    sync_voice(a);
}

static void point_at(int i)
{
    if (i != app.cur || app.chat != &app.agents[i].chat) app.chat_shown_at = osGetTime();
    app.cur = i;
    app.chat = &app.agents[i].chat;
}

// The chat showing has the user's attention: a question being answered, the mic or the camera open for it.
static bool busy_here(void) { return app.chat->approval || app.chat->clarify || voice_active() || camera_is_open(); }

void app_select_agent(int i)
{
    if (i < 0 || i >= app.nagents) return;
    Agent *a = &app.agents[i];
    a->unread = 0;
    app.dirty = true;
    if (i != app.cur) {
        speak_cancel();  // the other chat's reply isn't spoken over this one
        point_at(i);
        app.scroll = 0;
        tabs_agent_changed();
        snprintf(app.settings.agent, sizeof app.settings.agent, "%s", i ? a->id : "");
        store_save_settings(&app.settings);
    }
    agent_online(a);
}

void app_refresh_agents(void)
{
    if (app.net == NET_ONLINE && !app.agents_req) app.agents_req = app_request("agents.list", NULL);
}

static void fill_agent(Agent *a, const json_t *it)
{
    utf8_copy(a->id, sizeof a->id, jstr(it, "id"));
    utf8_copy(a->title, sizeof a->title, jstr(it, "title"));
    utf8_copy(a->desc, sizeof a->desc, jstr(it, "desc"));
    utf8_copy(a->model, sizeof a->model, jstr(it, "model"));
    a->has_avatar = blob_ref_parse(json_object_get(it, "avatar"), &a->avatar);
    a->primary = json_is_true(json_object_get(it, "primary"));
    a->listed = true;
    a->unreachable = json_is_false(json_object_get(it, "ready"));
    const json_t *v = json_object_get(it, "voice");
    if (json_is_boolean(v) && !a->voice_synced) {
        a->voice_known = true;
        a->voice = json_is_true(v);
    }
}

int app_agent_created(const json_t *item)
{
    Agent *a = agent_for_wire(jstr(item, "id"), true, true);
    if (!a) return -1;
    if (a == app.agents) return 0;
    fill_agent(a, item);
    app_refresh_agents();  // in the PC's order, with its live state
    if (busy_here()) return 0;
    app_select_agent((int)(a - app.agents));
    return 1;
}

// A bot the roster keeps even past AGENT_MAX: the one showing, one asking something, the one to bring back.
static bool must_keep(const char *id, const char *cur_id, const char *restore_id)
{
    const Agent *a = agent_by_id(id);
    return !strcmp(id, cur_id) || !strcmp(id, restore_id) || (a && asks(a));
}

static bool listed_in(const json_t *items, const char *id)
{
    for (size_t k = 0; k < json_array_size(items); k++)
        if (!strcmp(jstr(json_array_get(items, k), "id"), id)) return true;
    return false;
}

// agents.list_r: the main agent stays first, then the bots in the PC's order. Bots it no longer serves go.
void app_agents_reply(json_t *msg)
{
    static Agent next[AGENT_MAX];  // too big for the stack
    app.agents_req = 0;
    app.agents_loaded = true;
    app.agents_old = false;
    app.agents_err[0] = 0;
    app.agents_retry_at = app.agents_retry_ms = 0;
    app.multiplex = json_is_true(json_object_get(msg, "multiplex"));
    char cur_id[68];
    snprintf(cur_id, sizeof cur_id, "%s", app.cur ? app_agent()->id : "");
    // The first roster of a run brings back the bot selected last time, unless you are answering something here
    // (then a later one does).
    bool restore = !app.agents_restored && !cur_id[0] && app.settings.agent[0];
    bool later = restore && busy_here();
    if (later) restore = false;
    const char *restore_id = restore ? app.settings.agent : "";
    bool taken[AGENT_MAX] = {true};
    next[0] = app.agents[0];
    next[0].listed = false;
    int n = 1;
    json_t *items = json_object_get(msg, "items");
    // More bots than fit: those must_keep stay (and those asking that it doesn't list, below), the rest fill the
    // room left in the PC's order.
    int bots = 0, room = AGENT_MAX - 1;
    for (int j = 1; j < app.nagents; j++)
        if (asks(&app.agents[j]) && !listed_in(items, app.agents[j].id)) room--;
    for (size_t k = 0; k < json_array_size(items); k++) {
        json_t *it = json_array_get(items, k);
        if (json_is_true(json_object_get(it, "primary")) || !jstr(it, "id")[0]) continue;
        bots++;
        if (must_keep(jstr(it, "id"), cur_id, restore_id)) room--;
    }
    for (size_t k = 0; k < json_array_size(items); k++) {
        json_t *it = json_array_get(items, k);
        const char *id = jstr(it, "id");
        if (json_is_true(json_object_get(it, "primary"))) {
            fill_agent(&next[0], it);
            continue;
        }
        if (!id[0] || n == AGENT_MAX) continue;
        if (!must_keep(id, cur_id, restore_id) && room-- <= 0) continue;
        int j = 1;
        while (j < app.nagents && (taken[j] || strcmp(app.agents[j].id, id))) j++;
        if (j < app.nagents) {
            taken[j] = true;
            next[n] = app.agents[j];
        } else {
            memset(&next[n], 0, sizeof next[n]);
            chat_init(&next[n].chat);
        }
        fill_agent(&next[n++], it);
    }
    app.agents_more = bots - (n - 1);
    // One the PC doesn't list but whose question is still open stays until it's answered (or the connection
    // drops): it may have been made on the PC after this roster.
    for (int j = 1; j < app.nagents && n < AGENT_MAX; j++)
        if (!taken[j] && asks(&app.agents[j])) {
            taken[j] = true;
            next[n] = app.agents[j];
            next[n++].listed = false;
        }
    for (int j = 1; j < app.nagents; j++)
        if (!taken[j]) chat_free(&app.agents[j].chat);
    memcpy(app.agents, next, (size_t)n * sizeof *next);
    app.nagents = n;
    // Keep the selection, or bring back the bot selected last time. Without multiplex the list holds no bots, so
    // a later roster may still have it; with it, a bot that isn't there is gone and isn't looked for again.
    const char *want = cur_id[0] ? cur_id : restore_id;
    Agent *a = want[0] ? agent_by_id(want) : &app.agents[0];
    if (!later && (!restore || app.multiplex)) app.agents_restored = true;
    if (restore && app.multiplex && !a) {
        app.settings.agent[0] = 0;
        store_save_settings(&app.settings);
    }
    if (!a) {
        a = &app.agents[0];
        if (cur_id[0]) {
            char buf[128], who[33], back_to[33];
            snprintf(buf, sizeof buf, "%s left the gateway; back to %s", utf8_copy(who, sizeof who, cur_id),
                     utf8_copy(back_to, sizeof back_to, agent_title(a)));
            app_toast(buf);
        }
    }
    int i = (int)(a - app.agents);
    point_at(i);
    if (strcmp(cur_id, i ? a->id : "")) {  // a different agent than before
        speak_cancel();
        app.scroll = 0;
        tabs_agent_changed();
        if (cur_id[0]) {
            app.settings.agent[0] = 0;
            store_save_settings(&app.settings);
        }
    }
    agent_online(a);
    app.dirty = true;
}


void app_compose(const char *initial)
{
    char buf[3072], hint[96];
    snprintf(hint, sizeof hint, "Write to %s", agent_title(app_agent()));
    bool ok = app_keyboard(app.chat->typing ? "Queues until this step ends" : hint,
                           initial ? initial : app_agent()->draft, buf, sizeof buf, 1000, true, "Send");
    char *draft = app_agent()->draft;
    if (ok) {
        app_send_text(buf);
        draft[0] = 0;
    } else {
        snprintf(draft, sizeof app_agent()->draft, "%s", buf);  // keep what was typed
    }
}

static void stop_turn(void)
{
    chat_reveal_now(app.chat, true);
    app_request("stop", NULL);
    app_toast("Stopping...");
}

static void approve(const char *choice)
{
    if (!app.chat->approval || app.chat->prompt_req) return;  // one answer at a time
    app.chat->prompt_req =
        app_request("approve", json_pack("{s:s, s:s}", "req", jstr(app.chat->approval, "req"), "choice", choice));
}

static void answer_typed(void)
{
    json_t *cl = app.chat->clarify;
    if (!cl || app.chat->prompt_req) return;
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
    if (f && app.chat->clarify == cl) app.chat->prompt_req = app_request("answer", f);  // (still the same question)
    else json_decref(f);
}

// Multi-select: send the ticked choices.
static void answer_ticked(void)
{
    json_t *cl = app.chat->clarify;
    if (!cl || !clar_multi() || app.chat->prompt_req) return;
    json_t *picks = json_array();
    for (int i = 0; i < clar_count() && i < 32; i++)
        if (app.chat->clar_sel >> i & 1) json_array_append_new(picks, json_integer(i));
    if (!json_array_size(picks)) {
        json_decref(picks);
        app_toast("Tick at least one, or type an answer");
        return;
    }
    app.chat->prompt_req = app_request("answer", json_pack("{s:s, s:o}", "cid", jstr(cl, "cid"), "choices", picks));
}

// A on (or a tap of) choice i: answers a single-choice question, ticks a multi-select one.
static void clar_pick(int i)
{
    json_t *cl = app.chat->clarify;
    if (!cl || i < 0 || i >= clar_count() || app.chat->prompt_req) return;
    app.chat->clar_cur = i;
    if (clar_multi()) {
        if (i < 32) app.chat->clar_sel ^= 1u << i;
        return;
    }
    app.chat->prompt_req = app_request("answer", json_pack("{s:s, s:i}", "cid", jstr(cl, "cid"), "choice", i));
}

// A question panel ignores A, B, X, Y and touches for its first PROMPT_ARM_MS: its rows lie where the input box
// and the Talk / Photo tiles were, and A is otherwise "type a message", so a tap or press meant for those, made
// just as the panel appeared, must not answer it (allow a command, pick a choice). B too: it is the stop key, and
// a press meant for the chat before a switch to a bot that asks must not deny that bot's command.
#define PROMPT_ARM_MS 700

static bool prompt_armed(void)
{
    u64 since = app.chat->prompt_since > app.chat_shown_at ? app.chat->prompt_since : app.chat_shown_at;
    return osGetTime() - since >= PROMPT_ARM_MS;
}

static void primary(void)
{
    if ((app.chat->approval || app.chat->clarify) && !prompt_armed()) return;
    if (app.chat->approval) approve("once");
    else if (app.chat->clarify) clar_count() ? clar_pick(app.chat->clar_cur) : answer_typed();
    else if (app.net == NET_REFUSED) start_scan();
    else app_compose(NULL);
}

static void back(void)
{
    if (app.chat->approval) {
        if (prompt_armed()) approve("deny");
    } else if (app.chat->typing) stop_turn();
    else chat_reveal_now(app.chat, false);  // skip the rest of a reply that is still typing out
}

static void chat_touch(int x, int y)
{
    BottomLayout l;
    bottom_layout(&l);
    if ((app.chat->approval || app.chat->clarify) && !prompt_armed()) return;
    if (app.chat->approval) {  // the approval panel owns the screen
        for (int i = 0; i < APPR_N; i++)
            if (l.appr[i].w > 0 && rect_hit(&l.appr[i], x, y)) approve(APPR_CHOICE[i]);
        return;
    }
    if (app.chat->clarify) {  // so does the clarify panel
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

// A message that is only a spoken reply (voice audio, no text): the reply's text already played the reply sound.
static bool spoken_only(const json_t *msg)
{
    const json_t *attach = json_object_get(msg, "attach");
    if (jstr(msg, "text")[0] || !json_array_size(attach)) return false;
    for (size_t i = 0; i < json_array_size(attach); i++)
        if (!json_is_true(json_object_get(json_array_get(attach, i), "voice"))) return false;
    return true;
}

const json_t *app_chat_audio(void)
{
    const json_t *newest = speak_current();  // a reply being spoken has no bubble yet: Y pauses it
    if (newest) return newest;
    AudioStatus s;
    for (int i = 0; i < app.chat->n; i++)
        for (size_t k = 0; k < json_array_size(app.chat->m[i]->attach); k++) {
            const json_t *a = json_array_get(app.chat->m[i]->attach, k);
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
    if (app.chat->clarify && !app.chat->approval && clar_count()) {  // the D-pad moves through the choices
        int n = clar_count();
        if (down & KEY_DUP) app.chat->clar_cur = (app.chat->clar_cur + n - 1) % n;
        if (down & KEY_DDOWN) app.chat->clar_cur = (app.chat->clar_cur + 1) % n;
        if ((down & KEY_X) && prompt_armed()) answer_ticked();
        if ((down & KEY_Y) && prompt_armed()) answer_typed();
        down &= ~(u32)(KEY_DUP | KEY_DDOWN);
        held &= ~(u32)(KEY_DUP | KEY_DDOWN);
    } else if (app.chat->clarify && (down & KEY_Y)) {
        if (prompt_armed()) answer_typed();
    } else if (!app.chat->approval && !app.chat->clarify && (down & KEY_X)) {
        viewer_open(-1);
    } else if (!app.chat->approval && !app.chat->clarify && (down & KEY_Y)) {
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
            int k = t.px * TAB_COUNT / BOT_W;
            tabs_switch((Tab)(k < TAB_COUNT ? k : TAB_COUNT - 1));
            return;
        }
        touch = &t;
    }
    if (app.tab == TAB_CHAT) chat_input(down, held, touch);
    else tabs_input(down, held, touch);
}

static void load_history(Agent *a, const char *sid)
{
    a->history_asked = true;
    a->history_req = app_request_for(a, "sessions.messages", json_pack("{s:s, s:i}", "sid", sid, "limit", 20));
}

// The session changed (/new, /resume, the Sessions tab): show its history instead of the old transcript.
static void session_changed(Agent *a, const char *sid)
{
    chat_clear_transcript(&a->chat);
    if (a == app_agent()) app.scroll = 0;
    a->chat.session_switch = true;
    a->chat.history_loaded = false;
    load_history(a, sid);
}

// Hermes asks something in a chat that isn't showing: go there, unless the one showing asks too (or the mic
// is open for it), then the Bots tab marks it.
static bool show_asking(Agent *a)
{
    if (a == app_agent()) return true;
    if (busy_here()) {
        char buf[96], who[41];
        snprintf(buf, sizeof buf, "%s needs you too: see Bots", utf8_copy(who, sizeof who, agent_title(a)));
        app_toast(buf);
        return false;
    }
    viewer_close();
    app_select_agent((int)(a - app.agents));
    return true;
}

// Remembers the approval and question in a's chat (protocol.md §4.5 `req`, `cid`). True when one is new: not
// one the PC re-sent after a reconnect.
static bool note_prompt(Agent *a)
{
    bool fresh = false;
    const char *req = jstr(a->chat.approval, "req"), *cid = jstr(a->chat.clarify, "cid");
    if (a->chat.approval && (!req[0] || strncmp(req, a->seen_req, sizeof a->seen_req - 1))) {
        fresh = true;
        utf8_copy(a->seen_req, sizeof a->seen_req, req);
    }
    if (a->chat.clarify && (!cid[0] || strncmp(cid, a->seen_cid, sizeof a->seen_cid - 1))) {
        fresh = true;
        utf8_copy(a->seen_cid, sizeof a->seen_cid, cid);
    }
    return fresh;
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
        if (app.net == NET_ONLINE && ev->state != NET_ONLINE)
            for (int i = 0; i < app.nagents; i++) {
                Agent *a = &app.agents[i];
                chat_link_lost(&a->chat);
                a->status_asked = a->history_asked = a->voice_synced = false;
            }
        if (ev->state != NET_ONLINE) app.agents_req = app.agents_retry_at = 0;  // welcome asks again
        app.net = ev->state;
        blob_online(ev->state == NET_ONLINE);
        upload_online(ev->state == NET_ONLINE);
        app.retry_s = ev->retry_s;
        app.retry_at = osGetTime() + (u64)ev->retry_s * 1000;
        snprintf(app.net_detail, sizeof app.net_detail, "%s", ev->detail);
        if (ev->state == NET_REFUSED) {
            if (!strcmp(ev->detail, "not_paired"))
                chat_notice(app.chat, "%s doesn't know this 3DS any more. Press SELECT to pair again.",
                            app.server.name[0] ? app.server.name : "The PC");
            else chat_notice(app.chat, "The PC refused the connection: %s", ev->detail);
        }
        if (ev->state != NET_ONLINE)  // re-sent after welcome if still on
            for (int i = 0; i < app.nagents; i++) app.agents[i].chat.typing = false;
        break;
    case NET_EV_WELCOME: {
        if (memcmp(ev->server.key, app.server.key, 32)) break;  // a late welcome from the PC before a re-pair
        for (int i = 0; i < app.nagents; i++) chat_reconnected(&app.agents[i].chat);
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
        // The main agent's chat. Hermes' voice mode there (plugins that report it): in line with "Spoken replies".
        Agent *main = &app.agents[0];
        json_t *speaks = json_object_get(json_object_get(ev->json, "chat"), "voice");
        if (json_is_boolean(speaks)) {
            main->voice_known = true;
            main->voice = json_is_true(speaks);
            sync_voice(main);
        }
        json_t *sess = json_object_get(json_object_get(ev->json, "chat"), "session");
        const char *sid = jstr(sess, "id");
        if (sid[0]) snprintf(main->chat.session_id, sizeof main->chat.session_id, "%s", sid);
        if (!main->chat.history_loaded && sid[0]) load_history(main, sid);
        main->status_asked = true;
        app_request_for(main, "status", NULL);
        agent_online(app_agent());
        app.agents_retry_ms = 0;
        app_refresh_agents();  // the bots (and so whether the one selected last time is still there)
        tabs_online();
        break;
    }
    case NET_EV_JSON: {
        json_t *msg = ev->json;
        if (upload_on_json(msg)) break;                        // photos and voice going up
        if (blob_on_json(msg) || viewer_handle(msg)) break;  // blob transfers, Open on PC
        const char *t = jstr(msg, "t");
        uint32_t id = (uint32_t)json_integer_value(json_object_get(msg, "id"));
        if (!strcmp(t, "agents.list_r")) {
            app_agents_reply(msg);
            break;
        }
        if (id && id == app.agents_req) {  // an error
            app.agents_req = 0;
            utf8_copy(app.agents_err, sizeof app.agents_err, jstr(msg, "msg")[0] ? jstr(msg, "msg") : jstr(msg, "code"));
            if (!strcmp(jstr(msg, "code"), "bad_request")) {  // what a plugin from before bots says to it
                app.agents_loaded = false;
                app.agents_old = true;
                app.agents_more = 0;
                if (app.cur) {  // it would take a bot's messages for the main agent
                    speak_cancel();
                    point_at(0);
                    tabs_agent_changed();
                    app_toast("The PC no longer lists bots: back to the main agent");
                }
            } else {  // one that passes (the gateway still starting, say): keep what is here, ask again shortly
                app.agents_retry_ms = app.agents_retry_ms ? app.agents_retry_ms * 2 : 5000;
                if (app.agents_retry_ms > 60000) app.agents_retry_ms = 60000;
                app.agents_retry_at = osGetTime() + app.agents_retry_ms;
            }
            break;
        }
        if (tabs_handle(msg)) break;  // replies to the other tabs' requests
        bool ask = !strcmp(t, "approval") || !strcmp(t, "clarify");  // (a bot past AGENT_MAX gets a place for it)
        Agent *a = agent_for_wire(jstr(msg, "agent"), !id && (app.multiplex || !app.agents_loaded), ask);
        if (!a) break;  // a bot that left, or more bots than the 3DS keeps
        bool current = a == app_agent();
        Chat *c = &a->chat;
        if (!strcmp(t, "speak")) {  // a part of a spoken reply: played, not shown (only the chat showing speaks)
            speak_part(msg, current);
            break;
        }
        bool asking = c->approval || c->clarify;
        if (!strcmp(t, "status_r")) {
            double now = json_number_value(json_object_get(msg, "now"));
            if (now > 0) app.tz_off = lround(((double)time(NULL) - now) / 900.0) * 900;  // to 15 min
            const char *sid = jstr(json_object_get(msg, "session"), "id");
            if (sid[0] && c->session_id[0] && strcmp(sid, c->session_id)) session_changed(a, sid);
            else if (sid[0] && !c->history_loaded && !a->history_asked) load_history(a, sid);  // a bot's first look
        }
        if (!strcmp(t, "sessions.messages_r")) {
            if (!id || id != a->history_req) break;  // a Sessions preview the tabs gave up on
            a->history_req = 0;
            chat_load_history(c, json_object_get(msg, "items"), app.tz_off);
            if (c->session_switch) {
                c->session_switch = false;
                const char *name = c->title[0] ? c->title : current ? app.switch_name : "";
                if (!c->n) chat_notice(c, "New session started.");
                else if (name[0]) chat_notice(c, "Now in \"%s\".", name);
                else chat_notice(c, "Now in an earlier session.");
                if (current) app.switch_name[0] = 0;
            }
        } else {
            chat_handle(c, msg);
            if (!strcmp(t, "msg") && current) speak_arrive(msg);
        }
        if (c->replied) {
            c->replied = false;
            if (!current) {
                a->unread++;
                char buf[128];
                snprintf(buf, sizeof buf, "%s replied", agent_title(a));
                app_toast(buf);
            }
            if (!voice_active() && !spoken_only(msg)) sfx_play(SFX_REPLY);  // not into the mic
        }
        bool fresh = note_prompt(a);
        if (!asking && (c->approval || c->clarify) && fresh) {  // Hermes is waiting: show the question
            if (show_asking(a)) {
                viewer_close();
                char buf[128];
                snprintf(buf, sizeof buf, "Camera closed: %s is asking you something", agent_title(a));
                if (camera_is_open()) app_toast(buf);
                camera_close();
                if (app.tab != TAB_CHAT) tabs_switch(TAB_CHAT);
            }
            sfx_play(SFX_ATTENTION);
        }
        if (c->want_status) {
            c->want_status = false;
            app_request_for(a, "status", NULL);
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
        for (int i = 1; i < app.nagents; i++) chat_free(&app.agents[i].chat);  // another PC's bots
        app.nagents = 1;
        {
            Chat keep = app.agents[0].chat;  // cleared below; the rest was the other PC's main agent
            memset(&app.agents[0], 0, sizeof app.agents[0]);
            app.agents[0].chat = keep;
        }
        app.agents_loaded = app.agents_old = app.agents_restored = app.multiplex = false;
        app.agents_more = 0;
        app.agents_err[0] = 0;
        app.agents_req = app.agents_retry_at = app.agents_retry_ms = 0;
        app.settings.agent[0] = 0;  // a bot of the PC before
        store_save_settings(&app.settings);
        speak_cancel();
        point_at(0);
        tabs_agent_changed();
        chat_clear(app.chat);
        app.chat->history_loaded = false;
        chat_notice(app.chat, "Paired with %s.", app.server.name);
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

void app_tick(void)
{
    if (app.agents_retry_at && osGetTime() >= app.agents_retry_at) {
        app.agents_retry_at = 0;
        app_refresh_agents();
    }
}

bool app_animating(void)
{
    u64 now = osGetTime();
    if (app.toast_until > now) return true;
    if (app.screen == SCR_CHAT && app.tab != TAB_CHAT && tabs_animating()) return true;
    if (app.screen == SCR_PAIR) return app.pstep == PAIR_SCAN || app.pstep == PAIR_WORKING;
    if (app.chat->approval || app.chat->clarify) return true;  // the "waiting m:ss" clock
    if (blob_busy() || upload_busy()) return true;           // loading spinners, save and upload progress
    if (voice_active() || camera_animating()) return true;   // the level meter, the camera preview
    if (audio_animating()) return true;                      // the play position
    if (app.chat->typing || app.net == NET_CONNECTING || app.net == NET_WAITING) return true;
    for (int i = app.chat->n - 1; i >= 0 && i >= app.chat->n - 8; i--)
        if (!app.chat->m[i]->final || (app.chat->m[i]->role == R_TOOL && app.chat->m[i]->running)) return true;
    return false;
}

// --- lifecycle --------------------------------------------------------------

void app_init(void)
{
    app.nagents = 1;
    chat_init(&app.agents[0].chat);
    point_at(0);
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
    camera_exit();
    upload_exit();
    for (int k = 0; k < MEDIA_MAX; k++) {
        json_decref(media[k].fields);
        free(media[k].text);
    }
    speak_exit();  // before audio_exit: its prefetches are blob streams
    sfx_exit();    // before audio_exit: both use the DSP
    audio_exit();  // before blob_exit: it closes its stream
    blob_exit();
    for (int i = 0; i < app.nagents; i++) chat_free(&app.agents[i].chat);
}
