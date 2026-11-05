#include "dspdump.h"

#include <3ds.h>
#include <mbedtls/sha256.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "dspfirm.h"
#include "log.h"

#define FIRM_PATH "sdmc:/3ds/dspfirm.cdc"
#define MAX_CODE (16u << 20)  // HOME Menu's .code is a few MB unpacked; anything bigger isn't it

// HOME Menu's title ID low word per region (high word 0x00040030), as DSP1 tries them. AUS has the EUR one.
static const uint32_t HOME_MENU[] = {
    [CFG_REGION_JPN] = 0x00008202, [CFG_REGION_USA] = 0x00008F02, [CFG_REGION_EUR] = 0x00009802,
    [CFG_REGION_AUS] = 0x00009802, [CFG_REGION_CHN] = 0x0000A102, [CFG_REGION_KOR] = 0x0000A902,
    [CFG_REGION_TWN] = 0x0000B102,
};

static void sha256(const uint8_t *data, size_t n, uint8_t out[32])
{
    mbedtls_sha256_ret(data, n, out, 0);
}

// The title's ExeFS .code from NAND, through the title-content archive (the .3dsx gets it from Luma's loader,
// the CIA from CategoryFileSystemTool in its RSF).
static uint8_t *read_code(uint32_t tid_low, size_t *n)
{
    uint32_t title[4] = {tid_low, 0x00040030, MEDIATYPE_NAND, 0};
    static const uint32_t exefs_code[5] = {0, 0, 2, 0x646F632E, 0x00000065};  // ExeFS, ".code"
    Handle f;
    Result rc = FSUSER_OpenFileDirectly(&f, ARCHIVE_SAVEDATA_AND_CONTENT, (FS_Path){PATH_BINARY, sizeof title, title},
                                        (FS_Path){PATH_BINARY, sizeof exefs_code, exefs_code}, FS_OPEN_READ, 0);
    if (R_FAILED(rc)) {
        log_printf("dspdump: no .code for %08lX (%08lX)", (unsigned long)tid_low, (unsigned long)rc);
        return NULL;
    }
    u64 size = 0;
    uint8_t *buf = NULL;
    u32 got = 0;
    if (R_SUCCEEDED(FSFILE_GetSize(f, &size)) && size >= 8 && size <= MAX_CODE && (buf = malloc(size)) &&
        (R_FAILED(FSFILE_Read(f, &got, 0, buf, size)) || got != size)) {
        free(buf);
        buf = NULL;
    }
    FSFILE_Close(f);
    if (!buf) log_printf("dspdump: couldn't read %08lX .code (%llu bytes)", (unsigned long)tid_low, (unsigned long long)size);
    *n = size;
    return buf;
}

// The firmware in one HOME Menu's .code, copied out (the .code buffers are a few MB, freed straight away).
static void *from_title(uint32_t tid_low, uint32_t *size)
{
    size_t n, total;
    uint8_t *code = read_code(tid_low, &n), *plain = NULL;
    if (!code) return NULL;
    const uint8_t *firm = dspfirm_find(code, n, size, sha256);  // a .code stored uncompressed
    if (!firm && (total = dspfirm_unpack_size(code, n)) && total <= MAX_CODE && (plain = malloc(total))) {
        if (dspfirm_unpack(code, n, plain, total)) firm = dspfirm_find(plain, total, size, sha256);
        else log_printf("dspdump: %08lX .code didn't unpack", (unsigned long)tid_low);
        n = total;
    }
    void *out = firm ? malloc(*size) : NULL;
    if (out) memcpy(out, firm, *size);
    else log_printf("dspdump: no DSP firmware in %08lX .code (%lu bytes)", (unsigned long)tid_low, (unsigned long)n);
    free(plain);
    free(code);
    return out;
}

void dspdump_save(const void *firm, uint32_t size)
{
    struct stat st;
    if (!stat(FIRM_PATH, &st)) return;  // never replace a dump the user already has
    mkdir("sdmc:/3ds", 0777);
    FILE *f = fopen(FIRM_PATH ".part", "wb");
    bool ok = f && fwrite(firm, 1, size, f) == size;
    if (f && fclose(f)) ok = false;
    if (ok && !rename(FIRM_PATH ".part", FIRM_PATH)) {
        log_printf("dspdump: wrote " FIRM_PATH " (%lu bytes)", (unsigned long)size);
        return;
    }
    remove(FIRM_PATH ".part");
    log_printf("dspdump: couldn't write " FIRM_PATH);
}

void *dspdump_extract(uint32_t *size)
{
    u64 t0 = osGetTime();
    u8 region = CFG_REGION_USA;
    CFGU_SecureInfoGetRegion(&region);
    uint32_t first = region < sizeof HOME_MENU / sizeof *HOME_MENU ? HOME_MENU[region] : HOME_MENU[CFG_REGION_USA];
    void *firm = from_title(first, size);
    for (size_t i = 0; !firm && i < sizeof HOME_MENU / sizeof *HOME_MENU; i++)  // a region-changed console
        if (HOME_MENU[i] != first && i != CFG_REGION_AUS) firm = from_title(HOME_MENU[i], size);  // AUS = EUR
    if (!firm) return NULL;
    log_printf("dspdump: DSP firmware from HOME Menu, %lu bytes in %llu ms", (unsigned long)*size,
               (unsigned long long)(osGetTime() - t0));
    return firm;
}
