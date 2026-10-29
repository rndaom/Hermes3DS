#include "tls.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef H3_NO_TLS  // host builds without mbedTLS (test/h3probe): the away route is unavailable

int tls_open(Tls **out, sock_t fd, const char *sni, int timeout_ms, TlsInfo *info, char *err, size_t errlen)
{
    snprintf(err, errlen, "built without TLS");
    return TLS_ERR;
}
int tls_write(Tls *t, const uint8_t *p, size_t n) { return TLS_ERR; }
int tls_read(Tls *t, uint8_t *p, size_t n) { return TLS_ERR; }
void tls_close(Tls *t) {}
void tls_forget_session(void) {}

#else

#include <mbedtls/ctr_drbg.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/ssl.h>

struct Tls {
    mbedtls_ssl_context ssl;
    sock_t fd;
    int io_failed;  // the socket failed or closed (the PC is down), as opposed to a TLS-level error
};

// Fast on an ARM11 without AES instructions: ChaCha20-Poly1305 first (Go's TLS stack, which terminates the
// funnel, then picks it too), X25519 first for the key exchange. TLS 1.2 is all mbedTLS 2.28 speaks.
static const int SUITES[] = {
    MBEDTLS_TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256,
    MBEDTLS_TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256,
    MBEDTLS_TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,
    MBEDTLS_TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256,
    MBEDTLS_TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384,
    MBEDTLS_TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384,
    0,
};
static const mbedtls_ecp_group_id CURVES[] = {
    MBEDTLS_ECP_DP_CURVE25519,
    MBEDTLS_ECP_DP_SECP256R1,
    MBEDTLS_ECP_DP_SECP384R1,
    MBEDTLS_ECP_DP_NONE,
};

static mbedtls_ssl_config conf;
static mbedtls_ctr_drbg_context drbg;
static int conf_ready;

// One cached session (ticket) for the last server: a reconnect then skips the key exchange and the
// signature check, which is most of the handshake's CPU time on an Old 3DS.
static mbedtls_ssl_session cached;
static int have_cached;
static char cached_sni[128];

static int entropy(void *ctx, unsigned char *out, size_t len)
{
#ifdef __3DS__
    return R_FAILED(PS_GenerateRandomBytes(out, len)) ? MBEDTLS_ERR_CTR_DRBG_ENTROPY_SOURCE_FAILED : 0;
#else
#error "tls.c needs an entropy source on this platform (or build with -DH3_NO_TLS)"
#endif
}

static int setup_conf(void)
{
    if (conf_ready) return 0;
    mbedtls_ctr_drbg_init(&drbg);
    static const char pers[] = "hermes3ds tls";
    int r = mbedtls_ctr_drbg_seed(&drbg, entropy, NULL, (const unsigned char *)pers, sizeof pers - 1);
    if (r) return r;
    mbedtls_ssl_config_init(&conf);
    r = mbedtls_ssl_config_defaults(&conf, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                    MBEDTLS_SSL_PRESET_DEFAULT);
    if (r) return r;
    mbedtls_ssl_conf_authmode(&conf, MBEDTLS_SSL_VERIFY_NONE);  // Noise authenticates the PC
    mbedtls_ssl_conf_rng(&conf, mbedtls_ctr_drbg_random, &drbg);
    mbedtls_ssl_conf_ciphersuites(&conf, SUITES);
    mbedtls_ssl_conf_curves(&conf, CURVES);
    mbedtls_ssl_conf_min_version(&conf, MBEDTLS_SSL_MAJOR_VERSION_3, MBEDTLS_SSL_MINOR_VERSION_3);
    mbedtls_ssl_conf_session_tickets(&conf, MBEDTLS_SSL_SESSION_TICKETS_ENABLED);
    mbedtls_ssl_session_init(&cached);
    conf_ready = 1;
    return 0;
}

static int bio_send(void *ctx, const unsigned char *buf, size_t len)
{
    Tls *t = ctx;
    int r = (int)send(t->fd, (const char *)buf, (int)len, 0);
    if (r >= 0) return r;
    int e = sock_errno();
    if (e == SOCK_EWOULDBLOCK || e == SOCK_EINTR) return MBEDTLS_ERR_SSL_WANT_WRITE;
    t->io_failed = 1;
    return MBEDTLS_ERR_NET_SEND_FAILED;
}

static int bio_recv(void *ctx, unsigned char *buf, size_t len)
{
    Tls *t = ctx;
    int r = (int)recv(t->fd, (char *)buf, (int)len, 0);
    if (r > 0) return r;
    if (r == 0) {  // EOF
        t->io_failed = 1;
        return 0;
    }
    int e = sock_errno();
    if (e == SOCK_EWOULDBLOCK || e == SOCK_EINTR) return MBEDTLS_ERR_SSL_WANT_READ;
    t->io_failed = 1;
    return MBEDTLS_ERR_NET_RECV_FAILED;
}

