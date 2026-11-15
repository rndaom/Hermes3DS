#include "filelist.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

static int cmp_nocase(const char *a, const char *b)
{
    for (;; a++, b++) {
        int ca = tolower((unsigned char)*a), cb = tolower((unsigned char)*b);
        if (ca != cb) return ca < cb ? -1 : 1;
        if (!ca) return 0;
    }
}

int filelist_cmp(const void *a, const void *b)
{
    const FileEntry *x = a, *y = b;
    if (x->dir != y->dir) return x->dir ? -1 : 1;
    int c = cmp_nocase(x->name, y->name);
    return c ? c : strcmp(x->name, y->name);
}

const char *filelist_mime(const char *name)
{
    static const struct {
        const char *ext, *mime;
    } T[] = {
        {"txt", "text/plain"}, {"log", "text/plain"}, {"ini", "text/plain"}, {"cfg", "text/plain"},
        {"md", "text/markdown"}, {"csv", "text/csv"}, {"json", "application/json"}, {"xml", "application/xml"},
        {"yaml", "application/yaml"}, {"yml", "application/yaml"}, {"toml", "application/toml"},
        {"html", "text/html"}, {"htm", "text/html"}, {"py", "text/plain"}, {"c", "text/plain"}, {"h", "text/plain"},
        {"js", "text/plain"}, {"sh", "text/plain"}, {"lua", "text/plain"},
        {"pdf", "application/pdf"}, {"zip", "application/zip"}, {"doc", "application/msword"},
        {"docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        {"xls", "application/vnd.ms-excel"},
        {"xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        {"ppt", "application/vnd.ms-powerpoint"},
        {"pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
        {"jpg", "image/jpeg"}, {"jpeg", "image/jpeg"}, {"png", "image/png"}, {"gif", "image/gif"},
        {"bmp", "image/bmp"}, {"webp", "image/webp"},
        {"wav", "audio/wav"}, {"mp3", "audio/mpeg"}, {"ogg", "audio/ogg"}, {"m4a", "audio/mp4"},
        {"flac", "audio/flac"}, {"mp4", "video/mp4"}, {"mov", "video/quicktime"}, {"webm", "video/webm"},
    };
    const char *dot = name ? strrchr(name, '.') : NULL;
    if (dot && dot != name && dot[1])
        for (size_t i = 0; i < sizeof T / sizeof T[0]; i++)
            if (!cmp_nocase(dot + 1, T[i].ext)) return T[i].mime;
    return "application/octet-stream";
}

bool filelist_join(char *out, size_t cap, const char *dir, const char *name)
{
    size_t n = strlen(dir);
    int w = snprintf(out, cap, "%s%s%s", dir, n && dir[n - 1] == '/' ? "" : "/", name);
    return w > 0 && (size_t)w < cap;
}

bool filelist_up(char *path, char *child, size_t child_cap)
{
    size_t root = strlen(FILE_ROOT), n = strlen(path);
    while (n > root && path[n - 1] == '/') path[--n] = 0;
    if (n <= root || strncmp(path, FILE_ROOT, root)) return false;
    char *slash = strrchr(path, '/');
    if (child && child_cap) snprintf(child, child_cap, "%s", slash + 1);
    if ((size_t)(slash - path) < root) slash[1] = 0;  // the parent is the root, which keeps its slash
    else *slash = 0;
    return true;
}

const char *filelist_title(const char *path)
{
    size_t root = strlen(FILE_ROOT);
    if (strlen(path) <= root) return "SD card";
    const char *slash = strrchr(path, '/');
    return slash && slash[1] ? slash + 1 : "SD card";
}
