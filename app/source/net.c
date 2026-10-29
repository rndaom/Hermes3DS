#include "net.h"

#include <3ds.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "codec.h"
#include "emoji.h"
#include "log.h"
#include "queue.h"

#define POLL_MS 40            // socket wait per loop turn while online (also the send latency bound)
#define CONNECT_TIMEOUT 5000
#define LAN_TIMEOUT 2500           // home address first, when an away route can take over
#define AWAY_CONNECT_TIMEOUT 8000
#define TLS_TIMEOUT 20000          // a full TLS handshake on an Old 3DS plus internet round trips
#define HANDSHAKE_TIMEOUT 10000
#define SEND_TIMEOUT 5000
#define PING_EVERY 15000      // protocol.md §1.3
#define DEAD_AFTER 45000
#define STACK_SIZE (128 * 1024)  // jansson recurses (input is depth-checked: json_depth_ok), mbedTLS is deep

typedef enum { CMD_CONNECT, CMD_DISCONNECT, CMD_PAIR, CMD_DISCOVER, CMD_WAKE, CMD_QUIT } CmdKind;

typedef struct {
    CmdKind kind;
    ServerInfo server;
    uint8_t token[32];
    size_t token_len;
    char target[64];
} Cmd;

// An outgoing body: kind byte + data (JSON text or a BLOB chunk).
typedef struct {
    size_t len;
    uint8_t body[];
} Out;

static Thread thread;
static Queue cmds, sends, events;
static volatile uint32_t blob_queued;  // bytes of upload chunks waiting in `sends` (uploads pace themselves on it)
static LightEvent cmd_signal;
static NetIdentity ident;
static Conn conn;  // 128 KB of buffers: static, not on the worker stack

static ServerInfo target;
static bool want_online;

// --- helpers ----------------------------------------------------------------

bool json_depth_ok(const char *s, size_t n)
{
    int depth = 0;
    bool in_str = false;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (in_str) {
            if (c == '\\') i++;  // skips the escaped character (\" stays in the string)
            else if (c == '"') in_str = false;
        } else if (c == '"') {
            in_str = true;
        } else if (c == '[' || c == '{') {
            if (++depth > JSON_MAX_DEPTH) return false;
        } else if ((c == ']' || c == '}') && depth > 0) {
            depth--;
        }
    }
    return true;
}

static void emit(NetEvent *ev)
{
    while (!queue_push(&events, ev)) svcSleepThread(10 * 1000000LL);  // UI busy (e.g. swkbd open): wait, never drop
}

// NULL when out of memory (logged): the caller drops the event.
static NetEvent *new_event(NetEventKind kind)
{
    NetEvent *ev = calloc(1, sizeof *ev);
    if (!ev) {
        log_printf("net: out of memory, dropped event %d", (int)kind);
        return NULL;
    }
    ev->kind = kind;
    return ev;
}

// A BLOB body (after the kind byte): id_len u8, id, offset u32 BE, bytes.
static void emit_blob(const uint8_t *d, size_t n)
{
    if (n < 1 || d[0] == 0 || d[0] > 32 || n < 1u + d[0] + 4) {
        log_printf("bad blob frame (%u bytes)", (unsigned)n);
        return;
    }
    size_t idn = d[0], hdr = 1 + idn + 4;
    NetEvent *ev = new_event(NET_EV_BLOB);
    if (!ev) return;
    memcpy(ev->blob, d + 1, idn);
    ev->offset = (uint32_t)d[1 + idn] << 24 | (uint32_t)d[2 + idn] << 16 | (uint32_t)d[3 + idn] << 8 | d[4 + idn];
    ev->len = n - hdr;
    ev->data = malloc(ev->len ? ev->len : 1);
    if (!ev->data) {
        free(ev);
        log_printf("blob chunk: out of memory");
        return;
    }
    memcpy(ev->data, d + hdr, ev->len);
    emit(ev);
}

