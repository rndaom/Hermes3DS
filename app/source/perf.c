#include "perf.h"

#include <citro3d.h>
#include <malloc.h>
#include <stdio.h>
#include <string.h>

#include "log.h"

#define WINDOW_MS 10000
#define SLOW_MS 100.0f  // a pass this long is logged on its own (a visible hitch)

PerfCount perf_glyph, perf_emoji, perf_layout;
u32 perf_glyph_resets, perf_emoji_resets;

static float ms(u64 ticks) { return (float)ticks * 1000.0f / (float)SYSCLOCK_ARM11; }

// --- startup ----------------------------------------------------------------

static struct {
    u64 t0, last;
    char line[192];
    size_t len;
} st;

void perf_start(void) { st.t0 = st.last = perf_now(); }

void perf_step(const char *name)
{
    u64 now = perf_now();
    if (st.len < sizeof st.line)
        st.len += (size_t)snprintf(st.line + st.len, sizeof st.line - st.len, "%s%s %.0f", st.len ? ", " : "",
                                   name, ms(now - st.last));
    st.last = now;
}

void perf_startup_done(void)
{
    log_printf("startup ms: %s; total %.0f", st.line, ms(st.last - st.t0));
}

// --- running ----------------------------------------------------------------

static struct {
    u64 start_ms;
    u32 frames, passes;
    u64 draw_max, events_max, ticks_max;
    float cpu, cpu_max, gpu, gpu_max;
    const char *slowest_where;
} w;

static void window_reset(u64 now_ms)
{
    memset(&w, 0, sizeof w);
    w.start_ms = now_ms;
    memset(&perf_glyph, 0, sizeof perf_glyph);
    memset(&perf_emoji, 0, sizeof perf_emoji);
    memset(&perf_layout, 0, sizeof perf_layout);
    perf_glyph_resets = perf_emoji_resets = 0;
}

void perf_loop(u64 events, u64 ticks, u64 draw, bool drew, const char *where)
{
    u64 now = osGetTime();
    if (!w.start_ms) window_reset(now);
    w.passes++;
    if (events > w.events_max) w.events_max = events;
    if (ticks > w.ticks_max) w.ticks_max = ticks;
    if (drew) {
        w.frames++;
        if (draw > w.draw_max) w.draw_max = draw;  // wall time, includes waiting for the GPU and vblank
        float c = C3D_GetProcessingTime();         // this frame's CPU time building the scene (ms)
        w.cpu += c;
        if (c > w.cpu_max) {
            w.cpu_max = c;
            w.slowest_where = where;
        }
        float g = C3D_GetDrawingTime();  // the GPU time of the latest finished frame (ms)
        w.gpu += g;
        if (g > w.gpu_max) w.gpu_max = g;
    }
    float pass = ms(events + ticks + draw);
    if (pass >= SLOW_MS)
        log_printf("perf: slow pass %.0f ms on %s (events %.0f, ticks %.0f, draw %.0f)", pass, where, ms(events),
                   ms(ticks), ms(draw));
    if (now - w.start_ms < WINDOW_MS) return;
    if (w.frames) {
        struct mallinfo mi = mallinfo();
        float secs = (float)(now - w.start_ms) / 1000.0f;
        log_printf("perf %.0fs: %lu frames (%.1f fps), cpu avg %.1f max %.1f ms (%s), gpu avg %.1f max %.1f ms, "
                   "frame max %.1f, events max %.1f, ticks max %.1f ms; glyphs %lu (%.0f ms), emoji %lu (%.0f ms), "
                   "layout %lu (%.0f ms), resets %lu/%lu; heap %lu KB, linear free %lu KB",
                   secs, (unsigned long)w.frames, w.frames / secs, w.cpu / w.frames, w.cpu_max,
                   w.slowest_where ? w.slowest_where : "?", w.gpu / w.frames, w.gpu_max, ms(w.draw_max),
                   ms(w.events_max),
                   ms(w.ticks_max), (unsigned long)perf_glyph.n, ms(perf_glyph.ticks), (unsigned long)perf_emoji.n,
                   ms(perf_emoji.ticks), (unsigned long)perf_layout.n, ms(perf_layout.ticks),
                   (unsigned long)perf_glyph_resets, (unsigned long)perf_emoji_resets,
                   (unsigned long)(mi.uordblks >> 10), (unsigned long)(linearSpaceFree() >> 10));
    }
    window_reset(now);
}
