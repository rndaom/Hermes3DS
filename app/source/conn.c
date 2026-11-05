#include "conn.h"

#include <stdio.h>
#include <string.h>

volatile int conn_abort;

static int remaining(uint64_t deadline)
{
    uint64_t now = plat_ms();
    return conn_abort || now >= deadline ? 0 : (int)(deadline - now);
}

// Wait for `events` on the socket. 1 = ready, 0 = timeout, <0 = error. Polls in short slices so that
// conn_abort ends a long wait (a handshake reply, a connect) within a fraction of a second.
static int wait_fd(sock_t fd, short events, int timeout_ms)
{
    uint64_t deadline = plat_ms() + (uint64_t)(timeout_ms < 0 ? 0 : timeout_ms);
    for (;;) {
        int left = remaining(deadline);
        struct pollfd p = {.fd = fd, .events = events, .revents = 0};
        int r = sock_poll(&p, 1, left < 200 ? left : 200);
        if (r < 0) return sock_errno() == SOCK_EINTR ? 0 : -1;
        if (r > 0) {
            if (p.revents & (POLLERR | POLLNVAL)) return -1;
            return 1;  // POLLHUP with pending data still reads; recv() then reports EOF
        }
        if (left <= 200) return 0;
    }
}

void conn_init(Conn *c)
{
    memset(c, 0, offsetof(Conn, rx));
    c->fd = SOCK_INVALID;
}

char conn_last_addr[16];
int conn_last_errno;

static int resolve(const char *host, struct in_addr *out)
{
    if (inet_pton(AF_INET, host, out) == 1) return 0;
    struct hostent *he = gethostbyname(host);
    if (!he || he->h_addrtype != AF_INET || !he->h_addr_list[0]) return -1;
    memcpy(out, he->h_addr_list[0], sizeof *out);
    return 0;
}

int conn_open(Conn *c, const char *host, uint16_t port, int timeout_ms)
{
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons(port);
    if (resolve(host, &sa.sin_addr)) return CONN_ERR_RESOLVE;

    conn_close(c);
    inet_ntop(AF_INET, &sa.sin_addr, conn_last_addr, sizeof conn_last_addr);
    conn_last_errno = 0;
    c->fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (c->fd == SOCK_INVALID) {
        conn_last_errno = sock_errno();
        return CONN_ERR_CONNECT;
    }
    sock_set_nonblocking(c->fd);
    if (connect(c->fd, (struct sockaddr *)&sa, sizeof sa) != 0) {
        int e = sock_errno();
        if (e != SOCK_EINPROGRESS && e != SOCK_EWOULDBLOCK) {
            conn_last_errno = e;
            goto fail;
        }
#ifdef _WIN32
        if (wait_fd(c->fd, POLLOUT, timeout_ms) != 1) goto fail;
        int err = 0;
        socklen_t len = sizeof err;
        if (getsockopt(c->fd, SOL_SOCKET, SO_ERROR, (char *)&err, &len) == 0 && err != 0) {
            conn_last_errno = err;
            goto fail;
        }
#else
        // The 3DS's soc service doesn't reliably report a finished non-blocking connect through poll()/SO_ERROR
        // (only the call-back's accepted streams had ever worked on hardware). Ask connect() itself: 0 or
        // EISCONN = connected, EALREADY/EINPROGRESS = still going, anything else is the real failure.
        uint64_t deadline = plat_ms() + (uint64_t)timeout_ms;
        for (;;) {
            int left = remaining(deadline);
            if (left == 0) {
                conn_last_errno = ETIMEDOUT;
                goto fail;
            }
            wait_fd(c->fd, POLLOUT, left < 100 ? left : 100);
            if (connect(c->fd, (struct sockaddr *)&sa, sizeof sa) == 0) break;
            e = sock_errno();
            if (e == EISCONN) break;
            if (e != EALREADY && e != EINPROGRESS && e != EWOULDBLOCK && e != EINTR) {
                conn_last_errno = e;
                goto fail;
            }
        }
#endif
    }
    int one = 1;
    setsockopt(c->fd, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof one);  // best effort
    c->rx_len = 0;
    c->secure = 0;
    return CONN_OK;
fail:
    conn_close(c);
    return CONN_ERR_CONNECT;
}

