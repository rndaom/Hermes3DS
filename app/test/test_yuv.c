// Host-side test: app/source/yuv.c (the camera's YUV to the preview, the JPEG planes and the thumbnail).
// Build and run: app/test/run_tests.sh. Built with -DHAVE_TURBOJPEG it also encodes and decodes a real JPEG.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/yuv.h"

#ifdef HAVE_TURBOJPEG
#include <turbojpeg.h>
#endif

static int failures;

#define CHECK(cond, ...)                                       \
    do {                                                       \
        if (!(cond)) {                                         \
            failures++;                                        \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);        \
            printf(__VA_ARGS__);                               \
            printf("\n");                                      \
        }                                                      \
    } while (0)

typedef struct {
    const char *name;
    int r, g, b;
} Colour;

static const Colour COLOURS[] = {
    {"black", 0, 0, 0},       {"white", 255, 255, 255},  {"grey", 128, 128, 128},  {"red", 255, 0, 0},
    {"green", 0, 255, 0},     {"blue", 0, 0, 255},       {"brick", 200, 30, 30},   {"leaf", 40, 180, 60},
    {"navy", 30, 40, 200},    {"skin", 224, 172, 140},   {"sky", 120, 170, 230},   {"shadow", 20, 25, 18},
    {"yellow", 230, 220, 40}, {"purple", 140, 60, 170},
};
#define NCOL ((int)(sizeof COLOURS / sizeof COLOURS[0]))

static const YuvStd STDS[] = {YUV_601, YUV_709, YUV_601_TV, YUV_709_TV};

static int iabs(int v) { return v < 0 ? -v : v; }
static int clamp255(double v) { return v < 0 ? 0 : v > 255 ? 255 : (int)(v + 0.5); }

// What a camera of that standard sends for the colour (the reference, in doubles).
static void encode(YuvStd std, const Colour *c, uint8_t *y, uint8_t *u, uint8_t *v)
{
    int b709 = std == YUV_709 || std == YUV_709_TV, tv = std == YUV_601_TV || std == YUV_709_TV;
    double kr = b709 ? 0.2126 : 0.299, kb = b709 ? 0.0722 : 0.114;
    double r = c->r / 255.0, g = c->g / 255.0, b = c->b / 255.0;
    double yy = kr * r + (1 - kr - kb) * g + kb * b;
    double pb = (b - yy) / (2 * (1 - kb)), pr = (r - yy) / (2 * (1 - kr));
    *y = (uint8_t)clamp255(tv ? 16 + 219 * yy : 255 * yy);
    *u = (uint8_t)clamp255(128 + (tv ? 224 : 255) * pb);
    *v = (uint8_t)clamp255(128 + (tv ? 224 : 255) * pr);
}

// JPEG's (JFIF) YCbCr to RGB.
static void jfif_rgb(int y, int cb, int cr, int *r, int *g, int *b)
{
    *r = clamp255(y + 1.402 * (cr - 128));
    *g = clamp255(y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128));
    *b = clamp255(y + 1.772 * (cb - 128));
}

// A w x h YUYV frame of one colour.
static uint8_t *solid(YuvStd std, const Colour *c, int w, int h)
{
    uint8_t *f = malloc((size_t)w * h * 2), y, u, v;
    encode(std, c, &y, &u, &v);
    for (size_t i = 0; i < (size_t)w * h / 2; i++) {
        f[i * 4] = y;
        f[i * 4 + 1] = u;
        f[i * 4 + 2] = y;
        f[i * 4 + 3] = v;
    }
    return f;
}

