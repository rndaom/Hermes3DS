// Host test for motion.c: animation progress, easing and the per-eye shifts of the 3D layers.
#include <stdio.h>
#include <stdlib.h>

#include "../source/motion.h"

#define CHECK(c)                                                    \
    do {                                                            \
        if (!(c)) {                                                 \
            fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #c); \
            exit(1);                                                \
        }                                                           \
    } while (0)

int main(void)
{
    CHECK(motion_progress(1000, 0, 200) == 1.0f);     // never started: at rest
    CHECK(motion_progress(1000, 1000, 200) == 0.0f);
    CHECK(motion_progress(1100, 1000, 200) == 0.5f);
    CHECK(motion_progress(1200, 1000, 200) == 1.0f);
    CHECK(motion_progress(9999, 1000, 200) == 1.0f);
    CHECK(motion_progress(900, 1000, 200) == 1.0f);   // the clock went back: at rest, not stuck
    CHECK(motion_progress(1000, 1000, 0) == 1.0f);

    CHECK(motion_ease_out(-1) == 0 && motion_ease_out(0) == 0 && motion_ease_out(1) == 1 && motion_ease_out(2) == 1);
    float prev = 0;
    for (int i = 1; i <= 100; i++) {  // rises all the way, and fastest at the start
        float v = motion_ease_out((float)i / 100);
        CHECK(v >= prev && v <= 1);
        prev = v;
    }
    CHECK(motion_ease_out(0.5f) > 0.8f);

    int l, r;
    for (int d = -8; d <= 8; d++) {
        motion_eye_offsets((float)d, 0, &l, &r);  // slider down: exactly the 2D picture
        CHECK(l == 0 && r == 0);
        motion_eye_offsets((float)d, 1, &l, &r);  // slider up: the eyes differ by the depth, split evenly
        CHECK(r - l == d);
        CHECK(abs(l) <= (abs(d) + 1) / 2 && abs(r) <= (abs(d) + 1) / 2);
        if (d > 0) CHECK(l <= 0 && r >= 0);  // behind the screen: the left eye's picture is further left
        if (d < 0) CHECK(l >= 0 && r <= 0);
    }
    motion_eye_offsets(5, 0.5f, &l, &r);
    CHECK(r - l == 3 || r - l == 2);
    motion_eye_offsets(5, 7, &l, &r);  // a slider value out of range is clamped
    CHECK(r - l == 5);
    motion_eye_offsets(5, -3, &l, &r);
    CHECK(l == 0 && r == 0);
    puts("motion ok");
    return 0;
}