int conn_callback(Conn *c, const char *host, uint16_t discovery_port, int timeout_ms)
{
    struct sockaddr_in dst;
    memset(&dst, 0, sizeof dst);
    dst.sin_family = AF_INET;
    dst.sin_port = htons(discovery_port);
    if (resolve(host, &dst.sin_addr)) return CONN_ERR_RESOLVE;
    conn_close(c);

    sock_t ls = socket(AF_INET, SOCK_STREAM, IPPROTO_IP), us = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    int rc = CONN_ERR_CONNECT;
    uint16_t port = 0;
    if (ls == SOCK_INVALID || us == SOCK_INVALID) goto out;
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, (const char *)&one, sizeof one);
    for (uint16_t p = CALLBACK_PORT; p < CALLBACK_PORT + 4 && !port; p++) {
        struct sockaddr_in la;
        memset(&la, 0, sizeof la);
        la.sin_family = AF_INET;
        la.sin_port = htons(p);
        la.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(ls, (struct sockaddr *)&la, sizeof la) == 0) port = p;
    }
    if (!port || listen(ls, 2) != 0) goto out;
    sock_set_nonblocking(ls);
    sock_set_nonblocking(us);

    const uint8_t req[8] = {'H', '3', 'D', 'S', '<', 1, (uint8_t)(port >> 8), (uint8_t)port};
    uint64_t deadline = plat_ms() + (uint64_t)timeout_ms, next_send = 0;
    int sends = 0, acked = 0;
    while (remaining(deadline) > 0) {
        if (sends < 4 && plat_ms() >= next_send) {  // UDP can drop: repeat the request a few times
            sendto(us, (const char *)req, sizeof req, 0, (struct sockaddr *)&dst, sizeof dst);
            sends++;
            next_send = plat_ms() + 700;
        }
        // Non-blocking accept, polled: works whether or not poll() reports listening sockets.
        struct sockaddr_in from;
        socklen_t flen = sizeof from;
        sock_t a = accept(ls, (struct sockaddr *)&from, &flen);
        if (a != SOCK_INVALID) {
            sock_set_nonblocking(a);
            setsockopt(a, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof one);
            c->fd = a;
            c->rx_len = 0;
            c->secure = 0;
            rc = CONN_OK;
            break;
        }
        char buf[128];
        int r = (int)recvfrom(us, buf, (int)sizeof buf - 1, 0, NULL, NULL);
        if (r > 0) {
            buf[r] = 0;
            if (strstr(buf, "\"dialing\"")) acked = 1;
        }
        wait_fd(us, POLLIN, 50);
    }
    if (rc != CONN_OK && acked) rc = CONN_ERR_BLOCKED;
out:
    if (ls != SOCK_INVALID) sock_close(ls);
    if (us != SOCK_INVALID) sock_close(us);
    return rc;
}

int conn_start_tls(Conn *c, const char *sni, int timeout_ms, TlsInfo *info, char *err, size_t errlen)
{
    if (c->fd == SOCK_INVALID || c->tls) return CONN_ERR_TLS;
    return tls_open(&c->tls, c->fd, sni, timeout_ms, info, err, errlen) == 0 ? CONN_OK : CONN_ERR_TLS;
}

void conn_close(Conn *c)
{
    if (c->tls) {
        tls_close(c->tls);
        c->tls = NULL;
    }
    if (c->fd != SOCK_INVALID) {
        sock_close(c->fd);
        c->fd = SOCK_INVALID;
    }
    noise_wipe(&c->t, sizeof c->t);
    c->secure = 0;
    c->rx_len = 0;
}

// Stream I/O, plain or inside TLS: >0 bytes, 0 = peer closed, IO_WAIT_* = poll for that, then call again.
enum { IO_WAIT_READ = -1, IO_WAIT_WRITE = -2, IO_ERR = -3 };

static int io_send(Conn *c, const uint8_t *p, size_t n)
{
    if (c->tls) {
        int r = tls_write(c->tls, p, n);
        return r > 0 ? r : r == TLS_WANT_READ ? IO_WAIT_READ : r == TLS_WANT_WRITE ? IO_WAIT_WRITE : IO_ERR;
    }
    int r = (int)send(c->fd, (const char *)p, (int)n, 0);
    if (r > 0) return r;
    int e = sock_errno();
    return r < 0 && (e == SOCK_EWOULDBLOCK || e == SOCK_EINTR) ? IO_WAIT_WRITE : IO_ERR;
}