static void test_maps(void)
{
    for (int s = 0; s < 4; s++) {
        YuvConv c;
        yuv_conv_init(&c, STDS[s]);
        CHECK(c.std == STDS[s], "std %d kept", s);
        CHECK(c.jfif == (STDS[s] == YUV_601), "%s: jfif flag", yuv_std_name(STDS[s]));
        // yuv_planes relies on JPEG's chroma not depending on Y, whatever the source.
        CHECK(c.to_jfif.m[1][0] == 0 && c.to_jfif.m[2][0] == 0, "%s: chroma has a Y term (%d, %d)",
              yuv_std_name(STDS[s]), (int)c.to_jfif.m[1][0], (int)c.to_jfif.m[2][0]);
        // yuv_preview's shortcut: one Y weight for R, G and B; R without U, B without V.
        const YuvMat *m = &c.to_rgb;
        CHECK(m->m[0][0] == m->m[1][0] && m->m[1][0] == m->m[2][0] && m->m[0][1] == 0 && m->m[2][2] == 0,
              "%s: RGB map shape", yuv_std_name(STDS[s]));
    }
    YuvConv c;
    yuv_conv_init(&c, (YuvStd)7);
    CHECK(c.std == YUV_601, "an unknown standard falls back to BT.601");
    CHECK(!strcmp(yuv_std_name((YuvStd)7), "unknown"), "unknown name");
}

static void test_texel(void)
{
    // GPU tiles: 8x8 blocks left to right, then down; Morton order inside (x bit 0, y bit 0, x bit 1, ...).
    CHECK(yuv_texel(0, 0, 512) == 0, "(0,0)");
    CHECK(yuv_texel(1, 0, 512) == 1, "(1,0)");
    CHECK(yuv_texel(0, 1, 512) == 2, "(0,1)");
    CHECK(yuv_texel(1, 1, 512) == 3, "(1,1)");
    CHECK(yuv_texel(2, 0, 512) == 4, "(2,0)");
    CHECK(yuv_texel(0, 2, 512) == 8, "(0,2)");
    CHECK(yuv_texel(4, 0, 512) == 16, "(4,0)");
    CHECK(yuv_texel(0, 4, 512) == 32, "(0,4)");
    CHECK(yuv_texel(7, 7, 512) == 63, "(7,7)");
    CHECK(yuv_texel(8, 0, 512) == 64, "(8,0)");
    CHECK(yuv_texel(0, 8, 512) == 64 * 64, "(0,8)");
    CHECK(yuv_texel(319, 239, 512) == (29u * 64 + 39) * 64 + 63, "(319,239)");
}

// Each colour through the preview, in each standard: the RGB565 texel is the colour (to its 5/6 bits).
static void test_preview_colours(void)
{
    uint16_t tex[64];
    for (int s = 0; s < 4; s++) {
        YuvConv c;
        yuv_conv_init(&c, STDS[s]);
        for (int k = 0; k < NCOL; k++) {
            const Colour *col = &COLOURS[k];
            uint8_t *f = solid(STDS[s], col, 16, 16);  // halved: one 8x8 tile
            yuv_preview(&c, f, 16, 16, tex, 8, NULL);
            int r5 = tex[0] >> 11, g6 = (tex[0] >> 5) & 63, b5 = tex[0] & 31;
            CHECK(iabs(r5 - (col->r >> 3)) <= 1 && iabs(g6 - (col->g >> 2)) <= 1 && iabs(b5 - (col->b >> 3)) <= 1,
                  "%s %s: preview %d,%d,%d want %d,%d,%d", yuv_std_name(STDS[s]), col->name, r5 << 3, g6 << 2,
                  b5 << 3, col->r, col->g, col->b);
            for (int i = 1; i < 64; i++) CHECK(tex[i] == tex[0], "%s %s: uneven tile", yuv_std_name(STDS[s]), col->name);
            free(f);
        }
    }
}

// Each colour through the JPEG planes, in each standard: JFIF decodes it back to the colour.
static void test_planes_colours(void)
{
    enum { PWD = 8, PHT = 2 };
    uint8_t yp[PWD * PHT], up[PWD * PHT / 2], vp[PWD * PHT / 2];
    for (int s = 0; s < 4; s++) {
        YuvConv c;
        yuv_conv_init(&c, STDS[s]);
        for (int k = 0; k < NCOL; k++) {
            const Colour *col = &COLOURS[k];
            uint8_t *f = solid(STDS[s], col, PWD, PHT);
            yuv_planes(&c, f, PWD, PHT, yp, up, vp);
            int r, g, b;
            jfif_rgb(yp[3], up[1], vp[1], &r, &g, &b);
            CHECK(iabs(r - col->r) <= 3 && iabs(g - col->g) <= 3 && iabs(b - col->b) <= 3,
                  "%s %s: JPEG planes decode to %d,%d,%d want %d,%d,%d", yuv_std_name(STDS[s]), col->name, r, g, b,
                  col->r, col->g, col->b);
            free(f);
        }
    }
}

