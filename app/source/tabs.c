// Bots, Sessions, Skills, Jobs and More (design/mockups: Sessions, Commands). The bottom screen holds the list or
// grid; the top screen shows the selected item. Lists come from protocol.md §4.6 requests and are
// refetched when a tab opens if they are older than a minute or the connection was re-made.
#include "tabs.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "codec.h"
#include "camera.h"
#include "ui.h"

#define ROW_H 30
#define STALE_MS 60000

static const char *jstr(const json_t *o, const char *k)
{
    const char *s = json_string_value(json_object_get(o, k));
    return s ? s : "";
}

static double jnum(const json_t *o, const char *k) { return json_number_value(json_object_get(o, k)); }

// --- remote lists -----------------------------------------------------------

typedef struct {
    const char *req_t;  // request type
    json_t *items;      // reply items (owned)
    uint32_t req;       // request in flight, 0 = none
    bool loaded, stale;
    u64 at;  // when the reply arrived
    char err[120];
    int sel, top;  // selection and first visible row
} List;

enum { LS_SESSIONS, LS_SKILLS, LS_JOBS, LS_MODELS, LS_COMMANDS, LS_PREVIEW, LS_N };

static List lists[LS_N] = {
    [LS_SESSIONS] = {.req_t = "sessions.list"},   [LS_SKILLS] = {.req_t = "skills.list"},
    [LS_JOBS] = {.req_t = "jobs.list"},           [LS_MODELS] = {.req_t = "models.list"},
    [LS_COMMANDS] = {.req_t = "commands.list"},   [LS_PREVIEW] = {.req_t = "sessions.messages"},
};

static json_t *status;       // last status_r
static uint32_t act_req;     // resume / new / job action in flight
static char sess_q[192];     // session search (60 UTF-16 units)
static char preview_sid[64]; // session the preview list belongs to (or is being fetched for)
static u64 preview_due;      // fetch the preview at this time (debounces fast scrolling), 0 = none
static bool picker;          // More: the model picker is open on the top screen
static int cat, cell;        // More: command category and grid cell
static u64 yolo_armed;       // More: /yolo needs a second press

// Bots: the roster lives in app.agents (app.c); this is the tab's own state.
typedef enum { BOTS_LIST, BOTS_CONFIRM, BOTS_CREATING } BotsStep;
static BotsStep bstep;
static int bot_sel, bot_top;
static char bot_sel_id[68];  // the selected row's agent ("" = main), which a new roster may move
static void bots_select(int i);
static uint32_t create_req;
static char new_name[68], new_desc[400], create_err[200];

static int count(int k) { return (int)json_array_size(lists[k].items); }
static json_t *item(int k, int i) { return json_array_get(lists[k].items, (size_t)i); }
static json_t *selected(int k) { return item(k, lists[k].sel); }

static void fetch(int k, json_t *fields)
{
    List *l = &lists[k];
    if (app.net != NET_ONLINE) {
        json_decref(fields);
        if (!l->loaded) snprintf(l->err, sizeof l->err, "Not connected to the PC.");
        return;
    }
    l->req = app_request(l->req_t, fields);
    l->err[0] = 0;
}

static void refresh(int k)
{
    List *l = &lists[k];
    if (l->req) return;
    if (k == LS_SESSIONS)
        fetch(k, sess_q[0] ? json_pack("{s:i, s:s}", "limit", 50, "q", sess_q) : json_pack("{s:i}", "limit", 50));
    else fetch(k, NULL);
}

static void refresh_if_old(int k)
{
    List *l = &lists[k];
    if (!l->loaded || l->stale || osGetTime() - l->at > STALE_MS) refresh(k);
}

static void clamp_sel(List *l, int n)
{
    if (l->sel >= n) l->sel = n - 1;
    if (l->sel < 0) l->sel = 0;
}

// The selected session changed: fetch its last messages for the top screen shortly.
static void want_preview(void)
{
    const char *sid = jstr(selected(LS_SESSIONS), "id");
    if (!sid[0] || !strcmp(sid, preview_sid)) return;
    snprintf(preview_sid, sizeof preview_sid, "%s", sid);
    json_decref(lists[LS_PREVIEW].items);
    lists[LS_PREVIEW].items = NULL;
    lists[LS_PREVIEW].loaded = false;
    lists[LS_PREVIEW].err[0] = 0;
    preview_due = osGetTime() + 250;
}

// models.list_r groups models by provider; the picker wants one row per model.
static json_t *flatten_models(json_t *reply)
{
    json_t *out = json_array();
    json_t *provs = json_object_get(reply, "items");
    for (size_t p = 0; p < json_array_size(provs); p++) {
        json_t *pv = json_array_get(provs, p);
        json_t *models = json_object_get(pv, "models");
        for (size_t m = 0; m < json_array_size(models); m++) {
            json_t *md = json_array_get(models, m);
            json_array_append_new(out, json_pack("{s:s, s:s, s:s, s:b}", "id", jstr(md, "id"), "provider",
                                                 jstr(pv, "provider"), "name", jstr(pv, "name"), "current",
                                                 json_is_true(json_object_get(md, "current"))));
        }
    }
    return out;
}

bool tabs_handle(json_t *msg)
{
    const char *t = jstr(msg, "t");
    if (!strcmp(t, "status_r")) {
        if (app_is_current(msg)) {
            json_decref(status);
            status = json_incref(msg);
        }
        return false;  // app.c uses it too
    }
    uint32_t id = (uint32_t)json_integer_value(json_object_get(msg, "id"));
    if (!id) return false;
    bool error = !strcmp(t, "error");
    const char *emsg = jstr(msg, "msg")[0] ? jstr(msg, "msg") : jstr(msg, "code");
    if (id == create_req) {
        create_req = 0;
        char buf[160], name[51];
        if (error) {
            utf8_copy(create_err, sizeof create_err, emsg);
            bstep = BOTS_CONFIRM;  // (the Bots tab shows why; elsewhere a toast says it)
            if (app.tab != TAB_BOTS) {
                snprintf(buf, sizeof buf, "Couldn't make %s: %s", new_name, emsg);
                app_toast(buf);
            }
        } else {
            json_t *it = json_object_get(msg, "item");
            utf8_copy(name, sizeof name, jstr(it, "title")[0] ? jstr(it, "title") : jstr(it, "id"));
            bstep = BOTS_LIST;
            int placed = app_agent_created(it);
            if (placed > 0) {
                snprintf(buf, sizeof buf, "%s is ready", name);
                if (app.tab == TAB_BOTS) tabs_switch(TAB_CHAT);  // (not when you went on to another tab)
            } else if (!placed) {  // you're answering something here: it waits in Bots
                snprintf(buf, sizeof buf, "%s is ready: see Bots", name);
            } else {
                snprintf(buf, sizeof buf, "%s is ready, but the 3DS has no room for another bot", name);
            }
            app_toast(buf);
        }
        app.dirty = true;
        return true;
    }
    for (int k = 0; k < LS_N; k++) {
        List *l = &lists[k];
        if (l->req != id) continue;
        l->req = 0;
        if (k == LS_PREVIEW && strcmp(jstr(selected(LS_SESSIONS), "id"), preview_sid)) return true;  // moved on
        if (error) {
            snprintf(l->err, sizeof l->err, "%s", emsg);
            return true;
        }
        json_decref(l->items);
        l->items = k == LS_MODELS ? flatten_models(msg) : json_incref(json_object_get(msg, "items"));
        l->loaded = true;
        l->stale = false;
        l->at = osGetTime();
        if (k == LS_MODELS)
            for (int i = 0; i < count(k); i++)
                if (json_is_true(json_object_get(item(k, i), "current"))) l->sel = i;
        clamp_sel(l, count(k));
        if (k == LS_SESSIONS) want_preview();
        app.dirty = true;
        return true;
    }
    if (id != act_req) return false;
    act_req = 0;
    if (error) {
        app_toast(emsg);
    } else if (!strcmp(t, "jobs.action_r")) {
        json_t *job = json_object_get(msg, "job");
        for (int i = 0; i < count(LS_JOBS); i++)
            if (!strcmp(jstr(item(LS_JOBS, i), "id"), jstr(job, "id")))
                json_array_set(lists[LS_JOBS].items, (size_t)i, job);
    }
    return true;
}

void tabs_online(void)
{
    for (int k = 0; k < LS_N; k++) {
        lists[k].stale = true;
        lists[k].req = 0;  // replies to the old connection never come
    }
    act_req = 0;
    if (create_req) {  // the bot may exist now: the roster says
        create_req = 0;
        bstep = BOTS_LIST;
    }
    if (app.tab != TAB_CHAT) tabs_switch(app.tab);
}

void tabs_agent_changed(void)
{
    for (int k = 0; k < LS_N; k++) {
        List *l = &lists[k];
        json_decref(l->items);
        l->items = NULL;
        l->req = 0;  // a reply for the agent before is dropped
        l->loaded = l->stale = false;
        l->err[0] = 0;
        l->sel = l->top = 0;
    }
    json_decref(status);
    status = NULL;
    preview_sid[0] = 0;
    preview_due = 0;
    sess_q[0] = 0;
    picker = false;
    yolo_armed = 0;           // a second /yolo press counts for the same agent only
    app.switch_name[0] = 0;  // a resume asked for in the other agent's Sessions
    if (app.tab != TAB_CHAT && app.tab != TAB_BOTS) tabs_switch(app.tab);
}

