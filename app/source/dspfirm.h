// Finding the DSP firmware in the HOME Menu's code, so sound works without the Rosalina "Dump DSP firmware" step
// (libctru's ndsp otherwise needs sdmc:/3ds/dspfirm.cdc). Plain C, so the host tests can run it; dspdump.c reads
// the console's own HOME Menu .code and writes the file. Nothing of Nintendo's is shipped with the app.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DSPFIRM_MAX 0x10000  // a firmware's totalSize is never more (Rosalina checks the same)

typedef void (*DspfirmHash)(const uint8_t *data, size_t n, uint8_t out[32]);  // SHA-256

// ExeFS .code compression (the backward LZSS the loader and ctrtool unpack): the size `n` compressed bytes unpack
// to, or 0 when the footer doesn't describe a compressed file.
size_t dspfirm_unpack_size(const uint8_t *in, size_t n);
// Unpack `in` (n bytes) into `out`, which holds `total` bytes (from dspfirm_unpack_size). False on a corrupt stream.
bool dspfirm_unpack(const uint8_t *in, size_t n, uint8_t *out, size_t total);
// The firmware inside `code`: its start (the 0x100-byte signature before "DSP1") and its size, or NULL. Each
// segment's SHA-256 must match its header, so a stray "DSP1" or a bad unpack is never taken.
const uint8_t *dspfirm_find(const uint8_t *code, size_t n, uint32_t *size, DspfirmHash sha256);
