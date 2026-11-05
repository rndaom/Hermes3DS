#include "audio.h"

#include <3ds.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adpcm.h"
#include "app.h"
#include "blob.h"
#include "dspdump.h"
#include "log.h"

#define CH 0
#define NBUF 8              // ndsp buffers in the ring...
#define BUF_SAMPLES 4096    // ...of this many samples each: ~1.5 s at 22050 Hz, so slow frames don't gap it
#define MAX_BLOCK 4096      // biggest IMA-ADPCM block accepted (the PC sends 1024)
#define MAX_SPB ((MAX_BLOCK - 4) * 2 + 1)
#define DSP_FIRM_MISSING 0xD880A7FA  // ndspInit: no sdmc:/3ds/dspfirm.cdc

static struct {
    bool ndsp_tried, ndsp_ok;
    int16_t *mem;  // NBUF * BUF_SAMPLES samples in linear memory, for the DSP
    void *firm;    // the DSP firmware copied from the HOME Menu (dspdump.c), handed to ndsp until ndspExit
    ndspWaveBuf wb[NBUF];
    uint32_t wb_start[NBUF], wb_seq[NBUF], seq;

    bool loaded;  // a current track
    BlobRef ref;
    uint32_t rate, block, spb, total;  // total samples, padding of the last block left out
    bool playing, finished;
    uint32_t cur;       // next sample to decode into a buffer
    uint32_t have;       // bytes of the blob downloaded so far
    uint32_t blk;        // block decoded into pcm[] (UINT32_MAX: none)
    size_t blk_n;
    int16_t pcm[MAX_SPB];
    uint8_t raw[MAX_BLOCK];
} au;

static const char *js(const json_t *o, const char *k)
{
    return json_string_value(json_object_get(o, k)) ?: "";
}

static uint32_t ju(const json_t *o, const char *k)
{
    json_int_t v = json_integer_value(json_object_get(o, k));
    return v > 0 && v <= 0x7FFFFFFF ? (uint32_t)v : 0;
}

bool audio_playable(const json_t *a)
{
    BlobRef r;
    uint32_t rate = ju(a, "rate"), block = ju(a, "block");
    return !strcmp(js(a, "kind"), "audio") && blob_ref_parse(json_object_get(a, "audio"), &r) && rate >= 4000 &&
           rate <= 48000 && block >= 8 && block <= MAX_BLOCK;
}

// Free the copied firmware and point ndsp back at the SD file, so it never keeps the freed pointer.
static void drop_firm(void)
{
    if (!au.firm) return;
    ndspUseComponent(NULL, 0, 0xFF, 0xFF);
    free(au.firm);
    au.firm = NULL;
}

bool audio_dsp_ready(void)
{
    if (au.ndsp_tried) return au.ndsp_ok;
    au.ndsp_tried = true;
    au.mem = linearAlloc(NBUF * BUF_SAMPLES * sizeof(int16_t));
    if (!au.mem) {
        log_printf("audio: no linear memory");
        return false;
    }
    Result rc = ndspInit();
    if ((uint32_t)rc == DSP_FIRM_MISSING) {  // never dumped: copy it from the HOME Menu, then try again
        uint32_t n;
        if ((au.firm = dspdump_extract(&n))) {
            ndspUseComponent(au.firm, n, 0xFF, 0xFF);
            rc = ndspInit();
            if (R_SUCCEEDED(rc)) {
                dspdump_save(au.firm, n);  // only once the DSP took it, so a bad copy never sticks on the SD card
                if (osGetTime() >= app.toast_until)  // the first sound often follows a toast: keep that one
                    app_toast("Sound is set up: DSP firmware copied from the HOME Menu");
            }
        }
    }
    if (R_FAILED(rc)) {
        log_printf("audio: ndspInit failed %08lX", (unsigned long)rc);
        drop_firm();
        linearFree(au.mem);
        au.mem = NULL;
        return false;
    }
    ndspSetOutputMode(NDSP_OUTPUT_STEREO);
    for (int i = 0; i < NBUF; i++) au.wb[i].data_vaddr = au.mem + i * BUF_SAMPLES;
    au.ndsp_ok = true;
    return true;
}

static bool active(int i)
{
    return au.wb[i].status == NDSP_WBUF_QUEUED || au.wb[i].status == NDSP_WBUF_PLAYING;
}

static bool any_active(void)
{
    for (int i = 0; i < NBUF; i++)
        if (active(i)) return true;
    return false;
}

