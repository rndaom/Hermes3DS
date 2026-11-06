#include "chat.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "log.h"

static const char *jstr(const json_t *o, const char *k)
{
    const char *s = json_string_value(json_object_get(o, k));
    return s ? s : "";
}

static void mid_of(const json_t *o, char out[32])
{
    const json_t *v = json_object_get(o, "mid");
    if (json_is_string(v)) snprintf(out, 32, "%s", json_string_value(v));
    else if (json_is_integer(v)) snprintf(out, 32, "h%lld", (long long)json_integer_value(v));
    else out[0] = 0;
}

static void reveal_all(Msg *m)
{
    m->shown = m->len;
    m->shown_f = (float)m->len;
}

// Out of memory, these leave the text as it was (logged).
static void set_text(Msg *m, const char *s, size_t n)
{
    if (n + 1 > m->cap) {
        char *t = realloc(m->text, n + 64);
        if (!t) {
            log_printf("chat: out of memory for a %lu byte message", (unsigned long)n);
            return;
        }
        m->text = t;
        m->cap = (uint32_t)(n + 64);
    }
    memcpy(m->text, s, n);
    m->text[n] = 0;
    m->len = (uint32_t)n;
    if (m->shown > m->len) reveal_all(m);  // an edit shortened the text
    while (m->shown < m->len && ((unsigned char)m->text[m->shown] & 0xC0) == 0x80) m->shown++;
    if (m->shown_f < m->shown) m->shown_f = (float)m->shown;
    m->dirty = true;
}

static void append_text(Msg *m, const char *s, size_t n)
{
    if (m->len + n + 1 > m->cap) {
        uint32_t cap = (uint32_t)((m->len + n + 1) * 3 / 2 + 64);
        char *t = realloc(m->text, cap);
        if (!t) {
            log_printf("chat: out of memory, dropped %lu bytes of a message", (unsigned long)n);
            return;
        }
        m->text = t;
        m->cap = cap;
    }
    memcpy(m->text + m->len, s, n);
    m->len += (uint32_t)n;
    m->text[m->len] = 0;
    free(m->tl.segs);  // spans are clipped to the old length: rebuilt by the layout pass
    m->tl.segs = NULL;
    m->dirty = true;
}

static void set_spans(Msg *m, json_t *spans)
{
    json_decref(m->spans);
    m->spans = json_is_array(spans) ? json_incref(spans) : NULL;
    free(m->tl.segs);  // rebuilt by the layout pass
    m->tl.segs = NULL;
    m->dirty = true;
}

static void msg_free(Msg *m)
{
    if (!m) return;
    free(m->text);
    json_decref(m->spans);
    json_decref(m->attach);
    text_layout_free(&m->tl);
    free(m);
}

static Msg *msg_new(Role role)
{
    Msg *m = calloc(1, sizeof *m);
    m->role = role;
    m->final = true;
    m->dirty = true;
    m->local_time = time(NULL);
    set_text(m, "", 0);
    return m;
}

static void push(Chat *c, Msg *m)
{
    if (m->role != R_ASSISTANT && m->role != R_SYSTEM) reveal_all(m);
    if (c->n == CHAT_MAX) {
        msg_free(c->m[0]);
        memmove(c->m, c->m + 1, (CHAT_MAX - 1) * sizeof c->m[0]);
        c->n--;
    }
    c->m[c->n++] = m;
    c->version++;
}

static Msg *find(Chat *c, const char *mid)
{
    if (!mid[0]) return NULL;
    for (int i = c->n - 1; i >= 0; i--)
        if (!strcmp(c->m[i]->mid, mid)) return c->m[i];
    return NULL;
}

void chat_init(Chat *c) { memset(c, 0, sizeof *c); }

void chat_clear_transcript(Chat *c)
{
    for (int i = 0; i < c->n; i++) msg_free(c->m[i]);
    c->n = 0;
    c->version++;
}

void chat_clear(Chat *c)
{
    chat_clear_transcript(c);
    json_decref(c->approval);
    json_decref(c->clarify);
    c->approval = c->clarify = NULL;
    c->typing = c->stopping = false;
    c->version++;
}

