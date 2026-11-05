// Host test for source/dspfirm.c (the DSP firmware inside the HOME Menu's .code). A reference compressor packs a
// made-up .code holding a made-up firmware, so the unpacker, the search and the checks run without anything of
// Nintendo's; a fuzz pass then damages the packed file to show the unpacker never leaves its buffers.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/dspfirm.h"

static int fails;
#define CHECK(c)                                                       \
    do {                                                               \
        if (!(c)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);        \
            fails++;                                                   \
        }                                                              \
    } while (0)

static uint32_t rng = 0x3D5u;
static uint32_t next(void)
{
    rng ^= rng << 13, rng ^= rng >> 17, rng ^= rng << 5;
    return rng;
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v & 255, p[1] = v >> 8 & 255, p[2] = v >> 16 & 255, p[3] = v >> 24;
}

// Stands in for SHA-256: the parser only compares 32 bytes, so any hash that sees every byte will do.
static void toy_hash(const uint8_t *data, size_t n, uint8_t out[32])
{
    uint64_t h = 0xCBF29CE484222325ull;
    for (int r = 0; r < 4; r++) {
        for (size_t i = 0; i < n; i++) h = (h ^ data[i] ^ (uint64_t)r) * 0x100000001B3ull;
        for (int b = 0; b < 8; b++) out[r * 8 + b] = (uint8_t)(h >> (b * 8));
    }
}

// The packer the loader's unpacker expects: items from the top down, `keep` bytes at the bottom stored as is, an
// 8-byte footer. Greedy longest match; returns the packed size.
static size_t pack(const uint8_t *in, size_t total, size_t keep, uint8_t *out)
{
    uint8_t *rd = malloc(total * 2 + 16);  // the items in the order the unpacker reads them (top down)
    size_t r = 0, dst = total;
    while (dst > keep) {
        size_t flag_at = r++;
        uint8_t flags = 0;
        for (int i = 0; i < 8 && dst > keep; i++) {
            size_t best = 0, best_back = 0;
            for (size_t back = 3; back <= 4098 && dst - 1 + back < total; back++) {
                size_t len = 0;
                while (len < 18 && len < dst - keep && dst - 1 - len + back < total &&
                       in[dst - 1 - len] == in[dst - 1 - len + back])
                    len++;
                if (len > best) best = len, best_back = back;
            }
            if (best >= 3) {
                unsigned v = (unsigned)(best - 3) << 12 | (unsigned)(best_back - 3);
                flags |= (uint8_t)(0x80 >> i);
                rd[r++] = (uint8_t)(v >> 8);  // read as [lo, hi] going up, so hi comes first going down
                rd[r++] = (uint8_t)v;
                dst -= best;
            } else {
                rd[r++] = in[--dst];
            }
        }
        rd[flag_at] = flags;
    }
    memcpy(out, in, keep);
    for (size_t i = 0; i < r; i++) out[keep + i] = rd[r - 1 - i];
    size_t n = keep + r + 8;
    put32(out + n - 8, 8u << 24 | (uint32_t)(r + 8));
    put32(out + n - 4, (uint32_t)(total - n));
    free(rd);
    return n;
}

#define SEGS 3
static const uint32_t SEG_SIZE[SEGS] = {0x400, 0x123, 0x200};

// A firmware image: signature, header, 3 segments with their hashes.
static size_t make_firm(uint8_t *p)
{
    uint32_t off = 0x300;
    memset(p, 0, 0x300);
    for (int i = 0; i < 0x100; i++) p[i] = (uint8_t)next();
    memcpy(p + 0x100, "DSP1", 4);
    p[0x10E] = SEGS;
    for (int s = 0; s < SEGS; s++) {
        uint8_t *h = p + 0x120 + s * 0x30;
        for (uint32_t i = 0; i < SEG_SIZE[s]; i++) p[off + i] = (uint8_t)(s & 1 ? next() : i / 7);
        put32(h, off);
        put32(h + 4, 0x1000 * s);
        put32(h + 8, SEG_SIZE[s]);
        toy_hash(p + off, SEG_SIZE[s], h + 0x10);
        off += SEG_SIZE[s];
    }
    put32(p + 0x104, off);
    return off;
}

// A .code: compressible "code" at the bottom, noise near the top, a decoy "DSP1" string, the firmware.
static size_t make_code(uint8_t *code, size_t cap, size_t *firm_at, size_t *firm_len)
{
    static const char text[] = "HomeMenu::Applet::Launch(); sound effect table; ";
    size_t n = 0;
    while (n < 20000) code[n] = (uint8_t)text[n % (sizeof text - 1)], n++;
    memcpy(code + 9000, "DSP1 not a header", 17);  // a string that happens to match: skipped by the checks
    *firm_at = n + 3;                               // not word-aligned on purpose
    for (; n < *firm_at; n++) code[n] = 0;
    *firm_len = make_firm(code + n);
    n += *firm_len;
    for (; n < cap - 4096; n++) code[n] = (uint8_t)(n % 300 < 200 ? n / 64 : next());
    return n;
}

