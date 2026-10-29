#include "codec.h"

#include <ctype.h>
#include <string.h>

#include "monocypher.h"

static const char B64U[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
static const char B32[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

size_t b64u_encode(const uint8_t *in, size_t len, char *out)
{
    size_t o = 0;
    uint32_t acc = 0;
    int bits = 0;
    for (size_t i = 0; i < len; i++) {
        acc = (acc << 8) | in[i];
        bits += 8;
        while (bits >= 6) {
            bits -= 6;
            out[o++] = B64U[(acc >> bits) & 63];
        }
    }
    if (bits > 0) out[o++] = B64U[(acc << (6 - bits)) & 63];
    out[o] = 0;
    return o;
}

static int b64u_val(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '-' || c == '+') return 62;
    if (c == '_' || c == '/') return 63;
    return -1;
}

int b64u_decode(const char *in, uint8_t *out, size_t cap)
{
    size_t o = 0;
    uint32_t acc = 0;
    int bits = 0;
    for (; *in && *in != '='; in++) {
        int v = b64u_val(*in);
        if (v < 0) return -1;
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= cap) return -1;
            out[o++] = (uint8_t)(acc >> bits);
        }
    }
    return (int)o;
}

size_t b32_encode(const uint8_t *in, size_t len, char *out)
{
    size_t o = 0;
    uint32_t acc = 0;
    int bits = 0;
    for (size_t i = 0; i < len; i++) {
        acc = (acc << 8) | in[i];
        bits += 8;
        while (bits >= 5) {
            bits -= 5;
            out[o++] = B32[(acc >> bits) & 31];
        }
    }
    if (bits > 0) out[o++] = B32[(acc << (5 - bits)) & 31];
    out[o] = 0;
    return o;
}

int b32_decode(const char *in, uint8_t *out, size_t cap)
{
    size_t o = 0;
    uint32_t acc = 0;
    int bits = 0;
    for (; *in && *in != '='; in++) {
        char c = (char)toupper((unsigned char)*in);
        int v;
        if (c == ' ' || c == '-') continue;
        if (c >= 'A' && c <= 'Z') v = c - 'A';
        else if (c >= '2' && c <= '7') v = c - '2' + 26;
        else return -1;
        acc = (acc << 5) | (uint32_t)v;
        bits += 5;
        if (bits >= 8) {
            bits -= 8;
            if (o >= cap) return -1;
            out[o++] = (uint8_t)(acc >> bits);
        }
    }
    return (int)o;
}

void key_fingerprint(const uint8_t key[32], char out[9])
{
    uint8_t digest[32];
    char full[64];
    crypto_blake2b(digest, sizeof digest, key, 32);
    b32_encode(digest, sizeof digest, full);
    memcpy(out, full, 8);
    out[8] = 0;
}
