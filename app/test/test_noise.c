// Host-side test: app/source/noise.c and codec.c against the Noise vectors and the plugin's codecs.
// Build and run: app/test/run_tests.sh (clang or gcc on the PC).
#include <stdio.h>
#include <string.h>

#include "../source/codec.h"
#include "../source/noise.h"
#include "vectors.h"

static int failures;

#define CHECK(cond, ...)                                       \
    do {                                                       \
        if (!(cond)) {                                         \
            failures++;                                        \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);        \
            printf(__VA_ARGS__);                               \
            printf("\n");                                      \
        }                                                      \
    } while (0)

static void hexdump(const char *label, const uint8_t *p, size_t n)
{
    printf("  %s: ", label);
    for (size_t i = 0; i < n; i++) printf("%02x", p[i]);
    printf("\n");
}

static void test_vector(int idx, const Vector *v)
{
    NoiseInitiator hs;
    NoiseTransport t;
    static uint8_t buf[NOISE_MAX_MSG], pt[NOISE_MAX_MSG];
    size_t n, pn;
    int rc;

    noise_initiator_init(&hs, v->init_static.p, v->init_remote_static.p, v->prologue.p, v->prologue.n);

    // msg 0: initiator -> responder
    rc = noise_write_message_1(&hs, v->init_ephemeral.p, v->msgs[0].payload.p, v->msgs[0].payload.n, buf,
                               sizeof buf, &n);
    CHECK(rc == 0, "vector %d: write_message_1 rc=%d", idx, rc);
    CHECK(n == v->msgs[0].ciphertext.n && memcmp(buf, v->msgs[0].ciphertext.p, n) == 0,
          "vector %d: msg 0 ciphertext differs", idx);
    if (failures) {
        hexdump("got     ", buf, n);
        hexdump("expected", v->msgs[0].ciphertext.p, v->msgs[0].ciphertext.n);
    }

    // msg 1: responder -> initiator
    rc = noise_read_message_2(&hs, v->msgs[1].ciphertext.p, v->msgs[1].ciphertext.n, pt, sizeof pt, &pn, &t);
    CHECK(rc == 0, "vector %d: read_message_2 rc=%d", idx, rc);
    CHECK(pn == v->msgs[1].payload.n && memcmp(pt, v->msgs[1].payload.p, pn) == 0,
          "vector %d: msg 1 payload differs", idx);
    if (v->handshake_hash.n)  // not every vector source publishes it
        CHECK(memcmp(t.handshake_hash, v->handshake_hash.p, NOISE_HASHLEN) == 0, "vector %d: handshake hash", idx);

    // transport messages alternate: even = initiator sends, odd = initiator receives
    for (int m = 2; m < v->n_msgs; m++) {
        const VecMsg *vm = &v->msgs[m];
        if (m % 2 == 0) {
            memcpy(buf, vm->payload.p, vm->payload.n);  // in place, as net.c does
            rc = noise_encrypt(&t.send, NULL, 0, buf, vm->payload.n, buf);
            CHECK(rc == 0 && vm->payload.n + NOISE_TAGLEN == vm->ciphertext.n &&
                      memcmp(buf, vm->ciphertext.p, vm->ciphertext.n) == 0,
                  "vector %d: transport msg %d encrypt", idx, m);
        } else {
            memcpy(buf, vm->ciphertext.p, vm->ciphertext.n);
            rc = noise_decrypt(&t.recv, NULL, 0, buf, vm->ciphertext.n, buf);
            CHECK(rc == 0 && memcmp(buf, vm->payload.p, vm->payload.n) == 0,
                  "vector %d: transport msg %d decrypt", idx, m);
        }
    }
}