void tabs_switch(Tab t)
{
    app.tab = t;
    app.dirty = true;
    picker = false;
    if (t != TAB_BOTS && bstep == BOTS_CONFIRM) bstep = BOTS_LIST;
    switch (t) {
    case TAB_CHAT: break;
    case TAB_BOTS:
        bots_select(app.cur);
        app_refresh_agents();
        break;
    case TAB_SESSIONS:
        refresh(LS_SESSIONS);  // always: the current session and its title move
        break;
    case TAB_SKILLS: refresh_if_old(LS_SKILLS); break;
    case TAB_JOBS: refresh(LS_JOBS); break;
    case TAB_MORE:
        refresh_if_old(LS_COMMANDS);
        if (app.net == NET_ONLINE) app_request("status", NULL);
        break;
    case TAB_COUNT: break;
    }
}

bool tabs_animating(void)
{
    for (int k = 0; k < LS_N; k++)
        if (lists[k].req) return true;
    if (app.tab == TAB_BOTS) {
        if (app.agents_req || create_req) return true;
        for (int i = 0; i < app.nagents; i++)
            if (app.agents[i].chat.typing) return true;
    }
    return preview_due || act_req || yolo_armed > osGetTime();
}

// --- commands (More) --------------------------------------------------------

typedef struct {
    const char *name, *label;
} Cmd;

#define CATS 5
#define CELLS 9
#define CAT_3DS 4  // settings and actions on this 3DS, not Hermes commands
static const char *const CAT_NAMES[CATS] = {"SESSION", "AGENT", "SETUP", "TOOLS", "3DS"};
static const Cmd CMDS[CATS][CELLS] = {
    {{"new", "Fresh session"}, {"retry", "Resend last"}, {"undo", "Back up a turn"},
     {"title", "Name session"}, {"branch", "Fork session"}, {"compress", "Shrink context"},
     {"save", "Export chat"}, {"rollback", "Restore files"}, {"queue", "Next prompt"}},
    {{"model", "Switch model"}, {"reasoning", "Thinking depth"}, {"personality", "Voice and tone"},
     {"fast", "Quick replies"}, {"voice", "Spoken replies"}, {"approvals", "Ask before risky"},
     {"status", "Session info"}, {"usage", "Tokens and cost"}, {"yolo", "Skip approvals"}},
    {{"busy", "While working"}, {"footer", "Reply footer"}, {"memory", "Memory writes"},
     {"reload-skills", "Rescan skills"}, {"reload-mcp", "Reload MCP"}, {"sethome", "Home channel"},
     {"restart", "Restart gateway"}, {"version", "Hermes version"}, {"insights", "Usage stats"}},
    {{"diff", "Git changes"}, {"plan", "Write a plan"}, {"review", "Second opinion"},
     {"bg", "Background task"}, {"btw", "Side question"}, {"agents", "Running tasks"},
     {"goal", "Standing goal"}, {"loop", "Repeat a prompt"}, {"kanban", "Task board"}},
    {{"sounds", "App sounds"}, {"speak", "Spoken replies"}, {"photo", "Take a photo"}, {"talk", "Voice message"}},
};

static bool is_local(const Cmd *c) { return c >= CMDS[CAT_3DS] && c < CMDS[CAT_3DS] + CELLS; }

// A 3DS cell's second line: its state, or what it does.
static const char *local_label(const Cmd *c)
{
    if (!strcmp(c->name, "sounds")) return app.settings.sounds ? "On · A to mute" : "Off · A for sounds";
    if (!strcmp(c->name, "speak")) return app.settings.speak_replies ? "On · A to turn off" : "Off · A to turn on";
    return c->label;
}

static const char *local_desc(const Cmd *c)
{
    if (!strcmp(c->name, "sounds")) return "A short sound when a reply arrives, when Hermes needs an answer, and when a "
                                           "message goes. Saved on this 3DS.";
    if (!strcmp(c->name, "speak"))
        return "Hermes answers voice messages out loud as well (/voice on) and the 3DS plays them as they arrive. "
               "Needs text-to-speech working on the PC.";
    if (!strcmp(c->name, "photo")) return "The outer or inner camera at 640x480. Hermes looks at the photo; add a "
                                          "caption to ask about it.";
    return "Hold Y in the chat, or keep a finger on Talk, and speak. Let go to send; B cancels. Up to 2 minutes; the "
           "PC turns it into text.";
}

static void run_local(const Cmd *c)
{
    if (!strcmp(c->name, "sounds")) {
        app_set_sounds(!app.settings.sounds);
    } else if (!strcmp(c->name, "speak")) {
        app_set_speak(!app.settings.speak_replies);
    } else if (!strcmp(c->name, "photo")) {
        tabs_switch(TAB_CHAT);
        camera_open();
    } else {
        tabs_switch(TAB_CHAT);
        app_toast("Hold " GLYPH_Y " and talk; let go to send");
    }
}

// The commands.list entry for a name (NULL before the list loads or if this Hermes lacks it).
static json_t *cmd_info(const char *name)
{
    for (int i = 0; i < count(LS_COMMANDS); i++)
        if (!strcmp(jstr(item(LS_COMMANDS, i), "name"), name)) return item(LS_COMMANDS, i);
    return NULL;
}

static bool cmd_available(const Cmd *c)
{
    return c->name && (is_local(c) || !lists[LS_COMMANDS].loaded || cmd_info(c->name));
}

static void run_cmd(const Cmd *c, bool with_args)
{
    char buf[64];
    if (is_local(c)) {
        run_local(c);
        return;
    }
    if (!strcmp(c->name, "model") && !with_args) {
        picker = true;
        refresh(LS_MODELS);
        return;
    }
    const char *args = jstr(cmd_info(c->name), "args");
    if (with_args || args[0] == '<') {  // a required argument: let them type it
        snprintf(buf, sizeof buf, "/%s ", c->name);
        app_compose(buf);
        return;
    }
    if (!strcmp(c->name, "yolo") && yolo_armed <= osGetTime()) {
        yolo_armed = osGetTime() + 3000;
        app_toast("YOLO skips every approval. Press again to toggle it.");
        return;
    }
    yolo_armed = 0;
    snprintf(buf, sizeof buf, "/%s", c->name);
    app_send_text(buf);
}

// --- shared list drawing / input ---------------------------------------------

// -1 / +1 for up / down, with auto-repeat while held.
static int nav_step(u32 down, u32 held, u32 up_keys, u32 down_keys)
{
    static u64 since, last;
    int d = (down & up_keys) ? -1 : (down & down_keys) ? 1 : 0;
    if (d) {
        since = last = osGetTime();
        return d;
    }
    u32 h = held & (up_keys | down_keys);
    u64 now = osGetTime();
    if (h && now - since > 350 && now - last > 70) {
        last = now;
        return (h & up_keys) ? -1 : 1;
    }
    return 0;
}

#define UP_KEYS (KEY_DUP | KEY_CPAD_UP)
#define DOWN_KEYS (KEY_DDOWN | KEY_CPAD_DOWN)

// Moves the selection; returns true when it changed.
static bool list_nav(int k, u32 down, u32 held, int rows)
{
    List *l = &lists[k];
    int n = count(k), old = l->sel;
    l->sel += nav_step(down, held, UP_KEYS, DOWN_KEYS);
    if (down & KEY_DLEFT) l->sel -= rows;
    if (down & KEY_DRIGHT) l->sel += rows;
    clamp_sel(l, n);
    return l->sel != old;
}

// A tap on the list: selects a row, or returns true when the selected row was tapped again.
static bool list_tap(int k, const touchPosition *t, float y0, int rows)
{
    List *l = &lists[k];
    if (!t || t->py < y0 || t->py >= y0 + rows * ROW_H || t->px < 8 || t->px >= BOT_W - 8) return false;
    int i = l->top + (int)((t->py - y0) / ROW_H);
    if (i >= count(k)) return false;
    if (i == l->sel) return true;
    l->sel = i;
    return false;
}

typedef void (*RowFn)(json_t *it, const Rect *r, bool sel);

// Panel of rows; loading / error / empty states in its place.
static void draw_list(int k, float y0, int rows, RowFn row, const char *empty)
{
    List *l = &lists[k];
    int n = count(k);
    Rect p = {8, y0, BOT_W - 16, rows * ROW_H};
    rect(p.x, p.y, p.w, p.h, COL_PAPER);
    frame_rect(p.x, p.y, p.w, p.h, COL_RULE);
    if (!n) {
        float y = p.y + p.h / 2 - 6;
        if (l->req) {
            braille(p.x + p.w / 2 - 30, y + 1, 10, COL_BLUE);
            text_draw("Loading...", p.x + p.w / 2 - 16, y, S_SMALL, COL_TX_2);
        } else {
            const char *s = l->err[0] ? l->err : l->loaded ? empty : "Open this tab while connected.";
            text_block(s, p.x + 12, y, p.w - 24, S_SMALL, l->err[0] ? &DIM_COLORS : &BODY_COLORS, 3, NULL);
        }
        return;
    }
    if (l->sel < l->top) l->top = l->sel;
    if (l->sel >= l->top + rows) l->top = l->sel - rows + 1;
    if (l->top > n - rows) l->top = n - rows;
    if (l->top < 0) l->top = 0;
    for (int i = 0; i < rows && l->top + i < n; i++) {
        Rect r = {p.x, p.y + i * ROW_H, p.w, ROW_H};
        bool sel = l->top + i == l->sel;
        if (i) rect(r.x + 1, r.y, r.w - 2, 1, COL_RULE);
        if (sel) {
            rect(r.x, r.y, r.w, r.h, COL_BLUE_SOFT);
            frame_rect(r.x, r.y, r.w, r.h, COL_BLUE);
            frame_rect(r.x + 1, r.y + 1, r.w - 2, r.h - 2, COL_BLUE);
        }
        row(item(k, l->top + i), &r, sel);
    }
    if (n > rows) {  // scroll bar
        float track = p.h - 4, h = track * rows / n, pos = track * l->top / n;
        if (h < 8) h = 8;
        rect(p.x + p.w - 4, p.y + 2 + pos, 2, h, COL_LINE_B);
    }
    if (l->req) braille(p.x + p.w - 14, p.y - 13, 9, COL_BLUE);  // refreshing
}

