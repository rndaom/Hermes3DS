// Host test for adpcm.c: decode an IMA-ADPCM WAV made by ffmpeg and compare every sample with ffmpeg's
// own decoding of it (run_tests.sh makes both files).
//   test_adpcm <ima.wav> <reference.s16le>
// Encoder: encode a test signal (16360 Hz, like the 3DS mic), check it decodes close to the input, and write it
// as a WAV plus our own decoding, for run_tests.sh to compare with ffmpeg's decoding of that WAV.
//   test_adpcm --encode <out.wav> <ours.s16le>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/adpcm.h"

#ifndef M_PI  // strict C99 on glibc leaves it out (_USE_MATH_DEFINES only helps MSVC)
#define M_PI 3.14159265358979323846
#endif

static unsigned char *slurp(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    unsigned char *buf = malloc(n > 0 ? (size_t)n : 1);
    *len = buf ? fread(buf, 1, (size_t)n, f) : 0;
    fclose(f);
    return buf;
}

static unsigned u16(const unsigned char *p) { return p[0] | p[1] << 8; }
static unsigned long u32(const unsigned char *p) { return u16(p) | (unsigned long)u16(p + 2) << 16; }

static void put16(FILE *f, unsigned v) { fputc(v & 255, f), fputc(v >> 8 & 255, f); }
static void put32(FILE *f, unsigned long v) { put16(f, v & 0xFFFF), put16(f, v >> 16 & 0xFFFF); }

