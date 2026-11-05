#include "yuv.h"

#include <stddef.h>

static inline int clamp255(int v) { return v < 0 ? 0 : v > 255 ? 255 : v; }

static inline int iabs(int v) { return v < 0 ? -v : v; }

static int32_t q16(double x) { return (int32_t)(x * 65536.0 + (x < 0 ? -0.5 : 0.5)); }

// Output i of the map for one pixel; u and v centred on 0.
static inline int out(const YuvMat *m, int i, int y, int u, int v)
{
    return clamp255((m->m[i][0] * y + m->m[i][1] * u + m->m[i][2] * v + m->off[i]) >> 16);
}

static void set_map(YuvMat *m, const double k[3][3], double y0, const double base[3])
{
    for (int i = 0; i < 3; i++) {
        for (int j = 0; j < 3; j++) m->m[i][j] = q16(k[i][j]);
        m->off[i] = q16(base[i] - k[i][0] * y0 + 0.5);  // y0 folded in; + 0.5 rounds the >> 16
    }
}

void yuv_conv_init(YuvConv *c, YuvStd std)
{
    if ((unsigned)std > YUV_709_TV) std = YUV_601;
    bool b709 = std == YUV_709 || std == YUV_709_TV, tv = std == YUV_601_TV || std == YUV_709_TV;
    double kr = b709 ? 0.2126 : 0.299, kb = b709 ? 0.0722 : 0.114, kg = 1 - kr - kb;
    double y0 = tv ? 16 : 0, ys = tv ? 219.0 / 255 : 1, cs = tv ? 224.0 / 255 : 1;
    // R, G, B (0..255) from (Y - y0, U - 128, V - 128).
    double a[3][3] = {
        {1 / ys, 0, 2 * (1 - kr) / cs},
        {1 / ys, -2 * kb * (1 - kb) / (kg * cs), -2 * kr * (1 - kr) / (kg * cs)},
        {1 / ys, 2 * (1 - kb) / cs, 0},
    };
    // Full-range BT.601 Y, Cb - 128, Cr - 128 from R, G, B.
    static const double e[3][3] = {
        {0.299, 0.587, 0.114},
        {-0.299 / 1.772, -0.587 / 1.772, 0.886 / 1.772},
        {0.701 / 1.402, -0.587 / 1.402, -0.114 / 1.402},
    };
    double ea[3][3];
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++) ea[i][j] = e[i][0] * a[0][j] + e[i][1] * a[1][j] + e[i][2] * a[2][j];
    static const double rgb_base[3] = {0, 0, 0}, jfif_base[3] = {0, 128, 128};
    c->std = std;
    c->jfif = std == YUV_601;
    set_map(&c->to_rgb, a, y0, rgb_base);
    set_map(&c->to_jfif, ea, y0, jfif_base);
}

const char *yuv_std_name(YuvStd std)
{
    static const char *names[] = {"BT.601", "BT.709", "BT.601 TV range", "BT.709 TV range"};
    return (unsigned)std <= YUV_709_TV ? names[std] : "unknown";
}

