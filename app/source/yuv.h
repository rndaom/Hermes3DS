// The camera's YUV 4:2:2 frames (packed Y0 U Y1 V, as libctru's INPUT_YUV422_BATCH) turned into what the camera
// screen needs: the halved RGB565 preview in GPU tiles, full-range BT.601 planes for the JPEG (what JFIF means),
// a thumbnail, and per-frame brightness and colour for the warm-up. Portable C: app/test/test_yuv.c runs it on
// the PC.
#pragma once

#include <stdbool.h>
#include <stdint.h>

// Which YUV the camera gives: the same values as libctru's Y2RU_StandardCoefficient, which
// CAMU_GetSuitableY2rStandardCoefficient reports. "TV" is the 16..235 (chroma 16..240) range, the others 0..255.
typedef enum { YUV_601 = 0, YUV_709 = 1, YUV_601_TV = 2, YUV_709_TV = 3 } YuvStd;

// One affine map from (Y, U - 128, V - 128) to three outputs, 16.16 fixed point, offsets include the rounding.
typedef struct {
    int32_t m[3][3];
    int32_t off[3];
} YuvMat;

typedef struct {
    YuvStd std;
    bool jfif;      // already full-range BT.601: the JPEG planes are a plain copy
    YuvMat to_rgb;  // camera YUV to R, G, B
    YuvMat to_jfif; // camera YUV to full-range BT.601 Y, Cb, Cr
} YuvConv;

// A frame's mean brightness (0..255) and colour (U and V centred on 0, over the mid-tones only: dark pixels'
// colour is mostly noise, bright ones are clipped), all in the camera's own YUV.
typedef struct {
    int y, u, v;
} YuvStats;

// Watches frames after the camera starts: its auto exposure and white balance take a moment to settle, and the
// first frames are badly lit and tinted.
typedef struct {
    int frames, calm;
    YuvStats last;
} YuvSettle;

#define YUV_SETTLE_MIN 5   // frames always waited for
#define YUV_SETTLE_CALM 3  // then this many steady frames in a row
#define YUV_SETTLE_MAX 30  // or this many in all (a moving scene is never steady): 2 s at 15 fps

void yuv_conv_init(YuvConv *c, YuvStd std);
const char *yuv_std_name(YuvStd std);

// Halve a w x h frame (2x2 mean) into an RGB565 texture tex_w pixels wide, in the GPU's 8x8 Morton-order tiles,
// row 0 at the top. w is a multiple of 4, h of 2. Fills *st when it isn't NULL.
void yuv_preview(const YuvConv *c, const uint8_t *yuyv, int w, int h, uint16_t *tex, int tex_w, YuvStats *st);

// Split a w x h frame into 4:2:2 planes for libjpeg-turbo: Y (w x h), Cb and Cr (w/2 x h), full-range BT.601.
void yuv_planes(const YuvConv *c, const uint8_t *yuyv, int w, int h, uint8_t *yp, uint8_t *up, uint8_t *vp);

// A tw x th RGB (3 bytes a pixel) thumbnail of those planes, each pixel the mean of its area.
void yuv_thumb(const uint8_t *yp, const uint8_t *up, const uint8_t *vp, int w, int h, uint8_t *rgb, int tw, int th);

// Feed one frame's stats; true once settled (YUV_SETTLE_*). Start from a zeroed YuvSettle.
bool yuv_settle(YuvSettle *s, const YuvStats *st);

// The texel index of (x, y) in a tex_w wide tiled texture.
static inline uint32_t yuv_texel(int x, int y, int tex_w)
{
    uint32_t m = (uint32_t)((x & 1) | ((y & 1) << 1) | ((x & 2) << 1) | ((y & 2) << 2) | ((x & 4) << 2) |
                            ((y & 4) << 3));
    return (uint32_t)((y >> 3) * (tex_w >> 3) + (x >> 3)) * 64 + m;
}
