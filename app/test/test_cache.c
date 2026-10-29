// Host test for source/cache.c (blob cache pruning). Usage: test_cache <empty scratch dir>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "../source/cache.h"

static int fails;
#define CHECK(c)                                                       \
    do {                                                               \
        if (!(c)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);        \
            fails++;                                                   \
        }                                                              \
    } while (0)

static const char *dir;

static void put(const char *name, size_t n)
{
    char p[300];
    snprintf(p, sizeof p, "%s/%s", dir, name);
    FILE *f = fopen(p, "wb");
    for (size_t i = 0; i < n; i++) fputc('x', f);
    fclose(f);
}

static int exists(const char *name)
{
    char p[300];
    struct stat st;
    snprintf(p, sizeof p, "%s/%s", dir, name);
    return stat(p, &st) == 0;
}

static void rm(const char *name)
{
    char p[300];
    snprintf(p, sizeof p, "%s/%s", dir, name);
    remove(p);
}

static int index_lines(void)
{
    char p[300], line[96];
    snprintf(p, sizeof p, "%s/" CACHE_INDEX, dir);
    FILE *f = fopen(p, "r");
    if (!f) return 0;
    int n = 0;
    while (fgets(line, sizeof line, f)) n++;
    fclose(f);
    return n;
}

static void add(const char *id, size_t n)  // what blob.c does after writing a cache file
{
    put(id, n);
    cache_touch(dir, id, (uint32_t)n, true);
}

int main(int argc, char **argv)
{
    if (argc < 2) return 2;
    dir = argv[1];

    // Empty directory, no index.
    CHECK(cache_prune(dir, 100, 50, false) == 0);

    // Under the cap: nothing goes; totals come from the index.
    add("b1", 10);
    add("b2", 20);
    add("b3", 30);
    CHECK(cache_prune(dir, 100, 50, false) == 60);
    CHECK(exists("b1") && exists("b2") && exists("b3"));

    // Over the cap: least recently used first, down to the target. A cache hit on b1 makes it recent.
    cache_touch(dir, "b1", 10, false);
    add("b4", 50);  // total 110 > cap 100
    uint64_t left = cache_prune(dir, 100, 50, false);
    // Order of use: b2, b3, b1, b4. Removing b2 (→90), b3 (→60), b1 (→50).
    CHECK(left == 50);
    CHECK(!exists("b2") && !exists("b3") && !exists("b1") && exists("b4"));
    CHECK(index_lines() == 1);

    // Orphans (written but never indexed, e.g. before a crash) count by stat and go first.
    put("orphan1", 40);
    add("b5", 5);
    CHECK(cache_prune(dir, 1000, 500, false) == 95);
    CHECK(index_lines() == 3);  // adopted: the index was rewritten with the orphan in it
    add("b6", 60);              // 155 > 100: orphan1 (oldest) goes first, then b4
    CHECK(cache_prune(dir, 100, 80, false) == 65);
    CHECK(!exists("orphan1") && !exists("b4") && exists("b5") && exists("b6"));

    // Leftover temporary files and names that cannot be blob ids are removed; the index file is kept.
    put("b7.tmp", 3);
    put("index.tmp", 3);
    put("a_name_much_longer_than_thirty_two_chars", 3);
    CHECK(cache_prune(dir, 100, 80, false) == 65);
    CHECK(!exists("b7.tmp") && !exists("index.tmp") && !exists("a_name_much_longer_than_thirty_two_chars"));
    CHECK(exists(CACHE_INDEX) && exists("b5"));

    // A file deleted behind the cache's back drops out of the index when it is compacted.
    rm("b5");
    CHECK(cache_prune(dir, 100, 80, true) == 60);
    CHECK(index_lines() == 1);

    // Many repeated uses of one id compact to one line.
    for (int i = 0; i < 200; i++) cache_touch(dir, "b6", 60, false);
    CHECK(cache_prune(dir, 100, 80, false) == 60);
    CHECK(index_lines() == 1);

    // Malformed index lines are ignored.
    cache_close();
    {
        char p[300];
        snprintf(p, sizeof p, "%s/" CACHE_INDEX, dir);
        FILE *f = fopen(p, "a");
        fputs("garbage\n\n b6\n", f);
        fclose(f);
    }
    CHECK(cache_prune(dir, 100, 80, true) == 60);
    CHECK(index_lines() == 1);

    // Blob ids from the PC become file names here: only plain ones pass.
    CHECK(cache_id_ok("b1") && cache_id_ok("bmfrgg5lsmvzgs3tu") && cache_id_ok("u0123456789abcdef0123"));
    CHECK(cache_id_ok("u0123456789abcdef0123t") && cache_id_ok("A_b-9") && cache_id_ok("indexes"));
    CHECK(cache_id_ok("0123456789abcdef0123456789abcdef"));    // 32: the longest
    CHECK(!cache_id_ok("0123456789abcdef0123456789abcdef0"));  // 33
    CHECK(!cache_id_ok("") && !cache_id_ok(CACHE_INDEX) && !cache_id_ok("INDEX") && !cache_id_ok("Index"));
    CHECK(!cache_id_ok("..") && !cache_id_ok("../x") && !cache_id_ok("a/b") && !cache_id_ok("a\\b"));
    CHECK(!cache_id_ok("b1.tmp") && !cache_id_ok("a b") && !cache_id_ok("a\nb") && !cache_id_ok("sdmc:x"));
    CHECK(!cache_id_ok("caf\xc3\xa9"));

    cache_close();
    rm("b6");
    rm(CACHE_INDEX);
    if (fails) return 1;
    printf("cache: all tests passed\n");
    return 0;
}
