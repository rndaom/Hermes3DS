// Take a photo for Hermes (vision): live preview from the outer (or inner) camera at 640x480, take, retake or
// send with an optional caption. Full screen over the chat while open; everything is freed when it closes.
#pragma once

#include <3ds.h>
#include <stdbool.h>

void camera_open(void);
bool camera_is_open(void);
void camera_close(void);
void camera_input(u32 down, u32 held, const touchPosition *touch);
void camera_tick(void);  // once a frame while open: the next preview frame, the JPEG encode
void camera_draw_top(void);
void camera_draw_bottom(void);
bool camera_animating(void);
