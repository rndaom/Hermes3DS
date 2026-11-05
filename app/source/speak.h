// Spoken replies (protocol.md §4.7 `voice`, `speak`): plays the audio attachments Hermes flags as speech, one
// after another. While Hermes writes, a reply comes as `speak` events, a few seconds each, and then lands in the
// chat as one voice message holding all of it, which isn't spoken again (an older plugin sends the parts as
// voice messages instead). Each part waits for the one before (autoplay.c has the queue rules). Guarded by the
// Spoken replies toggle, the mic and the camera.
#pragma once

#include <jansson.h>

void speak_arrive(const json_t *msg);  // a chat `msg` came in: queue and start its spoken parts
void speak_part(const json_t *ev);     // a `speak` event came in: queue and start that part
const json_t *speak_current(void);     // the spoken part the player is on (playing or paused), or NULL
void speak_tick(void);                 // once a frame, after audio_tick: start the next, fetch ahead
void speak_exit(void);
