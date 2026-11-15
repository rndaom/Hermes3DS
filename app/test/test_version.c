// Host test for the update notice's version comparing (source/version.c).
#include <stdio.h>

#include "../source/version.h"

static int fails;
#define CHECK(c)                                                    \
    do {                                                            \
        if (!(c)) {                                                 \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);     \
            fails++;                                                \
        }                                                           \
    } while (0)

int main(void)
{
    int v[3];
    CHECK(version_parse("0.2.1", v) && v[0] == 0 && v[1] == 2 && v[2] == 1);
    CHECK(version_parse("v1.20.300", v) && v[0] == 1 && v[1] == 20 && v[2] == 300);
    CHECK(version_parse("3ds-0.2.1", v) && v[2] == 1);
    const char *junk[] = {"3ds-dev", "", "0.2", "0.2.", "0.2.1-rc1", "0.2.1 ", " 0.2.1", "1.2.3.4", "12345.0.0",
                          "0..1", "a.b.c", "v", "3ds-v0.2.1", "-1.0.0"};
    for (unsigned i = 0; i < sizeof junk / sizeof *junk; i++) CHECK(!version_parse(junk[i], v));
    CHECK(!version_parse(NULL, v));

    CHECK(version_newer("0.2.2", "3ds-0.2.1"));
    CHECK(version_newer("0.10.0", "3ds-0.9.9"));
    CHECK(version_newer("1.0.0", "3ds-0.99.99"));
    CHECK(!version_newer("0.2.1", "3ds-0.2.1"));
    CHECK(!version_newer("0.2.0", "3ds-0.2.1"));
    CHECK(!version_newer("0.2.2", "3ds-dev"));  // a dev build is never told to update
    CHECK(!version_newer("junk", "3ds-0.2.1"));
    CHECK(!version_newer(NULL, "3ds-0.2.1"));

    if (fails) return 1;
    printf("version ok\n");
    return 0;
}
