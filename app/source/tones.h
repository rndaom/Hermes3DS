// The app's UI sounds, played as plucks of the lyre from the HOME jingle (tools/build_meta.py): low and dark, so
// the 3DS speaker doesn't turn them into a shrill ring. Plain C, so the host tests can render them; sfx.c renders
// each one once and plays it on the DSP.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "sfx.h"

#define TONES_RATE 22050

uint32_t tones_length(Sfx s);            // in samples
bool tones_render(Sfx s, int16_t *out);  // tones_length(s) mono samples at TONES_RATE; false when out of memory