// Byte order: Y0 U Y1 V (libctru's INPUT_YUV422_BATCH, the camera's OUTPUT_YUV_422). A mix-up shows as a
// picture of luma in the colour planes: a heavy tint.
static void test_byte_order(void)
{
    enum { FW = 8, FH = 2 };
    uint8_t f[FW * FH * 2], yp[FW * FH], up[FW * FH / 2], vp[FW * FH / 2];
    for (int i = 0; i < FW * FH * 2; i++) f[i] = (uint8_t)(10 + i);
    YuvConv c;
    yuv_conv_init(&c, YUV_601);
    yuv_planes(&c, f, FW, FH, yp, up, vp);
    for (int i = 0; i < FW * FH / 2; i++) {
        CHECK(yp[i * 2] == f[i * 4] && yp[i * 2 + 1] == f[i * 4 + 2], "pair %d: Y", i);
        CHECK(up[i] == f[i * 4 + 1], "pair %d: U (Cb) from byte 1", i);
        CHECK(vp[i] == f[i * 4 + 3], "pair %d: V (Cr) from byte 3", i);
    }
    // The preview: blue in U, red in V. A frame with V high and U low must come out red, not blue.
    uint8_t g[16 * 16 * 2];
    for (int i = 0; i < 16 * 16 / 2; i++) {
        g[i * 4] = 82;      // Y
        g[i * 4 + 1] = 90;  // U low: less blue
        g[i * 4 + 2] = 82;
        g[i * 4 + 3] = 240; // V high: red
    }
    uint16_t tex[64];
    yuv_preview(&c, g, 16, 16, tex, 8, NULL);
    int r5 = tex[0] >> 11, g6 = (tex[0] >> 5) & 63, b5 = tex[0] & 31;
    CHECK(r5 > 25 && g6 < 16 && b5 < 8, "U/V order: want red, got %d,%d,%d", r5 << 3, g6 << 2, b5 << 3);
}

// Where each source pixel lands: the preview is the 2x2 mean, right way up and not mirrored.
static void test_preview_layout(void)
{
    enum { FW = 32, FH = 32, TW = 16 };
    uint8_t f[FW * FH * 2];
    for (int y = 0; y < FH; y++)
        for (int x = 0; x < FW; x++) {
            uint8_t *p = f + (y * FW + x) * 2;
            p[0] = (uint8_t)(x * 4 + y);  // Y differs everywhere; chroma neutral
            p[1] = 128;
        }
    uint16_t tex[TW * TW];
    YuvConv c;
    yuv_conv_init(&c, YUV_601);
    YuvStats st;
    yuv_preview(&c, f, FW, FH, tex, TW, &st);
    for (int y = 0; y < FH / 2; y++)
        for (int x = 0; x < FW / 2; x++) {
            int sx = x * 2, sy = y * 2;
            int want = (f[(sy * FW + sx) * 2] + f[(sy * FW + sx + 1) * 2] + f[((sy + 1) * FW + sx) * 2] +
                        f[((sy + 1) * FW + sx + 1) * 2] + 2) >> 2;
            int got = (tex[yuv_texel(x, y, TW)] >> 5) & 63;
            CHECK(got == want >> 2, "(%d,%d): green %d want %d", x, y, got << 2, want);
        }
    CHECK(st.u == 0 && st.v == 0, "neutral frame: mean U %d V %d", st.u, st.v);
}