// Eyebrow above a list: "51 SKILLS".
static void list_eyebrow(const char *s, float y) { spaced(s, 11, y, S_EYE, COL_INK_2, 1.4f); }

static void top_footer(const char *a, const char *x, const char *y_)
{
    float y = footer_begin(), fx = 7;
    if (a) fx += hint(GLYPH_A, a, fx, y, COL_ON);
    if (x) fx += hint(GLYPH_X, x, fx, y, COL_ON);
    if (y_) fx += hint(GLYPH_Y, y_, fx, y, COL_ON);
    const char *r = "Tabs";
    float w = text_width(r, strlen(r), S_HINT);
    text_draw(r, TOP_W - 7 - w, y, S_HINT, COL_ON_3);
    float gw = text_width(GLYPH_L GLYPH_R, strlen(GLYPH_L GLYPH_R), S_HINT * 1.15f);
    text_draw(GLYPH_L GLYPH_R, TOP_W - 7 - w - 3 - gw, y - 1, S_HINT * 1.15f, COL_ON_3);
}

// Big condensed-style title for the top screen.
static void big_title(const char *s, float y)
{
    float sc = S_BIG * 0.72f;
    text_draw_fit(s, PAD_X, y, sc, COL_INK, TOP_W - 2 * PAD_X);
}

// Small tag box: filled (blue, white text) or outlined.
static float tag(const char *s, float x, float y, bool filled)
{
    float w = spaced_width(s, S_EYE, 1.2f) + 8;
    if (filled) rect(x, y, w, 11, COL_BLUE);
    else frame_rect(x, y, w, 11, COL_LINE_B);
    spaced(s, x + 4, y + 1.5f, S_EYE, filled ? COL_ON : COL_BLUE, 1.2f);
    return w;
}

// --- Sessions ----------------------------------------------------------------

#define SESS_Y 66
#define SESS_ROWS 5
static const Rect SEARCH = {8, 33, 226, 26}, NEW_BTN = {240, 33, 72, 26};

static const char *session_title(json_t *it)
{
    const char *s = jstr(it, "title");
    if (!s[0]) s = jstr(it, "preview");
    return s[0] ? s : "Untitled session";
}

static void session_row(json_t *it, const Rect *r, bool sel)
{
    bool cur = json_is_true(json_object_get(it, "current"));
    char when[32];
    if (cur) snprintf(when, sizeof when, "current");
    else when_text(jnum(it, "last_active"), when, sizeof when);
    float ww = text_width(when, strlen(when), S_TOOL);
    if (cur) rect(r->x + 2, r->y + 2, 3, r->h - 4, COL_BLUE);
    text_draw(when, r->x + r->w - 10 - ww, r->y + 10, S_TOOL, cur ? COL_BLUE : COL_TX_3);
    text_draw_fit(session_title(it), r->x + 12, r->y + 8, S_BODY * 0.95f, sel ? COL_INK : COL_INK_2,
                  r->w - 34 - ww);
}

static void resume_selected(void)
{
    json_t *it = selected(LS_SESSIONS);
    if (!it) return;
    if (json_is_true(json_object_get(it, "current"))) {
        tabs_switch(TAB_CHAT);
        return;
    }
    act_req = app_request("sessions.resume", json_pack("{s:s}", "sid", jstr(it, "id")));
    snprintf(app.switch_name, sizeof app.switch_name, "%s", session_title(it));
    app_toast("Resuming...");
    tabs_switch(TAB_CHAT);
}

static void new_session(void)
{
    if (app.net != NET_ONLINE) {
        app_toast("Not connected to the PC");
        return;
    }
    act_req = app_request("sessions.new", NULL);
    app_toast("Starting a new session...");
    tabs_switch(TAB_CHAT);
}

static void search_sessions(void)
{
    char buf[sizeof sess_q];
    if (!app_keyboard("Search sessions", sess_q, buf, sizeof buf, 60, false, "Search")) return;
    snprintf(sess_q, sizeof sess_q, "%s", buf);
    lists[LS_SESSIONS].sel = 0;
    refresh(LS_SESSIONS);
}

static void sessions_input(u32 down, u32 held, const touchPosition *t)
{
    if (list_nav(LS_SESSIONS, down, held, SESS_ROWS)) want_preview();
    if (down & KEY_A) resume_selected();
    if (down & KEY_X) new_session();
    if (down & KEY_Y) search_sessions();
    if (down & KEY_B) {
        if (sess_q[0]) {  // clear the search first
            sess_q[0] = 0;
            refresh(LS_SESSIONS);
        } else {
            tabs_switch(TAB_CHAT);
        }
    }
    if (t) {
        if (rect_hit(&SEARCH, t->px, t->py)) search_sessions();
        else if (rect_hit(&NEW_BTN, t->px, t->py)) new_session();
        else if (list_tap(LS_SESSIONS, t, SESS_Y, SESS_ROWS)) resume_selected();
        else want_preview();
    }
    // While an earlier preview is still on its way, wait for it (its reply is dropped if the selection moved on)
    // and then fetch this one.
    if (preview_due && osGetTime() >= preview_due && !lists[LS_PREVIEW].req) {
        preview_due = 0;
        if (preview_sid[0]) fetch(LS_PREVIEW, json_pack("{s:s, s:i}", "sid", preview_sid, "limit", 8));
    }
}

static void sessions_bottom(void)
{
    // Search box: paper, ink border, hard blue shadow (as the chat input).
    const Rect *s = &SEARCH;
    rect(s->x + 2, s->y + 2, s->w, s->h, COL_BLUE);
    rect(s->x, s->y, s->w, s->h, COL_PAPER);
    frame_rect(s->x, s->y, s->w, s->h, COL_INK);
    frame_rect(s->x + 9, s->y + 8, 8, 8, COL_BLUE);  // magnifier
    C2D_DrawLine(s->x + 16, s->y + 15, COL_BLUE, s->x + 20, s->y + 19, COL_BLUE, 1.5f, Z);
    text_draw_fit(sess_q[0] ? sess_q : "Search sessions", s->x + 27, s->y + 6, S_BODY * 0.95f,
                  sess_q[0] ? COL_INK : COL_INK_3, s->w - 34);
    button(&NEW_BTN, "+ New", NULL, COL_BLUE, COL_ON);

    draw_list(LS_SESSIONS, SESS_Y, SESS_ROWS, session_row,
              sess_q[0] ? "No sessions match that search." : "No sessions on this 3DS yet.");
    char buf[sizeof sess_q + 40];
    int n = count(LS_SESSIONS), below = n - lists[LS_SESSIONS].top - SESS_ROWS;
    if (sess_q[0]) snprintf(buf, sizeof buf, "Results for \"%s\" · " GLYPH_B " clears", sess_q);
    else if (below > 0) snprintf(buf, sizeof buf, "%d more · " GLYPH_DPAD " to browse", below);
    else buf[0] = 0;
    text_draw_fit(buf, 11, SESS_Y + SESS_ROWS * ROW_H + 5, S_TOOL, COL_TX_3, BOT_W - 22);
}

// The selected session's last messages, newest at the bottom, as many as fit between y0 and y1.
static void draw_preview(float y0, float y1)
{
    List *l = &lists[LS_PREVIEW];
    if (!l->loaded) {
        if (l->err[0]) {
            text_draw_fit(l->err, PAD_X, y0, S_SMALL, COL_TX_3, TOP_W - 2 * PAD_X);
        } else {
            braille(PAD_X, y0 + 1, 10, COL_BLUE);
            text_draw("Loading messages...", PAD_X + 14, y0, S_SMALL, COL_TX_2);
        }
        return;
    }
    int n = count(LS_PREVIEW);
    if (!n) {
        text_draw("No messages yet.", PAD_X, y0, S_SMALL, COL_TX_3);
        return;
    }
    const float UW = 250, AW = TOP_W - 2 * PAD_X - 22, GAP = 7;
    float h[16];
    int first = n;
    float total = 0;
    for (int i = n - 1; i >= 0 && n - i <= 16; i--) {
        json_t *it = item(LS_PREVIEW, i);
        const char *role = jstr(it, "role"), *text = jstr(it, "text");
        float mh = !strcmp(role, "user")   ? text_block(text, 0, 0, UW - 14, S_SMALL, NULL, 2, NULL) + 10
                   : !strcmp(role, "tool") ? 11
                                           : text_block(text, 0, 0, AW, S_SMALL, NULL, 3, NULL);
        if (total + mh + (first < n ? GAP : 0) > y1 - y0) break;
        total += mh + (first < n ? GAP : 0);
        h[n - 1 - i] = mh;
        first = i;
    }
    float y = y0;
    for (int i = first; i < n; i++) {
        json_t *it = item(LS_PREVIEW, i);
        const char *role = jstr(it, "role"), *text = jstr(it, "text");
        float mh = h[n - 1 - i];
        if (!strcmp(role, "user")) {
            float w;
            text_block(text, 0, 0, UW - 14, S_SMALL, NULL, 2, &w);
            float bx = TOP_W - PAD_X - (w + 14);
            rect(bx, y, w + 14, mh, COL_BLUE);
            text_block(text, bx + 7, y + 5, UW - 14, S_SMALL, &USER_COLORS, 2, NULL);
        } else if (!strcmp(role, "tool")) {
            rect(PAD_X + 22, y, AW, 11, COL_SCR_2);
            text_draw_fit(text, PAD_X + 30, y + 0.5f, S_TOOL, COL_TX_2, AW - 16);
        } else {
            frame_rect(PAD_X, y, 15, 15, COL_LINE_B);
            hermes_mark(PAD_X + 3, y + 3, COL_BLUE);
            text_block(text, PAD_X + 22, y, AW, S_SMALL, &BODY_COLORS, 3, NULL);
        }
        y += mh + GAP;
    }
}

