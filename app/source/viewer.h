// Files in this chat and the picture viewer (design/mockups/screens/media.mjs): the picture or file on the
// top screen, the chat's files with Save to SD / Open on PC / Ask about it on the bottom.
#pragma once

#include <3ds.h>
#include <jansson.h>
#include <stdbool.h>

int files_count(void);       // attachments in the transcript
void viewer_open(int index);  // index into the chat's files (oldest first); -1 = the newest
bool viewer_is_open(void);
void viewer_close(void);
void viewer_input(u32 down, u32 held, const touchPosition *touch);
bool viewer_handle(const json_t *msg);  // replies to its requests (blob.open); true when consumed
bool viewer_animating(void);
void viewer_draw_top(void);
void viewer_draw_bottom(void);