static void emit_state(NetState st, int retry_s, const char *detail)
{
    NetEvent *ev = new_event(NET_EV_STATE);
    if (!ev) return;
    ev->state = st;
    ev->retry_s = retry_s;
    if (detail) snprintf(ev->detail, sizeof ev->detail, "%s", detail);
    emit(ev);
}

static void push_cmd(Cmd *c)
{
    while (!queue_push(&cmds, c)) svcSleepThread(5 * 1000000LL);
    LightEvent_Signal(&cmd_signal);
}

static Cmd *new_cmd(CmdKind kind)
{
    Cmd *c = calloc(1, sizeof *c);
    c->kind = kind;
    return c;
}

static json_t *hello_json(const uint8_t *pair_token, size_t token_len)
{
    json_t *h = json_pack("{s:s, s:i, s:s, s:s, s:s, s:{s:[i,i], s:[i,i]}, s:[]}", "t", "hello", "v", 1, "app",
                          ident.app, "device", ident.device, "model", ident.model, "screen", "top", 400, 240,
                          "bottom", 320, 240, "caps");
    const char *emoji = emoji_cap();  // the plugin sends colour emoji only to an app with its sprite set
    if (emoji) json_array_append_new(json_object_get(h, "caps"), json_string(emoji));
    if (pair_token) {
        char b64[64];
        b64u_encode(pair_token, token_len, b64);
        json_object_set_new(h, "pair", json_string(b64));
    }
    return h;
}

static const char *conn_err_text(int rc)
{
    switch (rc) {
    case CONN_ERR_RESOLVE: return "Can't find that address";
    case CONN_ERR_CONNECT: return "PC not reachable";
    case CONN_ERR_BLOCKED: return "The network blocks the PC and 3DS from connecting";
    case CONN_ERR_IO: return "Connection lost";
    case CONN_ERR_CRYPTO: return "Secure handshake failed";
    case CONN_ERR_PROTO: return "Protocol error";
    case CONN_ERR_TLS: return "Couldn't reach the PC over the internet (TLS)";
    default: return "Error";
    }
}

#define CALLBACK_TIMEOUT 4000
static bool via_callback;  // the last stream needed the call-back: try that first next time
static TlsInfo last_tls;   // the away route's last TLS handshake

static bool has_away(const ServerInfo *s) { return s->away_host[0] && s->away_port; }

// Is the 3DS on the PC's /24? Only then is looking for a PC that changed address worth it before the away route.
static bool same_subnet(const char *host)
{
    struct in_addr a;
    if (inet_pton(AF_INET, host, &a) != 1) return true;  // a name: can't tell
    uint32_t mine = (uint32_t)gethostid();
    return mine == 0 || ((ntohl(a.s_addr) ^ ntohl(mine)) & 0xFFFFFF00u) == 0;
}

// A TCP stream to the PC at home: direct connect, else ask the PC to dial us (IoT/guest Wi-Fi blocks the 3DS
// from opening connections to the PC but usually not the reverse).
static int open_lan(const ServerInfo *s)
{
    // The call-back is always worth trying: a 3DS on an IoT/guest Wi-Fi is on a different subnet from the PC
    // even at home. Away from home it costs CALLBACK_TIMEOUT once; after that the away route goes first.
    bool away = has_away(s);
    if (via_callback && conn_callback(&conn, s->host, DISCOVERY_PORT, CALLBACK_TIMEOUT) == CONN_OK) return CONN_OK;
    int rc = conn_open(&conn, s->host, s->port, away ? LAN_TIMEOUT : CONNECT_TIMEOUT);
    if (rc == CONN_OK) via_callback = false;
    if (rc != CONN_ERR_CONNECT || via_callback) return rc;
    int cb = conn_callback(&conn, s->host, DISCOVERY_PORT, CALLBACK_TIMEOUT);
    if (cb == CONN_OK) {
        log_printf("direct connect to %s:%u failed; the PC dialed back", s->host, s->port);
        via_callback = true;
        return CONN_OK;
    }
    return cb == CONN_ERR_BLOCKED ? cb : rc;
}