static void sessions_top(void)
{
    char buf[96], meta[192], when[32];
    int n = count(LS_SESSIONS);
    json_t *it = selected(LS_SESSIONS);
    if (n) snprintf(buf, sizeof buf, "Sessions · %d of %d", lists[LS_SESSIONS].sel + 1, n);
    else snprintf(buf, sizeof buf, "Sessions");
    float bottom = SCR_H - TOP_FOOTER_H - 6;
    if (it) {
        bool cur = json_is_true(json_object_get(it, "current"));
        float x = PAD_X + tag(cur ? "CURRENT" : "SESSION", PAD_X, 24, cur) + 6;
        when_text(jnum(it, "last_active"), when, sizeof when);
        int msgs = (int)jnum(it, "msgs");
        const char *model = cur && app.chat->model[0] ? app.chat->model : jstr(it, "model");  // live, not stored
        snprintf(meta, sizeof meta, "%s%s%d message%s%s%s", when, when[0] ? " · " : "", msgs, msgs == 1 ? "" : "s",
                 model[0] ? " · " : "", model);
        text_draw_fit(meta, x, 24.5f, S_TOOL, COL_TX_3, TOP_W - PAD_X - x);
        big_title(session_title(it), 39);
        rect(PAD_X, 69, TOP_W - 2 * PAD_X, 1, COL_RULE);
        draw_preview(76, bottom);
    } else {
        big_title(sess_q[0] ? "Nothing found" : "No sessions yet", 30);
        text_block(sess_q[0] ? "Try other words, or press " GLYPH_B " to clear the search."
                             : "Chats you have with Hermes from this 3DS show up here. Press " GLYPH_X
                               " to start one.",
                   PAD_X, 64, TOP_W - 2 * PAD_X, S_BODY, &BODY_COLORS, 4, NULL);
    }
    draw_header(buf);
    json_t *sel = selected(LS_SESSIONS);
    top_footer(sel ? (json_is_true(json_object_get(sel, "current")) ? "Open" : "Resume") : NULL, "New", "Search");
}

// --- Skills ------------------------------------------------------------------

#define LIST_Y 46
#define LIST_ROWS 6

static void skill_row(json_t *it, const Rect *r, bool sel)
{
    bool on = json_is_true(json_object_get(it, "enabled"));
    const char *catg = jstr(it, "category");
    float cw = text_width(catg, strlen(catg), S_TOOL);
    if (cw > 110) cw = 110;
    text_draw_fit(catg, r->x + r->w - 10 - cw, r->y + 10, S_TOOL, COL_TX_3, cw);
    text_draw_fit(jstr(it, "name"), r->x + 10, r->y + 8, S_BODY * 0.95f, on ? (sel ? COL_INK : COL_INK_2) : COL_TX_3,
                  r->w - 30 - cw);
}

// Left / right jump to the previous / next category (the list is sorted by category).
static void skills_jump(int dir)
{
    List *l = &lists[LS_SKILLS];
    int n = count(LS_SKILLS);
    if (!n) return;
    const char *c0 = jstr(selected(LS_SKILLS), "category");
    int i = l->sel;
    if (dir > 0) {
        while (i < n - 1 && !strcmp(jstr(item(LS_SKILLS, i), "category"), c0)) i++;
    } else {
        while (i > 0 && !strcmp(jstr(item(LS_SKILLS, i), "category"), c0)) i--;  // into the previous category
        const char *c1 = jstr(item(LS_SKILLS, i), "category");
        while (i > 0 && !strcmp(jstr(item(LS_SKILLS, i - 1), "category"), c1)) i--;  // to its first skill
    }
    l->sel = i;
}

static void use_skill(void)
{
    json_t *it = selected(LS_SKILLS);
    if (!it) return;
    char buf[96];
    snprintf(buf, sizeof buf, "/%s ", jstr(it, "name"));
    app_compose(buf);
}

static void skills_input(u32 down, u32 held, const touchPosition *t)
{
    u32 lr = down & (KEY_DLEFT | KEY_DRIGHT);
    list_nav(LS_SKILLS, down & ~lr, held, LIST_ROWS);
    if (lr) skills_jump((lr & KEY_DRIGHT) ? 1 : -1);
    if (down & KEY_A) use_skill();
    if (down & KEY_Y) refresh(LS_SKILLS);
    if (down & KEY_B) tabs_switch(TAB_CHAT);
    if (list_tap(LS_SKILLS, t, LIST_Y, LIST_ROWS)) use_skill();
}

static void skills_bottom(void)
{
    char buf[32];
    snprintf(buf, sizeof buf, "%d SKILLS", count(LS_SKILLS));
    list_eyebrow(lists[LS_SKILLS].loaded ? buf : "SKILLS", 32);
    draw_list(LS_SKILLS, LIST_Y, LIST_ROWS, skill_row, "No skills installed.");
    text_draw(GLYPH_DPAD " Up/down: skill · left/right: category", 11, LIST_Y + LIST_ROWS * ROW_H + 5, S_TOOL,
              COL_TX_3);
}

static void skills_top(void)
{
    char buf[96];
    int n = count(LS_SKILLS);
    json_t *it = selected(LS_SKILLS);
    snprintf(buf, sizeof buf, n ? "Skills · %d of %d" : "Skills", lists[LS_SKILLS].sel + 1, n);
    if (it) {
        char catg[64];
        snprintf(catg, sizeof catg, "%s", jstr(it, "category"));
        for (char *p = catg; *p; p++)
            if (*p >= 'a' && *p <= 'z') *p -= 32;
            else if (*p == '-') *p = ' ';
        spaced(catg[0] ? catg : "SKILL", PAD_X, 25, S_EYE, COL_BLUE, 1.6f);
        big_title(jstr(it, "name"), 37);
        float y = 70 + text_block(jstr(it, "desc"), PAD_X, 70, TOP_W - 2 * PAD_X, S_BODY, &BODY_COLORS, 6, NULL);
        y += 10;
        rect(PAD_X, y, TOP_W - 2 * PAD_X, 1, COL_RULE);
        y += 8;
        bool on = json_is_true(json_object_get(it, "enabled"));
        snprintf(buf + 40, 56, "/%s", jstr(it, "name"));
        float w = text_draw(buf + 40, PAD_X, y, S_BODY, COL_BLUE);
        text_draw("<what you want>", PAD_X + w + 5, y, S_BODY, COL_TX_3);
        text_draw(on ? "Type this in chat, or press " GLYPH_A " to start it."
                     : "Disabled for this platform. Enable it on the PC with: hermes skills",
                  PAD_X, y + 18, S_SMALL, on ? COL_TX_2 : COL_BAD);
    } else {
        big_title("Skills", 30);
        char what[128];
        snprintf(what, sizeof what, "What %s knows how to do. Each one is also a slash command.", agent_title(app_agent()));
        text_block(what, PAD_X, 64, TOP_W - 2 * PAD_X, S_BODY, &BODY_COLORS, 3, NULL);
    }
    draw_header(buf);
    top_footer(it ? "Use" : NULL, NULL, "Refresh");
}

// --- Jobs ---------------------------------------------------------------------

static bool job_paused(json_t *it) { return !strcmp(jstr(it, "state"), "paused"); }

static void job_row(json_t *it, const Rect *r, bool sel)
{
    char right[32];
    const char *state = jstr(it, "state");
    u32 rc = COL_TX_3;
    if (job_paused(it)) snprintf(right, sizeof right, "paused");
    else if (!strcmp(state, "running")) snprintf(right, sizeof right, "running"), rc = COL_BLUE;
    else if (!strcmp(state, "error")) snprintf(right, sizeof right, "failed"), rc = COL_BAD;
    else when_text(jnum(it, "next_run"), right, sizeof right);
    float rw = text_width(right, strlen(right), S_TOOL);
    text_draw(right, r->x + r->w - 10 - rw, r->y + 3, S_TOOL, rc);
    const char *sch = jstr(it, "schedule");
    float sw = text_width(sch, strlen(sch), S_TOOL);
    if (sw > 120) sw = 120;
    text_draw_fit(sch, r->x + r->w - 10 - sw, r->y + 16, S_TOOL, COL_TX_3, sw);
    float maxw = r->w - 30 - (rw > sw ? rw : sw);
    text_draw_fit(jstr(it, "name"), r->x + 10, r->y + 8, S_BODY * 0.95f, job_paused(it) ? COL_TX_3 : COL_INK, maxw);
    (void)sel;
}

static void job_action(const char *op)
{
    json_t *it = selected(LS_JOBS);
    if (!it || act_req) return;
    if (app.net != NET_ONLINE) {
        app_toast("Not connected to the PC");
        return;
    }
    act_req = app_request("jobs.action", json_pack("{s:s, s:s}", "job", jstr(it, "id"), "op", op));
    app_toast(!strcmp(op, "run") ? "Running it now..." : !strcmp(op, "pause") ? "Pausing..." : "Resuming...");
}

static void jobs_input(u32 down, u32 held, const touchPosition *t)
{
    list_nav(LS_JOBS, down, held, LIST_ROWS);
    json_t *it = selected(LS_JOBS);
    if ((down & KEY_A) && it) job_action("run");
    if ((down & KEY_X) && it) job_action(job_paused(it) ? "resume" : "pause");
    if (down & KEY_Y) refresh(LS_JOBS);
    if (down & KEY_B) tabs_switch(TAB_CHAT);
    if (list_tap(LS_JOBS, t, LIST_Y, LIST_ROWS)) job_action("run");
}

