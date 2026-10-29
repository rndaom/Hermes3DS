// Pictures from blobs (protocol.md §4.7): fetched, decoded (JPEG or PNG) and kept as GPU textures.
#pragma once

#include <citro2d.h>
#include <stdbool.h>

#include "blob.h"

typedef struct {
    C2D_Image img;
    int w, h;  // picture size in pixels
} Picture;

// The decoded picture for `r`, or NULL while it loads (state LOADING) or when it can't be shown (FAILED).
// A few pictures stay decoded; the least recently drawn are dropped first.
const Picture *image_get(const BlobRef *r, BlobState *state);
// Draw `p` scaled to fit inside the box (letterboxed, never upscaled past `max_scale`); returns the scale.
float image_draw_fit(const Picture *p, float x, float y, float w, float h, float max_scale);
// Draw `p` at `scale` with picture point (cx, cy) at the middle of the box, clipped to the box (zoom and pan).
void image_draw_at(const Picture *p, float x, float y, float w, float h, float scale, float cx, float cy);
// Draw `p` filling the box, cropped to the middle (thumbnails).
void image_draw_cover(const Picture *p, float x, float y, float w, float h);
void image_exit(void);
