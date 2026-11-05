// Queue for spoken replies (protocol.md §4.7 `voice`). Hermes may speak a reply as several short voice messages
// while it is still writing; the player has one track, so each waits for the one before. Plain C, no player or
// JSON in here, so the order and the timing rules run on the host (test/test_autoplay.c); speak.c ties it to
// the audio player. Items are opaque `ref`s the caller owns until it hands them in: this releases each one
// exactly once, whether it was played, dropped or never kept.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define AUTOPLAY_ID 40       // a blob id (32 chars) with room
#define AUTOPLAY_WAITING 32  // queued behind the current one
#define AUTOPLAY_SEEN 32     // ids remembered, so a replayed message (after a reconnect) isn't spoken twice
#define AUTOPLAY_HOLD_MS 90000  // a spoken reply left paused this long is given up on

// What the player is doing with the reply track this queue started last.
typedef enum {
    AP_ENDED,     // played to the end, or couldn't be played
    AP_PLAYING,   // playing or waiting for its download
    AP_PAUSED,    // started, then paused (the user, or the mic)
    AP_REPLACED,  // another track took the player: the user chose something else
} ApTrack;

typedef struct {
    char id[AUTOPLAY_ID];
    void *ref;
} ApItem;

typedef struct {
    void (*release)(void *ref);
    char seen[AUTOPLAY_SEEN][AUTOPLAY_ID];
    int seen_next;
    bool has_cur;
    ApItem cur;
    bool paused;
    uint64_t paused_since;
    ApItem wait[AUTOPLAY_WAITING];
    int n;
} Autoplay;

void autoplay_init(Autoplay *ap, void (*release)(void *ref));
// A spoken reply arrived. False when it is a replay or the queue is full; `ref` is released then. `cur` is
// what the player is doing with autoplay_current().
bool autoplay_add(Autoplay *ap, const char *id, void *ref, ApTrack cur, uint64_t now);
// Once a frame (and after an add): the ref to start now, or NULL. Starts the oldest waiting reply once the
// current one has ended. A paused one holds the queue (pause means "wait"), up to AUTOPLAY_HOLD_MS; the user
// picking another track gives the queue up.
void *autoplay_next(Autoplay *ap, ApTrack cur, uint64_t now);
void *autoplay_current(const Autoplay *ap);          // the one started last, while it still counts
void *autoplay_waiting(const Autoplay *ap, int i);   // i-th in line, NULL past the end
bool autoplay_busy(const Autoplay *ap);              // a current or a waiting reply
void autoplay_cancel(Autoplay *ap);                  // drop all of it (a recording starts, the camera opens)