static void jobs_bottom(void)
{
    char buf[32];
    snprintf(buf, sizeof buf, "%d SCHEDULED JOB%s", count(LS_JOBS), count(LS_JOBS) == 1 ? "" : "S");
    list_eyebrow(lists[LS_JOBS].loaded ? buf : "JOBS", 32);
    draw_list(LS_JOBS, LIST_Y, LIST_ROWS, job_row,
              "No scheduled jobs yet. Ask Hermes in chat, for example: \"every weekday at 8 am, summarize my inbox\".");
}

static void kv(const char *k, const char *v, float y, u32 vc)
{
    spaced(k, PAD_X, y + 2, S_EYE, COL_TX_3, 1.2f);
    text_draw_fit(v, PAD_X + 88, y, S_BODY * 0.95f, vc, TOP_W - PAD_X * 2 - 88);
}

static void jobs_top(void)
{
    char buf[160], when[32];
    int n = count(LS_JOBS);
    json_t *it = selected(LS_JOBS);
    snprintf(buf, sizeof buf, n ? "Jobs · %d of %d" : "Jobs", lists[LS_JOBS].sel + 1, n);
    if (it) {
        const char *state = jstr(it, "state");
        spaced(job_paused(it) ? "PAUSED" : !strcmp(state, "running") ? "RUNNING NOW" : "SCHEDULED", PAD_X, 25, S_EYE,
               job_paused(it) ? COL_TX_3 : COL_BLUE, 1.6f);
        big_title(jstr(it, "name"), 37);
        float y = 72;
        kv("SCHEDULE", jstr(it, "schedule"), y, COL_INK);
        when_text(jnum(it, "next_run"), when, sizeof when);
        kv("NEXT RUN", job_paused(it) ? "paused" : when[0] ? when : "not planned", y + 20, COL_INK);
        when_text(jnum(it, "last_run"), when, sizeof when);
        const char *ls = jstr(it, "last_status");
        char last[96];
        snprintf(last, sizeof last, "%s%s%s", when[0] ? when : "never", ls[0] ? " · " : "", ls);
        kv("LAST RUN", last, y + 40, !strcmp(ls, "error") || !strcmp(ls, "failed") ? COL_BAD : COL_INK);
        const char *err = jstr(it, "last_error");
        if (err[0]) text_block(err, PAD_X, y + 64, TOP_W - 2 * PAD_X, S_SMALL, &DIM_COLORS, 4, NULL);
    } else {
        big_title("Nothing scheduled", 30);
        char what[200];
        snprintf(what, sizeof what, "%s can run prompts on a schedule and send the results here, even while the 3DS "
                 "is closed: they wait for you and arrive when you reconnect.", agent_title(app_agent()));
        text_block(what, PAD_X, 64, TOP_W - 2 * PAD_X, S_BODY, &BODY_COLORS, 5, NULL);
    }
    draw_header(buf);
    top_footer(it ? "Run now" : NULL, it ? (job_paused(it) ? "Resume" : "Pause") : NULL, "Refresh");
}

// --- More: commands, model picker, status --------------------------------------

#define GRID_Y 86
#define CELL_W ((BOT_W - 16) / 3.0f)
#define CELL_H 48
static const Rect CMD_INPUT = {100, 31, 212, 24};

static Rect cat_rect(int i)
{
    return (Rect){8 + i * (BOT_W - 16) / (float)CATS, 61, (BOT_W - 16) / (float)CATS, 19};
}
static Rect cell_rect(int i) { return (Rect){8 + (i % 3) * CELL_W, GRID_Y + (i / 3) * CELL_H, CELL_W, CELL_H}; }

static const Cmd *cell_cmd(int i) { return i >= 0 && i < CELLS && cmd_available(&CMDS[cat][i]) ? &CMDS[cat][i] : NULL; }

static void type_command(void) { app_compose("/"); }

static void picker_input(u32 down, u32 held)
{
    List *l = &lists[LS_MODELS];
    l->sel += nav_step(down, held, UP_KEYS, DOWN_KEYS);
    clamp_sel(l, count(LS_MODELS));
    if (down & KEY_B) picker = false;
    if (down & KEY_Y) refresh(LS_MODELS);
    json_t *it = selected(LS_MODELS);
    if ((down & KEY_A) && it) {
        char buf[200];
        snprintf(buf, sizeof buf, "/model %s --provider %s", jstr(it, "id"), jstr(it, "provider"));
        picker = false;
        app_send_text(buf);
    }
}

static void more_input(u32 down, u32 held, const touchPosition *t)
{
    if (picker) {
        picker_input(down, held);
        return;
    }
    int col = cell % 3, row = cell / 3;
    if (down & KEY_DLEFT) {
        if (col > 0) cell--;
        else cat = (cat + CATS - 1) % CATS, cell = row * 3 + 2;
    }
    if (down & KEY_DRIGHT) {
        if (col < 2) cell++;
        else cat = (cat + 1) % CATS, cell = row * 3;
    }
    int d = nav_step(down & ~(KEY_DLEFT | KEY_DRIGHT), held, UP_KEYS, DOWN_KEYS);
    if (d) cell = ((row + d + 3) % 3) * 3 + col;
    const Cmd *c = cell_cmd(cell);
    if ((down & KEY_A) && c) run_cmd(c, false);
    if ((down & KEY_X) && c && !is_local(c)) run_cmd(c, true);
    if (down & KEY_Y) type_command();
    if (down & KEY_B) tabs_switch(TAB_CHAT);
    if (t) {
        if (rect_hit(&CMD_INPUT, t->px, t->py)) type_command();
        for (int i = 0; i < CATS; i++) {
            Rect r = cat_rect(i);
            if (rect_hit(&r, t->px, t->py)) cat = i;
        }
        for (int i = 0; i < CELLS; i++) {
            Rect r = cell_rect(i);
            if (rect_hit(&r, t->px, t->py) && cell_cmd(i)) {
                cell = i;
                run_cmd(cell_cmd(i), false);
            }
        }
    }
}

static void more_bottom(void)
{
    text_draw("Commands", 10, 31, S_BIG * 0.62f, COL_INK);
    const Rect *in = &CMD_INPUT;
    rect(in->x + 2, in->y + 2, in->w, in->h, COL_BLUE);
    rect(in->x, in->y, in->w, in->h, COL_PAPER);
    frame_rect(in->x, in->y, in->w, in->h, COL_INK);
    text_draw("/", in->x + 8, in->y + 5, S_BODY, COL_BLUE);
    text_draw("Type or tap a command", in->x + 20, in->y + 6, S_SMALL, COL_INK_3);

    for (int i = 0; i < CATS; i++) {
        Rect r = cat_rect(i);
        bool on = i == cat;
        rect(r.x, r.y, r.w, r.h, on ? COL_INK : COL_PAPER);
        frame_rect(r.x, r.y, r.w + (i < CATS - 1 ? 1 : 0), r.h, COL_INK);
        float w = spaced_width(CAT_NAMES[i], S_EYE, 1.4f);
        spaced(CAT_NAMES[i], r.x + (r.w - w) / 2, r.y + 5.5f, S_EYE, on ? COL_ON : COL_INK_2, 1.4f);
    }

    rect(8, GRID_Y, BOT_W - 16, CELL_H * 3, COL_PAPER);
    for (int i = 0; i < CELLS; i++) {
        Rect r = cell_rect(i);
        const Cmd *c = cell_cmd(i);
        bool sel = i == cell && !picker;
        if (sel && c) rect(r.x, r.y, r.w, r.h, COL_BLUE);
        frame_rect(r.x, r.y, r.w + 1, r.h + 1, COL_RULE);
        if (!c) continue;
        bool danger = !strcmp(c->name, "yolo"), local = is_local(c);
        char name[32];
        snprintf(name, sizeof name, "%s%s", local ? c->label : c->name, danger ? " !" : "");
        u32 fg = sel ? COL_ON : danger ? COL_BAD : COL_INK;
        float sw = local ? 0 : text_draw("/", r.x + 8, r.y + 10, S_BODY * 0.95f, sel ? COL_ON : COL_BLUE);
        text_draw_fit(name, r.x + 8 + sw, r.y + 10, S_BODY * 0.95f, fg, r.w - 14 - sw);
        text_draw_fit(local ? local_label(c) : c->label, r.x + 8, r.y + 27, S_TOOL, sel ? COL_ON_2 : COL_TX_3,
                      r.w - 14);
    }
    if (lists[LS_COMMANDS].req) braille(BOT_W - 18, GRID_Y - 14, 9, COL_BLUE);
}

static void picker_row(json_t *it, const Rect *r, bool sel)
{
    bool cur = json_is_true(json_object_get(it, "current"));
    u32 fg = sel ? COL_ON : COL_INK, fg2 = sel ? COL_ON_2 : COL_TX_3;
    text_draw(cur ? "(\xE2\x80\xA2)" : "( )", r->x + 12, r->y + 9, S_SMALL, sel ? COL_ON : COL_BLUE);
    float right = r->x + r->w - 10;
    if (sel && !cur) {
        float w = text_width("Switch", 6, S_SMALL) + 17;
        text_draw("Switch", right - w, r->y + 9, S_SMALL, COL_ON);
        text_draw(GLYPH_A, right - 13, r->y + 8, S_HINT * 1.15f, COL_ON);
        right -= w + 8;
    } else if (cur) {
        float w = spaced_width("IN USE", S_EYE, 1.2f) + 10;
        frame_rect(right - w, r->y + 9, w, 12, sel ? COL_ON : COL_BLUE);
        spaced("IN USE", right - w + 5, r->y + 11, S_EYE, sel ? COL_ON : COL_BLUE, 1.2f);
        right -= w + 8;
    }
    text_draw_fit(jstr(it, "id"), r->x + 40, r->y + 3, S_BODY * 0.95f, fg, right - (r->x + 40));
    text_draw_fit(jstr(it, "name")[0] ? jstr(it, "name") : jstr(it, "provider"), r->x + 40, r->y + 18, S_TOOL, fg2,
                  right - (r->x + 40));
}

