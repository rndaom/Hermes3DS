// IMA-ADPCM block decoding (protocol.md §4.7 `audio`): Microsoft WAV layout, mono. Each block is
// self-contained: int16 LE first sample, u8 step index, u8 reserved, then 4-bit codes, low nibble first.
// Portable C (host-tested in app/test).
#pragma once

#include <stddef.h>
#include <stdint.h>

// Samples a block of `len` bytes decodes to (0 when it is too short to have a header).
size_t adpcm_block_samples(size_t len);

// Decode one block into `out` (room for adpcm_block_samples(len) samples). Returns the samples written.
size_t adpcm_decode_block(const uint8_t *blk, size_t len, int16_t *out);

// Streaming encoder into the same layout (3DS voice messages, protocol.md §4.4.1). Blocks of `block` bytes are
// appended to buf[0..len); the step index carries over between blocks, the first sample of each is stored exactly.
typedef struct {
    uint8_t *buf;
    size_t len, cap;  // bytes written / room in buf (the caller's); the encoder stops when it is full
    size_t block;     // bytes per block (1024)
    size_t fill;      // samples in the block being written
    int pred, idx;
    uint64_t samples; // encoded so far
} AdpcmEnc;

void adpcm_enc_init(AdpcmEnc *e, uint8_t *buf, size_t cap, size_t block);
// Encode n samples; returns how many fit (fewer only when buf is full).
size_t adpcm_encode(AdpcmEnc *e, const int16_t *in, size_t n);
