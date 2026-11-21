#!/usr/bin/env python3
"""Shared host-page leases (NVMTL_HOST_PAGES_SHARE=1): wrappers of one surface share an import of its pages. The table
rules (reuse, stale cookie, idle expiry, eviction, busy entries) are proven by tools/test_import_cache.c; these arms prove
the wiring: off by default, keyed by surface and cookie, checked against the mapped address, and every lease given back."""
import pathlib, subprocess, sys, tempfile
root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
vk = (root / 'plugin/nvmtl_vk.c').read_text()
ob = (root / 'plugin/NVMTLObjects.m').read_text()
fails = 0
def check(name, ok):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name); fails += not ok
def func(src, head):
    i = src.find(head)
    return src[i:src.index('\n}\n', i)] if i >= 0 else ''
check('sharing is off unless NVMTL_HOST_PAGES_SHARE=1', "getenv(\"NVMTL_HOST_PAGES_SHARE\"); on = e && e[0] == '1'" in vk)
lease = func(vk, 'static int nvmtl_hp_lease(')
check('a lease is found by surface id, size and cookie', 'nvmtl_ic_acquire(&g_hp_ic, t_hp_key.sid, bytes, t_hp_key.cookie' in lease)
check('a lease is used only when the pages are mapped where the lease was made', 'tmp->map == base' in lease)
check('a moved page range hands the lease back before importing again', 'nvmtl_ic_release(' in lease)
check('a surface with no cookie never leases', 't_hp_key.set' in lease)
check('a very large surface is imported per copy as before', '64u << 20' in lease)
check('the copy uses a lease before the private cache', func(vk, 'static nvk_buffer *nvmtl_host_pages(').index('nvmtl_hp_lease') < func(vk, 'static nvk_buffer *nvmtl_host_pages(').index('cache->buffer.buf'))
done = func(vk, 'static void nvmtl_host_pages_done(')
check('a leased buffer is given back, never destroyed under its other users', 'nvmtl_ic_release(' in done and done.index('nvmtl_ic_release') < done.index('nvmtl_vk_buffer_destroy(hb)'))
check('both host copies name their surface and clear the key afterwards',
      ob.count('nvmtl_vk_host_pages_key(0, 0);') == 2 and
      'const uint32_t sid = IOSurfaceGetID(r->_surf);' in ob and
      'const uint64_t cookie = nvmtl_host_pages_cookie(r->_surf);' in ob and
      'nvmtl_vk_host_pages_key(sid, cookie);' in ob and
      'nvmtl_vk_host_pages_key((uint32_t)IOSurfaceGetID(r->_surf), nvmtl_host_pages_cookie(r->_surf));' in ob)
check('the default path never reads or writes the surface cookie', 'return on ? nvmtl_plane_cookie(s, YES) : 0;' in ob)
# Two imports can be in use on the same thread during a reentrant transfer.
fixture = r'''
#include <assert.h>
#include <pthread.h>
#include "nvmtl_import_cache.h"
#include "nvmtl_host_owner.h"
typedef struct { void *buf; uint8_t host_origin; } nvk_buffer;
static nvmtl_ic g_hp_ic;
static pthread_mutex_t g_hp_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned freed[3];
static uint64_t nvmtl_hp_hot_until_locked(uintptr_t id) { (void)id; return 0; }
static void nvmtl_hp_forget_locked(uintptr_t id) { (void)id; }
static const uint64_t nvmtl_hp_idle_ns=1, nvmtl_hp_burst_ns=1;
static void nvmtl_hp_schedule_locked(uint64_t delay) { (void)delay; }
static void nvmtl_vk_buffer_destroy(nvk_buffer *b) { freed[(uintptr_t)b->buf]++; }
#define clock_gettime_nsec_np(x) 1
'''
cases = r'''
int main(void) {
    nvk_buffer outer={(void *)1,NVMTL_HOST_SURFACE_LEASE}, inner={(void *)2,NVMTL_HOST_SURFACE_LEASE|NVMTL_HOST_SURFACE_READBACK};
    unsigned char old[NVMTL_IC_PAYLOAD]; uintptr_t id;
    assert(nvmtl_ic_insert(&g_hp_ic,1,64,1,1,&outer,sizeof outer,old,&id));
    assert(nvmtl_ic_insert(&g_hp_ic,2,64,2,2,&inner,sizeof inner,old,&id));
    nvmtl_host_pages_done(NULL,&inner);
    nvmtl_host_pages_done(NULL,&outer);
    assert(!freed[1] && !freed[2] && !g_hp_ic.s[0].refs && !g_hp_ic.s[1].refs);
}
'''
source = func(vk, 'static void nvmtl_host_pages_done(') + '\n}\n'
negative = source.replace('(hb->host_origin & ~NVMTL_HOST_SURFACE_READBACK) == NVMTL_HOST_SURFACE_LEASE',
                          'hb->buf == (void *)2')  # only the innermost handle remembered
