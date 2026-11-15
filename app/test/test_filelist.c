// Host test for the file picker's plain-C helpers (source/filelist.c).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/filelist.h"

static int fails;
#define CHECK(c)                                                    \
    do {                                                            \
        if (!(c)) {                                                 \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);     \
            fails++;                                                \
        }                                                           \
    } while (0)

static FileEntry entry(const char *name, bool dir)
{
    FileEntry e;
    memset(&e, 0, sizeof e);
    snprintf(e.name, sizeof e.name, "%s", name);
    e.dir = dir;
    e.size = FILE_SIZE_UNKNOWN;
    return e;
}

int main(void)
{
    // Folders first, then names without regard to case.
    FileEntry e[6] = {entry("notes.txt", false), entry("Zelda", true),  entry("boot.firm", false),
                      entry("3ds", true),        entry("Alpha.md", false), entry("alpha.md", false)};
    qsort(e, 6, sizeof e[0], filelist_cmp);
    const char *want[6] = {"3ds", "Zelda", "Alpha.md", "alpha.md", "boot.firm", "notes.txt"};
    for (int i = 0; i < 6; i++) CHECK(!strcmp(e[i].name, want[i]));

    CHECK(!strcmp(filelist_mime("report.PDF"), "application/pdf"));
    CHECK(!strcmp(filelist_mime("a.b.jpeg"), "image/jpeg"));
    CHECK(!strcmp(filelist_mime("notes.txt"), "text/plain"));
    CHECK(!strcmp(filelist_mime("game.3dsx"), "application/octet-stream"));
    CHECK(!strcmp(filelist_mime("noext"), "application/octet-stream"));
    CHECK(!strcmp(filelist_mime(".hidden"), "application/octet-stream"));
    CHECK(!strcmp(filelist_mime("dot."), "application/octet-stream"));

    char p[64];
    CHECK(filelist_join(p, sizeof p, "sdmc:/", "3ds") && !strcmp(p, "sdmc:/3ds"));
    CHECK(filelist_join(p, sizeof p, "sdmc:/3ds", "hermes") && !strcmp(p, "sdmc:/3ds/hermes"));
    char tiny[10];
    CHECK(!filelist_join(tiny, sizeof tiny, "sdmc:/3ds", "hermes"));

    char child[32];
    snprintf(p, sizeof p, "sdmc:/3ds/hermes");
    CHECK(filelist_up(p, child, sizeof child) && !strcmp(p, "sdmc:/3ds") && !strcmp(child, "hermes"));
    CHECK(!strcmp(filelist_title(p), "3ds"));
    CHECK(filelist_up(p, child, sizeof child) && !strcmp(p, "sdmc:/") && !strcmp(child, "3ds"));
    CHECK(!strcmp(filelist_title(p), "SD card"));
    CHECK(!filelist_up(p, child, sizeof child) && !strcmp(p, "sdmc:/"));
    snprintf(p, sizeof p, "sdmc:/DCIM/");
    CHECK(filelist_up(p, NULL, 0) && !strcmp(p, "sdmc:/"));
    snprintf(p, sizeof p, "romfs:/x");
    CHECK(!filelist_up(p, NULL, 0));

    if (fails) return 1;
    printf("filelist: ok\n");
    return 0;
}
