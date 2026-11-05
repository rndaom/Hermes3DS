// Host test for source/autoplay.c (the queue of spoken replies).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/autoplay.h"

static int fails;
#define CHECK(c)                                                       \
    do {                                                               \
        if (!(c)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);        \
            fails++;                                                   \
        }                                                              \
    } while (0)

// Refs are pointers into this array; `freed[i]` counts how often item i was released.
#define N 80
static char item[N];
static int freed[N];
#define REF(i) ((void *)&item[i])

static void release(void *ref)
{
    freed[(char *)ref - item]++;
}

static Autoplay ap;
static uint64_t now;

static void fresh(void)
{
    memset(freed, 0, sizeof freed);
    autoplay_init(&ap, release);
    now = 1000;
}

static bool add(int i, ApTrack cur)
{
    char id[AUTOPLAY_ID];
    snprintf(id, sizeof id, "blob%d", i);
    return autoplay_add(&ap, id, REF(i), cur, now);
}

static int released_total(void)
{
    int t = 0;
    for (int i = 0; i < N; i++) t += freed[i];
    return t;
}

static void test_idle_queue_starts_the_first_at_once(void)
{
    fresh();
    CHECK(!autoplay_busy(&ap));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == NULL);
    CHECK(add(0, AP_ENDED));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(0));
    CHECK(autoplay_current(&ap) == REF(0));
    CHECK(autoplay_busy(&ap));
    CHECK(autoplay_next(&ap, AP_PLAYING, now) == NULL);
    CHECK(released_total() == 0);
}

static void test_replies_wait_their_turn_in_order(void)
{
    fresh();
    add(0, AP_ENDED);
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(0));
    CHECK(add(1, AP_PLAYING));
    CHECK(add(2, AP_PLAYING));
    CHECK(add(3, AP_PLAYING));
    CHECK(autoplay_waiting(&ap, 0) == REF(1) && autoplay_waiting(&ap, 2) == REF(3));
    CHECK(autoplay_waiting(&ap, 3) == NULL && autoplay_waiting(&ap, -1) == NULL);
    CHECK(autoplay_next(&ap, AP_PLAYING, now) == NULL);  // still playing the first
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(1));  // it ended: the oldest waiting goes next
    CHECK(freed[0] == 1 && freed[1] == 0);               // the finished one is let go
    CHECK(autoplay_next(&ap, AP_PLAYING, now) == NULL);
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(2));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(3));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == NULL);  // the last one ended: idle
    CHECK(!autoplay_busy(&ap));
    CHECK(freed[3] == 1);
}

static void test_adding_after_the_current_ended_keeps_the_order(void)
{
    fresh();
    add(0, AP_ENDED);
    autoplay_next(&ap, AP_ENDED, now);
    add(1, AP_PLAYING);
    // 0 ended and 2 arrives before the next frame's tick: 1 still goes first.
    CHECK(add(2, AP_ENDED));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(1));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(2));
}

static void test_a_replayed_message_is_not_spoken_twice(void)
{
    fresh();
    CHECK(add(0, AP_ENDED));
    CHECK(!add(0, AP_ENDED));  // same id while waiting
    CHECK(freed[0] == 1);      // the duplicate's ref was handed back (the queued one holds the same pointer here)
    freed[0] = 0;
    autoplay_next(&ap, AP_ENDED, now);
    autoplay_next(&ap, AP_ENDED, now);  // played and over
    CHECK(!add(0, AP_ENDED));           // replayed after a reconnect
    CHECK(autoplay_next(&ap, AP_ENDED, now) == NULL);
}

static void test_the_seen_list_forgets_the_oldest(void)
{
    fresh();
    for (int i = 0; i < AUTOPLAY_SEEN; i++) {
        CHECK(add(i, AP_ENDED));
        autoplay_next(&ap, AP_ENDED, now);  // played and over
    }
    CHECK(!add(AUTOPLAY_SEEN - 1, AP_ENDED));
    CHECK(!add(0, AP_ENDED));
    CHECK(add(AUTOPLAY_SEEN, AP_ENDED));  // pushes id 0 out
    CHECK(add(0, AP_ENDED));
}

static void test_a_paused_reply_holds_the_queue(void)
{
    fresh();
    add(0, AP_ENDED);
    autoplay_next(&ap, AP_ENDED, now);
    add(1, AP_PLAYING);
    now += 5000;
    CHECK(autoplay_next(&ap, AP_PAUSED, now) == NULL);
    now += AUTOPLAY_HOLD_MS - 1;
    CHECK(autoplay_next(&ap, AP_PAUSED, now) == NULL);  // still held
    CHECK(add(2, AP_PAUSED));                           // a new part queues behind
    CHECK(released_total() == 0);
    CHECK(autoplay_next(&ap, AP_PLAYING, now) == NULL);  // resumed
    now += AUTOPLAY_HOLD_MS * 3;
    CHECK(autoplay_next(&ap, AP_PLAYING, now) == NULL);  // playing again: the clock restarted
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(1));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(2));
}