// The away route (protocol.md §2.4): TCP to the Tailscale Funnel, then TLS with the funnel name as SNI.
static int open_away(const ServerInfo *s)
{
    int rc = conn_open(&conn, s->away_host, s->away_port, AWAY_CONNECT_TIMEOUT);
    if (rc) return rc;
    char err[128];
    rc = conn_start_tls(&conn, s->away_host, TLS_TIMEOUT, &last_tls, err, sizeof err);
    if (rc) {
        log_printf("away %s:%u: %s", s->away_host, s->away_port, err);
        conn_close(&conn);
        return rc;
    }
    log_printf("TLS to %s:%u: %lu ms (%lu ms CPU), %s%s%s", s->away_host, s->away_port,
               (unsigned long)last_tls.ms, (unsigned long)last_tls.cpu_ms, last_tls.suite,
               last_tls.resumed ? ", resumed" : "", last_tls.ticket ? ", ticket" : "");
    return CONN_OK;
}

// Connect + Noise IK. On success returns the parsed reply (welcome or error) and the handshake time.
static int open_session(const ServerInfo *s, NetRoute route, const uint8_t *token, size_t token_len,
                        json_t **reply, uint32_t *handshake_ms)
{
    uint8_t e_priv[32];
    const uint8_t *rbody;
    size_t rlen;
    int rc = route == ROUTE_AWAY ? open_away(s) : open_lan(s);
    if (rc) return rc;

    json_t *hello = hello_json(token, token_len);
    char *text = json_dumps(hello, JSON_COMPACT);
    json_decref(hello);
    size_t n = text ? strlen(text) : 0;
    uint8_t *body = text ? malloc(n + 1) : NULL;
    if (!body) {  // out of memory: fails like a bad connection (retried with backoff)
        log_printf("hello: out of memory");
        free(text);
        conn_close(&conn);
        return CONN_ERR_IO;
    }
    body[0] = BODY_JSON;
    memcpy(body + 1, text, n);
    free(text);

    PS_GenerateRandomBytes(e_priv, sizeof e_priv);
    u64 t0 = osGetTime();
    rc = conn_handshake(&conn, ident.device_priv, s->key, e_priv, body, n + 1, &rbody, &rlen, HANDSHAKE_TIMEOUT);
    *handshake_ms = (uint32_t)(osGetTime() - t0);
    noise_wipe(e_priv, sizeof e_priv);
    free(body);
    if (rc) {
        conn_close(&conn);
        return rc;
    }
    json_error_t err;
    *reply = rbody[0] == BODY_JSON && json_depth_ok((const char *)rbody + 1, rlen - 1)
                 ? json_loadb((const char *)rbody + 1, rlen - 1, 0, &err)
                 : NULL;
    if (!*reply || !json_is_object(*reply)) {
        json_decref(*reply);
        conn_close(&conn);
        return CONN_ERR_PROTO;
    }
    return CONN_OK;
}

static const char *jstr(json_t *o, const char *k)
{
    const char *s = json_string_value(json_object_get(o, k));
    return s ? s : "";
}

// `away` in a welcome, status_r or away message (protocol.md §2.4): an address, null = none, absent = unknown
// (keep ours). Returns true when *s changed.
static bool parse_away(json_t *msg, ServerInfo *s)
{
    json_t *a = json_object_get(msg, "away");
    char host[sizeof s->away_host] = "";
    json_int_t port = 0;
    if (!a) return false;
    if (json_is_object(a)) {
        const char *h = json_string_value(json_object_get(a, "host"));
        port = json_integer_value(json_object_get(a, "port"));
        if (!h || !h[0] || strlen(h) >= sizeof host || port <= 0 || port > 65535) return false;
        snprintf(host, sizeof host, "%s", h);
    } else if (!json_is_null(a)) {
        return false;
    }
    if (!strcmp(host, s->away_host) && port == s->away_port) return false;
    snprintf(s->away_host, sizeof s->away_host, "%s", host);
    s->away_port = (uint16_t)port;
    return true;
}