static void picker_top(void)
{
    List *l = &lists[LS_MODELS];
    int n = count(LS_MODELS), rows = 5;
    char buf[160];
    snprintf(buf, sizeof buf, "for this session · from the next message · %d on %s", n,
             app.server.name[0] ? app.server.name : "the PC");
    big_title("Switch model", 22);
    text_draw_fit(buf, PAD_X + 2, 50, S_SMALL, COL_TX_3, TOP_W - 2 * PAD_X - 2);
    // The list panel: 5 rows from y 66.
    const float y0 = 66, x0 = PAD_X, w = TOP_W - 2 * PAD_X;
    rect(x0 + 3, y0 + 3, w, rows * ROW_H, COL_BLUE);
    rect(x0, y0, w, rows * ROW_H, COL_PAPER);
    frame_rect(x0, y0, w, rows * ROW_H, COL_INK);
    if (!n) {
        if (l->req) braille(x0 + 12, y0 + 12, 10, COL_BLUE);
        text_draw(l->req ? "Loading models..." : l->err[0] ? l->err : "No models configured.", x0 + 28, y0 + 11,
                  S_SMALL, COL_TX_2);
    } else {
        if (l->sel < l->top) l->top = l->sel;
        if (l->sel >= l->top + rows) l->top = l->sel - rows + 1;
        for (int i = 0; i < rows && l->top + i < n; i++) {
            Rect r = {x0, y0 + i * ROW_H, w, ROW_H};
            bool sel = l->top + i == l->sel;
            if (i) rect(r.x + 1, r.y, r.w - 2, 1, COL_RULE);
            if (sel) rect(r.x + 1, r.y + 1, r.w - 2, r.h - 2, COL_BLUE);
            picker_row(item(LS_MODELS, l->top + i), &r, sel);
        }
    }
    draw_header("/model");
    float y = footer_begin(), x = 7;
    x += hint(GLYPH_A, "Switch", x, y, COL_ON);
    x += hint(GLYPH_B, "Back", x, y, COL_ON);
    hint(GLYPH_DPAD, "Choose", TOP_W - 60, y, COL_ON_3);
}

static void more_top(void)
{
    if (picker) {
        picker_top();
        return;
    }
    char buf[200];
    big_title("Status", 22);
    json_t *s = json_object_get(status, "session");
    float y = 58;
    if (!status) {
        text_draw(app.net == NET_ONLINE ? "Loading..." : "Not connected.", PAD_X, y, S_BODY, COL_TX_2);
    } else {
        const char *title = jstr(s, "title");
        kv("SESSION", title[0] ? title : jstr(s, "id"), y, COL_INK);
        kv("MODEL", jstr(s, "model")[0] ? jstr(s, "model") : app.chat->model, y + 17, COL_INK);
        snprintf(buf, sizeof buf, "%d · tokens %d in, %d out", (int)jnum(s, "msgs"), (int)jnum(s, "tokens_in"),
                 (int)jnum(s, "tokens_out"));
        kv("MESSAGES", buf, y + 34, COL_INK);
        int queued = (int)jnum(status, "queued");
        snprintf(buf, sizeof buf, "%s%s", json_is_true(json_object_get(status, "busy")) ? "working" : "idle",
                 queued > 0 ? " · messages queued" : "");
        kv("AGENT", buf, y + 51, COL_INK);
    }
    snprintf(buf, sizeof buf, "%s · %s:%u · %s", app.server.name[0] ? app.server.name : "PC", app.server.host,
             app.server.port, app.net == NET_ONLINE ? "encrypted" : "offline");
    kv("PC", buf, y + 68, COL_INK);

    // The selected command, in full.
    const Cmd *c = cell_cmd(cell);
    rect(PAD_X, 150, TOP_W - 2 * PAD_X, 1, COL_RULE);
    if (c && is_local(c)) {
        text_draw(c->label, PAD_X, 157, S_BODY, COL_BLUE);
        text_block(local_desc(c), PAD_X, 175, TOP_W - 2 * PAD_X, S_SMALL, &BODY_COLORS, 3, NULL);
    } else if (c) {
        json_t *info = cmd_info(c->name);
        snprintf(buf, sizeof buf, "/%s", c->name);
        float w = text_draw(buf, PAD_X, 157, S_BODY, COL_BLUE);
        text_draw_fit(jstr(info, "args"), PAD_X + w + 6, 158, S_SMALL, COL_TX_3, TOP_W - 2 * PAD_X - w - 6);
        text_block(info ? jstr(info, "desc") : c->label, PAD_X, 175, TOP_W - 2 * PAD_X, S_SMALL, &BODY_COLORS, 3,
                   NULL);
    }
    draw_header("More");
    top_footer(c ? (is_local(c) ? "Choose" : "Run") : NULL, c && !is_local(c) ? "Add options" : NULL,
               "Type a command");
}


// --- Bots ---------------------------------------------------------------------
// Hermes profiles the gateway serves, as Bot Mode shows them (protocol.md §4.8). A picks one: the chat, the
// Sessions, Skills and Jobs tabs and the model picker are then that bot's.

#define BOTS_Y 66
#define BOTS_ROWS 5
static const Rect NEW_BOT = {214, 33, 98, 26};
// The new-bot step on the touch screen: the top screen explains, these act
static const Rect BOT_CREATE = {16, 96, 288, 40}, BOT_DESC = {16, 146, 140, 32}, BOT_CANCEL = {164, 146, 140, 32},
                  BOT_BACK = {90, 146, 140, 32};

static bool asks(const Agent *a) { return a->chat.approval || a->chat.clarify; }

// What the gateway can't do, in words, or NULL when bots work.
static const char *bots_trouble(void)
{
    if (app.agents_old)
        return "The Hermes 3DS plugin on the PC predates bots. Update it to chat with your Hermes profiles here.";
    if (app.agents_loaded && !app.multiplex)
        return "Bots are Hermes profiles. To reach them from the 3DS, set gateway.multiplex_profiles: true in the "
               "PC's config.yaml and restart the gateway.";
    return NULL;
}

static void bot_row(int i, const Rect *r, bool sel)
{
    const Agent *a = &app.agents[i];
    bool cur = i == app.cur;
    // Its picture, ringed for the agent in use; else a monogram box, filled for the agent in use.
    float bx = r->x + 8, by = r->y + 6;
    if (agent_picture(a, bx, by, 18)) {
        if (cur) {
            frame_rect(bx - 2, by - 2, 22, 22, COL_BLUE);
            frame_rect(bx - 1, by - 1, 20, 20, COL_BLUE);
        }
    } else {
        if (cur) rect(bx, by, 18, 18, COL_BLUE);
        else frame_rect(bx, by, 18, 18, COL_LINE_B);
        agent_mark(a, bx + 4.5f, by + 4.5f, cur ? COL_ON : COL_BLUE);
    }
    // Right: what it needs from you, or what it is doing.
    float right = r->x + r->w - 10;
    char buf[32];
    if (asks(a)) {
        float w = spaced_width("NEEDS YOU", S_EYE, 1.2f) + 8;
        rect(right - w, r->y + 9, w, 12, COL_ACCENT);
        frame_rect(right - w, r->y + 9, w, 12, COL_INK);
        spaced("NEEDS YOU", right - w + 4, r->y + 11, S_EYE, COL_INK, 1.2f);
        right -= w + 6;
    } else if (a->chat.typing) {
        float w = text_width("working", 7, S_TOOL);
        text_draw("working", right - w, r->y + 10, S_TOOL, COL_BLUE);
        braille(right - w - 10, r->y + 11, 9, COL_BLUE);
        right -= w + 16;
    } else if (a->unread) {
        snprintf(buf, sizeof buf, "%d", a->unread > 99 ? 99 : a->unread);
        float w = text_width(buf, strlen(buf), S_TOOL) + 8;
        if (w < 14) w = 14;
        rect(right - w, r->y + 8, w, 14, COL_BLUE);
        text_draw(buf, right - w + (w - text_width(buf, strlen(buf), S_TOOL)) / 2, r->y + 9.5f, S_TOOL, COL_ON);
        right -= w + 6;
    } else if (a->unreachable) {
        float w = text_width("not reachable", 13, S_TOOL);
        text_draw("not reachable", right - w, r->y + 10, S_TOOL, COL_BAD);
        right -= w + 6;
    } else if (cur) {
        float w = text_width("current", 7, S_TOOL);
        text_draw("current", right - w, r->y + 10, S_TOOL, COL_BLUE);
        right -= w + 6;
    }
    float tx = r->x + 34, tw = right - tx;
    const char *sub = a->chat.model[0] ? a->chat.model : a->model[0] ? a->model : a->desc;
    text_draw_fit(agent_title(a), tx, r->y + 3, S_BODY * 0.95f, sel || cur ? COL_INK : COL_INK_2, tw);
    if (a != app.agents) {
        snprintf(buf, sizeof buf, "@%s", a->id);
        float iw = text_width(buf, strlen(buf), S_TOOL);
        if (iw > tw * 0.55f) iw = tw * 0.55f;
        text_draw_fit(buf, tx, r->y + 17, S_TOOL, COL_BLUE, iw);
        if (sub[0]) text_draw_fit(sub, tx + iw + 6, r->y + 17, S_TOOL, COL_TX_3, tw - iw - 6);
    } else {
        text_draw_fit(sub[0] ? sub : "main agent", tx, r->y + 17, S_TOOL, COL_TX_3, tw);
    }
}

