#include "version.h"

#include <string.h>

bool version_parse(const char *s, int v[3])
{
    if (!s) return false;
    if (!strncmp(s, "3ds-", 4)) s += 4;
    else if (*s == 'v') s++;
    for (int i = 0; i < 3; i++) {
        int n = 0, digits = 0;
        while (*s >= '0' && *s <= '9' && digits < 4) {
            n = n * 10 + (*s++ - '0');
            digits++;
        }
        if (!digits || *s != (i < 2 ? '.' : '\0')) return false;
        if (i < 2) s++;
        v[i] = n;
    }
    return true;
}

bool version_newer(const char *latest, const char *mine)
{
    int a[3], b[3];
    if (!version_parse(latest, a) || !version_parse(mine, b)) return false;
    for (int i = 0; i < 3; i++)
        if (a[i] != b[i]) return a[i] > b[i];
    return false;
}