// The connected PC's away address changed: keep it for reconnects and tell the UI to save it.
static void take_away(json_t *msg)
{
    if (!parse_away(msg, &target)) return;
    if (has_away(&target)) log_printf("away address: %s:%u", target.away_host, target.away_port);
    else log_printf("away address: none");
    NetEvent *ev = new_event(NET_EV_AWAY);
    if (!ev) return;
    ev->server = target;
    emit(ev);
}

// --- discovery --------------------------------------------------------------

static int do_discover(const char *target_addr, DiscoveryReply *found, int max)
{
    return discover(target_addr ? target_addr : "255.255.255.255", DISCOVERY_PORT, 3, 500, found, max);
}

// After repeated failures: is our PC (same static key) answering from another address now (DHCP)?
static bool find_moved_server(void)
{
    DiscoveryReply found[NET_MAX_FOUND];
    int n = do_discover(NULL, found, NET_MAX_FOUND);
    for (int i = 0; i < n; i++) {
        json_t *j = json_depth_ok(found[i].json, strlen(found[i].json)) ? json_loads(found[i].json, 0, NULL) : NULL;
        uint8_t key[32];
        bool same = j && b64u_decode(jstr(j, "key"), key, sizeof key) == 32 && memcmp(key, target.key, 32) == 0;
        int port = (int)json_integer_value(json_object_get(j, "port"));
        json_decref(j);
        if (same && (strcmp(found[i].addr, target.host) != 0 || (port && port != target.port))) {
            log_printf("server moved %s:%u -> %s:%d", target.host, target.port, found[i].addr, port);
            snprintf(target.host, sizeof target.host, "%s", found[i].addr);
            if (port) target.port = (uint16_t)port;
            NetEvent *ev = new_event(NET_EV_MOVED);
            if (ev) {
                ev->server = target;
                emit(ev);
            }
            return true;
        }
    }
    return false;
}

// --- pairing ----------------------------------------------------------------

static void do_pair(Cmd *c)
{
    json_t *reply = NULL;
    uint32_t ms = 0;
    emit_state(NET_CONNECTING, 0, "Pairing");
    int rc = open_session(&c->server, ROUTE_LAN, c->token, c->token_len, &reply, &ms);  // pairing: home only
    noise_wipe(c->token, sizeof c->token);
    if (rc) {
        NetEvent *ev = new_event(NET_EV_PAIR_FAILED);
        if (ev) {
            snprintf(ev->detail, sizeof ev->detail, "%s", conn_err_text(rc));
            emit(ev);
        }
        emit_state(NET_OFFLINE, 0, NULL);
        return;
    }
    conn_close(&conn);  // the normal connect loop takes over after the UI saves the server
    if (strcmp(jstr(reply, "t"), "welcome") != 0) {
        NetEvent *ev = new_event(NET_EV_PAIR_FAILED);
        const char *code = jstr(reply, "code");
        if (ev)
            snprintf(ev->detail, sizeof ev->detail, "%s",
                 !strcmp(code, "not_paired")      ? "The PC refused the code. It may be mistyped or expired (5 min)."
                 : !strcmp(code, "pair_lan_only") ? "Pair at home: the 3DS must be on the same Wi-Fi as the PC."
                 : jstr(reply, "msg")[0]          ? jstr(reply, "msg")
                                                  : code);
        json_decref(reply);
        if (ev) emit(ev);
        emit_state(NET_OFFLINE, 0, NULL);
        return;
    }
    log_printf("paired with %s (%s:%u), handshake %lu ms", jstr(reply, "server"), c->server.host, c->server.port,
               (unsigned long)ms);
    parse_away(reply, &c->server);
    NetEvent *ev = new_event(NET_EV_PAIRED);
    if (!ev) {
        json_decref(reply);
        emit_state(NET_OFFLINE, 0, NULL);
        return;
    }
    ev->server = c->server;
    if (jstr(reply, "server")[0]) snprintf(ev->server.name, sizeof ev->server.name, "%s", jstr(reply, "server"));
    ev->json = reply;
    ev->handshake_ms = ms;
    emit(ev);
    // Stay with the PC just paired, even if this arrived while connected to (or retrying) another one.
    target = c->server;
    want_online = true;
}

