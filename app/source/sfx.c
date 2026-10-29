#include "sfx.h"

#include <3ds.h>
#include <math.h>
#include <string.h>

#include "audio.h"
#include "log.h"

#define CH 1
#define RATE 22050
#define MAX_NOTES 3

// A sound: up to three soft notes (sine with a little second harmonic, quick attack, exponential decay).
typedef struct {
    float freq[MAX_NOTES], to[MAX_NOTES];  // start and end frequency of each note (a glide when they differ)
    int notes;
    int ms;       // per note
    float level;  // 0..1
} Recipe;

static const Recipe RECIPES[SFX_COUNT] = {
    [SFX_REPLY] = {{1318.5f, 1760.0f}, {1318.5f, 1760.0f}, 2, 75, 0.30f},                     // E6 A6
    [SFX_ATTENTION] = {{880.0f, 1108.7f, 1318.5f}, {880.0f, 1108.7f, 1318.5f}, 3, 95, 0.42f},  // A5 C#6 E6
    [SFX_SENT] = {{620.0f}, {1240.0f}, 1, 70, 0.22f},                                         // a quick rise
    [SFX_REC_STOP] = {{990.0f, 660.0f}, {990.0f, 660.0f}, 2, 45, 0.25f},                      // down: done
    [SFX_ERROR] = {{330.0f, 247.0f}, {330.0f, 247.0f}, 2, 110, 0.30f},
};

static struct {
    bool tried, ok, muted;
    int16_t *pcm[SFX_COUNT];
    uint32_t n[SFX_COUNT];
    ndspWaveBuf wb;
} sx;

static void synth(Sfx s)
{
    const Recipe *r = &RECIPES[s];
    uint32_t per = (uint32_t)(RATE * r->ms / 1000), n = per * (uint32_t)r->notes;
    int16_t *p = linearAlloc(n * sizeof *p);
    if (!p) return;
    float phase = 0;
    for (int k = 0; k < r->notes; k++)
        for (uint32_t i = 0; i < per; i++) {
            float t = (float)i / (float)per;
            float f = r->freq[k] + (r->to[k] - r->freq[k]) * t;
            phase += 2.0f * (float)M_PI * f / RATE;
            float env = (i < 60 ? (float)i / 60.0f : 1.0f) * expf(-4.0f * t);
            float v = (sinf(phase) + 0.18f * sinf(2 * phase)) * env * r->level;
            p[k * per + i] = (int16_t)(v * 30000.0f);
        }
    DSP_FlushDataCache(p, n * sizeof *p);
    sx.pcm[s] = p;
    sx.n[s] = n;
}

static bool ready(void)
{
    if (sx.tried) return sx.ok;
    sx.tried = true;
    if (!audio_dsp_ready()) return false;  // no DSP firmware: no sounds, quietly
    for (int s = 0; s < SFX_COUNT; s++) synth((Sfx)s);
    ndspChnReset(CH);
    ndspChnSetInterp(CH, NDSP_INTERP_LINEAR);
    ndspChnSetRate(CH, RATE);
    ndspChnSetFormat(CH, NDSP_FORMAT_MONO_PCM16);
    float mix[12] = {1.0f, 1.0f};
    ndspChnSetMix(CH, mix);
    sx.ok = true;
    return true;
}

void sfx_play(Sfx s)
{
    if (sx.muted || (unsigned)s >= SFX_COUNT || !ready() || !sx.pcm[s]) return;
    ndspChnWaveBufClear(CH);  // a new sound cuts the one still playing
    memset(&sx.wb, 0, sizeof sx.wb);
    sx.wb.data_vaddr = sx.pcm[s];
    sx.wb.nsamples = sx.n[s];
    ndspChnWaveBufAdd(CH, &sx.wb);
}

void sfx_set_muted(bool muted)
{
    sx.muted = muted;
    if (muted && sx.ok) ndspChnWaveBufClear(CH);
}

void sfx_exit(void)
{
    if (sx.ok) ndspChnWaveBufClear(CH);
    for (int s = 0; s < SFX_COUNT; s++)
        if (sx.pcm[s]) linearFree(sx.pcm[s]);
    memset(&sx, 0, sizeof sx);
}
