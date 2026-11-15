// Send a file from the SD card to Hermes: browse folders, pick a file, add an optional note, send. Full screen
// over the chat while open (like the camera). The file goes up as an upload (protocol.md §5.1) and the PC hands
// it to Hermes as a document, or as a photo when it is a picture.
#pragma once

#include <3ds.h>
#include <stdbool.h>

void filepick_open(void);
bool filepick_is_open(void);
void filepick_close(void);
void filepick_set_supported(bool yes);  // the PC's plugin takes files, not only photos (welcome `upload.files`)
void filepick_input(u32 down, u32 held, const touchPosition *touch);
void filepick_tick(void);  // reads the chosen file a piece per pass, so the screen keeps drawing
void filepick_draw_top(void);
void filepick_draw_bottom(void);
bool filepick_animating(void);
