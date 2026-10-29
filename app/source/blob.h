// Blob transfers (protocol.md §5): fetches into memory (pictures; cached on the SD card), straight to a
// file (Save to SD), or streamed into the SD cache (audio). Transfers resume from where they stopped after
// a reconnect.
#pragma once

#include <jansson.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "net.h"

typedef struct {
    char id[33];
    uint32_t size;
    char mime[48];
} BlobRef;

typedef enum { BLOB_NONE, BLOB_LOADING, BLOB_READY, BLOB_FAILED } BlobState;

bool blob_ref_parse(const json_t *o, BlobRef *out);  // {"id","size","mime"}; false when malformed

// Memory fetch. Starts the transfer (or reads the SD cache) on first call; READY gives the bytes, which stay
// valid until blob_release(). Poll it each frame while LOADING.
BlobState blob_get(const BlobRef *r, const uint8_t **data, size_t *len);
void blob_release(const char *id);

// Save to `path` (written as path.part, renamed when complete). Progress 0..1 while LOADING.
BlobState blob_save(const BlobRef *r, const char *path);
BlobState blob_save_state(const char *id, float *progress);

// Streaming fetch into the SD cache (audio: too big for memory). Starts the transfer (or finds it cached)
// on first call; `have` is how many bytes from the start can be read so far (all of them once READY). Call
// it every frame while in use (an idle finished stream may be evicted). FAILED stays until released.
BlobState blob_stream(const BlobRef *r, uint32_t *have);
size_t blob_stream_read(const char *id, uint32_t offset, void *buf, size_t len);  // bytes read
void blob_stream_release(const char *id);  // closes it (cancels an unfinished download)

// Put bytes the 3DS made itself (a photo, a voice message) into the SD cache under `id`, so the fetches above
// find them there without asking the PC. False when the SD card write failed.
bool blob_cache_put(const char *id, const uint8_t *data, size_t len);

// Wiring from the app's event loop.
void blob_on_chunk(const NetEvent *ev);
bool blob_on_json(const json_t *msg);  // true when it was a reply to a blob request (consumed)
void blob_online(bool online);         // resume interrupted transfers on reconnect
void blob_tick(void);                  // once a frame: re-asks for transfers that stalled
bool blob_busy(void);                  // a transfer is running (keep redrawing for progress)
void blob_exit(void);