void chat_reconnected(Chat *c)
{
    json_decref(c->approval);
    json_decref(c->clarify);
    c->approval = c->clarify = NULL;
    c->prompt_req = 0;
    c->stopping = false;
    c->version++;
}

void chat_link_lost(Chat *c)
{
    for (int i = 0; i < c->n; i++) {
        Msg *m = c->m[i];
        if (m->role == R_USER && m->pending && m->req_id) {
            m->pending = false;
            m->failed = true;
            m->dirty = true;
        }
    }
    c->version++;
}

void chat_free(Chat *c)
{
    chat_clear(c);
    json_decref(c->approval);
    json_decref(c->clarify);
    memset(c, 0, sizeof *c);
}

Msg *chat_add_user(Chat *c, const char *text, uint32_t req_id)
{
    Msg *m = msg_new(R_USER);
    set_text(m, text, strlen(text));
    reveal_all(m);
    m->req_id = req_id;
    m->pending = true;
    push(c, m);
    return m;
}

Msg *chat_find_local(Chat *c, uint32_t local_id)
{
    for (int i = c->n - 1; i >= 0 && local_id; i--)
        if (c->m[i]->role == R_USER && c->m[i]->local_id == local_id) return c->m[i];
    return NULL;
}

void chat_notice(Chat *c, const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    Msg *m = msg_new(R_SYSTEM);
    set_text(m, buf, strlen(buf));
    reveal_all(m);
    push(c, m);
}

static void finish_tools(Chat *c)
{
    for (int i = c->n - 1; i >= 0 && i >= c->n - 40; i--) {
        Msg *m = c->m[i];
        if (m->role == R_TOOL && m->running) {
            m->running = false;
            m->ms = (u32)(osGetTime() - m->t0);
            m->dirty = true;
        }
    }
}

static void prompt_card(Chat *c, const char *kind, const char *id, const char *text)
{
    Msg *m = msg_new(R_PROMPT);
    snprintf(m->mid, sizeof m->mid, "%s:%s", kind, id);
    set_text(m, text, strlen(text));
    push(c, m);
}

static void resolve_card(Chat *c, const char *kind, const char *id, const char *how)
{
    char mid[32];
    snprintf(mid, sizeof mid, "%s:%s", kind, id);
    Msg *m = find(c, mid);
    if (!m || m->resolved) return;
    m->resolved = true;
    if (how && how[0]) {
        append_text(m, "\n-> ", 4);
        append_text(m, how, strlen(how));
    }
    c->version++;
}

