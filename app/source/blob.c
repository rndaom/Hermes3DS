#include "blob.h"

#include <3ds.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "app.h"
#include "cache.h"
#include "log.h"
#include "store.h"

#define CACHE_DIR STORE_DIR "/cache"
#define MAX_JOBS 24
#define MAX_ACTIVE 2               // transfers in flight at once; the rest wait their turn
#define MEM_LIMIT (4u << 20)       // memory fetches are pictures; anything bigger is refused
#define FILE_LIMIT (16u << 20)
#define STREAM_LIMIT (11u << 20)   // audio streamed into the cache (the PC cuts it at ~10 MB)
#define STALL_MS 15000             // no bytes for this long: ask again from where it stopped
#define CACHE_CAP (16ull << 20)    // SD cache size that triggers pruning (least recently used first)...
#define CACHE_KEEP (12ull << 20)   // ...down to this, so it doesn't run again after every few pictures

// How a job delivers its bytes: into memory (pictures, text; cached on the SD after), to a file (Save to
// SD), or straight into its SD cache file, readable while it arrives (audio).
typedef enum { J_MEM, J_SAVE, J_STREAM } JobKind;

typedef struct {
    BlobRef ref;
    BlobState state;
    JobKind kind;
    bool requested;
    uint32_t req;  // id of the outstanding blob.get
    uint32_t got;
    u64 last_rx;   // when the last chunk (or the request) went by
    uint8_t *buf;  // memory fetch
    FILE *f;       // save (path.part) or stream (path, "w+b" while loading)
    char path[160];
    u64 used;
} Job;

static Job jobs[MAX_JOBS];
static bool online;
static bool cache_checked;
static uint64_t cache_bytes;  // estimate: the total after the last prune plus what was written since

static const char *jstr(const json_t *o, const char *k)
{
    return json_string_value(json_object_get(o, k)) ?: "";
}

bool blob_ref_parse(const json_t *o, BlobRef *out)
{
    memset(out, 0, sizeof *out);
    const char *id = jstr(o, "id");
    json_t *size = json_object_get(o, "size");
    if (!json_is_integer(size) || json_integer_value(size) < 0 || json_integer_value(size) > 0xFFFFFFFF)
        return false;
    if (!cache_id_ok(id)) return false;  // it becomes a file name in the SD cache
    snprintf(out->id, sizeof out->id, "%s", id);
    out->size = (uint32_t)json_integer_value(size);
    snprintf(out->mime, sizeof out->mime, "%s", jstr(o, "mime"));
    return true;
}

static Job *find(const char *id, JobKind kind)
{
    for (int i = 0; i < MAX_JOBS; i++)
        if (jobs[i].state != BLOB_NONE && jobs[i].kind == kind && !strcmp(jobs[i].ref.id, id)) return &jobs[i];
    return NULL;
}

// Drop what a job wrote to the SD card when it did not complete.
static void discard_file(const Job *j)
{
    if (j->kind == J_STREAM) {
        remove(j->path);
        return;
    }
    char part[176];
    snprintf(part, sizeof part, "%s.part", j->path);
    remove(part);
}

static void job_free(Job *j)
{
    if (j->state == BLOB_LOADING && j->requested && online)
        net_send(json_pack("{s:s, s:s}", "t", "blob.cancel", "blob", j->ref.id));
    free(j->buf);
    if (j->f) {
        fclose(j->f);
        if (j->state != BLOB_READY) discard_file(j);
    }
    memset(j, 0, sizeof *j);
}

// A free slot, evicting the least recently used finished job when full.
static Job *alloc_job(void)
{
    Job *victim = NULL;
    for (int i = 0; i < MAX_JOBS; i++) {
        Job *j = &jobs[i];
        if (j->state == BLOB_NONE) return j;
        if (j->state != BLOB_LOADING && (!victim || j->used < victim->used)) victim = j;
    }
    if (!victim) return NULL;
    job_free(victim);
    return victim;
}

static void request(Job *j)
{
    j->req = app_request("blob.get", json_pack("{s:s, s:I}", "blob", j->ref.id, "offset", (json_int_t)j->got));
    j->requested = true;
    j->last_rx = osGetTime();
}

// Start waiting transfers while fewer than MAX_ACTIVE run.
static void pump(void)
{
    if (!online) return;
    int active = 0;
    for (int i = 0; i < MAX_JOBS; i++) active += jobs[i].state == BLOB_LOADING && jobs[i].requested;
    for (int i = 0; i < MAX_JOBS && active < MAX_ACTIVE; i++)
        if (jobs[i].state == BLOB_LOADING && !jobs[i].requested) {
            request(&jobs[i]);
            active++;
        }
}

static void cache_path(const char *id, char *out, size_t cap)
{
    snprintf(out, cap, CACHE_DIR "/%s", id);
}

// The first cache access of a run prunes the cache to its cap (one directory listing).
static void cache_check(void)
{
    if (cache_checked) return;
    cache_checked = true;
    u64 t0 = osGetTime();
    cache_bytes = cache_prune(CACHE_DIR, CACHE_CAP, CACHE_KEEP, false);
    log_printf("cache: %llu KB after check (%llu ms)", (unsigned long long)(cache_bytes >> 10),
               (unsigned long long)(osGetTime() - t0));
}