void yuv_preview(const YuvConv *c, const uint8_t *yuyv, int w, int h, uint16_t *tex, int tex_w, YuvStats *st)
{
    // Five multiplies a pixel, not nine: Y weighs the same in R, G and B, R has no U term and B no V term.
    const YuvMat *m = &c->to_rgb;
    int32_t ky = m->m[0][0], rv = m->m[0][2], gu = m->m[1][1], gv = m->m[1][2], bu = m->m[2][1];
    int32_t ro = m->off[0], go = m->off[1], bo = m->off[2];
    int pw = w / 2, ph = h / 2;
    int32_t sy = 0, su = 0, sv = 0, n = 0;
    for (int y = 0; y < ph; y++) {
        const uint8_t *r0 = yuyv + (size_t)(y * 2) * w * 2, *r1 = r0 + w * 2;
        for (int x = 0; x < pw; x++) {
            const uint8_t *a = r0 + x * 4, *b = r1 + x * 4;  // Y0 U Y1 V, two rows
            int yy = (a[0] + a[2] + b[0] + b[2] + 2) >> 2;
            int u = ((a[1] + b[1] + 1) >> 1) - 128, v = ((a[3] + b[3] + 1) >> 1) - 128;
            sy += yy;
            if (yy > 40 && yy < 235) {
                su += u;
                sv += v;
                n++;
            }
            int32_t yt = ky * yy;
            int r = clamp255((yt + rv * v + ro) >> 16), g = clamp255((yt + gu * u + gv * v + go) >> 16),
                bl = clamp255((yt + bu * u + bo) >> 16);
            tex[yuv_texel(x, y, tex_w)] = (uint16_t)((r >> 3) << 11 | (g >> 2) << 5 | bl >> 3);
        }
    }
    if (!st) return;
    st->y = pw > 0 && ph > 0 ? sy / (pw * ph) : 0;
    st->u = n ? su / n : 0;
    st->v = n ? sv / n : 0;
}

void yuv_planes(const YuvConv *c, const uint8_t *yuyv, int w, int h, uint8_t *yp, uint8_t *up, uint8_t *vp)
{
    size_t pairs = (size_t)w * h / 2;
    if (c->jfif) {  // the camera's YUV already is JPEG's
        for (size_t i = 0; i < pairs; i++) {
            const uint8_t *q = yuyv + i * 4;
            yp[i * 2] = q[0];
            yp[i * 2 + 1] = q[2];
            up[i] = q[1];
            vp[i] = q[3];
        }
        return;
    }
    const YuvMat *m = &c->to_jfif;
    for (size_t i = 0; i < pairs; i++) {
        const uint8_t *q = yuyv + i * 4;
        int u = q[1] - 128, v = q[3] - 128;
        yp[i * 2] = (uint8_t)out(m, 0, q[0], u, v);
        yp[i * 2 + 1] = (uint8_t)out(m, 0, q[2], u, v);
        // Chroma doesn't depend on Y between these standards (the Y column is 0), so either Y does.
        up[i] = (uint8_t)out(m, 1, q[0], u, v);
        vp[i] = (uint8_t)out(m, 2, q[0], u, v);
    }
}

void yuv_thumb(const uint8_t *yp, const uint8_t *up, const uint8_t *vp, int w, int h, uint8_t *rgb, int tw, int th)
{
    YuvConv j;
    yuv_conv_init(&j, YUV_601);
    for (int y = 0; y < th; y++)
        for (int x = 0; x < tw; x++) {
            int x0 = x * w / tw, x1 = (x + 1) * w / tw, y0 = y * h / th, y1 = (y + 1) * h / th;
            uint32_t sy = 0, su = 0, sv = 0, n = 0;
            for (int yy = y0; yy < y1; yy++)
                for (int xx = x0; xx < x1; xx++, n++) {
                    sy += yp[(size_t)yy * w + xx];
                    su += up[(size_t)yy * (w / 2) + xx / 2];
                    sv += vp[(size_t)yy * (w / 2) + xx / 2];
                }
            int yy = (int)(sy / n), u = (int)(su / n) - 128, v = (int)(sv / n) - 128;
            uint8_t *t = rgb + ((size_t)y * tw + x) * 3;
            for (int i = 0; i < 3; i++) t[i] = (uint8_t)out(&j.to_rgb, i, yy, u, v);
        }
}

bool yuv_settle(YuvSettle *s, const YuvStats *st)
{
    bool steady = s->frames > 0 && iabs(st->y - s->last.y) <= 3 && iabs(st->u - s->last.u) <= 2 &&
                  iabs(st->v - s->last.v) <= 2;
    s->calm = steady ? s->calm + 1 : 0;
    s->frames++;
    s->last = *st;
    return (s->frames >= YUV_SETTLE_MIN && s->calm >= YUV_SETTLE_CALM) || s->frames >= YUV_SETTLE_MAX;
}