static void test_tamper(const Vector *v)
{
    NoiseInitiator hs;
    NoiseTransport t;
    uint8_t buf[512], pt[512], ct[512];
    size_t n, pn;

    noise_initiator_init(&hs, v->init_static.p, v->init_remote_static.p, v->prologue.p, v->prologue.n);
    noise_write_message_1(&hs, v->init_ephemeral.p, v->msgs[0].payload.p, v->msgs[0].payload.n, buf, sizeof buf, &n);
    memcpy(ct, v->msgs[1].ciphertext.p, v->msgs[1].ciphertext.n);
    ct[v->msgs[1].ciphertext.n - 1] ^= 1;
    CHECK(noise_read_message_2(&hs, ct, v->msgs[1].ciphertext.n, pt, sizeof pt, &pn, &t) == NOISE_ERR_DECRYPT,
          "tampered msg 2 must fail");

    // Replay / reordering: a transport message decrypts only at its own nonce.
    noise_initiator_init(&hs, v->init_static.p, v->init_remote_static.p, v->prologue.p, v->prologue.n);
    noise_write_message_1(&hs, v->init_ephemeral.p, v->msgs[0].payload.p, v->msgs[0].payload.n, buf, sizeof buf, &n);
    noise_read_message_2(&hs, v->msgs[1].ciphertext.p, v->msgs[1].ciphertext.n, pt, sizeof pt, &pn, &t);
    if (v->n_msgs > 5) {
        CHECK(noise_decrypt(&t.recv, NULL, 0, v->msgs[5].ciphertext.p, v->msgs[5].ciphertext.n, pt) ==
                  NOISE_ERR_DECRYPT, "out-of-order message must fail");
        CHECK(noise_decrypt(&t.recv, NULL, 0, v->msgs[3].ciphertext.p, v->msgs[3].ciphertext.n, pt) == 0,
              "in-order message after a failure must still decrypt");
        CHECK(noise_decrypt(&t.recv, NULL, 0, v->msgs[3].ciphertext.p, v->msgs[3].ciphertext.n, pt) ==
                  NOISE_ERR_DECRYPT, "replay must fail");
    }

    // Low-order remote key (all zero) must be rejected.
    static const uint8_t zero[32] = {0};
    noise_initiator_init(&hs, v->init_static.p, zero, v->prologue.p, v->prologue.n);
    CHECK(noise_write_message_1(&hs, v->init_ephemeral.p, NULL, 0, buf, sizeof buf, &n) == NOISE_ERR_KEY,
          "zero remote static must fail");
}

static void test_codecs(void)
{
    char s[128];
    uint8_t b[64];
    for (size_t i = 0; i < sizeof FP_CASES / sizeof FP_CASES[0]; i++) {
        const FpCase *c = &FP_CASES[i];
        key_fingerprint(c->key.p, s);
        CHECK(strcmp(s, c->fp) == 0, "fingerprint %zu: %s != %s", i, s, c->fp);
        b64u_encode(c->key.p, c->key.n, s);
        CHECK(strcmp(s, c->b64u) == 0, "b64u encode %zu", i);
        CHECK(b64u_decode(c->b64u, b, sizeof b) == 32 && memcmp(b, c->key.p, 32) == 0, "b64u decode %zu", i);
    }
    for (size_t i = 0; i < sizeof CODE_CASES / sizeof CODE_CASES[0]; i++) {
        const CodeCase *c = &CODE_CASES[i];
        b32_encode(c->token.p, c->token.n, s);
        CHECK(strcmp(s, c->code) == 0, "b32 encode %zu: %s != %s", i, s, c->code);
        // The 3DS accepts the code as typed: lower case, spaces and dashes.
        char typed[32];
        snprintf(typed, sizeof typed, "%.4s-%s", c->code, c->code + 4);
        for (char *p = typed; *p; p++) *p = (char)(*p | 0x20);
        CHECK(b32_decode(typed, b, sizeof b) == 5 && memcmp(b, c->token.p, 5) == 0, "b32 decode %zu (%s)", i, typed);
    }
    CHECK(b32_decode("ABC1", b, sizeof b) == -1, "b32 must reject '1'");
    CHECK(b64u_decode("ab*c", b, sizeof b) == -1, "b64u must reject '*'");
}

int main(void)
{
    for (int i = 0; i < N_VECTORS; i++) test_vector(i, &VECTORS[i]);
    test_tamper(&VECTORS[0]);
    test_codecs();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("ok: %d Noise IK vectors, tamper/replay, codecs\n", N_VECTORS);
    return 0;
}
