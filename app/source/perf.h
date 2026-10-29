// Where the time goes on the real console: startup steps and a summary line in log.txt every 10 s while
// the app is drawing (frames, draw and GPU time, glyph/emoji baking, text layout, memory), plus any single
// slow frame. Cheap enough to leave on: a few tick reads per frame.
#pragma once

#include <3ds.h>
#include <stdbool.h>

typedef struct {
    u32 n;
    u64 ticks;
} PerfCount;

extern PerfCount perf_glyph, perf_emoji, perf_layout;  // bakes and layouts in this window
extern u32 perf_glyph_resets, perf_emoji_resets;      // atlas full -> everything re-baked

static inline u64 perf_now(void) { return svcGetSystemTick(); }
static inline void perf_add(PerfCount *c, u64 since)
{
    c->n++;
    c->ticks += svcGetSystemTick() - since;
}

void perf_start(void);              // startup clock
void perf_step(const char *name);  // startup: time since the previous step
void perf_startup_done(void);      // logs the startup line
// One main-loop pass: time spent on network events, ticking, and drawing (0 when nothing was drawn).
void perf_loop(u64 events, u64 ticks, u64 draw, bool drew, const char *where);