// Empty the ring (seek, stop, switch). The decode cursor stays where it is.
static void flush_ring(void)
{
    if (!au.ndsp_ok) return;
    ndspChnWaveBufClear(CH);
    for (int i = 0; i < NBUF; i++) au.wb[i].status = NDSP_WBUF_FREE;
}

// The sample being heard: the playing buffer's start + the DSP's position in it.
static uint32_t position(void)
{
    int oldest = -1;
    for (int i = 0; i < NBUF; i++) {
        if (au.wb[i].status == NDSP_WBUF_PLAYING) {
            uint32_t p = au.wb_start[i] + ndspChnGetSamplePos(CH);
            return p < au.total ? p : au.total;
        }
        if (au.wb[i].status == NDSP_WBUF_QUEUED && (oldest < 0 || au.wb_seq[i] < au.wb_seq[oldest])) oldest = i;
    }
    return oldest >= 0 ? au.wb_start[oldest] : au.cur;
}

static void unload(void)
{
    if (!au.loaded) return;
    flush_ring();
    blob_stream_release(au.ref.id);
    au.loaded = au.playing = false;
    app.dirty = true;
}

static void fail(const char *why)
{
    log_printf("audio %s: %s", au.ref.id, why);
    unload();
    app_toast("Couldn't play this audio");
}

// Decode the next buffer's worth into ring buffer i. False when there is nothing more to decode yet (the
// download hasn't reached it) or at all.
static bool fill(int i, bool complete)
{
    if (au.cur >= au.total) return false;
    uint32_t end = au.cur + BUF_SAMPLES < au.total ? au.cur + BUF_SAMPLES : au.total;
    uint32_t need = ((end - 1) / au.spb + 1) * au.block;
    if (need > au.ref.size) need = au.ref.size;
    if (!complete && au.have < need) return false;
    int16_t *dst = (int16_t *)au.wb[i].data_vaddr;
    uint32_t start = au.cur, n = 0;
    while (au.cur < end) {
        uint32_t b = au.cur / au.spb;
        if (b != au.blk) {
            uint32_t off = b * au.block;
            size_t len = au.ref.size - off < au.block ? au.ref.size - off : au.block;
            if (blob_stream_read(au.ref.id, off, au.raw, len) != len) {
                fail("SD read failed");
                return false;
            }
            au.blk_n = adpcm_decode_block(au.raw, len, au.pcm);
            au.blk = b;
        }
        uint32_t k = au.cur - b * au.spb;
        if (k >= au.blk_n) {  // a short last block: the end came early
            au.total = au.cur;
            break;
        }
        uint32_t take = au.blk_n - k < end - au.cur ? (uint32_t)au.blk_n - k : end - au.cur;
        memcpy(dst + n, au.pcm + k, take * sizeof(int16_t));
        n += take;
        au.cur += take;
    }
    if (!n) return false;
    au.wb[i].nsamples = n;
    au.wb[i].looping = false;
    au.wb_start[i] = start;
    au.wb_seq[i] = ++au.seq;
    DSP_FlushDataCache(dst, n * sizeof(int16_t));
    ndspChnWaveBufAdd(CH, &au.wb[i]);
    return true;
}

static bool load(const json_t *a)
{
    unload();
    memset(&au.ref, 0, sizeof au.ref);
    blob_ref_parse(json_object_get(a, "audio"), &au.ref);
    au.rate = ju(a, "rate");
    au.block = ju(a, "block");
    au.spb = (uint32_t)adpcm_block_samples(au.block);
    uint32_t blocks = au.ref.size / au.block, tail = au.ref.size % au.block;
    uint64_t total = (uint64_t)blocks * au.spb + adpcm_block_samples(tail);
    uint64_t by_ms = ((uint64_t)ju(a, "ms") * au.rate + 999) / 1000;  // the PC's length leaves out padding
    au.total = (uint32_t)(by_ms && by_ms < total ? by_ms : total);
    au.cur = au.have = 0;
    au.blk = UINT32_MAX;
    au.loaded = true;
    au.finished = false;
    au.playing = false;
    ndspChnReset(CH);
    ndspChnSetInterp(CH, NDSP_INTERP_LINEAR);
    ndspChnSetRate(CH, (float)au.rate);
    ndspChnSetFormat(CH, NDSP_FORMAT_MONO_PCM16);
    float mix[12] = {1.0f, 1.0f};
    ndspChnSetMix(CH, mix);
    return true;
}

