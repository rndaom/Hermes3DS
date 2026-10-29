// Uploads to the PC (protocol.md §5.1): photos and voice messages. One runs at a time, the rest wait; a dropped
// connection resumes where the PC says it stopped.
#pragma once

#include <jansson.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define UPLOAD_ID_LEN 22  // "u" + 20 hex digits + NUL

typedef void (*UploadDone)(uint32_t ctx, bool ok, const char *why);

// A fresh random upload id.
void upload_new_id(char out[UPLOAD_ID_LEN]);
// Upload data[0..len) as blob `id`; upload.c owns `data` from here (freed when done). `done` runs once, from
// the main loop. False when it can't start (too many waiting, too large for this PC).
bool upload_start(const char *id, uint8_t *data, size_t len, const char *mime, const char *name, UploadDone done,
                  uint32_t ctx);
// 0..1 while `id` is on its way, -1 when it isn't (done, failed or unknown).
float upload_progress(const char *id);
bool upload_busy(void);
// The PC takes uploads up to this many bytes (welcome `upload.max`); 0 = it doesn't (older plugin).
void upload_set_max(uint32_t max);
uint32_t upload_max(void);

bool upload_on_json(const json_t *msg);  // blob.put_r / blob.put_done / an error about an upload: true if used
void upload_online(bool on);
void upload_tick(void);
void upload_exit(void);