// --- online loop ------------------------------------------------------------

// Handles commands that arrive while online. Returns false when the connection must end.
static bool online_command(Cmd *c)
{
    switch (c->kind) {
    case CMD_DISCONNECT: want_online = false; return false;
    case CMD_WAKE: return false;  // the socket did not survive sleep; reconnect
    case CMD_CONNECT:
        target = c->server;
        want_online = true;
        return false;
    case CMD_QUIT: want_online = false; return false;
    case CMD_PAIR:
        conn_close(&conn);
        do_pair(c);
        return false;
    case CMD_DISCOVER: {
        NetEvent *ev = new_event(NET_EV_DISCOVERED);
        if (ev) {
            ev->nfound = do_discover(c->target[0] ? c->target : NULL, ev->found, NET_MAX_FOUND);
            emit(ev);
        }
        return true;
    }
    }
    return true;
}

static bool quit_requested;

// A message whose send failed; it goes out first on the next connection (the server dropped the partial frame).
static Out *unsent;

static bool is_blob(const Out *o) { return o->len && o->body[0] == BODY_BLOB; }

static void out_free(Out *o)
{
    if (o && is_blob(o)) __atomic_sub_fetch(&blob_queued, (uint32_t)o->len, __ATOMIC_RELAXED);
    free(o);
}

static bool drop_blob(void *o) { return is_blob(o); }
static void free_out(void *o) { out_free(o); }

// The connection dropped: upload chunks still queued belong to it (the upload asks the PC where to go on after
// reconnecting), so they are dropped; JSON messages stay, in order.
static void drop_queued_blobs(void)
{
    if (unsent && is_blob(unsent)) {
        out_free(unsent);
        unsent = NULL;
    }
    queue_filter(&sends, drop_blob, free_out);
}

static void run_online(void)
{
    u64 last_rx = osGetTime(), last_tx = last_rx;
    int ping_n = 0;
    for (;;) {
        // 1. outgoing
        Out *out;
        while ((out = unsent ? unsent : queue_pop(&sends))) {
            unsent = NULL;
            int rc = conn_send_body(&conn, out->body, out->len, SEND_TIMEOUT);
            if (rc) {
                log_printf("send failed: %d", rc);
                unsent = out;
                return;
            }
            out_free(out);
            last_tx = osGetTime();
            if (queue_count(&cmds)) break;  // a long upload must not hold up a reconnect or quit
        }
        // 2. control
        Cmd *c;
        while ((c = queue_pop(&cmds))) {
            bool keep = online_command(c);
            if (c->kind == CMD_QUIT) quit_requested = true;
            free(c);
            if (!keep) return;
        }
        // 3. incoming (blocks up to POLL_MS)
        const uint8_t *body;
        size_t len;
        int rc = conn_recv_body(&conn, POLL_MS, &body, &len);
        u64 now = osGetTime();
        if (rc == CONN_OK) {
            last_rx = now;
            if (body[0] == BODY_BLOB) {
                emit_blob(body + 1, len - 1);
                continue;
            }
            if (body[0] != BODY_JSON) continue;
            json_error_t err;
            bool shallow = json_depth_ok((const char *)body + 1, len - 1);
            json_t *msg = shallow ? json_loadb((const char *)body + 1, len - 1, 0, &err) : NULL;
            if (!msg || !json_is_object(msg) || !json_is_string(json_object_get(msg, "t"))) {
                json_decref(msg);
                log_printf("bad json from server: %s", shallow ? err.text : "nested too deep");
                return;
            }
            const char *t = jstr(msg, "t");
            if (!strcmp(t, "away")) {  // the PC's away address changed (protocol.md §2.4)
                take_away(msg);
                json_decref(msg);
                continue;
            }
            if (!strcmp(t, "status_r")) take_away(msg);
            if (!strcmp(t, "ping")) {
                json_t *pong = json_pack("{s:s, s:O?}", "t", "pong", "n", json_object_get(msg, "n"));
                char *s = json_dumps(pong, JSON_COMPACT);
                json_decref(pong);
                rc = s ? conn_send_json(&conn, s, SEND_TIMEOUT) : CONN_OK;  // (no memory: skip this pong)
                free(s);
                json_decref(msg);
                if (rc) return;
                last_tx = now;
            } else if (!strcmp(t, "pong")) {
                json_decref(msg);
            } else {
                NetEvent *ev = new_event(NET_EV_JSON);
                if (ev) {
                    ev->json = msg;
                    emit(ev);
                } else {
                    json_decref(msg);
                }
            }
        } else if (rc != CONN_TIMEOUT) {
            log_printf("recv: %s (%d)", conn_err_text(rc), rc);
            return;
        }
        // 4. keepalive
        if (now - last_rx > DEAD_AFTER) {
            log_printf("no data for %d s, reconnecting", DEAD_AFTER / 1000);
            return;
        }
        if (now - last_tx >= PING_EVERY) {
            char ping[48];
            snprintf(ping, sizeof ping, "{\"t\":\"ping\",\"n\":%d}", ++ping_n);
            if (conn_send_json(&conn, ping, SEND_TIMEOUT)) return;
            last_tx = now;
        }
    }
}