static void bots_clamp(void)
{
    for (int i = 0; i < app.nagents; i++)  // follow the selected agent through a reordered roster
        if (!strcmp(i ? app.agents[i].id : "", bot_sel_id)) bot_sel = i;
    if (bot_sel >= app.nagents) bot_sel = app.nagents - 1;
    if (bot_sel < 0) bot_sel = 0;
    if (bot_sel < bot_top) bot_top = bot_sel;
    if (bot_sel >= bot_top + BOTS_ROWS) bot_top = bot_sel - BOTS_ROWS + 1;
    if (bot_top > app.nagents - BOTS_ROWS) bot_top = app.nagents - BOTS_ROWS;
    if (bot_top < 0) bot_top = 0;
    snprintf(bot_sel_id, sizeof bot_sel_id, "%s", bot_sel ? app.agents[bot_sel].id : "");
}

static void bots_select(int i)
{
    bot_sel = i < 0 ? 0 : i;
    bot_sel_id[0] = 1;  // matches nothing: bots_clamp keeps the index and records its id
    bot_sel_id[1] = 0;
    bots_clamp();
}

static void open_bot(void)
{
    bots_clamp();
    app_select_agent(bot_sel);
    tabs_switch(TAB_CHAT);
}

// The new-bot flow: a name, then a confirmation on the top screen (with an optional description).
static void slug(const char *in, char *out, size_t cap)
{
    size_t n = 0;
    bool dash = false;
    for (const char *p = in; *p && n + 1 < cap && n < 64; p++) {
        char c = *p >= 'A' && *p <= 'Z' ? (char)(*p + 32) : *p;
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-') {
            if (c == '-' && (dash || !n)) continue;
            if (c == '_' && !n) continue;
            out[n++] = c;
            dash = c == '-';
        } else if (n && !dash) {
            out[n++] = '-';
            dash = true;
        }
    }
    while (n && (out[n - 1] == '-' || out[n - 1] == '_')) n--;
    out[n] = 0;
}

static void new_bot(void)
{
    char buf[200], id[68];
    const char *why = app.net != NET_ONLINE      ? "Not connected to the PC"
                      : app.agents_old           ? "Update the plugin on the PC first"
                      : !app.agents_loaded       ? (app.agents_err[0] ? "The PC couldn't list its bots yet; try again soon"
                                                                      : "Still asking the PC about its bots")
                      : !app.multiplex           ? "Turn on gateway.multiplex_profiles first"
                                                 : NULL;
    if (!why && app.nagents == AGENT_MAX) {
        snprintf(buf, sizeof buf, "No room for more: the 3DS keeps %d bots", AGENT_MAX - 1);
        why = buf;
    }
    if (create_req) {
        snprintf(buf, sizeof buf, "Still making %s on the PC", new_name);
        why = buf;
    }
    if (why) {
        app_toast(why);
        return;
    }
    if (!app_keyboard("Name for the new bot, like research or work-helper", NULL, buf, sizeof buf, 40, false, "Next"))
        return;
    slug(buf, id, sizeof id);
    if (!id[0]) {
        app_toast("Use letters or digits (a-z, 0-9) in the name");
        return;
    }
    if (!strcmp(id, "default") || !strcmp(id, "main") || !strcmp(id, app.agents[0].id)) {
        snprintf(buf, sizeof buf, "\"%s\" is the main agent's name: pick another", id);
        app_toast(buf);
        return;
    }
    if (agent_by_id(id)) {
        snprintf(buf, sizeof buf, "There is a bot called %s already", id);
        app_toast(buf);
        return;
    }
    snprintf(new_name, sizeof new_name, "%s", id);
    new_desc[0] = create_err[0] = 0;
    bstep = BOTS_CONFIRM;
}

static void describe_bot(void)
{
    char buf[sizeof new_desc];
    if (app_keyboard("What is this bot for? (shown in Bot Mode)", new_desc, buf, sizeof buf, 120, true, "OK"))
        snprintf(new_desc, sizeof new_desc, "%s", buf);
}

static void create_bot(void)
{
    if (create_req || app.net != NET_ONLINE) {
        if (app.net != NET_ONLINE) app_toast("Not connected to the PC");
        return;
    }
    json_t *f = json_pack("{s:s, s:s}", "name", new_name, "desc", new_desc);
    if (!f) return;
    create_req = app_request("agents.create", f);
    create_err[0] = 0;
    bstep = BOTS_CREATING;
}

static void bots_input(u32 down, u32 held, const touchPosition *t)
{
    if (bstep == BOTS_CREATING) {  // a few seconds: the PC copies the profile
        if (app.net != NET_ONLINE) {
            create_req = 0;
            bstep = BOTS_CONFIRM;
            snprintf(create_err, sizeof create_err,
                     "The connection dropped while the PC was making it. Once it is back, the list shows whether "
                     "it was made.");
        }
        if ((down & KEY_B) || (t && rect_hit(&BOT_BACK, t->px, t->py))) bstep = BOTS_LIST;  // its reply still selects it
        if (down & KEY_X) new_bot();  // (says it is still being made)
        return;
    }
    if (bstep == BOTS_CONFIRM) {
        if ((down & KEY_A) || (t && rect_hit(&BOT_CREATE, t->px, t->py))) create_bot();
        else if ((down & KEY_X) || (t && rect_hit(&BOT_DESC, t->px, t->py))) describe_bot();
        else if ((down & KEY_B) || (t && rect_hit(&BOT_CANCEL, t->px, t->py))) bstep = BOTS_LIST;
        else return;
        app.dirty = true;
        return;
    }
    bots_clamp();
    int old = bot_sel, to = bot_sel + nav_step(down, held, UP_KEYS, DOWN_KEYS);
    if (down & KEY_DLEFT) to -= BOTS_ROWS;
    if (down & KEY_DRIGHT) to += BOTS_ROWS;
    if (to != old) {
        bots_select(to);
        app.dirty = true;
    }
    if (down & KEY_A) open_bot();
    if (down & KEY_X) new_bot();
    if (down & KEY_Y) app_refresh_agents();
    if (down & KEY_B) tabs_switch(TAB_CHAT);
    if (t) {
        if (rect_hit(&NEW_BOT, t->px, t->py)) {
            new_bot();
        } else if (t->py >= BOTS_Y && t->py < BOTS_Y + BOTS_ROWS * ROW_H && t->px >= 8 && t->px < BOT_W - 8) {
            int i = bot_top + (int)((t->py - BOTS_Y) / ROW_H);
            if (i < app.nagents) {
                if (i == bot_sel) open_bot();
                else bots_select(i);
            }
        }
    }
}

// The new bot isn't made until it is confirmed: say so on the touch screen too, with the buttons that do it.
static void confirm_bottom(void)
{
    char buf[160];
    spaced(bstep == BOTS_CREATING ? "CREATING" : "NEW BOT", 11, 36, S_EYE, COL_INK_2, 1.4f);
    text_draw_fit(new_name, 11, 50, S_BODY * 1.2f, COL_INK, BOT_W - 22);
    if (bstep == BOTS_CREATING) {
        braille(16, 104, 10, COL_BLUE);
        text_draw("Setting it up on the PC...", 30, 103, S_SMALL, COL_TX_2);
        text_block("It shows up in the list when it's ready; you can go back meanwhile.", 16, 120, BOT_W - 32,
                   S_SMALL, &DIM_COLORS, 2, NULL);
        button(&BOT_BACK, "Back to the list", GLYPH_B, COL_PAPER_2, COL_INK);
        return;
    }
    text_draw_fit("Not made yet: create it to add it to the list.", 11, 74, S_TOOL, COL_TX_3, BOT_W - 22);
    snprintf(buf, sizeof buf, "Create %s", new_name);
    if (text_width(buf, strlen(buf), S_SMALL) > BOT_CREATE.w - 30) snprintf(buf, sizeof buf, "Create it");
    button(&BOT_CREATE, buf, GLYPH_A, COL_BLUE, COL_ON);
    button(&BOT_DESC, new_desc[0] ? "Description" : "Add description", GLYPH_X, COL_PAPER_2, COL_INK);
    button(&BOT_CANCEL, "Cancel", GLYPH_B, COL_PAPER_2, COL_INK);
    if (create_err[0]) text_block(create_err, 16, 188, BOT_W - 32, S_SMALL, &DIM_COLORS, 4, NULL);
}

