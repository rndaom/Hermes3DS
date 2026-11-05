// Take a photo for Hermes (vision): live preview from the outer (or inner) camera at 640x480, take, retake or
// send with an optional caption. Full screen over the chat while open; everything is freed when it closes.
#pragma once

#include <3ds.h>
#include <stdbool.h>

void camera_open(void);
bool camera_is_open(void);
void camera_close(void);  // at once: the camera turns off in the background
void camera_exit(void);   // at app exit: close, and wait (bounded) for the camera to be off
void camera_input(u32 down, u32 held, const touchPosition *touch);
void camera_tick(void);  // once a loop pass: the next preview frame, the finished JPEG, a closed camera's cleanup
void camera_draw_top(void);
void camera_draw_bottom(void);
bool camera_animating(void);