int main(void)
{
    enum { CAP = 48 * 1024 };
    uint8_t *code = malloc(CAP), *packed = malloc(CAP * 2), *plain = malloc(CAP), *copy = malloc(CAP * 2);
    size_t at, flen, n = make_code(code, CAP, &at, &flen);

    // Unpack what the packer made, byte for byte.
    size_t pn = pack(code, n, 0x200, packed);
    CHECK(pn < n);
    CHECK(dspfirm_unpack_size(packed, pn) == n);
    memset(plain, 0xEE, CAP);
    CHECK(dspfirm_unpack(packed, pn, plain, n));
    CHECK(!memcmp(plain, code, n));

    // Find it past the decoy, with its size.
    uint32_t size = 0;
    const uint8_t *firm = dspfirm_find(plain, n, &size, toy_hash);
    CHECK(firm == plain + at);
    CHECK(size == flen);

    // Nothing in the packed bytes (the segments don't survive packing as they are).
    CHECK(!dspfirm_find(packed, pn, &size, toy_hash));

    // One flipped byte in a segment, a damaged hash, a bad size or segment count: not taken.
    memcpy(copy, plain, n);
    copy[at + 0x300 + 5] ^= 1;
    CHECK(!dspfirm_find(copy, n, &size, toy_hash));
    memcpy(copy, plain, n);
    copy[at + 0x120 + 0x30 + 0x10] ^= 0x80;
    CHECK(!dspfirm_find(copy, n, &size, toy_hash));
    memcpy(copy, plain, n);
    put32(copy + at + 0x104, DSPFIRM_MAX + 1);
    CHECK(!dspfirm_find(copy, n, &size, toy_hash));
    memcpy(copy, plain, n);
    copy[at + 0x10E] = 11;
    CHECK(!dspfirm_find(copy, n, &size, toy_hash));
    memcpy(copy, plain, n);
    put32(copy + at + 0x120 + 8, flen);  // a segment running past the end
    CHECK(!dspfirm_find(copy, n, &size, toy_hash));
    memcpy(copy, plain, n);
    memset(copy + at + 0x120, 0, 0x30 * 10);  // no segments
    CHECK(!dspfirm_find(copy, n, &size, toy_hash));

    // Cut off: a buffer ending inside the firmware, or too short for a header.
    CHECK(!dspfirm_find(plain, at + flen - 1, &size, toy_hash));
    CHECK(dspfirm_find(plain, at + flen, &size, toy_hash) == plain + at);
    CHECK(!dspfirm_find(plain, 0x2FF, &size, toy_hash));

    // Footers that don't describe a packed file.
    uint8_t f[16] = {0};
    CHECK(dspfirm_unpack_size(f, 7) == 0);
    put32(f + 8, 8u << 24 | 16), put32(f + 12, 0);  // grows by 0: not packed
    CHECK(dspfirm_unpack_size(f, 16) == 0);
    put32(f + 8, 8u << 24 | 17), put32(f + 12, 100);  // region bigger than the file
    CHECK(dspfirm_unpack_size(f, 16) == 0);
    put32(f + 8, 4u << 24 | 16);  // header shorter than the footer
    CHECK(dspfirm_unpack_size(f, 16) == 0);
    CHECK(!dspfirm_unpack(packed, pn, plain, n + 1));  // the wrong output size

    // Damage the packed file at random: unpacking may fail or give garbage, but stays in bounds (run under ASan
    // to see it), and damaged segments are never accepted. The signature and the header fields outside the segment
    // hashes (load addresses, a bigger totalSize) aren't covered, as with Rosalina's and DSP1's dumps.
    int accepted_bad = 0;
    for (int t = 0; t < 3000; t++) {
        memcpy(copy, packed, pn);
        int hits = 1 + (int)(next() % 4);
        for (int h = 0; h < hits; h++) {
            size_t i = t % 5 == 0 ? pn - 1 - next() % 8 : next() % pn;  // the footer now and then
            copy[i] ^= (uint8_t)(1 + next() % 255);
        }
        size_t total = dspfirm_unpack_size(copy, pn);
        if (!total || total > CAP) continue;
        if (!dspfirm_unpack(copy, pn, plain, total)) continue;
        firm = dspfirm_find(plain, total, &size, toy_hash);
        if (firm && (size < flen || memcmp(firm + 0x300, code + at + 0x300, flen - 0x300))) accepted_bad++;
    }
    CHECK(accepted_bad == 0);

    free(code), free(packed), free(plain), free(copy);
    if (fails) return 1;
    printf("dspfirm: ok (%zu-byte .code packed to %zu, firmware %zu bytes)\n", n, pn, flen);
    return 0;
}
