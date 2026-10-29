// Fixed-size FIFO of pointers between the network worker and the UI thread (LightLock-protected).
#pragma once

#include <3ds.h>
#include <stdbool.h>

#define QUEUE_CAP 256

typedef struct {
    LightLock lock;
    void *items[QUEUE_CAP];
    unsigned head, count;
} Queue;

void queue_init(Queue *q);
bool queue_push(Queue *q, void *item);  // false when full
void *queue_pop(Queue *q);              // NULL when empty
unsigned queue_count(Queue *q);
// Drop the items `drop` says yes to (each passed to `del`), keeping the rest in order, under the lock.
void queue_filter(Queue *q, bool (*drop)(void *item), void (*del)(void *item));