void chat_handle(Chat *c, json_t *msg)
{
    const char *t = jstr(msg, "t");
    char mid[32];

    if (!strcmp(t, "msg")) {
        mid_of(msg, mid);
        Msg *m = find(c, mid);  // offline replay re-sends changed messages with the same mid
        bool created = !m;
        if (!m) {
            const char *role = jstr(msg, "role"), *kind = jstr(msg, "kind");
            m = msg_new(!strcmp(role, "assistant") && (!kind[0] || !strcmp(kind, "reply") || !strcmp(kind, "cron"))
                            ? R_ASSISTANT
                            : R_SYSTEM);
            snprintf(m->mid, sizeof m->mid, "%s", mid);
            push(c, m);
        }
        // A message new to this chat was never final here: a reply that arrives whole (streaming off, media, cron,
        // or replayed after a sleep) counts as a reply just like a streamed one that ends.
        bool fresh = m->len == 0 && m->shown == 0, was_final = !created && m->final;
        const char *text = jstr(msg, "text");
        set_text(m, text, strlen(text));
        set_spans(m, json_object_get(msg, "spans"));
        json_t *attach = json_object_get(msg, "attach");
        if (json_array_size(attach)) {
            json_decref(m->attach);
            m->attach = json_incref(attach);
            m->dirty = true;
        }
        m->final = json_is_true(json_object_get(msg, "final"));
        if (m->final && !was_final && m->role == R_ASSISTANT) c->replied = true;
        if (!fresh || m->final) reveal_all(m);  // only a live stream is typed out
        if (!strcmp(jstr(msg, "kind"), "command")) c->want_status = true;  // /new, /resume, /model, /title...
    } else if (!strcmp(t, "delete")) {  // a streamed preview the PC replaced or dropped
        mid_of(msg, mid);
        for (int i = 0; i < c->n; i++)
            if (mid[0] && !strcmp(c->m[i]->mid, mid)) {
                msg_free(c->m[i]);
                memmove(&c->m[i], &c->m[i + 1], (size_t)(c->n - i - 1) * sizeof c->m[0]);
                c->n--;
                break;
            }
    } else if (!strcmp(t, "append")) {
        mid_of(msg, mid);
        Msg *m = find(c, mid);
        if (!m) return;
        const char *text = jstr(msg, "text");
        append_text(m, text, strlen(text));
        json_t *spans = json_object_get(msg, "spans");  // the whole list, only when it changed
        if (spans) set_spans(m, spans);
        bool was_final = m->final;
        m->final = json_is_true(json_object_get(msg, "final"));
        if (m->final && !was_final && m->role == R_ASSISTANT) c->replied = true;
    } else if (!strcmp(t, "edit")) {
        mid_of(msg, mid);
        Msg *m = find(c, mid);
        if (!m) return;
        const char *text = jstr(msg, "text");
        set_text(m, text, strlen(text));
        set_spans(m, json_object_get(msg, "spans"));
        bool was_final = m->final;
        m->final = json_is_true(json_object_get(msg, "final"));
        if (m->final && !was_final && m->role == R_ASSISTANT) c->replied = true;
    } else if (!strcmp(t, "typing")) {
        bool on = json_is_true(json_object_get(msg, "on"));
        if (on && !c->typing) c->typing_since = osGetTime();
        if (!on) {
            finish_tools(c);
            c->want_status = true;  // the title may have changed
        }
        c->typing = on;
        if (!on) c->stopping = false;
    } else if (!strcmp(t, "tool")) {
        const char *state = jstr(msg, "state"), *text = jstr(msg, "text");
        if (!strcmp(state, "start")) {
            Msg *m = msg_new(R_TOOL);
            set_text(m, text[0] ? text : "Working", strlen(text[0] ? text : "Working"));
            m->running = true;
            m->t0 = osGetTime();
            push(c, m);
        } else {
            for (int i = c->n - 1; i >= 0; i--) {
                Msg *m = c->m[i];
                if (m->role == R_TOOL && m->running) {
                    m->running = false;
                    m->ms = (u32)(osGetTime() - m->t0);
                    if (text[0]) set_text(m, text, strlen(text));
                    m->dirty = true;
                    break;
                }
            }
        }
    } else if (!strcmp(t, "approval")) {
        if (!c->approval || strcmp(jstr(c->approval, "req"), jstr(msg, "req"))) c->prompt_since = osGetTime();
        // The pinned card shows it while it waits; the transcript gets a record once it's answered.
        json_decref(c->approval);
        c->approval = json_incref(msg);
    } else if (!strcmp(t, "approval_done")) {
        const char *req = jstr(msg, "req"), *choice = jstr(msg, "choice");
        const char *how = !strcmp(choice, "once")      ? "Allowed once"
                          : !strcmp(choice, "session") ? "Allowed for this session"
                          : !strcmp(choice, "always")  ? "Always allowed"
                          : !strcmp(choice, "deny")    ? "Denied"
                          : !strcmp(choice, "timeout") ? "Timed out"
                                                       : "Done";
        c->prompt_req = 0;
        if (c->approval && !strcmp(jstr(c->approval, "req"), req)) {
            char buf[600];
            snprintf(buf, sizeof buf, "Command approval\n%s", jstr(c->approval, "command"));
            prompt_card(c, "a", req, buf);
            resolve_card(c, "a", req, how);
            json_decref(c->approval);
            c->approval = NULL;
            if (c->clarify) c->prompt_since = osGetTime();  // the clarify panel it covered shows now
        } else {
            resolve_card(c, "a", req, how);
        }
    } else if (!strcmp(t, "clarify")) {
        bool fresh = !c->clarify || strcmp(jstr(c->clarify, "cid"), jstr(msg, "cid"));
        if (fresh && !c->approval) c->prompt_since = osGetTime();  // (behind an approval it shows later)
        if (fresh) {
            c->clar_cur = 0;
            c->clar_sel = 0;
        }
        json_decref(c->clarify);
        c->clarify = json_incref(msg);
        char buf[900];
        int n = snprintf(buf, sizeof buf, "%s", jstr(msg, "question"));
        json_t *choices = json_object_get(msg, "choices");
        for (size_t i = 0; i < json_array_size(choices) && n < (int)sizeof buf - 8; i++)
            n += snprintf(buf + n, sizeof buf - (size_t)n, "\n%u. %s", (unsigned)i + 1,
                          json_string_value(json_array_get(choices, i)) ?: "");
        snprintf(mid, sizeof mid, "c:%s", jstr(msg, "cid"));
        if (!find(c, mid)) prompt_card(c, "c", jstr(msg, "cid"), buf);  // re-sent after a reconnect: one card
    } else if (!strcmp(t, "clarify_done")) {
        const char *cid = jstr(msg, "cid");
        resolve_card(c, "c", cid, "Answered");
        c->prompt_req = 0;
        if (c->clarify && !strcmp(jstr(c->clarify, "cid"), cid)) {
            json_decref(c->clarify);
            c->clarify = NULL;
        }
    } else if (!strcmp(t, "send_r")) {
        json_int_t id = json_integer_value(json_object_get(msg, "id"));
        for (int i = c->n - 1; i >= 0; i--)
            if (c->m[i]->role == R_USER && c->m[i]->req_id == (uint32_t)id) {
                c->m[i]->pending = false;
                c->m[i]->failed = false;  // it did arrive (resent after a reconnect)
                c->m[i]->dirty = true;
                break;
            }
    } else if (!strcmp(t, "approve_r")) {
        c->prompt_req = 0;
    } else if (!strcmp(t, "answer_r")) {
        c->prompt_req = 0;
        if (!json_is_true(json_object_get(msg, "ok")) && c->clarify) {  // no longer waiting on the PC
            resolve_card(c, "c", jstr(c->clarify, "cid"), "Expired");
            json_decref(c->clarify);
            c->clarify = NULL;
            chat_notice(c, "That question had already been answered or had expired.");
        }
    } else if (!strcmp(t, "heard")) {  // the transcript of a voice message (protocol.md §4.4.1)
        json_int_t id = json_integer_value(json_object_get(msg, "req"));
        const char *text = jstr(msg, "text");
        for (int i = c->n - 1; i >= 0; i--)
            if (c->m[i]->role == R_USER && c->m[i]->req_id == (uint32_t)id) {
                Msg *m = c->m[i];
                set_text(m, text, strlen(text));
                reveal_all(m);
                free(m->tl.segs);  // plain segments for the old length
                m->tl.segs = NULL;
                if (m->len) {  // the words replace the recording (kept only when nothing was heard)
                    json_decref(m->attach);
                    m->attach = NULL;
                }
                break;
            }
    } else if (!strcmp(t, "error")) {
        const char *text = jstr(msg, "msg");
        uint32_t id = (uint32_t)json_integer_value(json_object_get(msg, "id"));
        if (id && id == c->prompt_req) {  // the approval / question this answered is gone on the PC
            c->prompt_req = 0;
            if (c->approval) {
                resolve_card(c, "a", jstr(c->approval, "req"), "Expired");
                json_decref(c->approval);
                c->approval = NULL;
                if (c->clarify) c->prompt_since = osGetTime();
            } else if (c->clarify) {
                resolve_card(c, "c", jstr(c->clarify, "cid"), "Expired");
                json_decref(c->clarify);
                c->clarify = NULL;
            }
            chat_notice(c, "That question had already been answered or had expired.");
            c->version++;
            return;
        }
        for (int i = c->n - 1; id && i >= 0; i--)
            if (c->m[i]->role == R_USER && c->m[i]->req_id == id && c->m[i]->pending) {
                c->m[i]->pending = false;  // the PC refused the send: say so on the bubble
                c->m[i]->failed = true;
                c->m[i]->dirty = true;
                break;
            }
        chat_notice(c, "Error: %s", text[0] ? text : jstr(msg, "code"));
    } else if (!strcmp(t, "status_r")) {
        json_t *s = json_object_get(msg, "session");
        snprintf(c->title, sizeof c->title, "%s", jstr(s, "title"));
        snprintf(c->model, sizeof c->model, "%s", jstr(s, "model"));
        if (jstr(s, "id")[0]) snprintf(c->session_id, sizeof c->session_id, "%s", jstr(s, "id"));
    } else {
        return;  // unknown types are ignored (protocol.md §4)
    }
    c->version++;
}

