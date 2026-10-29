#include "adpcm.h"

static const int16_t STEPS[89] = {
    7,     8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,    25,    28,
    31,    34,    37,    41,    45,    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
    130,   143,   157,   173,   190,   209,   230,   253,   279,   307,   337,   371,   408,   449,   494,
    544,   598,   658,   724,   796,   876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
    2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,
    9493,  10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767};

static const int8_t INDEX[8] = {-1, -1, -1, -1, 2, 4, 6, 8};

size_t adpcm_block_samples(size_t len)
{
    return len < 4 ? 0 : (len - 4) * 2 + 1;
}

static inline int16_t step(unsigned nib, int *pred, int *idx)
{
    int s = STEPS[*idx], diff = s >> 3;
    if (nib & 1) diff += s >> 2;
    if (nib & 2) diff += s >> 1;
    if (nib & 4) diff += s;
    int p = (nib & 8) ? *pred - diff : *pred + diff;
    *pred = p < -32768 ? -32768 : p > 32767 ? 32767 : p;
    int i = *idx + INDEX[nib & 7];
    *idx = i < 0 ? 0 : i > 88 ? 88 : i;
    return (int16_t)*pred;
}

size_t adpcm_decode_block(const uint8_t *blk, size_t len, int16_t *out)
{
    if (len < 4) return 0;
    int pred = (int16_t)(blk[0] | blk[1] << 8), idx = blk[2] > 88 ? 88 : blk[2];
    size_t n = 0;
    out[n++] = (int16_t)pred;
    for (size_t i = 4; i < len; i++) {
        out[n++] = step(blk[i] & 15, &pred, &idx);
        out[n++] = step(blk[i] >> 4, &pred, &idx);
    }
    return n;
}

void adpcm_enc_init(AdpcmEnc *e, uint8_t *buf, size_t cap, size_t block)
{
    e->buf = buf;
    e->cap = cap;
    e->len = 0;
    e->block = block;
    e->fill = 0;
    e->pred = 0;
    e->idx = 0;
    e->samples = 0;
}

// The 4-bit code nearest to `s` from the predictor, the decoder's own arithmetic applied after it.
static unsigned quantize(int s, int *pred, int *idx)
{
    int st = STEPS[*idx], diff = s - *pred;
    unsigned nib = 0;
    if (diff < 0) nib = 8, diff = -diff;
    if (diff >= st) nib |= 4, diff -= st;
    if (diff >= st >> 1) nib |= 2, diff -= st >> 1;
    if (diff >= st >> 2) nib |= 1;
    step(nib, pred, idx);
    return nib;
}

size_t adpcm_encode(AdpcmEnc *e, const int16_t *in, size_t n)
{
    size_t spb = adpcm_block_samples(e->block);
    for (size_t i = 0; i < n; i++) {
        if (e->fill == 0) {  // a new block: header with the sample itself
            if (e->len + 4 > e->cap) return i;
            uint8_t *h = e->buf + e->len;
            e->pred = in[i];
            h[0] = (uint8_t)(in[i] & 0xFF);
            h[1] = (uint8_t)((uint16_t)in[i] >> 8);
            h[2] = (uint8_t)e->idx;
            h[3] = 0;
            e->len += 4;
        } else {
            unsigned nib = quantize(in[i], &e->pred, &e->idx);
            if (e->fill & 1) {  // odd sample: low nibble of a new byte
                if (e->len + 1 > e->cap) return i;
                e->buf[e->len++] = (uint8_t)nib;
            } else {
                e->buf[e->len - 1] |= (uint8_t)(nib << 4);
            }
        }
        e->samples++;
        if (++e->fill == spb) e->fill = 0;
    }
    return n;
}
