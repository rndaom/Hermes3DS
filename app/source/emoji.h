// Colour emoji: Twemoji sprites from romfs:/emoji.bin (tools/build_emoji.py), drawn inline with text.
// The plugin sends each emoji as one private-use code point EMOJI_BASE + index (protocol.md §6).
#pragma once

#include <stdbool.h>
#include <stdint.h>

void emoji_init(void);  // without a sprite set the app announces no cap and gets `:name:` text instead
void emoji_exit(void);
void emoji_frame_begin(void);

const char *emoji_cap(void);  // hello cap "emoji:<set id>", or NULL
bool emoji_is(uint32_t cp);   // a sprite code point of the loaded set
// Draw sprite `cp` as a px x px square with its top-left at whole pixels (x, y).
void emoji_draw(uint32_t cp, float x, float y, int px);
