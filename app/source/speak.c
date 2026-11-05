#include "speak.h"

#include <3ds.h>
#include <stdio.h>
#include <string.h>

#include "app.h"
#include "audio.h"
#include "autoplay.h"
#include "blob.h"
#include "camera.h"
#include "voice.h"

#define PREFETCH 2  // waiting replies whose download starts early, so the next one begins without a gap
#define SPOKEN 8    // replies that came as `speak` events: their whole message in the chat isn't spoken again

static Autoplay ap;
static bool ready;
static char spoken[SPOKEN][24];
static int spoken_next;

static bool was_spoken(const char *id)
{
    for (int i = 0; i < SPOKEN; i++)
        if (id[0] && !strcmp(spoken[i], id)) return true;
    return false;
}

static void release(void *ref)
{
    audio_forget(ref);  // a download nobody will play stops (the current track is left alone)
    json_decref(ref);
}

static void init(void)
{
    if (ready) return;
    autoplay_init(&ap, release);
    ready = true;
}

static const char *blob_id(const json_t *a)
{
    const char *id = json_string_value(json_object_get(json_object_get(a, "audio"), "id"));
    return id ? id : "";
}

// How the player is doing with the reply started last (NULL: none).
static ApTrack track(void)
{
    const json_t *cur = autoplay_current(&ap);
    AudioStatus st;
    if (!cur) return AP_ENDED;
    if (!audio_status(cur, &st)) return audio_current() ? AP_REPLACED : AP_ENDED;
    if (st.playing) return AP_PLAYING;
    return st.finished ? AP_ENDED : AP_PAUSED;
}

// Not while the mic or the camera is in use, or when Spoken replies is off.
static bool wanted(void)
{
    return app.settings.speak_replies && !voice_active() && !camera_is_open();
}

static void start(const json_t *a)
{
    AudioStatus st;
    if (!audio_status(a, &st) || !st.playing) audio_toggle(a);
    const char *now = audio_current();
    if (!now || strcmp(now, blob_id(a))) autoplay_cancel(&ap);  // it didn't start (no DSP firmware): the rest wouldn't either
}

static void service(void)
{
    uint64_t now = osGetTime();
    void *next = autoplay_next(&ap, track(), now);
    if (next) start(next);
    for (int i = 0; i < PREFETCH; i++) {
        const json_t *w = autoplay_waiting(&ap, i);
        if (w) audio_prefetch(w);
    }
}

void speak_arrive(const json_t *msg)
{
    if (!wanted()) return;
    init();
    json_t *attach = json_object_get(msg, "attach");
    for (size_t i = 0; i < json_array_size(attach); i++) {
        json_t *a = json_array_get(attach, i);
        if (!json_is_true(json_object_get(a, "voice")) || !audio_playable(a)) continue;
        const char *sid = json_string_value(json_object_get(a, "speech"));
        if (sid && was_spoken(sid)) continue;  // the whole of a reply its `speak` parts already said
        autoplay_add(&ap, blob_id(a), json_incref(a), track(), osGetTime());
    }
    service();
}

void speak_part(const json_t *ev)
{
    // Remembered even when it isn't played (Spoken replies off, the mic or camera busy): a whole-reply voice
    // message with this id (servers before 0.1.0 posted one) is then left for the user to play, not spoken late.
    const char *sid = json_string_value(json_object_get(ev, "speech"));
    if (sid && sid[0] && !was_spoken(sid)) {
        snprintf(spoken[spoken_next], sizeof spoken[0], "%s", sid);
        spoken_next = (spoken_next + 1) % SPOKEN;
    }
    if (!wanted()) return;
    init();
    json_t *a = json_object_get(ev, "attach");
    if (!json_is_true(json_object_get(a, "voice")) || !audio_playable(a)) return;
    autoplay_add(&ap, blob_id(a), json_incref(a), track(), osGetTime());
    service();
}

const json_t *speak_current(void)
{
    if (!ready) return NULL;
    ApTrack t = track();
    return t == AP_PLAYING || t == AP_PAUSED ? autoplay_current(&ap) : NULL;
}

void speak_tick(void)
{
    if (!ready || !autoplay_busy(&ap)) return;
    if (!wanted()) {
        autoplay_cancel(&ap);
        return;
    }
    service();
}

void speak_exit(void)
{
    if (ready) autoplay_cancel(&ap);
}
