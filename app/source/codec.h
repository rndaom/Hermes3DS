// Text codecs used by pairing and discovery (protocol.md §2): base64url, base32, key fingerprints.
#pragma once

#include <stddef.h>
#include <stdint.h>

// Unpadded base64url. `out` needs ((len + 2) / 3) * 4 + 1 bytes. Returns the string length.
size_t b64u_encode(const uint8_t *in, size_t len, char *out);
// Accepts padded or unpadded base64url. Returns bytes written, or -1 on bad input / overflow.
int b64u_decode(const char *in, uint8_t *out, size_t cap);

// RFC 4648 base32 without padding (A-Z, 2-7). `out` needs ((len * 8 + 4) / 5) + 1 bytes.
size_t b32_encode(const uint8_t *in, size_t len, char *out);
// Case-insensitive; ignores spaces and '-'. Returns bytes written, or -1 on bad input.
int b32_decode(const char *in, uint8_t *out, size_t cap);

// 8 base32 chars of BLAKE2b-256(key) + NUL; the server fingerprint shown during pairing.
void key_fingerprint(const uint8_t key[32], char out[9]);
