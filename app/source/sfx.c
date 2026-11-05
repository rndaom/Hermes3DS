#include "sfx.h"

#include <3ds.h>
#include <string.h>

#include "audio.h"
#include "log.h"
#include "tones.h"

#define CH 1

static struct {
    bool tried, ok, muted;
    int16_t *pcm[SFX_COUNT];
    uint32_t n[SFX_COUNT];
    ndspWaveBuf wb;
} sx;

static void synth(Sfx s)
{
    uint32_t n = tones_length(s);
    int16_t *p = n ? linearAlloc(n * sizeof *p) : NULL;
    if (!p) return;
    if (!tones_render(s, p)) {
        linearFree(p);
        return;
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
    ndspChnReset(CH);
    ndspChnSetInterp(CH, NDSP_INTERP_LINEAR);
    ndspChnSetRate(CH, TONES_RATE);
    ndspChnSetFormat(CH, NDSP_FORMAT_MONO_PCM16);
    float mix[12] = {1.0f, 1.0f};
    ndspChnSetMix(CH, mix);
    sx.ok = true;
    return true;
}

void sfx_play(Sfx s)
{
    if (sx.muted || (unsigned)s >= SFX_COUNT || !ready()) return;
    if (!sx.pcm[s]) synth(s);  // on first use, one at a time: tens of ms each on an Old 3DS
    if (!sx.pcm[s]) return;
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