// --- worker -----------------------------------------------------------------

static const int BACKOFF[] = {1, 2, 4, 8, 15};

static NetRoute last_route = ROUTE_LAN;
static uint32_t last_route_ip;  // the 3DS's own address when last_route worked

// The home address first (short timeout when there is an away route), then the away route. Straight to the away
// route when that is what worked last time and the 3DS is still on the same network (a reconnect while away).
static int connect_routes(json_t **reply, uint32_t *ms, NetRoute *used)
{
    NetRoute order[2] = {ROUTE_LAN, ROUTE_AWAY};
    int n = has_away(&target) ? 2 : 1;
    uint32_t ip = (uint32_t)gethostid();
    if (n == 2 && last_route == ROUTE_AWAY && ip == last_route_ip) {
        order[0] = ROUTE_AWAY;
        order[1] = ROUTE_LAN;
    }
    int rc = CONN_ERR_CONNECT;
    for (int i = 0; i < n; i++) {
        emit_state(NET_CONNECTING, 0, order[i] == ROUTE_AWAY ? target.away_host : target.host);
        u64 t0 = osGetTime();
        rc = open_session(&target, order[i], NULL, 0, reply, ms);
        if (rc == CONN_OK) {
            *used = last_route = order[i];
            last_route_ip = ip;
            return rc;
        }
        log_printf("%s route failed after %lu ms: %s (last dial %s, errno %d)", order[i] == ROUTE_AWAY ? "away" : "home",
                   (unsigned long)(osGetTime() - t0), conn_err_text(rc), conn_last_addr, conn_last_errno);
        // Probably at home but the PC got a new address (DHCP): find it before going round the internet.
        if (order[i] == ROUTE_LAN && n == 2 && rc == CONN_ERR_CONNECT && same_subnet(target.host) &&
            find_moved_server()) {
            rc = open_session(&target, ROUTE_LAN, NULL, 0, reply, ms);
            if (rc == CONN_OK) {
                *used = last_route = ROUTE_LAN;
                last_route_ip = ip;
                return rc;
            }
        }
    }
    return rc;
}

