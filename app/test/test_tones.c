// Host test for source/tones.c (the UI sounds). With a directory argument it also writes each sound there as a WAV,
// to listen to on the PC.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/tones.h"

static int fails;
#define CHECK(c)                                                       \
    do {                                                               \
        if (!(c)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);        \
            fails++;                                                   \
        }                                                              \
    } while (0)

static const char *const NAMES[SFX_COUNT] = {"reply", "attention", "sent", "rec_stop", "error"};

static void put32(FILE *f, uint32_t v)
{
    fputc(v & 255, f), fputc(v >> 8 & 255, f), fputc(v >> 16 & 255, f), fputc(v >> 24, f);
}

static void write_wav(const char *dir, const char *name, const int16_t *pcm, uint32_t n)
{
    char path[512];
    snprintf(path, sizeof path, "%s/%s.wav", dir, name);
    FILE *f = fopen(path, "wb");
    if (!f) {
        printf("FAIL can't write %s\n", path);
        fails++;
        return;
    }
    fwrite("RIFF", 1, 4, f), put32(f, 36 + n * 2), fwrite("WAVEfmt ", 1, 8, f), put32(f, 16);
    put32(f, 1 | 1 << 16), put32(f, TONES_RATE), put32(f, TONES_RATE * 2), put32(f, 2 | 16 << 16);
    fwrite("data", 1, 4, f), put32(f, n * 2);
    for (uint32_t i = 0; i < n; i++) fputc(pcm[i] & 255, f), fputc(pcm[i] >> 8 & 255, f);
    fclose(f);
}

int main(int argc, char **argv)
{
    for (int s = 0; s < SFX_COUNT; s++) {
        uint32_t n = tones_length((Sfx)s);
        CHECK(n > TONES_RATE / 10 && n < TONES_RATE);  // between 0.1 and 1 s
        int16_t *a = malloc(n * sizeof *a), *b = malloc(n * sizeof *b);
        CHECK(tones_render((Sfx)s, a) && tones_render((Sfx)s, b));
        CHECK(!memcmp(a, b, n * sizeof *a));  // deterministic
        int peak = 0;
        double energy = 0;
        for (uint32_t i = 0; i < n; i++) {
            int v = abs(a[i]);
            if (v > peak) peak = v;
            energy += (double)a[i] * a[i];
        }
        CHECK(a[0] == 0 && a[n - 1] == 0);    // no click at either end
        CHECK(peak > 6000 && peak < 26000);   // audible, with headroom
        CHECK(energy / n > 300.0 * 300.0);    // not just a click
        if (argc > 1) write_wav(argv[1], NAMES[s], a, n);
        free(a);
        free(b);
    }
    CHECK(tones_length(SFX_COUNT) == 0);
    if (fails) return 1;
    printf("tones: %d sounds ok\n", SFX_COUNT);
    return 0;
}
