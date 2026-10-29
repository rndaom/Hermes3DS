// Network worker thread: owns the Conn, does discovery, pairing, the handshake, keepalive and reconnect
// with backoff (protocol.md §1.3). The UI talks to it only through commands and events.
#pragma once

#include <jansson.h>
#include <stdbool.h>
#include <stdint.h>

#include "conn.h"

typedef struct {
    char host[64];
    uint16_t port;
    uint8_t key[32];  // server static public key
    char name[64];    // server display name
    char away_host[128];  // away-from-home address (protocol.md §2.4), "" = none; reached over TLS
    uint16_t away_port;
} ServerInfo;

typedef enum {
    ROUTE_LAN,   // the home address, plain TCP (or the call-back)
    ROUTE_AWAY,  // the away address: TLS through Tailscale Funnel, Noise inside
} NetRoute;

typedef enum {
    NET_OFFLINE,     // no server configured / stopped
    NET_CONNECTING,  // TCP connect + handshake in progress
    NET_ONLINE,
    NET_WAITING,     // backing off before the next attempt (retry_s)
    NET_REFUSED,     // server said not_paired / version: needs the user, no automatic retry
} NetState;

typedef enum {
    NET_EV_STATE,       // state, retry_s, detail
    NET_EV_WELCOME,     // json = welcome; handshake_ms, route, tls (away route)
    NET_EV_JSON,        // json = a server message (ownership passes to the UI)
    NET_EV_DISCOVERED,  // found[0..nfound)
    NET_EV_PAIRED,      // server (fully filled in), json = welcome
    NET_EV_PAIR_FAILED, // detail
    NET_EV_MOVED,       // server: the paired PC answered discovery from a new address; save it
    NET_EV_BLOB,        // blob, offset, data[0..len): one chunk of a blob transfer (protocol.md §5)
    NET_EV_AWAY,        // server: the PC's away address changed (server.away_*); save it
} NetEventKind;

#define NET_MAX_FOUND 6

typedef struct {
    NetEventKind kind;
    NetState state;
    int retry_s;
    uint32_t handshake_ms;
    NetRoute route;
    TlsInfo tls;
    json_t *json;
    ServerInfo server;
    int nfound;
    DiscoveryReply found[NET_MAX_FOUND];
    char detail[128];
    char blob[33];
    uint32_t offset;
    uint8_t *data;
    size_t len;
} NetEvent;

typedef struct {
    char device[64];  // shown on the PC
    char model[16];   // old3ds / new3ds
    char app[16];
    uint8_t device_priv[32];
} NetIdentity;

bool net_init(const NetIdentity *id);  // starts the worker
void net_exit(void);

void net_connect(const ServerInfo *s);  // connect and stay connected (reconnects)
void net_disconnect(void);
void net_pair(const ServerInfo *s, const uint8_t *token, size_t token_len);
void net_discover(const char *target);  // NULL = broadcast
void net_wake(void);                    // after sleep: drop the old socket, reconnect now
// jansson parses recursively, up to 2048 levels: more than a thread's stack holds. True when `s[0..n)` nests
// arrays / objects at most JSON_MAX_DEPTH deep; check it before parsing JSON that came from the network.
#define JSON_MAX_DEPTH 64
bool json_depth_ok(const char *s, size_t n);

// Queue a JSON message (takes ownership of `msg`). Messages queue while offline and go out on reconnect.
void net_send(json_t *msg);
// Queue one upload chunk (a BLOB body, protocol.md §5.1). Chunks still queued when the connection drops are
// discarded: the upload asks the PC where to continue. False when the queue is full or memory ran out.
bool net_send_blob(const char *id, uint32_t offset, const uint8_t *data, size_t len);
uint32_t net_blob_queued(void);  // bytes of upload chunks not sent yet (uploads keep this small)

NetEvent *net_poll(void);  // next event or NULL; free with net_event_free
void net_event_free(NetEvent *ev);
