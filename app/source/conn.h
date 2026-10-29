// One TCP connection to the plugin: frames, Noise IK handshake, encrypted bodies (protocol.md §1, §3),
// plus UDP discovery (§2.1). Portable (3DS + PC test build), JSON-free, single-threaded: only the network
// worker touches a Conn.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "noise.h"
#include "sock.h"
#include "tls.h"

#define BODY_JSON 0x01
#define BODY_BLOB 0x02
#define FRAME_MAX 65535
#define BODY_MAX (FRAME_MAX - NOISE_TAGLEN)  // kind byte + data

enum {
    CONN_OK = 0,
    CONN_TIMEOUT = 1,       // conn_recv_body: nothing complete yet (not an error)
    CONN_ERR_RESOLVE = -1,
    CONN_ERR_CONNECT = -2,
    CONN_ERR_IO = -3,       // socket error or peer closed
    CONN_ERR_CRYPTO = -4,   // handshake or decrypt failure: close
    CONN_ERR_PROTO = -5,    // malformed frame/body
    CONN_ERR_BLOCKED = -6,  // call-back: the PC answered but its connection never arrived
    CONN_ERR_TLS = -7,      // TLS handshake failed (away route)
};

#define DISCOVERY_PORT 7334
#define CALLBACK_PORT 7335  // first port tried for the call-back listener (up to +3)

typedef struct {
    sock_t fd;
    Tls *tls;  // away route: the stream runs inside TLS (conn_start_tls)
    int secure;
    NoiseTransport t;
    size_t rx_len;
    uint8_t rx[2 + FRAME_MAX];
    uint8_t tx[2 + FRAME_MAX];
} Conn;

void conn_init(Conn *c);
// Resolve and connect within timeout_ms. `host` is an IPv4 literal or a name.
int conn_open(Conn *c, const char *host, uint16_t port, int timeout_ms);
// The address the last conn_open dialed and, when it failed, the OS error (0 = none), for logs.
extern char conn_last_addr[16];
extern int conn_last_errno;
// Call-back (protocol.md §2.3), for networks where the 3DS can't open TCP to the PC (IoT/guest Wi-Fi) but
// the PC can reach the 3DS: listen, ask the PC over UDP to dial us, accept. The stream then works exactly
// like conn_open's (the 3DS still runs the handshake as initiator).
int conn_callback(Conn *c, const char *host, uint16_t discovery_port, int timeout_ms);
// Away route (protocol.md §2.4): after conn_open to the funnel, wrap the stream in TLS (SNI = `sni`). Everything
// after it (handshake, bodies) is unchanged. CONN_OK, or CONN_ERR_TLS with the reason in err.
int conn_start_tls(Conn *c, const char *sni, int timeout_ms, TlsInfo *info, char *err, size_t errlen);
// Secure handshake. `hello` is a complete body (kind byte + JSON). On success the reply body (welcome or
// error) is returned via *reply / *reply_len, pointing into c->rx (valid until the next receive).
int conn_handshake(Conn *c, const uint8_t s_priv[32], const uint8_t server_pub[32], const uint8_t e_priv[32],
                   const uint8_t *hello, size_t hello_len, const uint8_t **reply, size_t *reply_len,
                   int timeout_ms);
// Encrypt and send one body; blocks up to timeout_ms for socket space.
int conn_send_body(Conn *c, const uint8_t *body, size_t len, int timeout_ms);
int conn_send_json(Conn *c, const char *json, int timeout_ms);
// Wait up to timeout_ms for one complete body. CONN_OK with *body pointing into c->rx, or CONN_TIMEOUT.
int conn_recv_body(Conn *c, int timeout_ms, const uint8_t **body, size_t *len);
void conn_close(Conn *c);

// --- discovery --------------------------------------------------------------

typedef struct {
    char addr[16];   // dotted IPv4 of the responder
    char json[512];  // its raw `here` reply (NUL-terminated)
} DiscoveryReply;

// Sends "H3DS?"+v to `target` (e.g. "255.255.255.255" or a cached IP) up to `tries` times, `interval_ms`
// apart, collecting distinct replies. Returns the number found (<= max), or <0 on socket error.
int discover(const char *target, uint16_t port, int tries, int interval_ms, DiscoveryReply *out, int max);