// No automatic tint correction: a green scene stays green and a grey one grey (the camera's own white balance
// does that job), and the stats measure the cast over the mid-tones only.
static void test_no_cast_cancel_and_stats(void)
{
    YuvConv c;
    yuv_conv_init(&c, YUV_601);
    uint16_t tex[64];
    YuvStats st;
    const Colour leaf = {"leaf", 40, 180, 60};
    uint8_t *f = solid(YUV_601, &leaf, 16, 16);
    for (int i = 0; i < 10; i++) yuv_preview(&c, f, 16, 16, tex, 8, &st);  // repeated frames change nothing
    int r5 = tex[0] >> 11, g6 = (tex[0] >> 5) & 63, b5 = tex[0] & 31;
    CHECK(g6 << 2 > (r5 << 3) + 80 && g6 << 2 > (b5 << 3) + 80, "a green scene stays green (%d,%d,%d)", r5 << 3,
          g6 << 2, b5 << 3);
    uint8_t y, u, v;
    encode(YUV_601, &leaf, &y, &u, &v);
    CHECK(st.y == y && st.u == u - 128 && st.v == v - 128, "leaf stats %d %d %d want %d %d %d", st.y, st.u, st.v, y,
          u - 128, v - 128);
    free(f);
    // Half near-black with a strong (noise) tint, half mid grey: the mean colour comes from the grey only.
    uint8_t g[16 * 16 * 2];
    for (int i = 0; i < 16 * 16 / 2; i++) {
        bool dark = i < 16 * 16 / 4;
        g[i * 4] = g[i * 4 + 2] = dark ? 10 : 128;
        g[i * 4 + 1] = dark ? 60 : 128;
        g[i * 4 + 3] = dark ? 70 : 128;
    }
    yuv_preview(&c, g, 16, 16, tex, 8, &st);
    CHECK(st.u == 0 && st.v == 0, "dark pixels' tint is ignored (U %d V %d)", st.u, st.v);
    CHECK(st.y == 69, "mean Y %d want 69", st.y);
}

static void test_thumb(void)
{
    enum { FW = 64, FH = 48, TW = 12, TH = 9 };
    uint8_t yp[FW * FH], up[FW * FH / 2], vp[FW * FH / 2], rgb[TW * TH * 3];
    for (int k = 0; k < NCOL; k++) {
        const Colour *col = &COLOURS[k];
        uint8_t *f = solid(YUV_601_TV, col, FW, FH);
        YuvConv c;
        yuv_conv_init(&c, YUV_601_TV);
        yuv_planes(&c, f, FW, FH, yp, up, vp);
        yuv_thumb(yp, up, vp, FW, FH, rgb, TW, TH);
        for (int i = 0; i < TW * TH; i++) {
            const uint8_t *t = rgb + i * 3;
            if (iabs(t[0] - col->r) > 3 || iabs(t[1] - col->g) > 3 || iabs(t[2] - col->b) > 3) {
                CHECK(0, "%s thumb pixel %d: %d,%d,%d", col->name, i, t[0], t[1], t[2]);
                break;
            }
        }
        free(f);
    }
}

static void test_settle(void)
{
    YuvSettle s;
    YuvStats st = {120, -3, 4};
    memset(&s, 0, sizeof s);
    for (int i = 1; i <= YUV_SETTLE_MIN; i++) {
        bool done = yuv_settle(&s, &st);
        CHECK(done == (i == YUV_SETTLE_MIN), "steady scene: frame %d settled %d", i, done);
    }
    // Auto exposure coming down from a white first frame, white balance drifting in.
    static const YuvStats run[] = {{250, -40, -35}, {200, -30, -28}, {150, -20, -18}, {120, -10, -9}, {110, -4, -3},
                                   {105, -2, -1},   {104, -1, 0},    {104, -1, 0},    {104, -1, 0}};
    memset(&s, 0, sizeof s);
    int at = 0;
    for (int i = 0; i < (int)(sizeof run / sizeof run[0]) && !at; i++)
        if (yuv_settle(&s, &run[i])) at = i + 1;
    CHECK(at == 9, "converging scene settled at frame %d want 9", at);
    // A scene that never holds still: given up waiting at the cap.
    memset(&s, 0, sizeof s);
    at = 0;
    for (int i = 0; i < 100 && !at; i++) {
        YuvStats m = {i % 2 ? 60 : 160, 0, 0};
        if (yuv_settle(&s, &m)) at = i + 1;
    }
    CHECK(at == YUV_SETTLE_MAX, "moving scene settled at frame %d want %d", at, YUV_SETTLE_MAX);
}

