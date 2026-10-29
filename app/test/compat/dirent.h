// Minimal <dirent.h> for host tests built against the MSVC runtime (run_tests.sh adds -Icompat there).
#pragma once

#include <io.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct dirent {
    char d_name[260];
};

typedef struct {
    intptr_t h;
    struct _finddata_t fd;
    int first;
    struct dirent de;
} DIR;

static DIR *opendir(const char *path)
{
    char pat[300];
    snprintf(pat, sizeof pat, "%s/*", path);
    DIR *d = calloc(1, sizeof *d);
    if (!d) return NULL;
    d->h = _findfirst(pat, &d->fd);
    if (d->h == -1) {
        free(d);
        return NULL;
    }
    d->first = 1;
    return d;
}

static struct dirent *readdir(DIR *d)
{
    if (!d->first && _findnext(d->h, &d->fd) != 0) return NULL;
    d->first = 0;
    snprintf(d->de.d_name, sizeof d->de.d_name, "%s", d->fd.name);
    return &d->de;
}

static int closedir(DIR *d)
{
    _findclose(d->h);
    free(d);
    return 0;
}