static bool is_current(const json_t *a)
{
    BlobRef r;
    return au.loaded && blob_ref_parse(json_object_get(a, "audio"), &r) && !strcmp(r.id, au.ref.id);
}

void audio_toggle(const json_t *a)
{
    if (!audio_playable(a)) return;
    if (!audio_dsp_ready()) {
        app_toast("Audio needs the DSP firmware: dump it in Rosalina");
        return;
    }
    if (!is_current(a)) load(a);
    au.playing = !au.playing;
    au.finished = false;
    ndspChnSetPaused(CH, !au.playing);
    log_printf("audio %s: %s at %lu/%lu", au.ref.id, au.playing ? "play" : "pause", (unsigned long)position(),
               (unsigned long)au.total);
    app.dirty = true;
    audio_tick();  // start filling now, not a frame later
}

void audio_prefetch(const json_t *a)
{
    BlobRef r;
    if (!audio_playable(a) || !blob_ref_parse(json_object_get(a, "audio"), &r) || is_current(a)) return;
    blob_stream(&r, NULL);  // a running or finished stream just stays fresh
}

void audio_forget(const json_t *a)
{
    BlobRef r;
    if (!blob_ref_parse(json_object_get(a, "audio"), &r) || is_current(a)) return;
    blob_stream_release(r.id);
}

void audio_seek(float seconds)
{
    if (!au.loaded) return;
    int64_t to = (int64_t)position() + (int64_t)(seconds * (float)au.rate);
    if (to < 0) to = 0;
    if (to >= au.total) to = au.total ? au.total - 1 : 0;
    log_printf("audio %s: seek %+.0f s, %lu -> %lld", au.ref.id, (double)seconds, (unsigned long)position(),
               (long long)to);
    flush_ring();
    au.cur = (uint32_t)to;
    au.finished = false;
    app.dirty = true;
    audio_tick();
}

bool audio_status(const json_t *a, AudioStatus *s)
{
    memset(s, 0, sizeof *s);
    s->dur = (float)ju(a, "ms") / 1000.0f;
    if (!is_current(a)) return false;
    s->playing = au.playing;
    s->buffering = au.playing && !any_active() && au.cur < au.total;
    s->finished = au.finished;
    s->pos = (float)position() / (float)au.rate;
    s->dur = (float)au.total / (float)au.rate;
    s->loaded = au.ref.size ? (float)au.have / (float)au.ref.size : 1;
    return true;
}

const char *audio_current(void)
{
    return au.loaded ? au.ref.id : NULL;
}

void audio_pause(void)
{
    if (!au.loaded || !au.playing) return;
    au.playing = false;
    ndspChnSetPaused(CH, true);
    log_printf("audio %s: pause at %lu/%lu", au.ref.id, (unsigned long)position(), (unsigned long)au.total);
    app.dirty = true;
}

bool audio_animating(void)
{
    return au.loaded && au.playing;
}

void audio_tick(void)
{
    if (!au.loaded) return;
    BlobState st = blob_stream(&au.ref, &au.have);  // also keeps the stream from being evicted
    if (st == BLOB_FAILED) {
        fail("download failed");
        return;
    }
    if (!au.playing) return;
    for (int i = 0; i < NBUF; i++)
        if (!active(i) && !fill(i, st == BLOB_READY)) break;
    if (!au.loaded) return;  // fill() failed
    if (au.cur >= au.total && !any_active()) {  // played to the end: back to the start, paused
        log_printf("audio %s: ended", au.ref.id);
        au.playing = false;
        au.finished = true;
        au.cur = 0;
        ndspChnSetPaused(CH, true);
        app.dirty = true;
    }
}

void audio_exit(void)
{
    unload();
    if (au.ndsp_ok) ndspExit();
    drop_firm();  // after ndspExit: ndsp doesn't free a component it was handed
    if (au.mem) linearFree(au.mem);
    memset(&au, 0, sizeof au);
}

void audio_time_text(float seconds, char *out, size_t cap)
{
    unsigned t = seconds > 0 ? (unsigned)seconds : 0;  // whole seconds gone by, like a player clock
    if (t >= 3600) snprintf(out, cap, "%u:%02u:%02u", t / 3600, t / 60 % 60, t % 60);
    else snprintf(out, cap, "%u:%02u", t / 60, t % 60);
}