#ifdef HAVE_TURBOJPEG
// The whole path as camera.c runs it: a frame of colour patches, the planes, libjpeg-turbo 4:2:2 at the app's
// settings, then decoded as any viewer (or Hermes) would.
static void test_jpeg(void)
{
    enum { FW = 128, FH = 64, P = 16 };
    for (int s = 0; s < 4; s++) {
        uint8_t *f = malloc(FW * FH * 2), *yp = malloc(FW * FH * 2), *up = yp + FW * FH, *vp = up + FW * FH / 2;
        for (int y = 0; y < FH; y++)
            for (int x = 0; x < FW; x += 2) {
                const Colour *col = &COLOURS[((y / P) * (FW / P) + x / P) % NCOL];
                uint8_t yy, u, v, *q = f + (y * FW + x) * 2;
                encode(STDS[s], col, &yy, &u, &v);
                q[0] = q[2] = yy;
                q[1] = u;
                q[3] = v;
            }
        YuvConv c;
        yuv_conv_init(&c, STDS[s]);
        yuv_planes(&c, f, FW, FH, yp, up, vp);
        tjhandle tj = tjInitCompress(), td = tjInitDecompress();
        unsigned char *jpg = NULL;
        unsigned long jlen = 0;
        const unsigned char *planes[3] = {yp, up, vp};
        int rc = tjCompressFromYUVPlanes(tj, planes, FW, NULL, FH, TJSAMP_422, &jpg, &jlen, 88, TJFLAG_FASTDCT);
        CHECK(rc == 0, "%s: compress: %s", yuv_std_name(STDS[s]), tjGetErrorStr2(tj));
        uint8_t *rgb = malloc(FW * FH * 3);
        rc = rc ? rc : tjDecompress2(td, jpg, jlen, rgb, FW, 0, FH, TJPF_RGB, 0);
        CHECK(rc == 0, "%s: decompress: %s", yuv_std_name(STDS[s]), tjGetErrorStr2(td));
        for (int py = 0; rc == 0 && py < FH / P; py++)
            for (int px = 0; px < FW / P; px++) {
                const Colour *col = &COLOURS[(py * (FW / P) + px) % NCOL];
                const uint8_t *t = rgb + ((py * P + P / 2) * FW + px * P + P / 2) * 3;
                CHECK(iabs(t[0] - col->r) <= 6 && iabs(t[1] - col->g) <= 6 && iabs(t[2] - col->b) <= 6,
                      "%s %s: JPEG decodes to %d,%d,%d want %d,%d,%d", yuv_std_name(STDS[s]), col->name, t[0], t[1],
                      t[2], col->r, col->g, col->b);
            }
        tjFree(jpg);
        tjDestroy(tj);
        tjDestroy(td);
        free(rgb);
        free(yp);
        free(f);
    }
}
#endif

int main(void)
{
    test_maps();
    test_texel();
    test_preview_colours();
    test_planes_colours();
    test_byte_order();
    test_preview_layout();
    test_no_cast_cancel_and_stats();
    test_thumb();
    test_settle();
#ifdef HAVE_TURBOJPEG
    test_jpeg();
    const char *jpeg = ", JPEG round trip";
#else
    const char *jpeg = " (JPEG round trip skipped: no libjpeg-turbo)";
#endif
    if (failures) {
        printf("yuv: %d failure(s)\n", failures);
        return 1;
    }
    printf("yuv: all checks passed (4 standards x %d colours%s)\n", NCOL, jpeg);
    return 0;
}
