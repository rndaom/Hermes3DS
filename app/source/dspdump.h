// Copies the DSP firmware out of this console's HOME Menu when sdmc:/3ds/dspfirm.cdc is missing: the same bytes
// Rosalina's "Dump DSP firmware" and DSP1 write, so the user never has to dump it by hand (dspfirm.h).
#pragma once

#include <stdint.h>

// The firmware (malloc'd, the caller frees it once ndsp is done with it), or NULL.
void *dspdump_extract(uint32_t *size);
// Write it to sdmc:/3ds/dspfirm.cdc for the next launch and for other homebrew (never over an existing dump).
void dspdump_save(const void *firm, uint32_t size);
