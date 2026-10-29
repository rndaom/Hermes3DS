#include "image.h"

#include <3ds.h>
#include <png.h>
#include <stdlib.h>
#include <string.h>
#include <turbojpeg.h>

#include "log.h"

#define SLOTS 16
#define TEX_MAX 1024
#define TEX_BUDGET (10u << 20)  // bytes of decoded textures kept (linear memory)
#define DECODE_MAX 4096         // longest side decoded; RGBA sizes of bigger headers can overflow a 32-bit size_t

typedef struct {
    char id[33];
    bool used, failed;
    Picture pic;
    C3D_Tex tex;
    Tex3DS_SubTexture sub;
    u64 drawn;
} Slot;

static Slot slots[SLOTS];

static inline uint32_t morton8(int x, int y)
{
    return (uint32_t)((x & 1) | ((y & 1) << 1) | ((x & 2) << 1) | ((y & 2) << 2) | ((x & 4) << 2) | ((y & 4) << 3));
}

// Texel index in a tiled (8x8 Morton) texture `w` texels wide; row 0 is the top of the image.
static inline uint32_t tex_index(int x, int y, int w)
{
    return (uint32_t)(((y >> 3) * (w >> 3) + (x >> 3)) * 64) + morton8(x & 7, y & 7);
}

static int pow2(int n)
{
    int p = 8;
    while (p < n) p <<= 1;
    return p;
}

// A header's size is from the PC (or a file): anything bigger than DECODE_MAX a side is not decoded.
static bool size_ok(uint64_t w, uint64_t h)
{
    if (w == 0 || h == 0 || w > DECODE_MAX || h > DECODE_MAX || w * h > (uint64_t)DECODE_MAX * DECODE_MAX) {
        log_printf("image: %llux%llu is too large to decode", (unsigned long long)w, (unsigned long long)h);
        return false;
    }
    return true;
}

// RGBA pixels (malloc'd) from JPEG or PNG bytes; NULL when it isn't a picture we can read.
static uint8_t *decode(const uint8_t *d, size_t n, int *w, int *h)
{
    if (n > 3 && d[0] == 0xFF && d[1] == 0xD8) {
        tjhandle tj = tjInitDecompress();
        if (!tj) return NULL;
        int sub, cs;
        uint8_t *out = NULL;
        if (tjDecompressHeader3(tj, d, (unsigned long)n, w, h, &sub, &cs) == 0 && *w > 0 && *h > 0 &&
            size_ok((uint64_t)*w, (uint64_t)*h) && (out = malloc((size_t)*w * (size_t)*h * 4)) &&
            tjDecompress2(tj, d, (unsigned long)n, out, *w, 0, *h, TJPF_RGBA, 0) != 0) {
            log_printf("jpeg: %s", tjGetErrorStr2(tj));
            free(out);
            out = NULL;
        }
        tjDestroy(tj);
        return out;
    }
    if (n > 8 && !memcmp(d, "\x89PNG", 4)) {
        png_image img;
        memset(&img, 0, sizeof img);
        img.version = PNG_IMAGE_VERSION;
        if (!png_image_begin_read_from_memory(&img, d, n)) return NULL;
        if (!size_ok(img.width, img.height)) {  // PNG_IMAGE_SIZE is 32-bit arithmetic too
            png_image_free(&img);
            return NULL;
        }
        img.format = PNG_FORMAT_RGBA;
        uint8_t *out = malloc(PNG_IMAGE_SIZE(img));
        if (!out || !png_image_finish_read(&img, NULL, out, 0, NULL)) {
            log_printf("png: %s", img.message);
            png_image_free(&img);
            free(out);
            return NULL;
        }
        *w = (int)img.width;
        *h = (int)img.height;
        return out;
    }
    return NULL;
}

static uint32_t slot_bytes(const Slot *s)
{
    return s->used && !s->failed ? s->tex.size : 0;
}

static void slot_free(Slot *s)
{
    if (s->used && !s->failed) C3D_TexDelete(&s->tex);
    memset(s, 0, sizeof *s);
}

// Make room for `need` more texture bytes and return a free slot.
static Slot *make_room(uint32_t need)
{
    for (;;) {
        uint32_t total = 0;
        Slot *lru = NULL, *empty = NULL;
        for (int i = 0; i < SLOTS; i++) {
            Slot *s = &slots[i];
            total += slot_bytes(s);
            if (!s->used) {
                if (!empty) empty = s;
            } else if (!lru || s->drawn < lru->drawn) {
                lru = s;
            }
        }
        if (empty && total + need <= TEX_BUDGET) return empty;
        if (!lru) return empty;
        slot_free(lru);
    }
}

