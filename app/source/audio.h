// Audio attachments (protocol.md §4.7 `audio`): IMA-ADPCM blocks streamed from the SD cache (blob_stream)
// as they download, decoded a block at a time into a small ring of ndsp buffers (~1.5 s, 64 KB). One track
// at a time; it keeps playing when the viewer closes or the tab changes.
#pragma once

#include <jansson.h>
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    bool playing;    // started and not paused (sound comes out once enough has arrived)
    bool buffering;  // playing, but waiting for the download
    bool finished;   // played to the end (rewound to the start, paused) and not started again
    float pos, dur;  // seconds
    float loaded;    // 0..1 of the audio downloaded
} AudioStatus;

bool audio_playable(const json_t *a);  // an attachment with a usable `audio` blob
void audio_toggle(const json_t *a);    // play/pause `a`, switching to it from another track
void audio_prefetch(const json_t *a);  // start downloading `a`, which is queued to play next
void audio_forget(const json_t *a);    // stop a prefetch that won't be played (not the current track)
void audio_seek(float seconds);        // relative, on the current track
void audio_pause(void);                // pause the current track (the mic is about to listen)
bool audio_status(const json_t *a, AudioStatus *s);  // true when `a` is the current track
const char *audio_current(void);       // the current track's audio blob id, or NULL
bool audio_animating(void);            // playing: redraw for the progress
void audio_tick(void);                 // once a frame: refill the buffers
void audio_exit(void);
// ndsp started (on first use; false without sdmc:/3ds/dspfirm.cdc). Channel 0 is the player's; sfx.c uses 1.
bool audio_dsp_ready(void);

void audio_time_text(float seconds, char *out, size_t cap);  // "1:05", "1:02:09"
