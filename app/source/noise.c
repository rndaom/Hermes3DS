// Noise_IK_25519_ChaChaPoly_BLAKE2b initiator; mirrors plugin/h3proto/noise.py (Initiator).
#include "noise.h"

#include <string.h>

#include "monocypher.h"

#define PROTOCOL_NAME "Noise_IK_25519_ChaChaPoly_BLAKE2b"
#define BLAKE2B_BLOCK 128

void noise_wipe(void *p, size_t n) { crypto_wipe(p, n); }

// --- primitives -------------------------------------------------------------

static void hmac_blake2b(uint8_t out[NOISE_HASHLEN], const uint8_t *key, size_t key_len,
                         const uint8_t *d1, size_t n1, const uint8_t *d2, size_t n2)
{
    // Keys here are at most HASHLEN (64) bytes, below the 128-byte block, so no pre-hash is needed.
    uint8_t pad[BLAKE2B_BLOCK], inner[NOISE_HASHLEN];
    crypto_blake2b_ctx ctx;

    memset(pad, 0x36, sizeof pad);
    for (size_t i = 0; i < key_len; i++) pad[i] ^= key[i];
    crypto_blake2b_init(&ctx, NOISE_HASHLEN);
    crypto_blake2b_update(&ctx, pad, sizeof pad);
    if (n1) crypto_blake2b_update(&ctx, d1, n1);
    if (n2) crypto_blake2b_update(&ctx, d2, n2);
    crypto_blake2b_final(&ctx, inner);

    memset(pad, 0x5c, sizeof pad);
    for (size_t i = 0; i < key_len; i++) pad[i] ^= key[i];
    crypto_blake2b_init(&ctx, NOISE_HASHLEN);
    crypto_blake2b_update(&ctx, pad, sizeof pad);
    crypto_blake2b_update(&ctx, inner, sizeof inner);
    crypto_blake2b_final(&ctx, out);

    crypto_wipe(pad, sizeof pad);
    crypto_wipe(inner, sizeof inner);
}

// HKDF with two outputs (Noise §4.3).
static void hkdf2(const uint8_t ck[NOISE_HASHLEN], const uint8_t *ikm, size_t ikm_len,
                  uint8_t out1[NOISE_HASHLEN], uint8_t out2[NOISE_HASHLEN])
{
    uint8_t temp[NOISE_HASHLEN];
    static const uint8_t one = 0x01, two = 0x02;
    hmac_blake2b(temp, ck, NOISE_HASHLEN, ikm, ikm_len, NULL, 0);
    hmac_blake2b(out1, temp, NOISE_HASHLEN, &one, 1, NULL, 0);
    hmac_blake2b(out2, temp, NOISE_HASHLEN, out1, NOISE_HASHLEN, &two, 1);
    crypto_wipe(temp, sizeof temp);
}

static int dh(uint8_t out[32], const uint8_t priv[32], const uint8_t pub[32])
{
    static const uint8_t zero[32] = {0};
    crypto_x25519(out, priv, pub);
    return crypto_verify32(out, zero) == 0 ? NOISE_ERR_KEY : NOISE_OK;
}

void noise_public_key(uint8_t pub[32], const uint8_t priv[32]) { crypto_x25519_public_key(pub, priv); }

// --- CipherState ------------------------------------------------------------

static void cipher_init(NoiseCipher *c, const uint8_t *key)
{
    if (key) {
        memcpy(c->k, key, 32);
        c->has_key = 1;
    } else {
        memset(c->k, 0, 32);
        c->has_key = 0;
    }
    c->n = 0;
}

static void ietf_ctx(crypto_aead_ctx *ctx, const NoiseCipher *c)
{
    // Noise ChaChaPoly nonce: 32 zero bits || 64-bit little-endian counter.
    uint8_t nonce[12] = {0};
    for (int i = 0; i < 8; i++) nonce[4 + i] = (uint8_t)(c->n >> (8 * i));
    crypto_aead_init_ietf(ctx, c->k, nonce);
}

int noise_encrypt(NoiseCipher *c, const uint8_t *ad, size_t ad_len, const uint8_t *pt, size_t len, uint8_t *out)
{
    if (!c->has_key) {
        if (out != pt) memmove(out, pt, len);
        return NOISE_OK;
    }
    if (c->n == UINT64_MAX) return NOISE_ERR_NONCE;
    crypto_aead_ctx ctx;
    ietf_ctx(&ctx, c);
    crypto_aead_write(&ctx, out, out + len, ad, ad_len, pt, len);
    crypto_wipe(&ctx, sizeof ctx);
    c->n++;
    return NOISE_OK;
}

int noise_decrypt(NoiseCipher *c, const uint8_t *ad, size_t ad_len, const uint8_t *in, size_t len, uint8_t *out)
{
    if (!c->has_key) {
        if (out != in) memmove(out, in, len);
        return NOISE_OK;
    }
    if (len < NOISE_TAGLEN) return NOISE_ERR_SIZE;
    if (c->n == UINT64_MAX) return NOISE_ERR_NONCE;
    size_t n = len - NOISE_TAGLEN;
    uint8_t mac[NOISE_TAGLEN];
    memcpy(mac, in + n, NOISE_TAGLEN);  // the tag may be overwritten when decrypting in place
    crypto_aead_ctx ctx;
    ietf_ctx(&ctx, c);
    int bad = crypto_aead_read(&ctx, out, mac, ad, ad_len, in, n);
    crypto_wipe(&ctx, sizeof ctx);
    if (bad) return NOISE_ERR_DECRYPT;
    c->n++;
    return NOISE_OK;
}

// --- SymmetricState ---------------------------------------------------------