static int io_recv(Conn *c, uint8_t *p, size_t n)
{
    if (c->tls) {
        // Reads come out of mbedTLS's record buffer first, so a whole buffered record never waits on poll().
        int r = tls_read(c->tls, p, n);
        return r >= 0 ? r : r == TLS_WANT_READ ? IO_WAIT_READ : r == TLS_WANT_WRITE ? IO_WAIT_WRITE : IO_ERR;
    }
    int r = (int)recv(c->fd, (char *)p, (int)n, 0);
    if (r >= 0) return r;
    int e = sock_errno();
    return e == SOCK_EWOULDBLOCK || e == SOCK_EINTR ? IO_WAIT_READ : IO_ERR;
}

static int send_all(Conn *c, const uint8_t *p, size_t n, int timeout_ms)
{
    uint64_t deadline = plat_ms() + (uint64_t)timeout_ms;
    while (n > 0) {
        int r = io_send(c, p, n);
        if (r > 0) {
            p += r;
            n -= (size_t)r;
            continue;
        }
        if (r == IO_WAIT_READ || r == IO_WAIT_WRITE) {
            int left = remaining(deadline);
            if (left == 0 || wait_fd(c->fd, r == IO_WAIT_READ ? POLLIN : POLLOUT, left) < 0) return CONN_ERR_IO;
            continue;
        }
        return CONN_ERR_IO;
    }
    return CONN_OK;
}

// Send c->tx[2 .. 2+payload_len) as one frame.
static int send_frame(Conn *c, size_t payload_len, int timeout_ms)
{
    if (payload_len > FRAME_MAX) return CONN_ERR_PROTO;
    c->tx[0] = (uint8_t)(payload_len >> 8);
    c->tx[1] = (uint8_t)payload_len;
    return send_all(c, c->tx, 2 + payload_len, timeout_ms);
}

// Read until one whole frame is in c->rx. CONN_OK -> payload at c->rx+2, length *len.
static int recv_frame(Conn *c, int timeout_ms, size_t *len)
{
    uint64_t deadline = plat_ms() + (uint64_t)(timeout_ms < 0 ? 0 : timeout_ms);
    for (;;) {
        size_t need = c->rx_len < 2 ? 2 : 2 + (((size_t)c->rx[0] << 8) | c->rx[1]);
        if (c->rx_len >= 2 && c->rx_len == need) {
            *len = need - 2;
            c->rx_len = 0;  // consumed; the payload stays readable until the next receive
            return CONN_OK;
        }
        int r = io_recv(c, c->rx + c->rx_len, need - c->rx_len);
        if (r > 0) {
            c->rx_len += (size_t)r;
            continue;
        }
        if (r == 0 || r == IO_ERR) return CONN_ERR_IO;  // peer closed or socket error
        int left = remaining(deadline);
        if (left == 0) return CONN_TIMEOUT;
        int w = wait_fd(c->fd, r == IO_WAIT_WRITE ? POLLOUT : POLLIN, left);
        if (w < 0) return CONN_ERR_IO;
        if (w == 0) return CONN_TIMEOUT;
    }
}

