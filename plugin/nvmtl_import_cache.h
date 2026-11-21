/* Short-lived import lookup table shared by the host-pages and family-VRAM paths.
   The caller supplies the lock, clock, handle identity and backing generation.
   The host-pages caller leases table slots while using their buffers. The family-VRAM
   caller leases reference-counted owners independently of their lookup slots. */
#ifndef NVMTL_IMPORT_CACHE_H
#define NVMTL_IMPORT_CACHE_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define NVMTL_IC_SLOTS 128
#define NVMTL_IC_PAYLOAD 160
typedef struct {
    uint32_t sid; size_t need; uint64_t cookie;
    uintptr_t id;                       /* identity of the handle (its buffer pointer); 0 = slot free */
    unsigned char payload[NVMTL_IC_PAYLOAD];
    int refs, stale; uint64_t idle_since;
} nvmtl_ic_slot;
typedef struct { nvmtl_ic_slot s[NVMTL_IC_SLOTS]; } nvmtl_ic;

/* Whole-card VRAM above 80%. Round the threshold down to whole bytes. */
static inline uint64_t nvmtl_ic_pressure_excess(uint64_t used, uint64_t total)
{
    const uint64_t threshold = total - total / 5 - (total % 5 != 0);
    return total && used > threshold ? used - threshold : 0;
}

/* Remove the least recently released idle entry, skipping imports still held
   by textures. Also used by the family-VRAM cache's aggregate byte limit. */
static inline int nvmtl_ic_evict_idle(nvmtl_ic *c, void *removed)
{
    int slot = -1;
    for (int i = 0; i < NVMTL_IC_SLOTS; i++) {
        nvmtl_ic_slot *e = &c->s[i];
        if (e->id && !e->refs && (slot < 0 || e->idle_since < c->s[slot].idle_since)) slot = i;
    }
    if (slot < 0) return 0;
    memcpy(removed, c->s[slot].payload, NVMTL_IC_PAYLOAD);
    memset(&c->s[slot], 0, sizeof c->s[slot]);
    return 1;
}

/* A live entry for this surface and size whose generation matches: its refcount goes up and its payload is copied
   out (1). An entry whose generation no longer matches is marked stale, so it is never
   handed out again and goes when its last wrapper does. 0 = nothing usable. */
static inline int nvmtl_ic_acquire(nvmtl_ic *c, uint32_t sid, size_t need, uint64_t cookie, void *payload, size_t n)
{
    for (int i = 0; i < NVMTL_IC_SLOTS; i++) {
        nvmtl_ic_slot *e = &c->s[i];
        if (!e->id || e->sid != sid || e->need != need || e->stale) continue;
        if (e->cookie != cookie) { e->stale = 1; continue; }
        e->refs++; memcpy(payload, e->payload, n < NVMTL_IC_PAYLOAD ? n : NVMTL_IC_PAYLOAD);
        return 1;
    }
    return 0;
}
/* Adds a fresh import with one wrapper holding it. A full table gives up the idle entry unused for longest, which is
   copied to `evicted` (its id is returned through *evicted_id, non-zero) for the caller to destroy outside its lock.
   Pass NULL for `evicted` to keep all existing entries. Returns 1 when the import is cached, 0 when it is not. */
static inline int nvmtl_ic_insert(nvmtl_ic *c, uint32_t sid, size_t need, uint64_t cookie, uintptr_t id,
                                  const void *payload, size_t n, void *evicted, uintptr_t *evicted_id)
{
    int slot = -1; *evicted_id = 0;
    if (!id) return 0;
    for (int i = 0; i < NVMTL_IC_SLOTS && slot < 0; i++) if (!c->s[i].id) slot = i;
    if (slot < 0) {
        if (!evicted) return 0;
        uint64_t oldest = UINT64_MAX;
        for (int i = 0; i < NVMTL_IC_SLOTS; i++) if (!c->s[i].refs && c->s[i].idle_since < oldest) { oldest = c->s[i].idle_since; slot = i; }
        if (slot < 0) return 0;
        *evicted_id = c->s[slot].id; memcpy(evicted, c->s[slot].payload, NVMTL_IC_PAYLOAD);
    }
    nvmtl_ic_slot *e = &c->s[slot];
    memset(e, 0, sizeof *e);
    e->sid = sid; e->need = need; e->cookie = cookie; e->id = id; e->refs = 1;
    memcpy(e->payload, payload, n < NVMTL_IC_PAYLOAD ? n : NVMTL_IC_PAYLOAD);
    return 1;
}
/* A wrapper lets go of the import with identity `id`. 0 = the cache does not know it (the caller destroys it).
   1 = the cache keeps it. 2 = it was stale and this was its last wrapper: copied to `gone`, the caller destroys it. */
static inline int nvmtl_ic_release(nvmtl_ic *c, uintptr_t id, uint64_t now, void *gone)
{
    for (int i = 0; i < NVMTL_IC_SLOTS; i++) {
        nvmtl_ic_slot *e = &c->s[i];
        if (!e->id || e->id != id) continue;
        if (e->refs > 0) e->refs--;
        if (e->refs) return 1;
        e->idle_since = now;
        if (!e->stale) return 1;
        memcpy(gone, e->payload, NVMTL_IC_PAYLOAD); memset(e, 0, sizeof *e);
        return 2;
    }
    return 0;
}
/* Entries with no wrapper that have been idle longer than idle_ns (every idle one when `all`, and every stale one) are
   removed; their payloads go to out[0..max) for the caller to destroy outside its lock. Returns how many. */
static inline int nvmtl_ic_expire(nvmtl_ic *c, uint64_t now, uint64_t idle_ns, int all, unsigned char (*out)[NVMTL_IC_PAYLOAD], int max)
{
    int n = 0;
    for (int i = 0; i < NVMTL_IC_SLOTS && n < max; i++) {
        nvmtl_ic_slot *e = &c->s[i];
        if (!e->id || e->refs) continue;
        if (!(all || e->stale || (now >= e->idle_since && now - e->idle_since > idle_ns))) continue;
        memcpy(out[n++], e->payload, NVMTL_IC_PAYLOAD); memset(e, 0, sizeof *e);
    }
    return n;
}
#endif
