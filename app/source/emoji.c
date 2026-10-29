#include "emoji.h"

#include <3ds.h>
#include <citro2d.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

#include "log.h"
#include "perf.h"

// emoji.bin (little-endian): "H3EM", u16 format 1, u16 master size M, u32 count, u32 base code point,
// char[8] set id, u32 offset[count + 1], then per sprite zlib(M x M premultiplied RGBA8, rows top down).
// Only the offset table stays in memory; a sprite is read, inflated and area-filtered to the size it is
// drawn at the first time it is needed, into RGBA8 atlas pages like text.c's glyph cache.

#define MAX_MASTER 64
#define PAGE_W 256
#define PAGE_H 256
#define MAX_PAGES 4      // 256 KB each, allocated as needed (~360 sprites at body size per page)
#define HASH_SIZE 1024   // power of two
#define MAX_ENTRIES 700

static FILE *file;
static uint32_t *offsets;
static uint32_t count, base;
static int master;
static char cap[16];

typedef struct {
    uint16_t idx;   // sprite index + 1; 0 = empty slot
    uint8_t px, page;
    uint16_t x, y;  // position in the page
} Ent;

typedef struct {
    C3D_Tex tex;
    int shelf_x, shelf_y, shelf_h;
} Page;

static Ent cache[HASH_SIZE];
static int ncached;
static Page pages[MAX_PAGES];
static int npages, cur_page;
static bool cache_full;

void emoji_init(void)
{
    file = fopen("romfs:/emoji.bin", "rb");
    if (!file) {
        log_printf("emoji: no sprite set in romfs");
        return;
    }
    uint8_t h[24];
    if (fread(h, 1, sizeof h, file) != sizeof h || memcmp(h, "H3EM", 4) || (h[4] | h[5] << 8) != 1) goto bad;
    master = h[6] | h[7] << 8;
    memcpy(&count, h + 8, 4);
    memcpy(&base, h + 12, 4);
    if (master < 1 || master > MAX_MASTER || count == 0 || count > 0xFFFF) goto bad;
    offsets = malloc((count + 1) * sizeof *offsets);
    if (!offsets || fread(offsets, sizeof *offsets, count + 1, file) != count + 1) goto bad;
    snprintf(cap, sizeof cap, "emoji:%.8s", (const char *)h + 16);
    log_printf("emoji: %lu sprites, %d px, %s", (unsigned long)count, master, cap);
    return;
bad:
    log_printf("emoji: romfs:/emoji.bin is not a sprite set");
    free(offsets);
    offsets = NULL;
    count = 0;
    fclose(file);
    file = NULL;
}

static void cache_reset(void)
{
    memset(cache, 0, sizeof cache);
    ncached = 0;
    for (int i = 0; i < npages; i++) pages[i].shelf_x = pages[i].shelf_y = pages[i].shelf_h = 0;
    cur_page = 0;
    cache_full = false;
}

void emoji_exit(void)
{
    for (int i = 0; i < npages; i++) C3D_TexDelete(&pages[i].tex);
    npages = 0;
    cache_reset();
    free(offsets);
    offsets = NULL;
    count = 0;
    if (file) fclose(file);
    file = NULL;
}

void emoji_frame_begin(void)
{
    if (cache_full) {  // the previous frame is done with the old sprites
        cache_reset();
        perf_emoji_resets++;
    }
}

const char *emoji_cap(void) { return count ? cap : NULL; }

bool emoji_is(uint32_t cp) { return count && cp >= base && cp - base < count; }

static inline uint32_t morton8(int x, int y)
{
    return (uint32_t)((x & 1) | ((y & 1) << 1) | ((x & 2) << 1) | ((y & 2) << 2) | ((x & 4) << 2) | ((y & 4) << 3));
}

// Texel index in a tiled (8x8 Morton) texture `w` texels wide; row 0 is the top of the image (v = 1).
static inline uint32_t tex_index(int x, int y, int w)
{
    return (uint32_t)(((y >> 3) * (w >> 3) + (x >> 3)) * 64) + morton8(x & 7, y & 7);
}

// Room for a w x h box plus a 1 px gutter. False when the atlas is full.
static bool atlas_alloc(int w, int h, int *page, int *x, int *y)
{
    for (;;) {
        if (cur_page >= npages) {
            if (npages == MAX_PAGES) return false;
            Page *np = &pages[npages];
            if (!C3D_TexInit(&np->tex, PAGE_W, PAGE_H, GPU_RGBA8)) return false;
            C3D_TexSetFilter(&np->tex, GPU_NEAREST, GPU_NEAREST);
            memset(np->tex.data, 0, np->tex.size);
            C3D_TexFlush(&np->tex);
            np->shelf_x = np->shelf_y = np->shelf_h = 0;
            npages++;
        }
        Page *p = &pages[cur_page];
        if (p->shelf_x + w + 1 > PAGE_W || h + 1 > p->shelf_h) {  // open a new shelf
            p->shelf_y += p->shelf_h;
            p->shelf_x = 0;
            p->shelf_h = h + 1;
            if (p->shelf_y + p->shelf_h > PAGE_H) {
                cur_page++;
                continue;
            }
        }
        *page = cur_page;
        *x = p->shelf_x;
        *y = p->shelf_y;
        p->shelf_x += w + 1;
        return true;
    }
}

