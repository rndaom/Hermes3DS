#include "dspfirm.h"

#include <string.h>

// Firmware header (3dbrew "DSP firmware", Luma's DspFirm): a 0x100-byte signature, "DSP1", totalSize, ..., the
// segment count at 0x10E, then up to 10 segment headers of 0x30 bytes (offset, load address, size, ..., SHA-256).
#define SIG 0x100
#define OFF_SIZE 0x104
#define OFF_NSEG 0x10E
#define OFF_SEGS 0x120
#define SEG_LEN 0x30
#define MAX_SEGS 10
#define HDR_LEN (OFF_SEGS + MAX_SEGS * SEG_LEN)  // 0x300: segment data starts after the headers

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

// The footer's last 8 bytes: [0] = header length << 24 | compressed region length (both counted from the end),
// [4] = how many bytes the file grows by when unpacked. Below the region the file is stored as is.
size_t dspfirm_unpack_size(const uint8_t *in, size_t n)
{
    if (n < 8) return 0;
    uint32_t bounds = le32(in + n - 8), extra = le32(in + n - 4);
    uint32_t region = bounds & 0xFFFFFF, footer = bounds >> 24;
    if (footer < 8 || region < footer || region > n || extra == 0 || extra > SIZE_MAX - n) return 0;
    return n + extra;
}

// Backward LZSS, read from the end down: a flag byte, then 8 items (high bit first), each a literal byte or a
// 2-byte copy of 3-18 bytes from 3-4098 bytes above the write position. Every index is checked, so a damaged
// file fails here or at the hash check instead of reading or writing out of bounds.
bool dspfirm_unpack(const uint8_t *in, size_t n, uint8_t *out, size_t total)
{
    if (dspfirm_unpack_size(in, n) != total) return false;
    uint32_t bounds = le32(in + n - 8);
    size_t stop = n - (bounds & 0xFFFFFF), src = n - (bounds >> 24), dst = total;
    memcpy(out, in, stop);
    while (src > stop) {
        uint8_t flags = in[--src];
        for (int i = 0; i < 8 && src > stop; i++, flags <<= 1) {
            if (flags & 0x80) {
                if (src - stop < 2) return false;
                src -= 2;
                unsigned v = in[src] | in[src + 1] << 8;
                size_t len = (v >> 12) + 3, back = (v & 0xFFF) + 3;
                if (dst - stop < len || back > total - dst) return false;  // reads stay in what's written
                for (; len; len--, dst--) out[dst - 1] = out[dst - 1 + back];
            } else {
                if (dst <= stop) return false;
                out[--dst] = in[--src];
            }
        }
    }
    return true;
}

static bool valid(const uint8_t *p, size_t room, uint32_t *size, DspfirmHash sha256)
{
    uint32_t total = le32(p + OFF_SIZE);
    if (total < HDR_LEN || total > DSPFIRM_MAX || total > room || p[OFF_NSEG] > MAX_SEGS) return false;
    int segs = 0;
    for (int i = 0; i < MAX_SEGS; i++) {
        const uint8_t *h = p + OFF_SEGS + i * SEG_LEN;
        uint32_t off = le32(h), len = le32(h + 8);
        if (!len) break;  // the used headers come first (DSP1 stops at the first empty one too)
        if (off < HDR_LEN || off > total || len > total - off) return false;
        uint8_t hash[32];
        sha256(p + off, len, hash);
        if (memcmp(hash, h + 0x10, 32)) return false;
        segs++;
    }
    if (!segs) return false;
    *size = total;
    return true;
}

const uint8_t *dspfirm_find(const uint8_t *code, size_t n, uint32_t *size, DspfirmHash sha256)
{
    if (n < HDR_LEN) return NULL;
    for (const uint8_t *m = code + SIG; m + 4 <= code + n; m++) {
        m = memchr(m, 'D', (size_t)(code + n - m));
        if (!m || m + 4 > code + n) break;
        if (memcmp(m, "DSP1", 4)) continue;
        const uint8_t *p = m - SIG;
        if ((size_t)(code + n - p) >= HDR_LEN && valid(p, (size_t)(code + n - p), size, sha256)) return p;
    }
    return NULL;
}
