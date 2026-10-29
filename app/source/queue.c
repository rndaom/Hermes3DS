#include "queue.h"

void queue_init(Queue *q)
{
    LightLock_Init(&q->lock);
    q->head = q->count = 0;
}

bool queue_push(Queue *q, void *item)
{
    bool ok = false;
    LightLock_Lock(&q->lock);
    if (q->count < QUEUE_CAP) {
        q->items[(q->head + q->count) % QUEUE_CAP] = item;
        q->count++;
        ok = true;
    }
    LightLock_Unlock(&q->lock);
    return ok;
}

void *queue_pop(Queue *q)
{
    void *item = NULL;
    LightLock_Lock(&q->lock);
    if (q->count) {
        item = q->items[q->head];
        q->head = (q->head + 1) % QUEUE_CAP;
        q->count--;
    }
    LightLock_Unlock(&q->lock);
    return item;
}

unsigned queue_count(Queue *q)
{
    LightLock_Lock(&q->lock);
    unsigned n = q->count;
    LightLock_Unlock(&q->lock);
    return n;
}

void queue_filter(Queue *q, bool (*drop)(void *item), void (*del)(void *item))
{
    LightLock_Lock(&q->lock);
    unsigned n = 0;
    for (unsigned i = 0; i < q->count; i++) {
        void *it = q->items[(q->head + i) % QUEUE_CAP];
        if (drop(it)) del(it);
        else q->items[(q->head + n++) % QUEUE_CAP] = it;
    }
    q->count = n;
    LightLock_Unlock(&q->lock);
}
