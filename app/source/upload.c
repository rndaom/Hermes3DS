#include "upload.h"

#include <3ds.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app.h"
#include "log.h"

#define MAX_UPLOADS 6
#define CHUNK 16384             // bytes per BLOB frame (the protocol allows 32768; smaller keeps allocations small)
#define IN_FLIGHT (48u << 10)   // queued in the network worker at most; the rest is sent as that drains
#define DONE_WAIT_MS 20000      // all sent but no blob.put_done: ask where the PC is
#define MAX_RETRIES 6

typedef enum { U_FREE, U_WAITING, U_PUTTING, U_SENDING, U_SENT } UState;

typedef struct {
    UState state;
    char id[UPLOAD_ID_LEN];
    char mime[48], name[48];
    uint8_t *data;
    uint32_t len;
    uint32_t next;   // next byte to hand to the network worker
    uint32_t req;    // the blob.put in flight
    uint32_t base;   // where this connection's sending started (progress while chunks drain)
    int retries;
    u64 since;
    UploadDone done;
    uint32_t ctx;
} Upload;

static Upload ups[MAX_UPLOADS];
static bool online;
static uint32_t max_size;

static const char *js(const json_t *o, const char *k)
{
    return json_string_value(json_object_get(o, k)) ?: "";
}

void upload_new_id(char out[UPLOAD_ID_LEN])
{
    uint8_t r[10];
    if (R_FAILED(PS_GenerateRandomBytes(r, sizeof r))) {  // never expected; still unique enough for one device
        u64 t = osGetTime() ^ svcGetSystemTick();
        for (int i = 0; i < 10; i++) r[i] = (uint8_t)(t >> (i * 6));
    }
    out[0] = 'u';
    for (int i = 0; i < 10; i++) snprintf(out + 1 + i * 2, 3, "%02x", r[i]);
}

void upload_set_max(uint32_t max) { max_size = max; }
uint32_t upload_max(void) { return max_size; }

static Upload *find(const char *id)
{
    for (int i = 0; i < MAX_UPLOADS; i++)
        if (ups[i].state != U_FREE && !strcmp(ups[i].id, id)) return &ups[i];
    return NULL;
}

static void finish(Upload *u, bool ok, const char *why)
{
    log_printf("upload %s: %s%s%s (%lu bytes)", u->id, ok ? "done" : "failed", why ? ": " : "", why ? why : "",
               (unsigned long)u->len);
    UploadDone done = u->done;
    uint32_t ctx = u->ctx;
    free(u->data);
    memset(u, 0, sizeof *u);
    app.dirty = true;
    if (done) done(ctx, ok, why);
}

bool upload_start(const char *id, uint8_t *data, size_t len, const char *mime, const char *name, UploadDone done,
                  uint32_t ctx)
{
    Upload *u = NULL;
    for (int i = 0; i < MAX_UPLOADS && !u; i++)
        if (ups[i].state == U_FREE) u = &ups[i];
    if (!u || !len || (max_size && len > max_size) || strlen(id) >= UPLOAD_ID_LEN) {
        free(data);
        return false;
    }
    memset(u, 0, sizeof *u);
    u->state = U_WAITING;
    snprintf(u->id, sizeof u->id, "%s", id);
    snprintf(u->mime, sizeof u->mime, "%s", mime);
    snprintf(u->name, sizeof u->name, "%s", name);
    u->data = data;
    u->len = (uint32_t)len;
    u->done = done;
    u->ctx = ctx;
    log_printf("upload %s: %lu bytes of %s queued", u->id, (unsigned long)len, mime);
    upload_tick();
    return true;
}

float upload_progress(const char *id)
{
    Upload *u = find(id);
    if (!u) return -1;
    uint32_t queued = net_blob_queued(), sent = u->next;
    if (u->state == U_SENDING || u->state == U_SENT) sent = sent > queued ? sent - queued : 0;
    if (sent < u->base) sent = u->base;
    return u->len ? (float)sent / (float)u->len : 0;
}

bool upload_busy(void)
{
    for (int i = 0; i < MAX_UPLOADS; i++)
        if (ups[i].state != U_FREE) return true;
    return false;
}