static void test_a_reply_paused_too_long_is_given_up(void)
{
    fresh();
    add(0, AP_ENDED);
    autoplay_next(&ap, AP_ENDED, now);
    add(1, AP_PLAYING);
    add(2, AP_PLAYING);
    CHECK(autoplay_next(&ap, AP_PAUSED, now) == NULL);
    now += AUTOPLAY_HOLD_MS;
    CHECK(autoplay_next(&ap, AP_PAUSED, now) == NULL);
    CHECK(!autoplay_busy(&ap));
    CHECK(freed[0] == 1 && freed[1] == 1 && freed[2] == 1);
    // The next reply starts at once, nothing stale ahead of it.
    CHECK(add(3, AP_ENDED));
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(3));
}

static void test_a_new_reply_arriving_after_a_stale_pause_is_not_swallowed(void)
{
    fresh();
    add(0, AP_ENDED);
    autoplay_next(&ap, AP_ENDED, now);
    autoplay_next(&ap, AP_PAUSED, now);
    now += AUTOPLAY_HOLD_MS + 10;
    CHECK(add(1, AP_PAUSED));  // the paused one is let go first, then this one joins
    CHECK(freed[0] == 1 && freed[1] == 0);
    CHECK(autoplay_next(&ap, AP_ENDED, now) == REF(1));
}

static void test_the_user_picking_another_track_gives_the_queue_up(void)
{
    fresh();
    add(0, AP_ENDED);
    autoplay_next(&ap, AP_ENDED, now);
    add(1, AP_PLAYING);
    add(2, AP_PLAYING);
    CHECK(autoplay_next(&ap, AP_REPLACED, now) == NULL);
    CHECK(!autoplay_busy(&ap));
    CHECK(freed[0] == 1 && freed[1] == 1 && freed[2] == 1);
}

static void test_cancel_releases_everything_once(void)
{
    fresh();
    add(0, AP_ENDED);
    autoplay_next(&ap, AP_ENDED, now);
    for (int i = 1; i < 5; i++) add(i, AP_PLAYING);
    autoplay_cancel(&ap);
    autoplay_cancel(&ap);
    for (int i = 0; i < 5; i++) CHECK(freed[i] == 1);
    CHECK(!autoplay_busy(&ap) && autoplay_current(&ap) == NULL);
    CHECK(!add(0, AP_ENDED));  // cancelled replies still count as seen
    CHECK(add(5, AP_ENDED));
}

static void test_a_full_queue_refuses_more_and_releases_them(void)
{
    fresh();
    add(0, AP_ENDED);
    autoplay_next(&ap, AP_ENDED, now);
    for (int i = 1; i <= AUTOPLAY_WAITING; i++) CHECK(add(i, AP_PLAYING));
    CHECK(!add(AUTOPLAY_WAITING + 1, AP_PLAYING));
    CHECK(freed[AUTOPLAY_WAITING + 1] == 1);
    for (int i = 1; i <= AUTOPLAY_WAITING; i++) {
        void *r = autoplay_next(&ap, AP_ENDED, now);
        CHECK(r == REF(i));
    }
    autoplay_cancel(&ap);
    for (int i = 0; i <= AUTOPLAY_WAITING; i++) CHECK(freed[i] == 1);
}

static void test_a_long_id_is_cut_not_overrun(void)
{
    fresh();
    char id[200];
    memset(id, 'a', sizeof id - 1);
    id[sizeof id - 1] = 0;
    CHECK(autoplay_add(&ap, id, REF(0), AP_ENDED, now));
    CHECK(!autoplay_add(&ap, id, REF(1), AP_ENDED, now));
    CHECK(!autoplay_add(&ap, "", REF(2), AP_ENDED, now));  // no id: refused, ref handed back
    CHECK(freed[2] == 1);
}

int main(void)
{
    test_idle_queue_starts_the_first_at_once();
    test_replies_wait_their_turn_in_order();
    test_adding_after_the_current_ended_keeps_the_order();
    test_a_replayed_message_is_not_spoken_twice();
    test_the_seen_list_forgets_the_oldest();
    test_a_paused_reply_holds_the_queue();
    test_a_reply_paused_too_long_is_given_up();
    test_a_new_reply_arriving_after_a_stale_pause_is_not_swallowed();
    test_the_user_picking_another_track_gives_the_queue_up();
    test_cancel_releases_everything_once();
    test_a_full_queue_refuses_more_and_releases_them();
    test_a_long_id_is_cut_not_overrun();
    if (fails) {
        printf("autoplay: %d check(s) failed\n", fails);
        return 1;
    }
    printf("autoplay: ok\n");
    return 0;
}