void chat_load_history(Chat *c, json_t *items, long tz_off)
{
    size_t n = json_array_size(items);
    Msg *hist[64];
    int nh = 0;
    for (size_t i = 0; i < n && nh < 64; i++) {
        json_t *it = json_array_get(items, i);
        const char *role = jstr(it, "role"), *text = jstr(it, "text");
        if (!text[0]) continue;
        bool dup = false;  // the outbox replay may already have shown it
        for (int k = 0; k < c->n && !dup; k++) dup = !strcmp(c->m[k]->text, text);
        if (dup) continue;
        Msg *m = msg_new(!strcmp(role, "user") ? R_USER : !strcmp(role, "tool") ? R_TOOL : R_ASSISTANT);
        mid_of(it, m->mid);
        set_text(m, text, strlen(text));
        set_spans(m, json_object_get(it, "spans"));
        reveal_all(m);
        double ts = json_number_value(json_object_get(it, "ts"));
        m->local_time = ts > 0 ? (time_t)ts + tz_off : 0;
        hist[nh++] = m;
    }
    // Keep the newest messages if the history would overflow.
    int room = CHAT_MAX - c->n;
    int skip = nh > room ? nh - room : 0;
    for (int i = 0; i < skip; i++) msg_free(hist[i]);
    int add = nh - skip;
    memmove(c->m + add, c->m, (size_t)c->n * sizeof c->m[0]);
    memcpy(c->m, hist + skip, (size_t)add * sizeof c->m[0]);
    c->n += add;
    c->history_loaded = true;
    c->version++;
}