// Wait up to ms for a command and handle it. Returns true when a connect/wake asks to retry right now.
static bool idle_wait(int ms)
{
    if (queue_count(&cmds) == 0) {
        if (ms < 0) LightEvent_Wait(&cmd_signal);
        else LightEvent_WaitTimeout(&cmd_signal, (s64)ms * 1000000LL);
    }
    Cmd *c;
    bool retry_now = false;
    while ((c = queue_pop(&cmds))) {
        switch (c->kind) {
        case CMD_CONNECT:
            target = c->server;
            want_online = true;
            retry_now = true;
            break;
        case CMD_DISCONNECT: want_online = false; break;
        case CMD_PAIR: do_pair(c); break;
        case CMD_DISCOVER: {
            NetEvent *ev = new_event(NET_EV_DISCOVERED);
            if (ev) {
                ev->nfound = do_discover(c->target[0] ? c->target : NULL, ev->found, NET_MAX_FOUND);
                emit(ev);
            }
            break;
        }
        case CMD_WAKE: retry_now = true; break;
        case CMD_QUIT: quit_requested = true; break;
        }
        free(c);
    }
    return retry_now;
}

static void worker(void *arg)
{
    int failures = 0;
    while (!quit_requested) {
        if (!want_online) {
            emit_state(NET_OFFLINE, 0, NULL);
            while (!want_online && !quit_requested) idle_wait(-1);
            failures = 0;
            continue;
        }
        json_t *reply = NULL;
        uint32_t ms = 0;
        NetRoute route = ROUTE_LAN;
        int rc = connect_routes(&reply, &ms, &route);
        if (rc == CONN_OK && !strcmp(jstr(reply, "t"), "welcome")) {
            if (route == ROUTE_AWAY)
                log_printf("online away: %s:%u, TLS %lu ms, Noise handshake %lu ms", target.away_host,
                           target.away_port, (unsigned long)last_tls.ms, (unsigned long)ms);
            else log_printf("online: %s:%u, handshake %lu ms", target.host, target.port, (unsigned long)ms);
            failures = 0;
            take_away(reply);
            NetEvent *ev = new_event(NET_EV_WELCOME);
            if (ev) {
                ev->json = reply;
                ev->handshake_ms = ms;
                ev->route = route;
                if (route == ROUTE_AWAY) ev->tls = last_tls;
                ev->server = target;
                emit(ev);
            } else {
                json_decref(reply);
            }
            emit_state(NET_ONLINE, 0, NULL);
            run_online();
            conn_close(&conn);
            drop_queued_blobs();
            continue;  // reconnect right away once; backoff applies if that fails
        }
        if (rc == CONN_OK) {  // error reply: not_paired, version, ...
            const char *code = jstr(reply, "code");
            log_printf("server refused: %s %s", code, jstr(reply, "msg"));
            char detail[128];
            snprintf(detail, sizeof detail, "%s", !strcmp(code, "not_paired") ? "not_paired" : jstr(reply, "msg"));
            json_decref(reply);
            conn_close(&conn);
            want_online = false;
            emit_state(NET_REFUSED, 0, detail);
            while (!want_online && !quit_requested) idle_wait(-1);
            continue;
        }
        failures++;
        log_printf("connect %s:%u%s failed: %s", target.host, target.port, has_away(&target) ? " (and away)" : "",
                   conn_err_text(rc));
        if (failures % 3 == 0 && find_moved_server()) continue;
        int wait_s = BACKOFF[failures - 1 < 4 ? failures - 1 : 4];
        emit_state(NET_WAITING, wait_s, conn_err_text(rc));
        u64 until = osGetTime() + (u64)wait_s * 1000;
        while (want_online && !quit_requested) {
            s64 left = (s64)(until - osGetTime());
            if (left <= 0) break;
            if (idle_wait((int)left)) {  // woke from sleep or a new target: retry now
                failures = 0;
                break;
            }
        }
    }
    conn_close(&conn);
}

