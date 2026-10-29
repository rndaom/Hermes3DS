// Short UI sounds (reply arrived, needs you, sent, recording stopped), synthesized at start-up and played on ndsp
// channel 1 (the player has channel 0). Silent when muted in settings or without the DSP firmware.
#pragma once

#include <stdbool.h>

typedef enum { SFX_REPLY, SFX_ATTENTION, SFX_SENT, SFX_REC_STOP, SFX_ERROR, SFX_COUNT } Sfx;

void sfx_play(Sfx s);
void sfx_set_muted(bool muted);
void sfx_exit(void);  // before audio_exit (which stops ndsp)