static void bots_bottom(void)
{
    char buf[160];
    if (bstep != BOTS_LIST) {
        confirm_bottom();
        return;
    }
    bots_clamp();
    snprintf(buf, sizeof buf, app.nagents == 1 ? "1 AGENT" : "%d AGENTS", app.nagents);
    spaced(buf, 11, 36, S_EYE, COL_INK_2, 1.4f);
    // Under it: a list that failed to refresh (it is asked for again), else bots that didn't fit.
    float sw = NEW_BOT.x - 16 - 11;
    if (app.agents_err[0] && !app.agents_old) {
        snprintf(buf, sizeof buf, "Couldn't refresh: %s", app.agents_err);
        text_draw_fit(buf, 11, 47, S_TOOL, COL_BAD, sw);
    } else if (app.agents_more > 0) {
        snprintf(buf, sizeof buf, "%d more on the PC (no room here)", app.agents_more);
        text_draw_fit(buf, 11, 47, S_TOOL, COL_TX_3, sw);
    } else {
        text_draw("Each one keeps its own chats", 11, 47, S_TOOL, COL_TX_3);
    }
    bool can = !bots_trouble() && app.agents_loaded && app.nagents < AGENT_MAX;
    button(&NEW_BOT, "+ New bot", NULL, can ? COL_BLUE : COL_PAPER_2, can ? COL_ON : COL_TX_3);
    if (app.agents_req) braille(NEW_BOT.x - 14, NEW_BOT.y + 8, 9, COL_BLUE);

    Rect p = {8, BOTS_Y, BOT_W - 16, BOTS_ROWS * ROW_H};
    rect(p.x, p.y, p.w, p.h, COL_PAPER);
    frame_rect(p.x, p.y, p.w, p.h, COL_RULE);
    int shown = 0;
    for (int i = 0; i < BOTS_ROWS && bot_top + i < app.nagents; i++, shown++) {
        Rect r = {p.x, p.y + i * ROW_H, p.w, ROW_H};
        int k = bot_top + i;
        bool sel = k == bot_sel && bstep == BOTS_LIST;
        if (i) rect(r.x + 1, r.y, r.w - 2, 1, COL_RULE);
        if (sel) {
            rect(r.x, r.y, r.w, r.h, COL_BLUE_SOFT);
            frame_rect(r.x, r.y, r.w, r.h, COL_BLUE);
            frame_rect(r.x + 1, r.y + 1, r.w - 2, r.h - 2, COL_BLUE);
        }
        if (k == app.cur) rect(r.x + 2, r.y + 2, 3, r.h - 4, COL_BLUE);
        bot_row(k, &r, sel);
    }
    if (shown < BOTS_ROWS) {  // room under the rows: why there are no bots, or how to make one
        const char *why = bots_trouble();
        const char *s = why                    ? why
                        : app.agents_loaded && app.nagents > 1
                            ? "Press " GLYPH_X " to add another bot, or use Bot Mode in Hermes on the PC."
                        : app.agents_loaded    ? "No bots yet. Press " GLYPH_X " to make one here, or use Bot Mode in "
                                                 "Hermes on the PC."
                        : app.net != NET_ONLINE ? "Connect to the PC to see its bots."
                        : app.agents_err[0]     ? "The PC couldn't list its bots yet. Asking again shortly..."
                                                : "Asking the PC about its bots...";
        float y = p.y + shown * ROW_H + 8;
        int lines = (int)((p.y + p.h - y) / 12);
        if (!why && app.agents_loaded && app.nagents > 1 && lines < 2) s = "Press " GLYPH_X " to add another bot.";
        rect(p.x + 1, y - 8, p.w - 2, 1, COL_RULE);
        text_block(s, p.x + 12, y, p.w - 24, S_SMALL, why ? &DIM_COLORS : &BODY_COLORS, lines, NULL);
    }
    if (app.nagents > BOTS_ROWS) {  // scroll bar
        float track = p.h - 4, h = track * BOTS_ROWS / app.nagents, pos = track * bot_top / app.nagents;
        if (h < 8) h = 8;
        rect(p.x + p.w - 4, p.y + 2 + pos, 2, h, COL_LINE_B);
    }
    text_draw(GLYPH_A " Chat · " GLYPH_X " New bot · " GLYPH_Y " Refresh", 11, BOTS_Y + BOTS_ROWS * ROW_H + 5, S_TOOL,
              COL_TX_3);
}

static void confirm_top(void)
{
    char buf[200];
    const Agent *m = app.agents;
    tag(bstep == BOTS_CREATING ? "CREATING" : "NEW BOT", PAD_X, 24, true);
    big_title(new_name, 37);
    float y = 70;
    if (new_desc[0]) y += text_block(new_desc, PAD_X, y, TOP_W - 2 * PAD_X, S_BODY, &BODY_COLORS, 3, NULL) + 6;
    else {
        text_draw("No description. " GLYPH_X " adds one.", PAD_X, y, S_SMALL, COL_TX_3);
        y += 18;
    }
    rect(PAD_X, y, TOP_W - 2 * PAD_X, 1, COL_RULE);
    y += 8;
    snprintf(buf, sizeof buf,
             "A new Hermes profile, copied from %s: its model, settings, API keys, SOUL, skills and memory notes. "
             "Its chats, sessions and jobs start empty, and it shows up in Bot Mode on the PC too.",
             agent_title(m));
    y += text_block(buf, PAD_X, y, TOP_W - 2 * PAD_X, S_SMALL, &BODY_COLORS, 4, NULL) + 6;
    if (bstep == BOTS_CREATING) {
        braille(PAD_X, y + 1, 10, COL_BLUE);
        text_draw("Setting it up on the PC...", PAD_X + 14, y, S_SMALL, COL_TX_2);
    } else if (create_err[0]) {
        text_block(create_err, PAD_X, y, TOP_W - 2 * PAD_X, S_SMALL, &DIM_COLORS, 3, NULL);
    }
    draw_header("New bot");
    float fy = footer_begin(), fx = 7;
    if (bstep == BOTS_CONFIRM) {
        fx += hint(GLYPH_A, "Create", fx, fy, COL_ON);
        fx += hint(GLYPH_X, new_desc[0] ? "Edit description" : "Add description", fx, fy, COL_ON);
        hint(GLYPH_B, "Cancel", fx, fy, COL_ON);
    } else {
        hint(GLYPH_B, "Back to the list", fx, fy, COL_ON);
    }
}

static void bots_top(void)
{
    if (bstep != BOTS_LIST) {
        confirm_top();
        return;
    }
    bots_clamp();
    const Agent *a = &app.agents[bot_sel];
    bool cur = bot_sel == app.cur;
    char buf[200];
    snprintf(buf, sizeof buf, "Bots · %d of %d", bot_sel + 1, app.nagents);
    // Its picture at the right, 1:1 (the PC draws it at 36 px)
    float right = TOP_W - PAD_X;
    if (agent_picture(a, right - 36, 24, 36)) right -= 36 + 10;
    float x = PAD_X + tag(a == app.agents ? "MAIN AGENT" : "BOT", PAD_X, 24, cur) + 6;
    char idb[80];
    snprintf(idb, sizeof idb, a != app.agents ? "@%s" : a->id[0] ? "profile %s" : "", a->id);
    text_draw_fit(idb, x, 24.5f, S_TOOL, COL_TX_3, right - x);
    text_draw_fit(agent_title(a), PAD_X, 37, S_BIG * 0.72f, COL_INK, right - PAD_X);
    const char *desc = a->desc[0]                ? a->desc
                       : a == app.agents         ? "The agent this gateway runs as. Bots are your other Hermes profiles, "
                                                   "each with its own sessions, memory and skills."
                                                 : "A Hermes profile with its own sessions, memory and skills.";
    float y = 70 + text_block(desc, PAD_X, 70, TOP_W - 2 * PAD_X, S_BODY, &BODY_COLORS, 3, NULL) + 8;
    rect(PAD_X, y, TOP_W - 2 * PAD_X, 1, COL_RULE);
    y += 8;
    const char *model = a->chat.model[0] ? a->chat.model : a->model;
    kv("MODEL", model[0] ? model : "-", y, COL_INK);
    kv("SESSION", a->chat.title[0] ? a->chat.title : a->chat.session_id[0] ? "untitled" : "none yet", y + 17, COL_INK);
    u32 sc = COL_INK;
    char st[64];
    if (asks(a)) snprintf(st, sizeof st, "waiting for you"), sc = COL_BAD;
    else if (a->chat.typing) snprintf(st, sizeof st, "working"), sc = COL_BLUE;
    else if (a->unreachable) snprintf(st, sizeof st, "not reachable: turn on 3ds in its profile"), sc = COL_BAD;
    else if (a->unread) snprintf(st, sizeof st, "%d new repl%s", a->unread, a->unread == 1 ? "y" : "ies");
    else snprintf(st, sizeof st, "idle");
    kv("STATUS", st, y + 34, sc);
    if (a->voice_known) kv("VOICE", a->voice ? "speaks its replies" : "text replies", y + 51, COL_INK);
    snprintf(buf, sizeof buf, "Bots · %d of %d", bot_sel + 1, app.nagents);
    draw_header(buf);
    top_footer(cur ? "Back to chat" : "Chat", "New bot", "Refresh");
}

// --- dispatch -----------------------------------------------------------------

void tabs_input(u32 down, u32 held, const touchPosition *t)
{
    switch (app.tab) {
    case TAB_BOTS: bots_input(down, held, t); break;
    case TAB_SESSIONS: sessions_input(down, held, t); break;
    case TAB_SKILLS: skills_input(down, held, t); break;
    case TAB_JOBS: jobs_input(down, held, t); break;
    case TAB_MORE: more_input(down, held, t); break;
    default: break;
    }
}

void tabs_draw_top(void)
{
    rect(0, 0, TOP_W, SCR_H, COL_PAPER);
    switch (app.tab) {
    case TAB_BOTS: bots_top(); break;
    case TAB_SESSIONS: sessions_top(); break;
    case TAB_SKILLS: skills_top(); break;
    case TAB_JOBS: jobs_top(); break;
    case TAB_MORE: more_top(); break;
    default: break;
    }
}

void tabs_draw_bottom(void)
{
    rect(0, 0, BOT_W, SCR_H, COL_PAPER_2);
    switch (app.tab) {
    case TAB_BOTS: bots_bottom(); break;
    case TAB_SESSIONS: sessions_bottom(); break;
    case TAB_SKILLS: skills_bottom(); break;
    case TAB_JOBS: jobs_bottom(); break;
    case TAB_MORE: more_bottom(); break;
    default: break;
    }
    draw_tabs();
}