with tempfile.TemporaryDirectory() as folder:
    c, exe = pathlib.Path(folder) / 'lease.c', pathlib.Path(folder) / 'lease'
    for impl, want in ((source, True), (negative, False)):
        c.write_text(fixture + impl + cases)
        subprocess.run(['cc','-std=c11','-Wall','-Werror','-I',str(root/'plugin'),str(c),'-o',str(exe)], check=True)
        result = subprocess.run([str(exe)], capture_output=True)
        check('nested borrowed transfers release their own leases' if want else 'tracking only the inner lease fails',
              (result.returncode == 0) == want)
# The shared lease has already tried the import when it reports refusal.
# Keep a usable private mapping, retire a stale one, and retry on a subsequent copy.
lookup = r"""
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdlib.h>
#include "nvmtl_host_owner.h"
typedef struct { void *buf, *map; size_t size; int origin; } nvk_buffer;
typedef struct { nvk_buffer buffer; nvmtl_host_alias *alias; void *retirement; } nvmtl_host_page_cache;
static int outcome, privateAttempts, drops, leaseCalls, aliasDrops, aliasTransfers;
static bool allowAlias, refuseAliasImport;
static unsigned char aliasAddress;
static void *lastImport;
static _Atomic uint64_t g_host_pages_kept;
static int nvmtl_hp_lease(void *base, size_t span, nvk_buffer *tmp, uint8_t direction, size_t split_bytes, const void *slice) {
    assert(!split_bytes && !slice); (void)span; (void)direction; leaseCalls++; tmp->map=base; return outcome;
}
static void nvmtl_vk_buffer_destroy(nvk_buffer *b) { assert(b->buf); drops++; }
static uint64_t nvmtl_host_pages_cap(void) { return 256ull << 20; }
static bool nvmtl_surface_cache_alias_on(void) { return true; }
static nvmtl_host_alias *nvmtl_vk_host_alias_create(void *base, size_t bytes) {
    assert(base != &aliasAddress && bytes > 64ull << 20);
    if (!allowAlias) return NULL;
    nvmtl_host_alias *alias=malloc(sizeof *alias); assert(alias);
    *alias=(nvmtl_host_alias){&aliasAddress,bytes}; return alias;
}
static void nvmtl_vk_host_alias_release(void *context) { assert(context); aliasDrops++; free(context); }
static void *nvmtl_vk_reserve_host_retirement(void) { return malloc(1); }
static void nvmtl_vk_destroy_owned_host_buffer(nvk_buffer *b, nvmtl_host_owner owner, void *reserved) {
    assert(b->origin == NVMTL_HOST_SURFACE_CACHE_ALIAS && owner.release && reserved);
    aliasTransfers++; owner.release(owner.context); free(reserved);
}
static int nvmtl_try_surface_import(void *base, size_t size, nvk_buffer *buf, uint8_t direction) {
    (void)direction;
    privateAttempts++; lastImport=base;
    if (base == &aliasAddress && refuseAliasImport) return -1;
    buf->buf=buf->map=base; buf->size=size; return 0;
}
static void nvmtl_vk_note_host_import(nvk_buffer *b,int kind,int owned) {
    assert(owned == (kind == NVMTL_HOST_SURFACE_CACHE_ALIAS)); b->origin=kind;
}
"""
lookup += func(vk,'void nvmtl_vk_host_pages_drop(') + '\n}\n'
lookup += func(vk,'static bool nvmtl_surface_cache_import_alias(') + '\n}\n'
lookup += func(vk,'static nvk_buffer *nvmtl_host_pages(') + '\n}\n'
lookup += r"""
int main(void) {
    int data[2]; nvmtl_host_page_cache cache={.buffer={data,data,sizeof data,0}}; nvk_buffer tmp={0};
    g_host_pages_kept=sizeof data;
    outcome=-1;
    assert(nvmtl_host_pages(&cache,data,sizeof data,&tmp,0)==&cache.buffer && !drops && !privateAttempts);
    assert(!nvmtl_host_pages(&cache,data+1,sizeof(int),&tmp,0) && drops==1 && !cache.buffer.buf && !privateAttempts);
    outcome=1;
    assert(nvmtl_host_pages(NULL,data+1,sizeof(int),&tmp,0)==&tmp && leaseCalls==3 && !privateAttempts);
    outcome=0;
    assert(nvmtl_host_pages(NULL,data,sizeof data,&tmp,0)==&tmp && privateAttempts==1);
    const size_t size=77ull<<20;
    allowAlias=true;
    assert(nvmtl_host_pages(&cache,data,size,&tmp,0)==&cache.buffer);
    assert(cache.alias && cache.retirement && cache.buffer.map==data && lastImport==&aliasAddress);
    assert(g_host_pages_kept==size && privateAttempts==2);
    // An unchanged address and size reuse the alias. Changing address retires its prior owner once.
    assert(nvmtl_host_pages(&cache,data,size,&tmp,0)==&cache.buffer && privateAttempts==2);
    assert(nvmtl_host_pages(&cache,data+1,size,&tmp,0)==&cache.buffer && privateAttempts==3);
    assert(aliasTransfers==1 && aliasDrops==1 && g_host_pages_kept==size);
    nvmtl_vk_host_pages_drop(&cache);
    assert(aliasTransfers==2 && aliasDrops==2 && !g_host_pages_kept && !cache.alias && !cache.retirement);
    refuseAliasImport=true;
    assert(nvmtl_host_pages(&cache,data,size,&tmp,0)==&cache.buffer && privateAttempts==5);
    assert(!cache.alias && !cache.retirement && lastImport==data && aliasDrops==3);
    nvmtl_vk_host_pages_drop(&cache);
    assert(drops==2 && aliasTransfers==2 && !g_host_pages_kept);
    // Readback-only imports still use the original CPU address.
    refuseAliasImport=false;
    assert(nvmtl_host_pages(&cache,data,size,&tmp,NVMTL_HOST_SURFACE_READBACK)==&cache.buffer);
    assert(!cache.alias && lastImport==data && aliasDrops==3);
    nvmtl_vk_host_pages_drop(&cache);
}
"""
with tempfile.TemporaryDirectory() as folder:
    c, exe = pathlib.Path(folder)/'lookup.c', pathlib.Path(folder)/'lookup'
    c.write_text(lookup)
    subprocess.run(['cc','-std=c11','-Wall','-Werror','-I',str(root/'plugin'),str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
check('a refused import keeps only matching private pages and retries on a later transfer', True)
check('owned upload alias keeps its original cache key, retires exactly once and falls through on refusal', True)
# Drive the real acquire/release and asynchronous expiry code with a deterministic
# clock and one scheduled callback. Destruction re-enters acquisition outside the lock.
scheduler = r"""
#include <stdint.h>
#include <stdbool.h>
#include <pthread.h>
#include <assert.h>
#include "nvmtl_import_cache.h"
#include "nvmtl_host_owner.h"
typedef struct { void *buf,*map; uint8_t host_origin; } nvk_buffer;
static nvmtl_ic g_hp_ic;
static pthread_mutex_t g_hp_lock = PTHREAD_MUTEX_INITIALIZER;
static struct { int set; uint32_t sid; uint64_t cookie; } t_hp_key;
#define NSEC_PER_MSEC 1000000ull
#define NSEC_PER_SEC 1000000000ull
#define DISPATCH_TIME_NOW 0
#define QOS_CLASS_UTILITY 21
#define DISPATCH_SOURCE_TYPE_TIMER 1
#define DISPATCH_TIME_FOREVER UINT64_MAX
typedef void *dispatch_source_t;
static int timerToken;
static uint64_t budget=256ull<<20;
static uint64_t nvmtl_host_pages_cap(void) { return budget; }
static uint64_t now=1, due;
static unsigned armed, imports, freed[256];
static bool sharing=true, refuseImport;
static void (*job)(void *);
static void (*onDestroy)(void);
static uint64_t fake_now(void) { return now; }
#define clock_gettime_nsec_np(clock) fake_now()
static uint64_t dispatch_time(int base, int64_t delay) {
    assert(base==DISPATCH_TIME_NOW && delay>0); return now+delay;
}
static int dispatch_get_global_queue(int qos,unsigned flags) {
    assert(qos==QOS_CLASS_UTILITY && !flags); return qos;
}
static void (*handler)(void *);
static dispatch_source_t dispatch_source_create(int type,int x,int y,int queue) {
    assert(type==1 && !x && !y && queue==QOS_CLASS_UTILITY); return &timerToken;
}
static void dispatch_resume(dispatch_source_t t) { assert(t==&timerToken); }
static void dispatch_source_set_event_handler_f(dispatch_source_t t,void (*fn)(void *)) {
    assert(t==&timerToken); handler=fn;
}
static void dispatch_source_set_timer(dispatch_source_t t,uint64_t when,uint64_t repeat,unsigned leeway) {
    assert(t==&timerToken && repeat==UINT64_MAX && !leeway); due=when; job=handler; armed++;
}
static void fire(void) {
    assert(job); if (now<due) now=due; void (*fn)(void *)=job; job=NULL; fn(NULL);
}
static int nvmtl_hp_share_on(void) { return sharing; }
static int nvmtl_try_surface_import(void *base,size_t bytes,nvk_buffer *b,uint8_t direction) {
    (void)direction;
    assert(bytes>0); if(refuseImport) return -1; b->buf=(void *)(uintptr_t)++imports; b->map=base; return 0;
}
static void nvmtl_vk_note_host_import(nvk_buffer *b,uint8_t origin,bool owned) { (void)b;(void)origin;(void)owned; }
static void nvmtl_vk_buffer_destroy(nvk_buffer *b) {
    assert(!pthread_mutex_trylock(&g_hp_lock)); pthread_mutex_unlock(&g_hp_lock);
    assert(++freed[(uintptr_t)b->buf]==1); if(onDestroy) onDestroy();
}
"""
scheduler += vk[vk.index('static const uint64_t nvmtl_hp_idle_ns'):vk.index('static bool nvmtl_surface_cache_alias_on(')]
scheduler += done + '\n}\n'
scheduler += r"""
static nvk_buffer take(unsigned sid,unsigned cookie,uintptr_t base) {
    t_hp_key.set=1; t_hp_key.sid=sid; t_hp_key.cookie=cookie;
    nvk_buffer b={0}; assert(nvmtl_hp_lease((void *)base,4096,&b,0,0,NULL)==1); return b;
}
static void release(nvk_buffer *b) { nvmtl_host_pages_done(NULL,b); }
static void touch(unsigned sid) { nvk_buffer b=take(sid,1,sid); release(&b); }
static void duringDestruction(void) {
    onDestroy=NULL; unsigned n=armed; touch(6);
    assert(imports==6 && armed==n); // one sweep owns the queue through destruction
}
static int split(unsigned sid,unsigned cookie,uintptr_t base,size_t bytes,nvk_buffer *b) {
    t_hp_key.set=cookie!=0; t_hp_key.sid=sid; t_hp_key.cookie=cookie;
    return nvmtl_hp_lease((void *)base,bytes-(bytes%4096),b,0,bytes,(void *)base);
}
static void admission(void) {
    const size_t bytes=29141504;
    nvk_buffer b={0}; unsigned start=imports, before=armed;
    assert(split(0,1,4096,bytes,&b)==NVMTL_HP_COLD && split(1,0,4096,bytes,&b)==NVMTL_HP_COLD);
    for(unsigned i=0;i<2;i++) {
        sharing=false; assert(split(11,1,4096,bytes,&b)==NVMTL_HP_COLD); sharing=true;
        assert(split(11,1,4096,(65u<<20)+16,&b)==NVMTL_HP_COLD);
    }
    // Two sub-page slices can share an interior mapping, even the length. An
    // earlier cold encounter of the first must not admit the other slice.
    t_hp_key.set=1; t_hp_key.sid=501; t_hp_key.cookie=88;
    assert(nvmtl_hp_lease((void *)8192,bytes-2560,&b,0,bytes,(void *)8000)==NVMTL_HP_COLD);
    assert(nvmtl_hp_lease((void *)8192,bytes-2560,&b,0,bytes,(void *)8040)==NVMTL_HP_COLD);
    // Distinct windows rotate through one process. First encounters have no
    // mappings, no cleanup timers and allocate no GPU resources.
    for(unsigned i=0;i<4;i++) assert(split(11+i,1,4096+i*8192,bytes,&b)==NVMTL_HP_COLD);
    assert(imports==start && armed==before && !job);
    for(unsigned i=0;i<4;i++) { assert(split(11+i,1,4096+i*8192,bytes,&b)==1); release(&b); }
    assert(imports==start+4);
    // A matching mapping still alive can be reused after the nominal TTL.
    now+=2200*NSEC_PER_MSEC;
    assert(split(11,1,4096,bytes,&b)==1 && imports==start+4); release(&b);
    while(job) fire(); // really idle: all four mappings have now expired
    assert(split(11,1,4096,bytes,&b)==NVMTL_HP_COLD && imports==start+4);
    assert(split(11,1,4096,bytes,&b)==1 && imports==start+5); release(&b);
    assert(split(11,2,4096,bytes,&b)==NVMTL_HP_COLD); // recycled surface ID
    assert(split(11,2,8192,bytes,&b)==NVMTL_HP_COLD); // moved pages
    assert(split(11,2,8192,bytes+16,&b)==NVMTL_HP_COLD); // size changed inside the same page
    assert(split(11,2,8192,bytes+16,&b)==1 && imports==start+6); release(&b);
    while(job) fire();
    // A scan through more one-shot surfaces than history slots must not pin
    // memory. Evicting a history hint only makes its next visit cold again.
    for(unsigned i=0;i<=NVMTL_IC_SLOTS;i++) {
        now++;
        assert(split(100+i,17,4096,bytes,&b)==NVMTL_HP_COLD);
    }
    assert(split(100,17,4096,bytes,&b)==NVMTL_HP_COLD && !job && imports==start+6);
    // Even a hot candidate should not create a throwaway import when every
    // shared lease slot is in use. Admission resumes once a slot is available.
    for(unsigned i=0;i<NVMTL_IC_SLOTS;i++) { g_hp_ic.s[i].id=500+i; g_hp_ic.s[i].refs=1; }
    assert(split(100,17,4096,bytes,&b)==NVMTL_HP_COLD && imports==start+6);
    memset(&g_hp_ic,0,sizeof g_hp_ic);
    assert(split(100,17,4096,bytes,&b)==1 && imports==start+7); release(&b);
    while(job) fire(); assert(!g_hp_sweep_pending);
}
static nvk_buffer qualify(unsigned sid,size_t bytes) {
    nvk_buffer b={0};
    assert(split(sid,41,sid*4096,bytes,&b)==NVMTL_HP_COLD);
    assert(split(sid,41,sid*4096,bytes,&b)==1);
    return b;
}
static void retention(void) {
    const size_t bytes=29141504;
    nvk_buffer b[6]={0};
    for(unsigned i=0;i<5;i++) { now++; b[i]=qualify(600+i,bytes); if(i!=1)release(&b[i]); }
    // Five imports fit the base cache. The last four alone get an extension.
    const uintptr_t first=(uintptr_t)b[0].buf, live=(uintptr_t)b[1].buf;
    assert(!nvmtl_hp_hot_until_locked(first));
    uint64_t total=0; for(unsigned i=0;i<4;i++) total+=g_hp_hot[i].bytes;
    assert(total==4*(bytes/4096*4096) && total<=(128ull<<20));
    while(job && due<=now+1) fire();
    uint64_t t0=now; now=t0+4*NSEC_PER_SEC;
    // Force the delayed timer to run at 4s. It must drop the displaced oldest
    // idle lease, keep the four chosen ones, and preserve any active reader.
    fire(); assert(freed[first] && !freed[live]);
    for(unsigned i=2;i<5;i++) {
        unsigned prior=imports;
        nvk_buffer reuse={0};
        assert(split(600+i,41,(600+i)*4096,bytes,&reuse)==1 && reuse.buf==b[i].buf && imports==prior);
        release(&reuse);
    }
    // An ordinary import must still expire at ~2s, even when the single timer
    // already targets a retained split much later.
    nvk_buffer normal=take(700,1,700); release(&normal);
    while(job && due <= t0+7*NSEC_PER_SEC) fire();
    assert(freed[(uintptr_t)normal.buf] && !freed[(uintptr_t)b[2].buf] && !freed[live]);
    now=t0+13*NSEC_PER_SEC; // timer delayed beyond hard TTL since last split at t0+4s
    unsigned prior=imports; nvk_buffer retry={0};
    assert(split(602,41,602*4096,bytes,&retry)==NVMTL_HP_COLD && imports==prior);
    while(job) fire();
    assert(freed[(uintptr_t)b[2].buf] && !freed[live]);
    release(&b[1]); while(job) fire(); assert(freed[live] && !job);
    // Two large imports exceed a 40 MiB user budget; only the most recently
    // touched one qualifies. A smaller cap than one mapping protects none.
    budget=40ull<<20;
    for(unsigned i=0;i<2;i++) { now++; b[i]=qualify(710+i,bytes); release(&b[i]); }
    assert(!nvmtl_hp_hot_until_locked((uintptr_t)b[0].buf) && nvmtl_hp_hot_until_locked((uintptr_t)b[1].buf)>now);
    now+=2*NSEC_PER_SEC+NSEC_PER_SEC/2; fire();
    assert(freed[(uintptr_t)b[0].buf] && !freed[(uintptr_t)b[1].buf]);
    // Stale generation loses protection even before expiry.
    nvk_buffer mismatch={0};
    assert(split(711,42,711*4096,bytes,&mismatch)==NVMTL_HP_COLD);
    while(job) fire(); assert(freed[(uintptr_t)b[1].buf] && !g_hp_sweep_pending);
    budget=0; b[0]=qualify(720,bytes); release(&b[0]); assert(!nvmtl_hp_hot_until_locked((uintptr_t)b[0].buf));
    while(job) fire(); assert(freed[(uintptr_t)b[0].buf] && !job);
}
int main(void) {
    nvk_buffer unused={0};
    assert(!nvmtl_hp_lease((void *)1,4096,&unused,0,0,NULL)); // no cookie
    t_hp_key.set=1; sharing=false; assert(!nvmtl_hp_lease((void *)1,4096,&unused,0,0,NULL));
    sharing=true; assert(!nvmtl_hp_lease((void *)1,65u<<20,&unused,0,0,NULL)); assert(!job && !imports);
    refuseImport=true;
    for(int i=0;i<8;i++) assert(nvmtl_hp_lease((void *)1,4096,&unused,0,0,NULL)==-1);
    assert(!job && !armed); refuseImport=false;
    for(unsigned i=1;i<=4;i++) touch(i);
    nvk_buffer held=take(5,1,5);
    assert(armed==1 && imports==5);
    fire(); // keep unexpired, coalesce next deadline
    now=1+2200*NSEC_PER_MSEC;
    touch(1); assert(imports==5 && !freed[4]);
    fire(); // idle-sweep deadline within the first returning burst
    assert(!freed[4] && due==1+2450*NSEC_PER_MSEC);
    now=1+2280*NSEC_PER_MSEC;
    touch(2);touch(3);assert(imports==5);
    onDestroy=duringDestruction;
    fire(); assert(!freed[1] && !freed[2] && !freed[3] && freed[4]==1 && !freed[5] && imports==6);
    while(job) { assert(armed<12); fire(); }
    assert(freed[1] && freed[2] && freed[3] && freed[6] && !freed[5] && !g_hp_sweep_pending);
    release(&held); assert(job); fire(); assert(freed[5] && !job && !g_hp_sweep_pending);
    // Moving a mapping with a live reader makes the old entry unreachable.
    // Reusing its surface id under a new cookie also requires a fresh import.
    nvk_buffer old=take(7,1,100), fresh=take(7,1,200), again=take(7,1,200);
    assert(old.buf!=fresh.buf && again.buf==fresh.buf && !freed[(uintptr_t)old.buf]);
    release(&old); assert(freed[(uintptr_t)old.buf]);
    nvk_buffer changed=take(7,2,200);
    assert(changed.buf!=fresh.buf); release(&fresh); assert(!freed[(uintptr_t)again.buf]);
    release(&again); assert(freed[(uintptr_t)again.buf]); release(&changed);
    while(job) { assert(armed<20); fire(); } assert(freed[(uintptr_t)changed.buf]);
    admission(); retention();
}
"""
with tempfile.TemporaryDirectory() as folder:
    c, exe = pathlib.Path(folder)/'expiry.c', pathlib.Path(folder)/'expiry'
    c.write_text(scheduler)
    subprocess.run(['cc','-std=c11','-Wall','-Werror','-I',str(root/'plugin'),str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
check('a resumed burst reclaims live imports before coalesced expiry; idle cleanup drains and stops', True)
check('split admission distinguishes cold and hot identities, reuses existing mappings and stays bounded', True)
check('only four qualifying handles under the byte cap extend past 2s; ordinary, displaced and expired handles drain',True)
sys.exit(1 if fails else 0)
