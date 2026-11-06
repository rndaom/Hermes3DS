// App state shared by the logic (app.c) and the renderer (draw.c).
#pragma once

#include <3ds.h>
#include <citro2d.h>
#include <stdbool.h>

#include "blob.h"
#include "chat.h"
#include "net.h"
#include "store.h"

// Same packing as C2D_Color32, but usable in constant initializers.
#define C32(r, g, b, a) ((u32)(r) | ((u32)(g) << 8) | ((u32)(b) << 16) | ((u32)(a) << 24))

// Design tokens (design/mockups/lib.mjs).
#define COL_PAPER C32(0xff, 0xff, 0xff, 0xff)
#define COL_PAPER_2 C32(0xf2, 0xf2, 0xf4, 0xff)
#define COL_SCR C32(0xfb, 0xfb, 0xfe, 0xff)
#define COL_SCR_2 C32(0xec, 0xec, 0xfc, 0xff)
#define COL_INK C32(0x0d, 0x0d, 0x24, 0xff)
#define COL_INK_2 C32(0x43, 0x43, 0x6a, 0xff)
#define COL_INK_3 C32(0x5f, 0x5f, 0x88, 0xff)
#define COL_TX_2 C32(0x3a, 0x3a, 0x63, 0xff)
#define COL_TX_3 C32(0x66, 0x66, 0x8e, 0xff)
#define COL_BLUE C32(0x00, 0x00, 0xf2, 0xff)
#define COL_BLUE_SOFT C32(0xee, 0xee, 0xfe, 0xff)  // rgba(0,0,242,.07) on white
#define COL_LINE_B C32(0xb3, 0xb3, 0xfb, 0xff)     // rgba(0,0,242,.30) on white
#define COL_RULE C32(0xdb, 0xdb, 0xdf, 0xff)       // rgba(13,13,36,.16) on paper
#define COL_ON C32(0xff, 0xff, 0xff, 0xff)
#define COL_ON_2 C32(0xcc, 0xcc, 0xfc, 0xff)  // white .80 on blue
#define COL_ON_3 C32(0x94, 0x94, 0xf9, 0xff)  // white .58 on blue
#define COL_ACCENT C32(0xf2, 0xf2, 0x00, 0xff)
#define COL_OK C32(0x0b, 0x7a, 0x3a, 0xff)
#define COL_BAD C32(0xc8, 0x10, 0x1c, 0xff)

typedef enum { SCR_PAIR, SCR_CHAT } Screen;

// Bottom-screen tabs (SCR_CHAT only). L/R or a tap switches them.
typedef enum { TAB_CHAT, TAB_BOTS, TAB_SESSIONS, TAB_SKILLS, TAB_JOBS, TAB_MORE, TAB_COUNT } Tab;

// The agents this 3DS can talk to (protocol.md §4.8): the main Hermes agent, then the bots, which are Hermes
// profiles the gateway also serves (Bot Mode shows the same ones). Each has its own chat; the Sessions, Skills
// and Jobs tabs and the model picker follow the one selected.
#define AGENT_MAX 12
typedef struct {
    char id[68];       // profile name on the wire; "" for the main agent until the roster names it
    char title[64];    // what the roster calls it ("Hermes" for the main agent)
    char desc[480];
    char model[64];
    BlobRef avatar;    // its Bot Mode picture (a PNG blob) when has_avatar
    bool has_avatar;
    bool primary;
    bool listed;       // in the last agents.list
    bool unreachable;  // listed, but the PC has no 3DS connection into its profile (`ready: false`)
    bool voice_known;  // the PC said whether Hermes speaks its replies in this chat (/voice)...
    bool voice;        // ...and this is what it said
    bool voice_synced; // /voice was brought in line with Spoken replies on this connection
    bool status_asked; // a status was asked for on this connection
    bool history_asked;
    uint32_t history_req; // the sessions.messages for its chat in flight (others' replies are a tab's preview)
    char draft[3072];     // what was typed for it and not sent
    int unread;        // replies that arrived while another agent was selected
    // The approval and question last seen in its chat: the PC re-sends pending ones after a reconnect, and those
    // mustn't switch chats or sound again.
    char seen_req[72], seen_cid[72];
    Chat chat;
} Agent;

typedef enum {
    PAIR_SCAN,     // discovery running
    PAIR_LIST,     // pick a PC (or enter an address)
    PAIR_CONFIRM,  // code entered: compare fingerprints, A to pair
    PAIR_WORKING,  // handshake with the pairing token
    PAIR_ERROR,
} PairStep;