static void put(Upload *u)
{
    u->req = app_request("blob.put", json_pack("{s:s, s:I, s:s, s:s}", "blob", u->id, "size", (json_int_t)u->len,
                                               "mime", u->mime, "name", u->name));
    u->state = U_PUTTING;
    u->since = osGetTime();
}

// Something went wrong mid-way: ask the PC where it stands (a few times), else give up.
static void retry(Upload *u, const char *why)
{
    if (++u->retries > MAX_RETRIES) {
        finish(u, false, why);
        return;
    }
    log_printf("upload %s: %s; asking the PC where it stands", u->id, why);
    u->state = U_WAITING;
}

bool upload_on_json(const json_t *msg)
{
    const char *t = js(msg, "t");
    const char *bid = js(msg, "blob");
    json_t *idv = json_object_get(msg, "id");
    Upload *u = NULL;
    if (json_is_integer(idv)) {  // the reply to a blob.put
        for (int i = 0; i < MAX_UPLOADS && !u; i++)
            if (ups[i].state == U_PUTTING && ups[i].req == (uint32_t)json_integer_value(idv)) u = &ups[i];
        if (!u) return false;
        if (!strcmp(t, "error")) {
            finish(u, false, js(msg, "msg")[0] ? js(msg, "msg") : js(msg, "code"));
            return true;
        }
        if (strcmp(t, "blob.put_r")) return true;
        json_int_t off = json_integer_value(json_object_get(msg, "offset"));
        if (off < 0 || off > u->len) {
            finish(u, false, "the PC answered with a bad offset");
            return true;
        }
        u->next = u->base = (uint32_t)off;
        u->state = off == u->len ? U_SENT : U_SENDING;  // complete already: blob.put_done follows
        u->since = osGetTime();
        if (off) log_printf("upload %s: resuming at %lu", u->id, (unsigned long)off);
        upload_tick();
        return true;
    }
    if (!bid[0] || !(u = find(bid))) return false;
    if (!strcmp(t, "blob.put_done")) {
        finish(u, true, NULL);
    } else if (!strcmp(t, "error") && (u->state == U_SENDING || u->state == U_SENT)) {
        retry(u, js(msg, "code"));
    }
    return true;  // also errors about chunks of a superseded attempt: nothing for the chat to show
}

void upload_online(bool on)
{
    if (on == online) return;
    online = on;
    if (on) return;
    for (int i = 0; i < MAX_UPLOADS; i++)  // the chunks in flight died with the connection
        if (ups[i].state != U_FREE) ups[i].state = U_WAITING;
}

void upload_tick(void)
{
    if (!online) return;
    u64 now = osGetTime();
    // One at a time, oldest first: it is the one whose bubble shows progress.
    Upload *u = NULL;
    for (int i = 0; i < MAX_UPLOADS; i++)
        if (ups[i].state != U_FREE && ups[i].state != U_WAITING) u = &ups[i];
    if (!u)
        for (int i = 0; i < MAX_UPLOADS && !u; i++)
            if (ups[i].state == U_WAITING) u = &ups[i];
    if (!u) return;
    switch (u->state) {
    case U_WAITING: put(u); break;
    case U_PUTTING:
        if (now - u->since > DONE_WAIT_MS) retry(u, "no answer to blob.put");
        break;
    case U_SENDING:
        while (u->next < u->len && net_blob_queued() < IN_FLIGHT) {
            uint32_t n = u->len - u->next < CHUNK ? u->len - u->next : CHUNK;
            if (!net_send_blob(u->id, u->next, u->data + u->next, n)) break;  // queue full: next frame
            u->next += n;
            app.dirty = true;
        }
        if (u->next == u->len) {
            u->state = U_SENT;
            u->since = now;
        }
        break;
    case U_SENT:
        if (net_blob_queued() == 0 && now - u->since > DONE_WAIT_MS) retry(u, "no blob.put_done");
        else if (net_blob_queued()) u->since = now;
        break;
    case U_FREE: break;
    }
}

void upload_exit(void)
{
    for (int i = 0; i < MAX_UPLOADS; i++) {
        free(ups[i].data);
        memset(&ups[i], 0, sizeof ups[i]);
    }
}
