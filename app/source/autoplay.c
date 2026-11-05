#include "autoplay.h"

#include <string.h>

void autoplay_init(Autoplay *ap, void (*release)(void *ref))
{
    memset(ap, 0, sizeof *ap);
    ap->release = release;
}

static void drop_current(Autoplay *ap)
{
    if (!ap->has_cur) return;
    ap->has_cur = ap->paused = false;
    ap->release(ap->cur.ref);
    ap->cur.ref = NULL;
}

void autoplay_cancel(Autoplay *ap)
{
    drop_current(ap);
    for (int i = 0; i < ap->n; i++) ap->release(ap->wait[i].ref);
    ap->n = 0;
}

// What the player says about the current reply: forget it once it is over, give everything up when the user
// has taken the player or left it paused too long.
static void settle(Autoplay *ap, ApTrack cur, uint64_t now)
{
    if (!ap->has_cur) return;
    switch (cur) {
    case AP_PLAYING:
        ap->paused = false;
        break;
    case AP_PAUSED:
        if (!ap->paused) {
            ap->paused = true;
            ap->paused_since = now;
        } else if (now - ap->paused_since >= AUTOPLAY_HOLD_MS) {
            autoplay_cancel(ap);
        }
        break;
    case AP_REPLACED:
        autoplay_cancel(ap);
        break;
    case AP_ENDED:
        drop_current(ap);
        break;
    }
}

bool autoplay_add(Autoplay *ap, const char *id, void *ref, ApTrack cur, uint64_t now)
{
    bool seen = !id[0];  // no id: nothing to play
    for (int k = 0; k < AUTOPLAY_SEEN; k++) seen |= !strncmp(ap->seen[k], id, AUTOPLAY_ID - 1);
    if (seen) {
        ap->release(ref);
        return false;
    }
    strncpy(ap->seen[ap->seen_next], id, AUTOPLAY_ID - 1);
    ap->seen[ap->seen_next][AUTOPLAY_ID - 1] = 0;
    ap->seen_next = (ap->seen_next + 1) % AUTOPLAY_SEEN;
    settle(ap, cur, now);  // before the new one joins: a stale paused reply makes way for it
    if (ap->n == AUTOPLAY_WAITING) {
        ap->release(ref);
        return false;
    }
    ApItem *w = &ap->wait[ap->n++];
    strncpy(w->id, id, AUTOPLAY_ID - 1);
    w->id[AUTOPLAY_ID - 1] = 0;
    w->ref = ref;
    return true;
}

void *autoplay_next(Autoplay *ap, ApTrack cur, uint64_t now)
{
    settle(ap, cur, now);
    if (ap->has_cur || !ap->n) return NULL;
    ap->cur = ap->wait[0];
    ap->has_cur = true;
    ap->paused = false;
    memmove(ap->wait, ap->wait + 1, (size_t)(--ap->n) * sizeof ap->wait[0]);
    return ap->cur.ref;
}

void *autoplay_current(const Autoplay *ap)
{
    return ap->has_cur ? ap->cur.ref : NULL;
}

void *autoplay_waiting(const Autoplay *ap, int i)
{
    return i >= 0 && i < ap->n ? ap->wait[i].ref : NULL;
}

bool autoplay_busy(const Autoplay *ap)
{
    return ap->has_cur || ap->n > 0;
}