static bool upload(Slot *s, const uint8_t *rgba, int w, int h)
{
    // Pictures larger than a texture can hold are shrunk (nearest pixel; the PC normally sends them fitted).
    int step_num = 1, step_den = 1;
    if (w > TEX_MAX || h > TEX_MAX) {
        step_num = w > h ? w : h;
        step_den = TEX_MAX;
    }
    int dw = (int)((long long)w * step_den / step_num), dh = (int)((long long)h * step_den / step_num);
    if (dw < 1) dw = 1;
    if (dh < 1) dh = 1;
    int tw = pow2(dw), th = pow2(dh);
    if (!C3D_TexInit(&s->tex, (u16)tw, (u16)th, GPU_RGBA8)) return false;
    C3D_TexSetFilter(&s->tex, GPU_LINEAR, GPU_LINEAR);
    C3D_TexSetWrap(&s->tex, GPU_CLAMP_TO_EDGE, GPU_CLAMP_TO_EDGE);
    uint32_t *dst = s->tex.data;
    memset(dst, 0, s->tex.size);
    for (int y = 0; y < dh; y++) {
        const uint8_t *row = rgba + (size_t)((long long)y * step_num / step_den) * (size_t)w * 4;
        for (int x = 0; x < dw; x++) {
            const uint8_t *p = row + (size_t)((long long)x * step_num / step_den) * 4;
            dst[tex_index(x, y, tw)] = (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
        }
    }
    C3D_TexFlush(&s->tex);
    s->sub = (Tex3DS_SubTexture){
        .width = (u16)dw,
        .height = (u16)dh,
        .left = 0,
        .top = 1.0f,
        .right = dw / (float)tw,
        .bottom = 1.0f - dh / (float)th,
    };
    s->pic.img = (C2D_Image){&s->tex, &s->sub};
    s->pic.w = dw;
    s->pic.h = dh;
    return true;
}

const Picture *image_get(const BlobRef *r, BlobState *state)
{
    for (int i = 0; i < SLOTS; i++) {
        Slot *s = &slots[i];
        if (!s->used || strcmp(s->id, r->id)) continue;
        s->drawn = osGetTime();
        *state = s->failed ? BLOB_FAILED : BLOB_READY;
        return s->failed ? NULL : &s->pic;
    }
    const uint8_t *data;
    size_t len;
    *state = blob_get(r, &data, &len);
    if (*state != BLOB_READY) return NULL;
    int w = 0, h = 0;
    uint8_t *rgba = decode(data, len, &w, &h);
    blob_release(r->id);
    uint32_t need = rgba ? (uint32_t)pow2(w < TEX_MAX ? w : TEX_MAX) * (uint32_t)pow2(h < TEX_MAX ? h : TEX_MAX) * 4 : 0;
    Slot *s = make_room(need);
    if (!s) {
        free(rgba);
        *state = BLOB_LOADING;  // every slot holds a picture drawn this frame; try again later
        return NULL;
    }
    snprintf(s->id, sizeof s->id, "%s", r->id);
    s->used = true;
    s->drawn = osGetTime();
    if (!rgba || !upload(s, rgba, w, h)) {
        if (rgba) log_printf("image %s: no memory for a %dx%d texture", r->id, w, h);
        else log_printf("image %s: not a JPEG or PNG it can read", r->id);
        s->failed = true;
        free(rgba);
        *state = BLOB_FAILED;
        return NULL;
    }
    free(rgba);
    return &s->pic;
}

// Draw the part of `p` around picture point (cx, cy) at `scale` into the box, clipped to it.
static void draw_region(const Picture *p, float bx, float by, float bw, float bh, float scale, float cx, float cy)
{
    float u0 = cx - bw / 2 / scale, v0 = cy - bh / 2 / scale, u1 = cx + bw / 2 / scale, v1 = cy + bh / 2 / scale;
    if (u0 < 0) u0 = 0;
    if (v0 < 0) v0 = 0;
    if (u1 > p->w) u1 = (float)p->w;
    if (v1 > p->h) v1 = (float)p->h;
    if (u1 <= u0 || v1 <= v0) return;
    const Tex3DS_SubTexture *full = p->img.subtex;
    float tw = p->w / (full->right - full->left), th = p->h / (full->top - full->bottom);
    Tex3DS_SubTexture sub = {
        .width = (u16)(u1 - u0 + 0.5f),
        .height = (u16)(v1 - v0 + 0.5f),
        .left = u0 / tw,
        .top = 1.0f - v0 / th,
        .right = u1 / tw,
        .bottom = 1.0f - v1 / th,
    };
    C2D_Image img = {p->img.tex, &sub};
    float sx = (u1 - u0) * scale / sub.width, sy = (v1 - v0) * scale / sub.height;
    C2D_DrawImageAt(img, bx + bw / 2 + (u0 - cx) * scale, by + bh / 2 + (v0 - cy) * scale, 0.5f, NULL, sx, sy);
}

float image_draw_fit(const Picture *p, float x, float y, float w, float h, float max_scale)
{
    float s = w / p->w < h / p->h ? w / p->w : h / p->h;
    if (s > max_scale) s = max_scale;
    draw_region(p, x, y, w, h, s, p->w / 2.0f, p->h / 2.0f);
    return s;
}

void image_draw_cover(const Picture *p, float x, float y, float w, float h)
{
    float s = w / p->w > h / p->h ? w / p->w : h / p->h;
    draw_region(p, x, y, w, h, s, p->w / 2.0f, p->h / 2.0f);
}

void image_draw_at(const Picture *p, float x, float y, float w, float h, float scale, float cx, float cy)
{
    draw_region(p, x, y, w, h, scale, cx, cy);
}

void image_exit(void)
{
    for (int i = 0; i < SLOTS; i++) slot_free(&slots[i]);
}
