// Motion and stereo depth, as plain numbers (no drawing): how far along an animation is, its easing, and
// how far each eye's picture is shifted for a layer. Portable C with a host test (test/test_motion.c).
#pragma once

#include <stdint.h>

// How long each motion takes, in ms.
#define MOTION_MSG_MS 180    // a new message sliding up and fading in
#define MOTION_CARD_MS 240   // the approval card coming in
#define MOTION_PRESS_MS 160  // a touched button shrinking and springing back

// Layer depths: the picture's shift between the two eyes, in pixels, with the 3D slider all the way up.
// Positive is behind the screen, negative in front of it, 0 on it.
#define DEPTH_PAGE 5.0f    // the transcript
#define DEPTH_FLOAT 2.0f   // bubbles, file cards and pictures, a little above the page
#define DEPTH_FRONT 0.0f   // the header, the status line, the footer; a question still waiting
#define DEPTH_CARD (-3.0f) // the approval card, in front of everything

// 0 at `start`, 1 once `dur_ms` have passed (and for start == 0, "no animation", or a clock that went back).
float motion_progress(uint64_t now, uint64_t start, unsigned dur_ms);

// Fast at first, settling at the end; t outside 0..1 is clamped.
float motion_ease_out(float t);

// The x shift of each eye's picture for a layer `depth` px deep with the slider at `slider` (0..1). Whole
// pixels, so text stays crisp; right - left is the rounded depth, and both are 0 with the slider down.
void motion_eye_offsets(float depth, float slider, int *left, int *right);