static bool cache_read(Job *j)
{
    cache_check();
    char path[96];
    cache_path(j->ref.id, path, sizeof path);
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    bool ok = fread(j->buf, 1, j->ref.size, f) == j->ref.size && fgetc(f) == EOF;
    fclose(f);
    if (ok) cache_touch(CACHE_DIR, j->ref.id, j->ref.size, false);
    return ok;
}

bool blob_cache_put(const char *id, const uint8_t *data, size_t len)
{
    cache_check();
    mkdir(STORE_DIR, 0777);
    mkdir(CACHE_DIR, 0777);
    char path[96], tmp[104];
    cache_path(id, path, sizeof path);
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    if (!f) return false;
    bool ok = fwrite(data, 1, len, f) == len;
    ok = fclose(f) == 0 && ok;
    remove(path);
    if (!ok || rename(tmp, path) != 0) {
        remove(tmp);
        return false;
    }
    cache_touch(CACHE_DIR, id, len, true);
    cache_bytes += len;
    if (cache_bytes > CACHE_CAP) cache_bytes = cache_prune(CACHE_DIR, CACHE_CAP, CACHE_KEEP, false);
    return true;
}

static void cache_write(const Job *j)
{
    blob_cache_put(j->ref.id, j->buf, j->ref.size);
}

static void finish(Job *j)
{
    j->state = BLOB_READY;
    j->requested = false;
    if (j->kind == J_STREAM) {  // the file stays open for reading; touched again so pruning keeps it
        if (fflush(j->f) != 0) {
            log_printf("blob %s: could not write the cache file", j->ref.id);
            fclose(j->f);
            j->f = NULL;
            remove(j->path);
            j->state = BLOB_FAILED;
        } else {
            cache_touch(CACHE_DIR, j->ref.id, j->ref.size, true);
            cache_bytes += j->ref.size;
            if (cache_bytes > CACHE_CAP) cache_bytes = cache_prune(CACHE_DIR, CACHE_CAP, CACHE_KEEP, false);
        }
    } else if (j->f) {
        bool ok = fclose(j->f) == 0;
        j->f = NULL;
        char part[176];
        snprintf(part, sizeof part, "%s.part", j->path);
        remove(j->path);
        if (!ok || rename(part, j->path) != 0) {
            log_printf("blob %s: could not write %s", j->ref.id, j->path);
            remove(part);
            j->state = BLOB_FAILED;
        }
    } else {
        cache_write(j);
    }
    app.dirty = true;
    pump();
}

static void fail(Job *j, const char *why)
{
    log_printf("blob %s failed: %s", j->ref.id, why);
    if (j->f) {
        fclose(j->f);
        j->f = NULL;
        discard_file(j);
    }
    free(j->buf);
    j->buf = NULL;
    j->state = BLOB_FAILED;
    j->requested = false;
    app.dirty = true;
    pump();
}

BlobState blob_get(const BlobRef *r, const uint8_t **data, size_t *len)
{
    Job *j = find(r->id, J_MEM);
    if (!j) {
        if (r->size > MEM_LIMIT) return BLOB_FAILED;
        if (!(j = alloc_job())) return BLOB_LOADING;  // every slot busy: try again next frame
        j->ref = *r;
        j->buf = malloc(r->size ? r->size : 1);
        if (!j->buf) {
            j->state = BLOB_FAILED;
            return BLOB_FAILED;
        }
        if (cache_read(j)) {
            j->state = BLOB_READY;
        } else {
            j->state = BLOB_LOADING;
            if (r->size == 0) finish(j);
            else pump();
        }
    }
    j->used = osGetTime();
    if (j->state == BLOB_READY) {
        *data = j->buf;
        *len = j->ref.size;
    }
    return j->state;
}

void blob_release(const char *id)
{
    Job *j = find(id, J_MEM);
    if (j) job_free(j);
    pump();
}

BlobState blob_save(const BlobRef *r, const char *path)
{
    Job *j = find(r->id, J_SAVE);
    if (j && j->state != BLOB_FAILED) return j->state;
    if (j) job_free(j);
    if (r->size > FILE_LIMIT) return BLOB_FAILED;
    if (!(j = alloc_job())) return BLOB_FAILED;
    j->ref = *r;
    j->kind = J_SAVE;
    j->used = osGetTime();
    snprintf(j->path, sizeof j->path, "%s", path);
    char part[176];
    snprintf(part, sizeof part, "%s.part", path);
    j->f = fopen(part, "wb");
    if (!j->f) {
        log_printf("blob save: cannot create %s", part);
        j->state = BLOB_FAILED;
        return BLOB_FAILED;
    }
    j->state = BLOB_LOADING;
    if (r->size == 0) finish(j);
    else pump();
    return j->state;
}