int conn_handshake(Conn *c, const uint8_t s_priv[32], const uint8_t server_pub[32], const uint8_t e_priv[32],
                   const uint8_t *hello, size_t hello_len, const uint8_t **reply, size_t *reply_len,
                   int timeout_ms)
{
    NoiseInitiator hs;
    size_t n;
    int rc;
    uint64_t deadline = plat_ms() + (uint64_t)timeout_ms;

    noise_initiator_init(&hs, s_priv, server_pub, (const uint8_t *)NOISE_PROLOGUE, sizeof(NOISE_PROLOGUE) - 1);
    if (noise_write_message_1(&hs, e_priv, hello, hello_len, c->tx + 2, FRAME_MAX, &n)) {
        noise_wipe(&hs, sizeof hs);
        return CONN_ERR_CRYPTO;
    }
    if ((rc = send_frame(c, n, remaining(deadline)))) goto out;
    do {
        rc = recv_frame(c, remaining(deadline), &n);
    } while (rc == CONN_TIMEOUT && remaining(deadline) > 0);
    if (rc == CONN_TIMEOUT) rc = CONN_ERR_IO;
    if (rc) goto out;
    // The payload is decrypted out of place into tx (free again after the send).
    if (noise_read_message_2(&hs, c->rx + 2, n, c->tx, sizeof c->tx, reply_len, &c->t)) {
        rc = CONN_ERR_CRYPTO;
        goto out;
    }
    memcpy(c->rx, c->tx, *reply_len);
    *reply = c->rx;
    if (*reply_len < 1) rc = CONN_ERR_PROTO;
    c->secure = 1;
out:
    noise_wipe(&hs, sizeof hs);
    return rc;
}

int conn_send_body(Conn *c, const uint8_t *body, size_t len, int timeout_ms)
{
    if (len == 0 || len > BODY_MAX) return CONN_ERR_PROTO;
    if (body != c->tx + 2) memmove(c->tx + 2, body, len);
    if (c->secure) {
        if (noise_encrypt(&c->t.send, NULL, 0, c->tx + 2, len, c->tx + 2)) return CONN_ERR_CRYPTO;
        len += NOISE_TAGLEN;
    }
    return send_frame(c, len, timeout_ms);
}

int conn_send_json(Conn *c, const char *json, int timeout_ms)
{
    size_t n = strlen(json);
    if (n + 1 > BODY_MAX) return CONN_ERR_PROTO;
    c->tx[2] = BODY_JSON;
    memcpy(c->tx + 3, json, n);
    return conn_send_body(c, c->tx + 2, n + 1, timeout_ms);
}

int conn_recv_body(Conn *c, int timeout_ms, const uint8_t **body, size_t *len)
{
    size_t n;
    int rc = recv_frame(c, timeout_ms, &n);
    if (rc) return rc;
    uint8_t *p = c->rx + 2;
    if (c->secure) {
        if (noise_decrypt(&c->t.recv, NULL, 0, p, n, p)) return CONN_ERR_CRYPTO;
        n -= NOISE_TAGLEN;
    }
    if (n < 1) return CONN_ERR_PROTO;
    *body = p;
    *len = n;
    return CONN_OK;
}

// --- discovery --------------------------------------------------------------

int discover(const char *target, uint16_t port, int tries, int interval_ms, DiscoveryReply *out, int max)
{
    struct sockaddr_in dst;
    memset(&dst, 0, sizeof dst);
    dst.sin_family = AF_INET;
    dst.sin_port = htons(port);
    if (inet_pton(AF_INET, target, &dst.sin_addr) != 1) return -1;

    sock_t s = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (s == SOCK_INVALID) return -1;
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_BROADCAST, (const char *)&one, sizeof one);
    sock_set_nonblocking(s);

    static const uint8_t probe[6] = {'H', '3', 'D', 'S', '?', 1};
    int found = 0;
    char buf[sizeof out[0].json];
    for (int t = 0; t < tries && found < max; t++) {
        sendto(s, (const char *)probe, sizeof probe, 0, (struct sockaddr *)&dst, sizeof dst);
        uint64_t deadline = plat_ms() + (uint64_t)interval_ms;
        int left;
        while ((left = remaining(deadline)) > 0 && found < max) {
            if (wait_fd(s, POLLIN, left) != 1) break;
            struct sockaddr_in from;
            socklen_t flen = sizeof from;
            int r = (int)recvfrom(s, buf, (int)sizeof buf - 1, 0, (struct sockaddr *)&from, &flen);
            if (r <= 0) continue;
            buf[r] = 0;
            char addr[16];
            inet_ntop(AF_INET, &from.sin_addr, addr, sizeof addr);
            int dup = 0;
            for (int i = 0; i < found; i++) dup |= strcmp(out[i].addr, addr) == 0;
            if (dup) continue;
            memcpy(out[found].addr, addr, sizeof addr);
            memcpy(out[found].json, buf, (size_t)r + 1);
            found++;
        }
        if (found) break;  // one round of answers is enough
    }
    sock_close(s);
    return found;
}