bool chat_reveal_now(Chat *c, bool stopping)
{
    bool any = false;
    for (int i = 0; i < c->n; i++)
        if (c->m[i]->shown < c->m[i]->len) {
            reveal_all(c->m[i]);
            any = true;
        }
    if (any) c->version++;
    if (stopping && c->typing) c->stopping = true;
    return any;
}

bool chat_tick(Chat *c, float dt)
{
    if (c->stopping) return chat_reveal_now(c, false);
    bool any = false;
    for (int i = c->n - 1; i >= 0; i--) {
        Msg *m = c->m[i];
        if (m->shown >= m->len) continue;
        if (i < c->n - 6) {  // only recent messages stream; late growth further up appears at once
            reveal_all(m);
            c->version++;
            any = true;  // one more frame to draw it
            continue;
        }
        // Drain the backlog in ~0.6 s (0.3 s once final), never slower than 60 bytes/s, so the reveal
        // keeps pace with any edit cadence without falling behind.
        float backlog = (float)(m->len - m->shown);
        float rate = backlog / (m->final ? 0.3f : 0.6f);
        if (rate < 60) rate = 60;
        m->shown_f += rate * dt;
        uint32_t s = m->shown_f >= m->len ? m->len : (uint32_t)m->shown_f;
        while (s < m->len && ((unsigned char)m->text[s] & 0xC0) == 0x80) s++;  // whole code points
        m->shown = s;
        if (m->shown_f < m->shown) m->shown_f = (float)m->shown;
        c->version++;
        any = true;  // also true on the tick that reveals the last bytes, so they get drawn
    }
    return any;
}
