#include "tones.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define PI 3.14159265358979323846
#define MAX_NOTES 3

// One pluck: start (s), MIDI pitch, level, and how long it rings (1 = a freely ringing string).
typedef struct {
    float t0, midi, amp, ring;
} Note;

// A sound: a few plucks, its length (s) and its peak level (0..1).
typedef struct {
    Note note[MAX_NOTES];
    int notes;
    float len, peak;
} Recipe;

static const Recipe RECIPES[SFX_COUNT] = {
    [SFX_REPLY] = {{{0.000f, 69, 0.80f, 0.25f}, {0.090f, 74, 1.00f, 0.30f}}, 2, 0.50f, 0.60f},  // A4 D5
    [SFX_ATTENTION] = {{{0.000f, 69, 0.80f, 0.25f}, {0.110f, 74, 0.85f, 0.25f}, {0.220f, 78, 1.00f, 0.32f}},
                       3, 0.65f, 0.66f},                                                       // A4 D5 F#5
    [SFX_SENT] = {{{0.000f, 74, 1.00f, 0.15f}}, 1, 0.22f, 0.30f},                               // D5, short
    [SFX_REC_STOP] = {{{0.000f, 76, 0.90f, 0.12f}, {0.070f, 69, 1.00f, 0.15f}}, 2, 0.28f, 0.32f},  // E5 A4: done
    [SFX_ERROR] = {{{0.000f, 62, 1.00f, 0.15f}, {0.120f, 61, 1.00f, 0.18f}}, 2, 0.40f, 0.40f},  // D4 C#4: a drop
};

uint32_t tones_length(Sfx s)
{
    return (unsigned)s < SFX_COUNT ? (uint32_t)(RECIPES[s].len * TONES_RATE) : 0;
}

// A finger-plucked gut string as a sum of decaying partials, the same model as the jingle's: pluck-position comb,
// a soft fingertip (steep roll-off), higher partials dying faster, slight stiffness, and two slightly detuned
// "polarisations" on the low partials for a living two-stage decay. Each partial is a damped resonator.
static void pluck(float *x, uint32_t n, const Note *nt)
{
    static const double SHARE[2] = {0.62, 0.38}, TM[2] = {0.75, 1.35}, CENTS[2] = {-0.4, 0.4};
    double f = 440.0 * pow(2.0, (nt->midi - 69.0) / 12.0);
    double t1 = nt->ring * 2.3 * pow(440.0 / f, 0.6), pos = 0.16 + 0.012 * ((int)nt->midi % 6);
    for (int k = 1; k < 40; k++) {
        double fk = f * k * sqrt(1.0 + 1e-4 * k * k);
        if (fk > 6000.0) break;
        double a = nt->amp * sin(k * PI * pos) / k / (1.0 + (fk / 1500.0) * (fk / 1500.0));
        double t60 = t1 / (1.0 + 0.45 * (k - 1) + (fk / 2000.0) * (fk / 2000.0));
        int split = k <= 6;
        for (int p = 0; p < (split ? 2 : 1); p++) {
            double share = split ? SHARE[p] : 1.0, tm = split ? TM[p] : 1.0, cents = split ? CENTS[p] : 0.0;
            double r = pow(10.0, -3.0 / (t60 * tm * TONES_RATE));
            double w = 2.0 * PI * fk * pow(2.0, cents / 1200.0) / TONES_RATE;
            double c = 2.0 * r * cos(w), r2 = r * r, y1 = 0.0, y2 = -a * share * sin(w) / r;
            uint32_t end = (uint32_t)(1.4 * t60 * tm * TONES_RATE);
            if (end > n) end = n;
            for (uint32_t i = 1; i < end; i++) {
                double y = c * y1 - r2 * y2;
                x[i] += (float)y;
                y2 = y1;
                y1 = y;
            }
        }
    }
    uint32_t att = (uint32_t)(0.0015 * TONES_RATE);  // 1.5 ms raised-cosine attack
    for (uint32_t i = 0; i < att && i < n; i++) x[i] *= (float)(0.5 - 0.5 * cos(PI * i / att));
}

// An RBJ biquad, in place.
static void biquad(float *x, uint32_t n, char kind, double f, double q)
{
    double w = 2.0 * PI * f / TONES_RATE, cw = cos(w), al = sin(w) / (2.0 * q), a0 = 1.0 + al;
    double b0, b1, b2;
    if (kind == 'h') b0 = (1 + cw) / 2, b1 = -(1 + cw), b2 = (1 + cw) / 2;
    else if (kind == 'l') b0 = (1 - cw) / 2, b1 = 1 - cw, b2 = (1 - cw) / 2;
    else b0 = al, b1 = 0.0, b2 = -al;  // band-pass, 0 dB peak
    b0 /= a0, b1 /= a0, b2 /= a0;
    double a1 = -2.0 * cw / a0, a2 = (1.0 - al) / a0, x1 = 0, x2 = 0, y1 = 0, y2 = 0;
    for (uint32_t i = 0; i < n; i++) {
        double v = x[i], o = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
        x2 = x1, x1 = v, y2 = y1, y1 = o;
        x[i] = (float)o;
    }
}

bool tones_render(Sfx s, int16_t *out)
{
    uint32_t n = tones_length(s);
    if (!n) return false;
    const Recipe *r = &RECIPES[s];
    float *bus = calloc(n, sizeof *bus), *tmp = malloc(n * sizeof *tmp), *dry = malloc(n * sizeof *dry);
    if (!bus || !tmp || !dry) {
        free(bus);
        free(tmp);
        free(dry);
        return false;
    }
    for (int k = 0; k < r->notes; k++) {
        uint32_t s0 = (uint32_t)(r->note[k].t0 * TONES_RATE + 0.5f);
        if (s0 >= n) continue;
        memset(tmp, 0, (n - s0) * sizeof *tmp);
        pluck(tmp, n - s0, &r->note[k]);
        for (uint32_t i = s0; i < n; i++) bus[i] += tmp[i - s0];
    }
    // The wooden soundbox (two broad body resonances), then the jingle's 300 Hz high-pass and 2.4 kHz low-pass.
    static const double BOX[2][3] = {{330.0, 1.5, 0.30}, {700.0, 2.0, 0.22}};
    memcpy(dry, bus, n * sizeof *dry);
    for (int b = 0; b < 2; b++) {
        memcpy(tmp, dry, n * sizeof *tmp);
        biquad(tmp, n, 'b', BOX[b][0], BOX[b][1]);
        for (uint32_t i = 0; i < n; i++) bus[i] += (float)BOX[b][2] * tmp[i];
    }
    biquad(bus, n, 'h', 300.0, 0.5412);
    biquad(bus, n, 'h', 300.0, 1.3066);
    biquad(bus, n, 'l', 2400.0, 0.7071);
    // Fade the last third out to an exact zero, then set the peak.
    uint32_t fo = n / 3;
    for (uint32_t i = 0; i < fo; i++) bus[n - fo + i] *= (float)(0.5 + 0.5 * cos(PI * i / (fo - 1)));
    float peak = 0;
    for (uint32_t i = 0; i < n; i++) peak = fmaxf(peak, fabsf(bus[i]));
    float g = peak > 0 ? r->peak * 32767.0f / peak : 0;
    for (uint32_t i = 0; i < n; i++) out[i] = (int16_t)lrintf(bus[i] * g);
    free(bus);
    free(tmp);
    free(dry);
    return true;
}
