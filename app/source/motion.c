#include "motion.h"

#include <math.h>

float motion_progress(uint64_t now, uint64_t start, unsigned dur_ms)
{
    if (!start || !dur_ms || now < start) return 1.0f;
    uint64_t el = now - start;
    return el >= dur_ms ? 1.0f : (float)el / (float)dur_ms;
}

float motion_ease_out(float t)
{
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    float u = 1 - t;
    return 1 - u * u * u;
}

void motion_eye_offsets(float depth, float slider, int *left, int *right)
{
    if (slider < 0) slider = 0;
    if (slider > 1) slider = 1;
    int p = (int)lroundf(depth * slider);
    *left = -(p / 2);
    *right = p + *left;
}
