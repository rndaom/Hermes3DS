// Size cap for the blob cache directory (sdmc:/3ds/hermes/cache). Portable C (host-tested in app/test).
//
// st_mtime is not dependable on the 3DS SD card and a stat per file is slow, so the cache keeps its own
// use order: `<dir>/index` is an append-only list of "<id> <size>" lines, one per write or cache hit, so
// the last line naming an id is its most recent use. Pruning lists the directory once, deletes the least
// recently used files until the total fits, and rewrites the index compactly. Files missing from the index
// (written just before a crash) are adopted as the oldest, with a stat each.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define CACHE_INDEX "index"

// Whether `id` (from the PC) can name a cache file: 1..32 of [A-Za-z0-9_-], and not the index (the SD card's
// FAT names ignore case, so neither is "INDEX"). Keeps ids from reaching outside the directory or into the
// index's "<id> <size>" lines.
bool cache_id_ok(const char *id);

// Record a use of `id` (written or read) of `size` bytes. Appends to the index, kept open between calls.
// `flush`: write it out now (after a new cache file, so a crash doesn't leave it unindexed; cache hits are
// only buffered, cache_close flushes them).
void cache_touch(const char *dir, const char *id, uint32_t size, bool flush);

// Delete least recently used files until at most `target` bytes remain, when more than `cap` bytes are
// cached (or always compact the index when `force`). Also removes leftover "*.tmp" files and index lines
// for files that are gone. Returns the bytes cached afterwards.
uint64_t cache_prune(const char *dir, uint64_t cap, uint64_t target, bool force);

void cache_close(void);