static int encode_test(const char *wav_path, const char *pcm_path)
{
    enum { RATE = 16360, N = RATE * 3, BLOCK = 1024 };
    static int16_t in[N], out[N + 4096];
    static uint8_t enc[N];
    unsigned seed = 1;
    for (int i = 0; i < N; i++) {  // a tone that swells, speech-like bursts, a silent stretch, some noise
        double t = (double)i / RATE, env = i > N / 2 && i < N * 2 / 3 ? 0 : 0.5 + 0.5 * sin(t * 3);
        seed = seed * 1103515245u + 12345u;
        double noise = ((double)(seed >> 16 & 0x7FFF) / 16384.0 - 1.0) * 1500;
        double v = env * (14000 * sin(2 * M_PI * 220 * t) + 6000 * sin(2 * M_PI * 1730 * t)) + noise;
        in[i] = (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
    }
    AdpcmEnc e;
    adpcm_enc_init(&e, enc, sizeof enc, BLOCK);
    // Fed in uneven pieces, as the mic ring delivers them.
    size_t done = 0, piece = 1;
    while (done < N) {
        size_t k = N - done < piece ? N - done : piece;
        if (adpcm_encode(&e, in + done, k) != k) return fprintf(stderr, "encoder: buffer full\n"), 1;
        done += k;
        piece = piece * 3 % 997 + 1;
    }
    size_t blocks = (e.len + BLOCK - 1) / BLOCK, n = 0;
    for (size_t off = 0; off < e.len; off += BLOCK)
        n += adpcm_decode_block(enc + off, e.len - off < BLOCK ? e.len - off : BLOCK, out + n);
    int bad = 0;
    if (e.samples != N || n < N || n > N + 1 || e.len != (N / adpcm_block_samples(BLOCK)) * BLOCK + 4 +
                                                      (N % adpcm_block_samples(BLOCK)) / 2) {  // + the short last block
        fprintf(stderr, "encoder: %llu samples in %zu bytes, decoded %zu\n", (unsigned long long)e.samples, e.len, n);
        bad++;
    }
    double sig = 0, err = 0;
    for (int i = 0; i < N; i++) sig += (double)in[i] * in[i], err += (double)(in[i] - out[i]) * (in[i] - out[i]);
    double snr = 10 * log10(sig / (err > 0 ? err : 1));
    for (size_t b = 0; b < blocks; b++)  // each block starts with the exact sample
        if (out[b * adpcm_block_samples(BLOCK)] != in[b * adpcm_block_samples(BLOCK)]) bad++;
    if (snr < 20) bad++;
    FILE *w = fopen(wav_path, "wb"), *p = fopen(pcm_path, "wb");
    if (!w || !p) return fprintf(stderr, "encoder: cannot write the output files\n"), 1;
    fputs("RIFF", w), put32(w, 4 + 28 + 12 + 8 + e.len), fputs("WAVE", w);
    fputs("fmt ", w), put32(w, 20), put16(w, 0x11), put16(w, 1), put32(w, RATE), put32(w, RATE / 2);
    put16(w, BLOCK), put16(w, 4), put16(w, 2), put16(w, (unsigned)adpcm_block_samples(BLOCK));
    fputs("fact", w), put32(w, 4), put32(w, N);
    fputs("data", w), put32(w, e.len), fwrite(enc, 1, e.len, w);
    for (int i = 0; i < N; i++) put16(p, (uint16_t)out[i]);
    fclose(w), fclose(p);
    printf("adpcm encoder: %zu blocks, SNR %.1f dB, %s\n", blocks, snr, bad ? "FAILED" : "ok");
    return bad ? 1 : 0;
}

int main(int argc, char **argv)
{
    if (argc == 4 && !strcmp(argv[1], "--encode")) return encode_test(argv[2], argv[3]);
    if (argc != 3) {
        fprintf(stderr, "usage: test_adpcm <ima.wav> <reference.s16le>\n");
        return 2;
    }
    size_t wlen, rlen;
    unsigned char *wav = slurp(argv[1], &wlen), *ref = slurp(argv[2], &rlen);
    if (!wav || !ref || wlen < 12 || memcmp(wav, "RIFF", 4) || memcmp(wav + 8, "WAVE", 4)) {
        fprintf(stderr, "test_adpcm: cannot read the input files\n");
        return 1;
    }
    size_t block = 0, data = 0, dlen = 0, samples = 0;
    for (size_t pos = 12; pos + 8 <= wlen;) {
        size_t size = u32(wav + pos + 4);
        if (!memcmp(wav + pos, "fmt ", 4) && u16(wav + pos + 8) == 0x11 && u16(wav + pos + 10) == 1)
            block = u16(wav + pos + 20);
        if (!memcmp(wav + pos, "fact", 4)) samples = u32(wav + pos + 8);
        if (!memcmp(wav + pos, "data", 4)) data = pos + 8, dlen = size;
        pos += 8 + size + (size & 1);
    }
    if (!block || !data || data + dlen > wlen) {
        fprintf(stderr, "test_adpcm: not a mono IMA-ADPCM WAV\n");
        return 1;
    }
    int16_t *out = malloc(adpcm_block_samples(block) * sizeof *out);
    size_t n = 0, bad = 0, blocks = 0;
    for (size_t off = 0; off < dlen; off += block, blocks++) {
        size_t len = dlen - off < block ? dlen - off : block;
        size_t got = adpcm_decode_block(wav + data + off, len, out);
        if (got != adpcm_block_samples(len)) bad++;
        for (size_t i = 0; i < got && (n + 1) * 2 <= rlen; i++, n++) {
            int16_t want = (int16_t)u16(ref + n * 2);
            if (out[i] != want && bad++ < 5)
                fprintf(stderr, "sample %zu: got %d, ffmpeg %d\n", n, out[i], want);
        }
    }
    // ffmpeg's decoding stops at the fact count; ours covers whole blocks.
    if (n * 2 != rlen || (samples && samples > n)) {
        fprintf(stderr, "test_adpcm: decoded %zu samples, ffmpeg %zu (fact %zu)\n", n, rlen / 2, samples);
        bad++;
    }
    if (adpcm_block_samples(3) != 0 || adpcm_block_samples(4) != 1 || adpcm_block_samples(1024) != 2041) bad++;
    printf("adpcm: %zu blocks, %zu samples, %s\n", blocks, n, bad ? "FAILED" : "ok");
    free(out);
    free(wav);
    free(ref);
    return bad ? 1 : 0;
}