BlobState blob_stream(const BlobRef *r, uint32_t *have)
{
    Job *j = find(r->id, J_STREAM);
    if (!j) {
        if (r->size > STREAM_LIMIT) return BLOB_FAILED;
        if (!(j = alloc_job())) return BLOB_LOADING;  // every slot busy: try again next frame
        j->ref = *r;
        j->kind = J_STREAM;
        cache_check();
        cache_path(r->id, j->path, sizeof j->path);
        if ((j->f = fopen(j->path, "rb"))) {  // cached whole already?
            if (fseek(j->f, 0, SEEK_END) == 0 && ftell(j->f) == (long)r->size) {
                j->got = r->size;
                j->state = BLOB_READY;
                cache_touch(CACHE_DIR, r->id, r->size, false);
            } else {
                fclose(j->f);
                j->f = NULL;
            }
        }
        if (j->state != BLOB_READY) {
            mkdir(STORE_DIR, 0777);
            mkdir(CACHE_DIR, 0777);
            if (!(j->f = fopen(j->path, "w+b"))) {
                log_printf("blob stream: cannot create %s", j->path);
                j->state = BLOB_FAILED;
                return BLOB_FAILED;
            }
            // Indexed from the start, so a prune while it downloads counts it as just used.
            cache_touch(CACHE_DIR, r->id, r->size, true);
            j->state = BLOB_LOADING;
            if (r->size == 0) finish(j);
            else pump();
        }
    }
    j->used = osGetTime();
    if (have) *have = j->got;
    return j->state;
}

size_t blob_stream_read(const char *id, uint32_t offset, void *buf, size_t len)
{
    Job *j = find(id, J_STREAM);
    if (!j || !j->f || offset >= j->got) return 0;
    if (len > j->got - offset) len = j->got - offset;
    if (fseek(j->f, (long)offset, SEEK_SET) != 0) return 0;  // also flushes what was written
    return fread(buf, 1, len, j->f);
}

void blob_stream_release(const char *id)
{
    Job *j = find(id, J_STREAM);
    if (j) job_free(j);
    pump();
}

BlobState blob_save_state(const char *id, float *progress)
{
    Job *j = find(id, J_SAVE);
    if (!j) return BLOB_NONE;
    if (progress) *progress = j->ref.size ? (float)j->got / (float)j->ref.size : 1.0f;
    return j->state;
}

void blob_on_chunk(const NetEvent *ev)
{
    for (int i = 0; i < MAX_JOBS; i++) {
        Job *j = &jobs[i];
        if (j->state != BLOB_LOADING || strcmp(j->ref.id, ev->blob)) continue;
        // Chunks come in order; one from a stale duplicate request (offset already passed) is skipped.
        if (ev->offset != j->got) continue;
        if (ev->len > j->ref.size - j->got) {
            fail(j, "more bytes than announced");
            continue;
        }
        if (j->f) {
            // A stream file is read from too: put the position back at the end before writing.
            if ((j->kind == J_STREAM && fseek(j->f, (long)j->got, SEEK_SET) != 0) ||
                fwrite(ev->data, 1, ev->len, j->f) != ev->len) {
                fail(j, "SD write failed");
                continue;
            }
        } else {
            memcpy(j->buf + j->got, ev->data, ev->len);
        }
        j->got += (uint32_t)ev->len;
        j->last_rx = osGetTime();
        if (j->got == j->ref.size) finish(j);
    }
}

bool blob_on_json(const json_t *msg)
{
    json_t *idv = json_object_get(msg, "id");
    if (!json_is_integer(idv)) return false;
    uint32_t id = (uint32_t)json_integer_value(idv);
    const char *t = jstr(msg, "t");
    bool hit = false;
    for (int i = 0; i < MAX_JOBS; i++) {
        Job *j = &jobs[i];
        if (j->state != BLOB_LOADING || !j->requested || j->req != id) continue;
        hit = true;
        if (!strcmp(t, "error")) {
            fail(j, jstr(msg, "msg"));
        } else if (!strcmp(t, "blob.get_r")) {
            json_t *size = json_object_get(msg, "size");
            if (json_is_integer(size) && (uint32_t)json_integer_value(size) != j->ref.size)
                fail(j, "size changed on the PC");
        }
    }
    return hit;
}

void blob_online(bool on)
{
    if (on == online) return;
    online = on;
    // A dropped connection ends its transfers; ask again from where each one stopped.
    for (int i = 0; i < MAX_JOBS; i++) jobs[i].requested = false;
    pump();
}

void blob_tick(void)
{
    if (!online) return;
    u64 now = osGetTime();
    for (int i = 0; i < MAX_JOBS; i++) {
        Job *j = &jobs[i];
        if (j->state == BLOB_LOADING && j->requested && now - j->last_rx > STALL_MS) {
            log_printf("blob %s stalled at %lu/%lu; asking again", j->ref.id, (unsigned long)j->got,
                       (unsigned long)j->ref.size);
            request(j);
        }
    }
}

bool blob_busy(void)
{
    for (int i = 0; i < MAX_JOBS; i++)
        if (jobs[i].state == BLOB_LOADING) return true;
    return false;
}

void blob_exit(void)
{
    for (int i = 0; i < MAX_JOBS; i++) job_free(&jobs[i]);
    cache_close();
}