bool net_init(const NetIdentity *id)
{
    ident = *id;
    queue_init(&cmds);
    queue_init(&sends);
    queue_init(&events);
    LightEvent_Init(&cmd_signal, RESET_ONESHOT);
    conn_init(&conn);
    s32 prio = 0x30;
    svcGetThreadPriority(&prio, CUR_THREAD_HANDLE);
    // Same core as the UI, lower priority: the UI blocks on vsync, the worker on poll().
    thread = threadCreate(worker, NULL, STACK_SIZE, prio + 1, -2, false);
    return thread != NULL;
}

void net_exit(void)
{
    if (!thread) return;
    push_cmd(new_cmd(CMD_QUIT));
    // No timeout: threadFree on a running thread is a no-op, and the worker must be gone before socExit.
    // A blocked connect plus handshake bounds this at about 15 s.
    threadJoin(thread, U64_MAX);
    threadFree(thread);
    thread = NULL;
    void *p;
    while ((p = queue_pop(&sends))) out_free(p);
    out_free(unsent);
    unsent = NULL;
    NetEvent *ev;
    while ((ev = queue_pop(&events))) net_event_free(ev);
    noise_wipe(&ident, sizeof ident);
}

void net_connect(const ServerInfo *s)
{
    Cmd *c = new_cmd(CMD_CONNECT);
    c->server = *s;
    push_cmd(c);
}

void net_disconnect(void) { push_cmd(new_cmd(CMD_DISCONNECT)); }
void net_wake(void) { push_cmd(new_cmd(CMD_WAKE)); }

void net_pair(const ServerInfo *s, const uint8_t *token, size_t token_len)
{
    Cmd *c = new_cmd(CMD_PAIR);
    c->server = *s;
    c->token_len = token_len < sizeof c->token ? token_len : sizeof c->token;
    memcpy(c->token, token, c->token_len);
    push_cmd(c);
}

void net_discover(const char *target_addr)
{
    Cmd *c = new_cmd(CMD_DISCOVER);
    if (target_addr) snprintf(c->target, sizeof c->target, "%s", target_addr);
    push_cmd(c);
}

void net_send(json_t *msg)
{
    char *s = json_dumps(msg, JSON_COMPACT);
    json_decref(msg);
    if (!s) return;
    size_t n = strlen(s);
    Out *o = n + 1 <= BODY_MAX ? malloc(sizeof *o + n + 1) : NULL;
    if (o) {
        o->len = n + 1;
        o->body[0] = BODY_JSON;
        memcpy(o->body + 1, s, n);
    }
    free(s);
    if (!o || !queue_push(&sends, o)) {
        log_printf(o ? "send queue full, dropped a message" : "message too large or no memory, dropped it");
        free(o);
    }
}

bool net_send_blob(const char *id, uint32_t offset, const uint8_t *data, size_t len)
{
    size_t idn = strlen(id);
    if (!idn || idn > 32 || 1 + 1 + idn + 4 + len > BODY_MAX) return false;
    Out *o = malloc(sizeof *o + 1 + 1 + idn + 4 + len);
    if (!o) return false;
    uint8_t *b = o->body;
    *b++ = BODY_BLOB;
    *b++ = (uint8_t)idn;
    memcpy(b, id, idn);
    b += idn;
    *b++ = (uint8_t)(offset >> 24), *b++ = (uint8_t)(offset >> 16), *b++ = (uint8_t)(offset >> 8), *b++ = (uint8_t)offset;
    memcpy(b, data, len);
    o->len = (size_t)(b - o->body) + len;
    __atomic_add_fetch(&blob_queued, (uint32_t)o->len, __ATOMIC_RELAXED);
    if (!queue_push(&sends, o)) {
        out_free(o);
        return false;
    }
    return true;
}

uint32_t net_blob_queued(void) { return __atomic_load_n(&blob_queued, __ATOMIC_RELAXED); }

NetEvent *net_poll(void) { return queue_pop(&events); }

void net_event_free(NetEvent *ev)
{
    if (!ev) return;
    json_decref(ev->json);
    free(ev->data);
    free(ev);
}
