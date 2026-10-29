// Hold-to-talk voice messages (design/mockups/screens/voice.mjs): the 3DS microphone at 16360 Hz, encoded to
// IMA-ADPCM while it records, sent as a voice message (protocol.md §4.4.1) that Hermes transcribes.
#pragma once

#include <3ds.h>
#include <stdbool.h>

// Y went down (or a finger on the Talk tile): the mic starts at once, so the first word isn't lost. Until the
// hold passes ~0.3 s it may still turn out to be a tap. False when voice messages can't be recorded now.
// `quiet`: a tap of Y does something else too, so no toast when voice messages are unavailable.
bool voice_press(bool touch, bool quiet);
// The button or finger came up. True when it was only a tap (nothing recorded; Y's tap action applies).
bool voice_release(void);
bool voice_active(void);     // pressed (the mic is running)
bool voice_recording(void);  // past the tap threshold: the recording screens are up
// While recording: B cancels, Y up / finger up sends. Call instead of the chat's input handling.
void voice_input(u32 down, u32 held, u32 up);
void voice_tick(void);       // once a frame: drain the mic, the time limit
void voice_draw_top(void);
void voice_draw_bottom(void);
void voice_exit(void);
