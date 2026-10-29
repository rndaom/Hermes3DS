// Noise_IK_25519_ChaChaPoly_BLAKE2b, initiator side only (docs/protocol.md §3).
// Portable C99 on top of Monocypher; no allocation, no platform calls. Also built on the PC by app/test.
#pragma once

#include <stddef.h>
#include <stdint.h>

#define NOISE_DHLEN 32
#define NOISE_HASHLEN 64
#define NOISE_TAGLEN 16
#define NOISE_MAX_MSG 65535

#define NOISE_PROLOGUE "hermes3ds/1"

// Monocypher's crypto_aead_write/read ratchet their key after every message, which Noise does not.
// A CipherState therefore keeps the raw key and runs a fresh crypto_aead_init_ietf per message.
typedef struct {
    uint8_t k[32];
    uint64_t n;
    int has_key;
} NoiseCipher;

typedef struct {
    uint8_t ck[NOISE_HASHLEN];
    uint8_t h[NOISE_HASHLEN];
    NoiseCipher cs;
} NoiseSymmetric;

typedef struct {
    NoiseSymmetric ss;
    uint8_t s_priv[32], s_pub[32];
    uint8_t e_priv[32], e_pub[32];
    uint8_t rs[32];
} NoiseInitiator;

typedef struct {
    NoiseCipher send, recv;
    uint8_t handshake_hash[NOISE_HASHLEN];
} NoiseTransport;

enum {
    NOISE_OK = 0,
    NOISE_ERR_SIZE = -1,     // buffer too small / message too large or too short
    NOISE_ERR_DECRYPT = -2,  // authentication failed
    NOISE_ERR_KEY = -3,      // DH produced the all-zero output (low-order public key)
    NOISE_ERR_NONCE = -4,
};

// X25519 public key from a 32-byte private key.
void noise_public_key(uint8_t pub[32], const uint8_t priv[32]);

void noise_initiator_init(NoiseInitiator *hs, const uint8_t s_priv[32], const uint8_t rs[32],
                          const uint8_t *prologue, size_t prologue_len);

// -> e, es, s, ss + payload. `e_priv` is 32 fresh random bytes (fixed in tests).
// `out` needs 32 + 48 + payload_len + 16 bytes.
int noise_write_message_1(NoiseInitiator *hs, const uint8_t e_priv[32], const uint8_t *payload,
                          size_t payload_len, uint8_t *out, size_t out_cap, size_t *out_len);

// <- e, ee, se + payload. On success fills `t` (send/recv ciphers) and wipes the handshake state.
// `payload_out` needs msg_len - 48 bytes.
int noise_read_message_2(NoiseInitiator *hs, const uint8_t *msg, size_t msg_len, uint8_t *payload_out,
                         size_t payload_cap, size_t *payload_len, NoiseTransport *t);

// Transport messages: out = ciphertext || tag (len + 16). In-place (out == in) is allowed.
int noise_encrypt(NoiseCipher *c, const uint8_t *ad, size_t ad_len, const uint8_t *pt, size_t len,
                  uint8_t *out);
// in = ciphertext || tag; out gets len - 16 bytes. In-place allowed.
int noise_decrypt(NoiseCipher *c, const uint8_t *ad, size_t ad_len, const uint8_t *in, size_t len,
                  uint8_t *out);

void noise_wipe(void *p, size_t n);