typedef struct {
    Screen screen;
    Tab tab;
    bool quit;
    bool dirty;
    u64 now;

    Agent agents[AGENT_MAX];
    int nagents;           // agents[0] is the main agent
    int cur;               // the agent selected
    Chat *chat;            // &agents[cur].chat
    bool agents_loaded;    // the PC answered agents.list
    bool agents_old;       // ...with bad_request: a plugin from before bots
    bool agents_restored;  // the bot selected last time was looked for in the roster
    uint32_t agents_req;   // agents.list in flight
    bool multiplex;        // the gateway serves several profiles (without it there are no bots)
    int agents_more;       // bots the PC lists that don't fit in agents[]
    char agents_err[120];  // why the last agents.list failed ("" once one worked)
    u64 agents_retry_at;   // ask again at this time after a failure that may pass (0 = not planned)
    u32 agents_retry_ms;
    ServerInfo server;
    bool have_server;
    NetState net;
    int retry_s;
    u64 retry_at;
    char net_detail[128];
    uint32_t handshake_ms;
    NetRoute route;        // how the current connection reaches the PC (status bar: LAN / AWAY)
    TlsInfo tls;           // the away route's TLS handshake
    uint32_t next_id;
    char device[64];
    long tz_off;           // 3DS clock (local time) minus Unix time, from status_r `now`
    char switch_name[96];  // what the Sessions tab called the session being resumed

    // pairing
    PairStep pstep;
    int nfound;
    DiscoveryReply found[NET_MAX_FOUND];
    ServerInfo found_info[NET_MAX_FOUND];
    int sel;
    ServerInfo cand;
    uint8_t token[8];
    size_t token_len;
    char pair_msg[160];
    bool manual_lookup;  // the discovery in flight is a unicast lookup of a typed address

    // chat view
    float scroll;      // pixels above the bottom (0 = follow new text)
    float content_h;
    float view_h;
    u64 chat_shown_at;     // when the chat showing became the selected agent's (question panels arm after it)
    char toast[96];
    u64 toast_until;
    Settings settings;
} App;

extern App app;

// Shared by app.c and tabs.c.
uint32_t app_request(const char *t, json_t *fields);  // sends {t, id, ...fields}; returns the id
void app_toast(const char *msg);
void app_send_text(const char *text);  // a chat message or slash command, shown in the transcript
// Agents (protocol.md §4.8). Requests app_request makes for the selected agent carry its name when it is a bot.
Agent *app_agent(void);                        // the selected one
bool app_is_current(const json_t *msg);        // an event or reply for the selected agent
uint32_t app_request_for(const Agent *a, const char *t, json_t *fields);
void app_refresh_agents(void);                 // asks the PC for the roster
Agent *agent_by_id(const char *id);            // "" is the main agent; NULL when the 3DS doesn't keep it
const char *agent_title(const Agent *a);       // never empty
void app_select_agent(int i);                  // show agent i's chat; the tabs follow it
void app_agents_reply(json_t *msg);            // agents.list_r
// agents.create_r: selects the new bot unless you are busy here. 1: selected; 0: it waits in Bots; -1: no room
// for it on the 3DS (every slot holds the chat showing or a question).
int app_agent_created(const json_t *item);
bool app_any_waiting(void);                    // some agent waits for an approval or an answer
// System keyboard; false when cancelled (out then holds what was typed).
bool app_keyboard(const char *hint, const char *initial, char *out, size_t cap, int maxlen, bool multiline,
                  const char *ok_label);
void app_compose(const char *initial);  // keyboard prefilled with `initial`, then send
// A photo or voice message: the bubble (with `attach`, a §4.7 attachment the 3DS has cached under its blob
// ids) shows at once, `data` uploads as blob `id` (ownership passes on), then `send` goes out with `fields`
// (+ `text` when not empty). Takes ownership of attach and fields.
void app_send_media(const char *id, uint8_t *data, size_t len, const char *mime, const char *name, json_t *attach,
                    json_t *fields, const char *text);
void app_set_sounds(bool on);  // saved on the SD card
void app_set_speak(bool on);   // saved, and Hermes' /voice on|off
// What Y plays/pauses in the chat: a reply being spoken, the current track when it is in this chat, else
// its newest audio.
const json_t *app_chat_audio(void);

void app_init(void);
void app_exit(void);
void app_handle_event(NetEvent *ev);
void app_input(void);
void app_tick(void);       // once a frame: timed retries
bool app_animating(void);  // spinner / caret / toast need redraws

void draw_init(void);
void draw_exit(void);
void draw_frame(C3D_RenderTarget *top, C3D_RenderTarget *bottom);

// Layout shared by draw.c and app.c (touch areas).
#define TOP_HEADER_H 16
#define TOP_FOOTER_H 15
#define TOP_STATUS_H 16
#define TABS_H 25
#define N_CHIPS 5

typedef struct {
    float x, y, w, h;
} Rect;

typedef enum { STRIP_NONE, STRIP_WORKING, STRIP_REFUSED } StripKind;

// Approval choices, in protocol.md §4.5 order; the bottom screen shows the ones the request offers.
enum { APPR_ONCE, APPR_SESSION, APPR_ALWAYS, APPR_DENY, APPR_N };
extern const char *const APPR_CHOICE[APPR_N];

#define CLAR_ROWS 5  // clarify choices the bottom panel shows at once (it scrolls past that)

typedef struct {
    Rect tabs[TAB_COUNT];
    Rect appr[APPR_N];  // approval panel buttons (w == 0: not offered)
    // Clarify panel: question top, then choice rows clar[0..clar_rows) showing choices clar_first.., then
    // "Type an answer" (and, for multi-select, "Send") along the bottom.
    float clar_q_h;
    int clar_first, clar_rows;
    Rect clar[CLAR_ROWS], clar_type, clar_send;
    StripKind strip_kind;
    Rect strip, strip_btn;
    Rect input;
    Rect talk, photo;  // quick tiles under the input
    Rect panel;
    Rect files_btn;  // w == 0 while the chat has no files
    Rect chips[N_CHIPS];
} BottomLayout;

extern const char *const CHIP_CMDS[N_CHIPS];

void bottom_layout(BottomLayout *l);
int clar_count(void);          // the pending clarify's choices (0: open-ended)
const char *clar_choice(int i);
bool clar_multi(void);         // several choices may be picked
static inline bool rect_hit(const Rect *r, int x, int y)
{
    return x >= r->x && x < r->x + r->w && y >= r->y && y < r->y + r->h;
}
