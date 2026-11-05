// The chat transcript: server events (protocol.md §4.4, §4.5) applied to a list of messages.
#pragma once

#include <3ds.h>
#include <jansson.h>
#include <stdbool.h>
#include <time.h>

#include "text.h"

#define CHAT_MAX 160  // oldest messages are dropped beyond this

typedef enum {
    R_USER,
    R_ASSISTANT,
    R_SYSTEM,  // system role, slash-command replies, local notices
    R_TOOL,    // one line of tool activity
    R_PROMPT,  // approval or clarify card
} Role;

typedef struct {
    char mid[32];
    Role role;
    char *text;
    uint32_t len, cap;
    json_t *spans;
    json_t *attach;  // protocol.md §4.7 attachments (array), or NULL
    bool final;
    // Streaming reveal: Hermes sends text in bursts (one edit every streaming.edit_interval), so the
    // client types it out smoothly. `shown` bytes are visible; the rest is drawn over the next frames.
    uint32_t shown;
    float shown_f;
    time_t local_time;  // 3DS clock when it arrived; 0 for history
    uint32_t req_id;    // user messages: id of the `send` request
    bool pending;       // user messages: not acknowledged yet
    bool failed;        // user messages: the upload or send didn't go through
    uint32_t local_id;  // user messages with media: the 3DS's handle until the upload is done
    char upload_id[24]; // user messages with media: the upload still on its way ("" once sent)
    bool running;       // tool lines
    u64 t0;
    u32 ms;
    bool resolved;      // prompts
    // layout cache (ui.c)
    TextLayout tl;
    float layout_w;
    bool dirty;
    float h;
} Msg;

typedef struct {
    Msg *m[CHAT_MAX];
    int n;
    bool typing;
    u64 typing_since;
    bool stopping;     // B stopped the turn: text arriving until it ends shows at once
    json_t *approval;  // pending approval request, or NULL
    json_t *clarify;   // pending clarify request, or NULL
    u64 prompt_since;  // when the approval / clarify panel showing now appeared (osGetTime)
    uint32_t prompt_req;  // id of the approve / answer request still waiting for its reply (0 = none)
    int clar_cur;      // clarify panel: highlighted choice
    uint32_t clar_sel; // clarify panel: ticked choices (multi-select; bit i = choice i)
    char title[96];
    char model[48];
    char session_id[64];
    bool history_loaded;
    bool want_status;  // UI should send a `status` request (turn ended / new session)
    bool replied;      // a live assistant reply just finished (the app plays a sound and clears it)
    unsigned version;  // bumped on every change
} Chat;

void chat_init(Chat *c);
void chat_free(Chat *c);
Msg *chat_add_user(Chat *c, const char *text, uint32_t req_id);
Msg *chat_find_local(Chat *c, uint32_t local_id);  // a user message by its local_id, or NULL
void chat_notice(Chat *c, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
// Apply one server message (ownership stays with the caller).
void chat_handle(Chat *c, json_t *msg);
// Messages for `sessions.messages_r` items, inserted before what is already shown (deduplicated).
// `tz_off` converts their Unix `ts` to the 3DS clock's local time.
void chat_load_history(Chat *c, json_t *items, long tz_off);
void chat_clear(Chat *c);             // transcript and pending prompts
// A new connection is up: drop the approval / clarify panels (the PC re-sends the ones still waiting; the rest
// expired while the 3DS was away) and the stop state of a turn that ended meanwhile.
void chat_reconnected(Chat *c);
// The connection dropped: messages sent on it that the PC never acknowledged may be lost; mark them NOT SENT
// (a late `send_r`, after a resend of the unsent frame, still clears that).
void chat_link_lost(Chat *c);
void chat_clear_transcript(Chat *c);  // messages only
// Advance streaming reveals by dt seconds. Returns true while any text is still being revealed.
bool chat_tick(Chat *c, float dt);
// Show everything typed so far at once; with `stopping`, keep doing so until the turn ends.
// Returns true when there was text still typing out.
bool chat_reveal_now(Chat *c, bool stopping);