static void set_err(char *err, size_t errlen, const char *what, int r)
{
    char msg[96];
    mbedtls_strerror(r, msg, sizeof msg);
    snprintf(err, errlen, "%s: -0x%04x %s", what, (unsigned)-r, msg);
}

int tls_open(Tls **out, sock_t fd, const char *sni, int timeout_ms, TlsInfo *info, char *err, size_t errlen)
{
    memset(info, 0, sizeof *info);
    int r = setup_conf();
    if (r) {
        set_err(err, errlen, "TLS setup", r);
        return TLS_ERR;
    }
    Tls *t = calloc(1, sizeof *t);
    if (!t) {
        snprintf(err, errlen, "out of memory");
        return TLS_ERR;
    }
    t->fd = fd;
    mbedtls_ssl_init(&t->ssl);
    if ((r = mbedtls_ssl_setup(&t->ssl, &conf)) || (r = mbedtls_ssl_set_hostname(&t->ssl, sni))) {
        set_err(err, errlen, "TLS setup", r);
        goto fail;
    }
    mbedtls_ssl_set_bio(&t->ssl, t, bio_send, bio_recv, NULL);
    if (have_cached && !strcmp(cached_sni, sni)) mbedtls_ssl_set_session(&t->ssl, &cached);

    uint64_t t0 = plat_ms(), deadline = t0 + (uint64_t)timeout_ms, cpu = 0;
    int full = 0;
    while (t->ssl.state != MBEDTLS_SSL_HANDSHAKE_OVER) {
        uint64_t s0 = plat_ms();
        r = mbedtls_ssl_handshake_step(&t->ssl);
        cpu += plat_ms() - s0;
        if (t->ssl.state == MBEDTLS_SSL_CLIENT_KEY_EXCHANGE) full = 1;  // never reached when resuming
        if (r == 0) continue;
        if (r != MBEDTLS_ERR_SSL_WANT_READ && r != MBEDTLS_ERR_SSL_WANT_WRITE) {
            if (t->io_failed) snprintf(err, errlen, "TLS handshake: the connection dropped (PC not answering?)");
            else set_err(err, errlen, "TLS handshake", r);
            goto fail;
        }
        uint64_t now = plat_ms();
        if (now >= deadline) {
            snprintf(err, errlen, "TLS handshake timed out");
            t->io_failed = 1;
            goto fail;
        }
        struct pollfd p = {.fd = fd, .events = r == MBEDTLS_ERR_SSL_WANT_READ ? POLLIN : POLLOUT, .revents = 0};
        if (sock_poll(&p, 1, (int)(deadline - now)) < 0 && sock_errno() != SOCK_EINTR) {
            snprintf(err, errlen, "TLS handshake: socket error");
            t->io_failed = 1;
            goto fail;
        }
    }
    info->ms = (uint32_t)(plat_ms() - t0);
    info->resumed = !full;
    info->ticket = t->ssl.session != NULL && t->ssl.session->ticket_len > 0;
    snprintf(info->suite, sizeof info->suite, "%s", mbedtls_ssl_get_ciphersuite(&t->ssl));
    info->cpu_ms = (uint32_t)cpu;

    mbedtls_ssl_session_free(&cached);
    mbedtls_ssl_session_init(&cached);
    have_cached = mbedtls_ssl_get_session(&t->ssl, &cached) == 0;
    snprintf(cached_sni, sizeof cached_sni, "%s", sni);
    *out = t;
    return 0;
fail:
    // Keep the cached session when the PC was just unreachable: that is when resumption pays off. A server
    // that rejects a stale ticket falls back to a full handshake by itself, so only drop it after a TLS-level
    // failure, in case the cached state itself is the problem.
    if (!t->io_failed && have_cached) tls_forget_session();
    mbedtls_ssl_free(&t->ssl);
    free(t);
    return TLS_ERR;
}

static int map(int r)
{
    if (r >= 0) return r;
    if (r == MBEDTLS_ERR_SSL_WANT_READ) return TLS_WANT_READ;
    if (r == MBEDTLS_ERR_SSL_WANT_WRITE) return TLS_WANT_WRITE;
    return TLS_ERR;
}

int tls_write(Tls *t, const uint8_t *p, size_t n) { return map(mbedtls_ssl_write(&t->ssl, p, n)); }

int tls_read(Tls *t, uint8_t *p, size_t n)
{
    int r = mbedtls_ssl_read(&t->ssl, p, n);
    if (r == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY || r == MBEDTLS_ERR_SSL_CONN_EOF) return 0;
    return map(r);
}

void tls_close(Tls *t)
{
    if (!t) return;
    mbedtls_ssl_close_notify(&t->ssl);  // non-blocking: if it doesn't fit in the socket now, skip it
    mbedtls_ssl_free(&t->ssl);
    free(t);
}

void tls_forget_session(void)
{
    if (!conf_ready) return;
    mbedtls_ssl_session_free(&cached);
    mbedtls_ssl_session_init(&cached);
    have_cached = 0;
}

#endif
