// Plain-C helpers for the SD card file picker (filepick.c): ordering, paths and file types. No 3DS calls, so the
// host tests cover it (test/test_filelist.c).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define FILE_SIZE_UNKNOWN 0xffffffffu  // not looked up yet (a size past 4 GiB - 2 reads as 4 GiB - 2)
#define FILE_ROOT "sdmc:/"

typedef struct {
    char name[256];
    bool dir;
    uint32_t size;
} FileEntry;

// For qsort: folders first, then by name without regard to case (ties by exact name, so the order is total).
int filelist_cmp(const void *a, const void *b);
// The MIME type the PC is told, from the name's extension; "application/octet-stream" when unknown.
const char *filelist_mime(const char *name);
// dir + "/" + name into out; false when it doesn't fit.
bool filelist_join(char *out, size_t cap, const char *dir, const char *name);
// Cut `path` to its parent folder; `child` (may be NULL) gets the name cut off. False at the SD card's root.
bool filelist_up(char *path, char *child, size_t child_cap);
// The last part of a folder path, for a title: "Photos" from "sdmc:/DCIM/Photos", "SD card" for the root.
const char *filelist_title(const char *path);
