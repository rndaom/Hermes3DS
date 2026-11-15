// Release versions: this build's, and whether another is newer (the update notice, protocol.md §4.3).
#pragma once
#include <stdbool.h>

#ifndef APP_VERSION  // set by the Makefile (VERSION_MAJOR/MINOR/MICRO)
#define APP_VERSION "3ds-dev"
#endif

// `X.Y.Z`, with or without a `v` or the app's `3ds-` in front, as numbers. False for anything else (a dev
// build, junk): at most four digits each, nothing before or after.
bool version_parse(const char *s, int v[3]);

// Whether `latest` is a later release than `mine`. False when either is not a release version.
bool version_newer(const char *latest, const char *mine);