static void mix_hash(NoiseSymmetric *ss, const uint8_t *data, size_t len)
{
    crypto_blake2b_ctx ctx;
    crypto_blake2b_init(&ctx, NOISE_HASHLEN);
    crypto_blake2b_update(&ctx, ss->h, NOISE_HASHLEN);
    crypto_blake2b_update(&ctx, data, len);
    crypto_blake2b_final(&ctx, ss->h);
}

static void mix_key(NoiseSymmetric *ss, const uint8_t ikm[32])
{
    uint8_t temp_k[NOISE_HASHLEN];
    hkdf2(ss->ck, ikm, 32, ss->ck, temp_k);
    cipher_init(&ss->cs, temp_k);  // truncated to 32 bytes
    crypto_wipe(temp_k, sizeof temp_k);
}

static int encrypt_and_hash(NoiseSymmetric *ss, const uint8_t *pt, size_t len, uint8_t *out, size_t *out_len)
{
    int rc = noise_encrypt(&ss->cs, ss->h, NOISE_HASHLEN, pt, len, out);
    if (rc) return rc;
    *out_len = len + (ss->cs.has_key ? NOISE_TAGLEN : 0);
    mix_hash(ss, out, *out_len);
    return NOISE_OK;
}

static int decrypt_and_hash(NoiseSymmetric *ss, const uint8_t *ct, size_t len, uint8_t *out, size_t *out_len)
{
    // h must absorb the ciphertext, so decrypt out of place.
    int rc = noise_decrypt(&ss->cs, ss->h, NOISE_HASHLEN, ct, len, out);
    if (rc) return rc;
    *out_len = len - (ss->cs.has_key ? NOISE_TAGLEN : 0);
    mix_hash(ss, ct, len);
    return NOISE_OK;
}

// --- IK initiator -----------------------------------------------------------

void noise_initiator_init(NoiseInitiator *hs, const uint8_t s_priv[32], const uint8_t rs[32],
                          const uint8_t *prologue, size_t prologue_len)
{
    memset(hs, 0, sizeof *hs);
    size_t name_len = sizeof(PROTOCOL_NAME) - 1;  // <= HASHLEN: h = name zero-padded
    memcpy(hs->ss.h, PROTOCOL_NAME, name_len);
    memcpy(hs->ss.ck, hs->ss.h, NOISE_HASHLEN);
    cipher_init(&hs->ss.cs, NULL);
    mix_hash(&hs->ss, prologue, prologue_len);
    memcpy(hs->s_priv, s_priv, 32);
    noise_public_key(hs->s_pub, s_priv);
    memcpy(hs->rs, rs, 32);
    mix_hash(&hs->ss, rs, 32);  // <- s
}

int noise_write_message_1(NoiseInitiator *hs, const uint8_t e_priv[32], const uint8_t *payload,
                          size_t payload_len, uint8_t *out, size_t out_cap, size_t *out_len)
{
    NoiseSymmetric *ss = &hs->ss;
    size_t need = NOISE_DHLEN + NOISE_DHLEN + NOISE_TAGLEN + payload_len + NOISE_TAGLEN, n;
    uint8_t shared[32];
    int rc;

    if (need > NOISE_MAX_MSG || need > out_cap) return NOISE_ERR_SIZE;
    memcpy(hs->e_priv, e_priv, 32);
    noise_public_key(hs->e_pub, e_priv);

    memcpy(out, hs->e_pub, 32);                                   // e
    mix_hash(ss, hs->e_pub, 32);
    if ((rc = dh(shared, hs->e_priv, hs->rs))) goto done;         // es
    mix_key(ss, shared);
    if ((rc = encrypt_and_hash(ss, hs->s_pub, 32, out + 32, &n))) goto done;  // s
    if ((rc = dh(shared, hs->s_priv, hs->rs))) goto done;         // ss
    mix_key(ss, shared);
    if ((rc = encrypt_and_hash(ss, payload, payload_len, out + 32 + n, &n))) goto done;
    *out_len = need;
done:
    crypto_wipe(shared, sizeof shared);
    return rc;
}

int noise_read_message_2(NoiseInitiator *hs, const uint8_t *msg, size_t msg_len, uint8_t *payload_out,
                         size_t payload_cap, size_t *payload_len, NoiseTransport *t)
{
    NoiseSymmetric *ss = &hs->ss;
    uint8_t shared[32], k1[NOISE_HASHLEN], k2[NOISE_HASHLEN];
    int rc;

    if (msg_len < NOISE_DHLEN + NOISE_TAGLEN) return NOISE_ERR_SIZE;
    if (msg_len - NOISE_DHLEN - NOISE_TAGLEN > payload_cap) return NOISE_ERR_SIZE;
    const uint8_t *re = msg;
    mix_hash(ss, re, 32);                                         // e
    if ((rc = dh(shared, hs->e_priv, re))) goto done;             // ee
    mix_key(ss, shared);
    if ((rc = dh(shared, hs->s_priv, re))) goto done;             // se
    mix_key(ss, shared);
    if ((rc = decrypt_and_hash(ss, msg + 32, msg_len - 32, payload_out, payload_len))) goto done;

    hkdf2(ss->ck, NULL, 0, k1, k2);                               // Split()
    cipher_init(&t->send, k1);
    cipher_init(&t->recv, k2);
    memcpy(t->handshake_hash, ss->h, NOISE_HASHLEN);
    crypto_wipe(k1, sizeof k1);
    crypto_wipe(k2, sizeof k2);
    crypto_wipe(hs, sizeof *hs);
done:
    crypto_wipe(shared, sizeof shared);
    return rc;
}
