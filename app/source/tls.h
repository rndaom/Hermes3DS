// TLS 1.2 client (mbedTLS) over a connected non-blocking socket, for the away-from-home route (protocol.md
// §2.4): Tailscale Funnel terminates TLS on the PC and the Noise stream runs inside. The certificate is not
// checked: Noise IK already authenticates the PC by its pinned key, so TLS only has to get us through the
// funnel (SNI picks the machine). Single-threaded: only the network worker uses it.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "sock.h"

typedef struct Tls Tls;

enum {
    TLS_WANT_READ = -1000,   // poll for POLLIN, then call again with the same arguments
    TLS_WANT_WRITE = -1001,  // poll for POLLOUT, then call again
    TLS_ERR = -1002,
};

typedef struct {
    uint32_t ms;     // handshake wall time
    uint32_t cpu_ms; // of which spent computing (the rest is network round trips)
    int resumed;     // abbreviated handshake (session ticket / id from the last connection)
    int ticket;      // the server issued a session ticket (the next connect can resume)
    char suite[48];  // negotiated ciphersuite
} TlsInfo;

// Handshake within timeout_ms. 0 with *out set, or <0 (err gets a short reason).
int tls_open(Tls **out, sock_t fd, const char *sni, int timeout_ms, TlsInfo *info, char *err, size_t errlen);
int tls_write(Tls *t, const uint8_t *p, size_t n);  // >0 bytes written, TLS_WANT_*, TLS_ERR
int tls_read(Tls *t, uint8_t *p, size_t n);         // >0 bytes, 0 = peer closed, TLS_WANT_*, TLS_ERR
void tls_close(Tls *t);                             // close_notify (best effort) and free; the socket stays open
void tls_forget_session(void);                      // drop the cached session (e.g. after a re-pair)
