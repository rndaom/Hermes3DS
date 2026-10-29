#include "cache.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#ifndef S_ISDIR  // MSVC runtime (host tests)
#define S_ISDIR(m) (((m) & S_IFMT) == S_IFDIR)
#endif

#define COMPACT_SLACK 64  // rewrite the index once it holds this many more lines than files

typedef struct {
    char id[33];
    uint32_t size;
    int32_t rank;  // index line of the last use; -1 = not in the index (oldest)
    bool present;
} Entry;

typedef struct {
    Entry *e;
    int n, cap;
} Entries;

static FILE *idx;
static char idx_dir[128];

bool cache_id_ok(const char *id)
{
    size_t n = strlen(id);
    if (n == 0 || n > 32) return false;
    for (size_t i = 0; i < n; i++) {
        char c = id[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-'))
            return false;
    }
    char low[33];
    for (size_t i = 0; i <= n; i++) low[i] = (char)(id[i] >= 'A' && id[i] <= 'Z' ? id[i] + 32 : id[i]);
    return strcmp(low, CACHE_INDEX) != 0;
}

static Entry *push(Entries *l)
{
    if (l->n == l->cap) {
        int cap = l->cap ? l->cap * 2 : 256;
        Entry *e = realloc(l->e, (size_t)cap * sizeof *e);
        if (!e) return NULL;
        l->e = e;
        l->cap = cap;
    }
    Entry *e = &l->e[l->n++];
    memset(e, 0, sizeof *e);
    return e;
}

static int by_id_then_rank(const void *a, const void *b)
{
    const Entry *x = a, *y = b;
    int c = strcmp(x->id, y->id);
    return c ? c : (x->rank > y->rank) - (x->rank < y->rank);
}

static int by_rank(const void *a, const void *b)
{
    const Entry *x = a, *y = b;
    return (x->rank > y->rank) - (x->rank < y->rank);
}

static int by_id(const void *key, const void *elem)
{
    return strcmp(key, ((const Entry *)elem)->id);
}

static void path_in(const char *dir, const char *name, char *out, size_t cap)
{
    snprintf(out, cap, "%s/%s", dir, name);
}

void cache_close(void)
{
    if (idx) fclose(idx);
    idx = NULL;
}

void cache_touch(const char *dir, const char *id, uint32_t size, bool flush)
{
    if (idx && strcmp(idx_dir, dir)) cache_close();
    if (!idx) {
        char path[192];
        path_in(dir, CACHE_INDEX, path, sizeof path);
        if (!(idx = fopen(path, "a"))) return;
        snprintf(idx_dir, sizeof idx_dir, "%s", dir);
    }
    fprintf(idx, "%s %lu\n", id, (unsigned long)size);
    if (flush) fflush(idx);
}

// Index lines → one entry per id (its last use), sorted by id. Returns the number of lines read.
static int read_index(const char *dir, Entries *l)
{
    char path[192], line[96];
    path_in(dir, CACHE_INDEX, path, sizeof path);
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    int lines = 0;
    while (fgets(line, sizeof line, f)) {
        char id[33];
        unsigned long size;
        if (sscanf(line, "%32s %lu", id, &size) != 2) continue;
        Entry *e = push(l);
        if (!e) break;
        snprintf(e->id, sizeof e->id, "%s", id);
        e->size = (uint32_t)size;
        e->rank = lines++;
    }
    fclose(f);
    qsort(l->e, (size_t)l->n, sizeof *l->e, by_id_then_rank);
    int out = 0;  // keep the last (highest rank) entry of each id
    for (int i = 0; i < l->n; i++) {
        if (out && !strcmp(l->e[out - 1].id, l->e[i].id)) l->e[out - 1] = l->e[i];
        else l->e[out++] = l->e[i];
    }
    l->n = out;
    return lines;
}

static bool ends_with(const char *s, const char *tail)
{
    size_t n = strlen(s), m = strlen(tail);
    return n >= m && !strcmp(s + n - m, tail);
}

static bool write_index(const char *dir, const Entries *l)
{
    char path[192], tmp[200];
    path_in(dir, CACHE_INDEX, path, sizeof path);
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "w");
    if (!f) return false;
    for (int i = 0; i < l->n; i++)
        if (l->e[i].present) fprintf(f, "%s %lu\n", l->e[i].id, (unsigned long)l->e[i].size);
    bool ok = fclose(f) == 0;
    remove(path);
    if (!ok || rename(tmp, path) != 0) {
        remove(tmp);
        return false;
    }
    return true;
}

uint64_t cache_prune(const char *dir, uint64_t cap, uint64_t target, bool force)
{
    cache_close();
    Entries l = {0}, orphans = {0};
    int lines = read_index(dir, &l);

    DIR *d = opendir(dir);
    if (!d) {
        free(l.e);
        return 0;
    }
    char path[400];  // a directory entry name can be up to 255 bytes
    struct dirent *de;
    while ((de = readdir(d))) {
        const char *name = de->d_name;
        if (!strcmp(name, ".") || !strcmp(name, "..") || !strcmp(name, CACHE_INDEX)) continue;
        path_in(dir, name, path, sizeof path);
        if (ends_with(name, ".tmp") || strlen(name) > 32) {  // an interrupted write, or not a blob id
            remove(path);
            continue;
        }
        Entry *e = l.n ? bsearch(name, l.e, (size_t)l.n, sizeof *l.e, by_id) : NULL;
        if (e) {
            e->present = true;
            continue;
        }
        struct stat st;
        if (stat(path, &st) != 0 || S_ISDIR(st.st_mode)) continue;
        if (!(e = push(&orphans))) continue;
        snprintf(e->id, sizeof e->id, "%s", name);
        e->size = (uint32_t)st.st_size;
        e->rank = -1;
        e->present = true;
    }
    closedir(d);

    // One list: present files, oldest use first.
    int kept = 0;
    for (int i = 0; i < l.n; i++)
        if (l.e[i].present) l.e[kept++] = l.e[i];
    l.n = kept;
    for (int i = 0; i < orphans.n; i++) {
        Entry *e = push(&l);
        if (e) *e = orphans.e[i];
    }
    free(orphans.e);
    qsort(l.e, (size_t)l.n, sizeof *l.e, by_rank);

    uint64_t total = 0;
    for (int i = 0; i < l.n; i++) total += l.e[i].size;
    bool changed = force || lines > l.n + COMPACT_SLACK || orphans.n;
    if (total > cap) {
        for (int i = 0; i < l.n && total > target; i++) {
            path_in(dir, l.e[i].id, path, sizeof path);
            if (remove(path) == 0) {
                total -= l.e[i].size;
                l.e[i].present = false;
                changed = true;
            }
        }
    }
    if (changed) write_index(dir, &l);
    free(l.e);
    return total;
}