// Read sprite `idx` and area-filter it from master size to px x px into the atlas.
static bool bake(Ent *e, uint32_t idx, int px)
{
    static uint8_t src[MAX_MASTER * MAX_MASTER * 4];
    static float rows[MAX_MASTER * MAX_MASTER * 4];  // source rows x destination columns, RGBA
    uint32_t len = offsets[idx + 1] - offsets[idx];
    uLongf n = (uLongf)master * master * 4;
    uint8_t *z = malloc(len);
    bool ok = z && fseek(file, (long)offsets[idx], SEEK_SET) == 0 && fread(z, 1, len, file) == len &&
              uncompress(src, &n, z, len) == Z_OK && n == (uLongf)master * master * 4;
    free(z);
    if (!ok) return false;

    int page = 0, ax = 0, ay = 0;
    if (!atlas_alloc(px, px, &page, &ax, &ay)) return false;
    *e = (Ent){(uint16_t)(idx + 1), (uint8_t)px, (uint8_t)page, (uint16_t)ax, (uint16_t)ay};
    ncached++;

    // Separable box filter on premultiplied colour: destination pixel u covers source [u, u+1) * M / px.
    float k = (float)master / px;
    for (int j = 0; j < master; j++)
        for (int u = 0; u < px; u++) {
            float a = u * k, b = (u + 1) * k, acc[4] = {0};
            int i1 = b > master ? master : (int)ceilf(b);
            for (int i = (int)a; i < i1; i++) {
                float lo = a > i ? a : (float)i, hi = b < i + 1 ? b : (float)(i + 1);
                const uint8_t *s = &src[(j * master + i) * 4];
                for (int c = 0; c < 4; c++) acc[c] += (hi - lo) * s[c];
            }
            memcpy(&rows[(j * px + u) * 4], acc, sizeof acc);
        }
    u32 *dst = (u32 *)pages[page].tex.data;
    float norm = 1.0f / (k * k);
    for (int v = 0; v < px; v++) {
        float a = v * k, b = (v + 1) * k;
        int j1 = b > master ? master : (int)ceilf(b);
        for (int u = 0; u < px; u++) {
            float acc[4] = {0};
            for (int j = (int)a; j < j1; j++) {
                float lo = a > j ? a : (float)j, hi = b < j + 1 ? b : (float)(j + 1);
                const float *r = &rows[(j * px + u) * 4];
                for (int c = 0; c < 4; c++) acc[c] += (hi - lo) * r[c];
            }
            float al = acc[3] * norm, un = al > 0.5f ? 255.0f / al : 0;  // back to straight alpha
            uint32_t rgba[4];
            for (int c = 0; c < 3; c++) {
                float x = acc[c] * norm * un;
                rgba[c] = x >= 255 ? 255 : (uint32_t)(x + 0.5f);
            }
            rgba[3] = al >= 255 ? 255 : (uint32_t)(al + 0.5f);
            dst[tex_index(ax + u, ay + v, PAGE_W)] = rgba[0] << 24 | rgba[1] << 16 | rgba[2] << 8 | rgba[3];
        }
    }
    // Flush the tile rows written so the GPU reads them this frame.
    int t0 = ay >> 3, t1 = (ay + px - 1) >> 3;
    GSPGPU_FlushDataCache(dst + t0 * 8 * PAGE_W, (u32)((t1 - t0 + 1) * 8 * PAGE_W * 4));
    return true;
}

static Ent *sprite_get(uint32_t idx, int px)
{
    uint32_t h = ((idx + 1) * 2654435761u ^ (uint32_t)px * 40503u) & (HASH_SIZE - 1);
    for (;;) {
        Ent *e = &cache[h];
        if (e->idx == idx + 1 && e->px == px) return e;
        if (e->idx == 0) {
            if (ncached >= MAX_ENTRIES) break;
            u64 t = perf_now();
            bool ok = bake(e, idx, px);
            perf_add(&perf_emoji, t);
            if (ok) return e;
            break;
        }
        h = (h + 1) & (HASH_SIZE - 1);
    }
    cache_full = true;  // cleared at the next frame start; the sprite is skipped this frame
    return NULL;
}

void emoji_draw(uint32_t cp, float x, float y, int px)
{
    if (!emoji_is(cp) || px < 1 || px > 128) return;
    Ent *e = sprite_get(cp - base, px);
    if (!e) return;
    Tex3DS_SubTexture sub = {
        .width = (u16)px,
        .height = (u16)px,
        .left = e->x / (float)PAGE_W,
        .top = 1.0f - e->y / (float)PAGE_H,
        .right = (e->x + px) / (float)PAGE_W,
        .bottom = 1.0f - (e->y + px) / (float)PAGE_H,
    };
    C2D_Image img = {&pages[e->page].tex, &sub};
    C2D_DrawImageAt(img, x, y, 0.5f, NULL, 1.0f, 1.0f);
}
