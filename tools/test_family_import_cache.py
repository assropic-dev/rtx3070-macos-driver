#!/usr/bin/env python3
"""Test the production family-import cache with controlled kernel generations on macOS."""
from pathlib import Path
import os
import subprocess
import tempfile

repo = Path(__file__).resolve().parents[1]
source = (repo / 'plugin/NVMTLDevice.m').read_text()
owner = source[source.index('@interface NVMTLFamilyImport'):source.index('@implementation NVMTLDevice\n')]
cache = source[source.index('// Shared family-VRAM imports'):source.index('// VT publishes')]

stubs = r'''
#import <Foundation/Foundation.h>
#include <stdatomic.h>
#include <assert.h>
#include <sys/sysctl.h>
#include <unistd.h>
#include "../kexts/NVRM/nvrm_surfshare_abi.h"
#include "nvmtl_import_cache.h"

@interface FakeSurface : NSObject {
    @public uint32_t sid;
    size_t rows, pitch, alloc;
}
@end
@implementation FakeSurface
@end
static FakeSurface *make_surface(size_t rows, size_t pitch) {
    static uint32_t next;
    FakeSurface *s = [FakeSurface new];
    s->sid = ++next; s->rows = rows; s->pitch = pitch; s->alloc = rows * pitch;
    return s;
}
#define VALUE(s, field) (((__bridge FakeSurface *)(s))->field)
#define IOSurfaceGetID(s) VALUE(s, sid)
#define IOSurfaceGetHeight(s) VALUE(s, rows)
#define IOSurfaceGetBytesPerRow(s) VALUE(s, pitch)
#define IOSurfaceGetAllocSize(s) VALUE(s, alloc)
#define IOSurfaceGetPlaneCount(s) (0)
static void nvlog(const char *format, ...) { (void)format; }

typedef struct { void *buf; size_t span; uint64_t backing; } nvk_buffer;
static _Atomic uint64_t created, destroyed, epochs[3];
static uintptr_t device = 1;
static bool cookies, change_before_export;
static uint64_t cookie_base = 1;
static bool available = true, rebind_on_import, rebind_before_validation;
static unsigned cookie_queries;
static size_t kernel_max = SIZE_MAX;
static int read_mib(const char *name, int *out, size_t *n) {
    out[0] = strstr(name, "grants") ? 0 : strstr(name, "releases") ? 1 : 2; *n = 1; return 0;
}
static int read_counter(int *mib, unsigned len, void *out, size_t *n, void *unused, size_t zero) {
    if (!available) return -1;
    if (rebind_before_validation) { rebind_before_validation = false; epochs[0]++; }
    *(uint64_t *)out = epochs[mib[0]]; *n = sizeof(uint64_t); return 0;
}
#define sysctl read_counter
#define sysctlnametomib read_mib
static uintptr_t nvmtl_vk_import_device(void) { return device; }
static uint32_t nvmtl_vk_import_cookie(uint32_t sid) {
    if (!cookies) return NVRM_SS_NO_CACHE;
    if (!sid) return NVRM_SS_PLANE_COOKIE;
    cookie_queries++;
    if (rebind_before_validation) { rebind_before_validation = false; cookie_base++; }
    if (!available || cookie_base + sid > NVRM_SS_COOKIE_MAX) return NVRM_SS_NO_VRAM;
    return (uint32_t)(cookie_base + sid);
}
static uint64_t nvmtl_vk_vram_bytes(void) { return 8ull << 30; }
static uint64_t card_usage;
static uint64_t nvmtl_vk_family_cache_excess(void) {
    return nvmtl_ic_pressure_excess(card_usage, nvmtl_vk_vram_bytes());
}
static int nvmtl_vk_surface_vram(uint32_t sid, uint32_t plane, size_t need, nvk_buffer *out) {
    if (need > kernel_max) return -1;
    if (change_before_export) { change_before_export = false; cookie_base++; }
    if (plane && plane != (NVRM_SS_PLANE_COOKIE | nvmtl_vk_import_cookie(sid))) return -1;
    out->buf = (void *)(uintptr_t)(atomic_fetch_add(&created, 1) + 1); out->span = need;
    out->backing = cookies ? cookie_base + sid : epochs[2];
    if (rebind_on_import) { rebind_on_import = false; epochs[2]++; cookie_base++; }
    return 0;
}
static void nvmtl_vk_buffer_destroy(nvk_buffer *b) {
    assert(b->buf); b->buf = NULL; destroyed++;
}
static CFTypeRef retain_after_validation(CFTypeRef value);
#define CFRetain(value) retain_after_validation(value)
'''

checks = r'''
static NVMTLFamilyImport *get(FakeSurface *s, size_t bytes, nvk_buffer *buf) {
    NVMTLFamilyImport *owner = nil;
    assert(!nvmtl_ic_get((__bridge IOSurfaceRef)s, 0, bytes, buf, &owner));
    return owner;
}
#undef CFRetain
static bool publish_replacement;
static NVMTLFamilyImport *winner;
static CFTypeRef retain_after_validation(CFTypeRef value) {
    // Interleave a second importer between the first importer's final query and insert lock.
    if (publish_replacement) {
        publish_replacement = false;
        cookie_base++; epochs[2]++;
        nvk_buffer b; winner = get((__bridge FakeSurface *)value, 64, &b);
        assert(winner);
    }
    return CFRetain(value);
}
static void identity_and_epochs(void) {
    FakeSurface *s = make_surface(20, 128);
    nvk_buffer a, b;
    NVMTLFamilyImport *old = get(s, 64, &a);
    assert(old && a.span == 20 * 128 && get(s, 1280, &b) == old && b.buf == a.buf);
    for (unsigned i = 0; i < 3; i++) {
        epochs[i]++;
        if (cookies) {
            assert(get(s, 64, &b) == old && b.buf == a.buf); // unrelated lifecycle event
            cookie_base++;
        }
        NVMTLFamilyImport *fresh = get(s, 64, &b);
        assert(fresh && old != fresh && a.buf != b.buf && old->buffer.buf == a.buf);
        old = fresh; a = b;
    }
    device++;
    NVMTLFamilyImport *new_device = get(s, 64, &b);
    assert(new_device && new_device != old && a.buf != b.buf);
    available = false;
    assert(!get(s, 64, &a) && a.buf != b.buf); nvmtl_vk_buffer_destroy(&a);
    available = true; rebind_before_validation = true;
    NVMTLFamilyImport *rebound = get(s, 64, &a);
    assert(rebound && rebound != new_device && a.buf != b.buf);
    FakeSurface *other = make_surface(16, 128);
    rebind_on_import = true;
    assert(!get(other, 64, &b) && b.backing == (cookies ? cookie_base + other->sid : epochs[2]) && b.span == 64);
    nvmtl_vk_buffer_destroy(&b);
    if (cookies) {
        change_before_export = true;
        assert(!get(other, other->alloc, &b) && b.backing == cookie_base + other->sid);
        nvmtl_vk_buffer_destroy(&b);
        uint64_t saved = cookie_base; cookie_base = UINT64_C(1) << 32;
        assert(!get(other, 64, &b) && b.backing == cookie_base + other->sid);
        nvmtl_vk_buffer_destroy(&b); cookie_base = saved + 1;
    }
    kernel_max = 512;
    assert(!get(other, 128, &b) && b.span == 128); nvmtl_vk_buffer_destroy(&b);
    kernel_max = SIZE_MAX;
    FakeSurface *raced = make_surface(8, 128);
    publish_replacement = true;
    assert(!get(raced, 64, &b) && b.backing == (cookies ? cookie_base + raced->sid : epochs[2]));
    nvmtl_vk_buffer_destroy(&b);
    assert(get(raced, 64, &b) == winner);
    winner = nil;
}
static void active_and_budget(void) {
    // Hold all 132 cold clients. Six new hot surfaces must still enter the cache.
    NSMutableArray *held = [NSMutableArray array]; nvk_buffer b;
    for (unsigned i = 0; i < 132; i++)
        [held addObject:get(make_surface(1, 64), 64, &b)];
    NSMutableArray *hot = [NSMutableArray array];
    for (unsigned i = 0; i < 6; i++) {
        FakeSurface *s = make_surface(1, 64); [hot addObject:s];
        assert(get(s, 64, &b));
    }
    uint64_t before = created;
    for (unsigned round = 0; round < 8; round++) for (FakeSurface *s in hot) assert(get(s, 64, &b));
    assert(created == before);
    for (NVMTLFamilyImport *live in held) assert(live->buffer.buf);
    [hot removeAllObjects];
    for (unsigned i = 0; i < 6; i++) {
        FakeSurface *s = make_surface(2880, 5120 * 4); [hot addObject:s];
        assert(get(s, s->alloc, &b));
    }
    before = created;
    const uint64_t budget = nvmtl_ic_budget();
    assert(budget == strtoull(getenv("NVMTL_SURF_IMPORT_CACHE_BUDGET_MB"), NULL, 10) << 20);
    const unsigned probes = cookie_queries;
    for (unsigned j = 0; j < 3; j++) {
        if (cookies) epochs[0]++; // unrelated backing allocation on every round
        for (FakeSurface *s in hot) assert(get(s, s->alloc, &b));
    }
    const bool fits = budget >= 6 * 5120ull * 2880 * 4;
    assert(created - before == (fits ? 0 : 18));
    if (cookies && fits) assert(cookie_queries - probes == 18);
    uint64_t stats[6]; nvmtl_surface_import_stats(stats);
    assert(stats[4] >= 3 * 5120ull * 2880 * 4 && stats[4] <= budget && stats[5] <= 128);
}
static void idle_timer(void) {
    __weak FakeSurface *retained;
    @autoreleasepool {
        FakeSurface *s = make_surface(32, 32); retained = s; nvk_buffer b;
        assert(get(s, 512, &b));
    }
    assert(retained);
    for (unsigned i = 0; i < 500; i++) {
        uint64_t stats[6]; nvmtl_surface_import_stats(stats);
        if (!stats[4] && !stats[5] && atomic_load(&destroyed) == atomic_load(&created)) break;
        usleep(10000);
    }
    assert(!retained);
    uint64_t stats[6]; nvmtl_surface_import_stats(stats);
    assert(!stats[4] && !stats[5] && destroyed == created);
    assert(stats[0] && stats[1] && stats[2] && stats[3]);
}
static void pressure_policy(void) {
    assert(nvmtl_ic_keep_warm());
    const uint64_t threshold = (8ull << 30) * 4 / 5;
    assert(!nvmtl_ic_pressure_excess(threshold, 8ull << 30));
    assert(nvmtl_ic_pressure_excess(threshold + 1, 8ull << 30) == 1);
    FakeSurface *surfaces[10]; nvk_buffer buffers[10], next;
    __attribute__((objc_precise_lifetime)) NVMTLFamilyImport *first = nil;
    for (unsigned i = 0; i < 10; i++) {
        surfaces[i] = make_surface(2880, 5120 * 4);
        NVMTLFamilyImport *owner = get(surfaces[i], surfaces[i]->alloc, &buffers[i]);
        assert(owner);
        if (!i) first = owner;
    }
    uint64_t stats[6];
    card_usage = threshold;
    nvmtl_ic_cleanup(); nvmtl_surface_import_stats(stats);
    assert(stats[5] == 10);
    card_usage += 64ull << 20;
    nvmtl_ic_cleanup(); nvmtl_surface_import_stats(stats);
    assert(stats[5] == 8 && first->buffer.buf == buffers[0].buf);
    assert(get(surfaces[2], surfaces[2]->alloc, &next) && next.buf == buffers[2].buf);
    card_usage = threshold + (1ull << 30);
    nvmtl_ic_cleanup(); nvmtl_surface_import_stats(stats);
    assert(stats[5] == 3); // At most a 256 MiB target per tick, rounded to whole surfaces.
    card_usage = threshold;
    nvmtl_ic_cleanup(); nvmtl_surface_import_stats(stats);
    assert(stats[5] == 3);
    assert(get(surfaces[0], surfaces[0]->alloc, &next) != first && next.buf != buffers[0].buf);
}
static void keep_existing_at_capacity(void) {
    assert(nvmtl_ic_keep_warm());
    FakeSurface *first = make_surface(2880, 5120 * 4);
    nvk_buffer original, next;
    assert(get(first, first->alloc, &original));
    for (unsigned i = 0; i < 5; i++) {
        FakeSurface *s = make_surface(2880, 5120 * 4);
        assert(get(s, s->alloc, &next));
    }
    uint64_t stats[6]; nvmtl_surface_import_stats(stats);
    assert(stats[5] == 4);
    assert(get(first, first->alloc, &next) && next.buf == original.buf);
    for (unsigned i = 0; i < NVMTL_IC_SLOTS; i++) assert(get(make_surface(1, 64), 64, &next));
    nvmtl_surface_import_stats(stats);
    assert(stats[5] == NVMTL_IC_SLOTS);
    assert(get(first, first->alloc, &next) && next.buf == original.buf);
}
int main(void) {
    cookies = getenv("TEST_COOKIES") != NULL;
    if (getenv("TEST_WS_DEFAULT")) {
        setprogname("WindowServer");
        assert(nvmtl_ic_keep_warm() && nvmtl_ic_budget() == (8ull << 30));
        return 0;
    }
    if (getenv("TEST_PRESSURE_POLICY")) { @autoreleasepool { pressure_policy(); } return 0; }
    if (getenv("TEST_WARM_CAPACITY")) { @autoreleasepool { keep_existing_at_capacity(); } return 0; }
    @autoreleasepool { identity_and_epochs(); active_and_budget(); }
    idle_timer();
    puts("family-import cache: retained clients, generation races, multi-view, 5K budget and idle reclamation passed");
}
'''

with tempfile.TemporaryDirectory() as d:
    src, binary = Path(d) / 'family.m', Path(d) / 'test'
    src.write_text(stubs + owner + cache + checks)
    subprocess.run(['xcrun', 'clang', '-std=gnu11', '-O1', '-fobjc-arc', '-Wall',
                    '-Werror', '-I', str(repo / 'plugin'), '-framework', 'Foundation',
                    str(src), '-o', str(binary)], check=True)
    env = dict(os.environ, NVMTL_SURF_IMPORT_CACHE='1', NVMTL_SURF_IMPORT_CACHE_IDLE_MS='100',
               NVMTL_SURF_IMPORT_CACHE_MAX_MB='64', NVMTL_SURF_IMPORT_CACHE_KEEP='0')
    for budget_mb in ('256', '512', '8192'):
        environment = dict(env, NVMTL_SURF_IMPORT_CACHE_BUDGET_MB=budget_mb)
        environment.pop('TEST_COOKIES', None)
        subprocess.run([str(binary)], check=True, env=environment)
        subprocess.run([str(binary)], check=True, env=dict(environment, TEST_COOKIES='1'))
    default = {key: value for key, value in env.items() if not key.startswith('NVMTL_SURF_IMPORT_CACHE')}
    subprocess.run([str(binary)], check=True, env=dict(default, TEST_WS_DEFAULT='1'))
    warm = dict(env, TEST_COOKIES='1', NVMTL_SURF_IMPORT_CACHE_KEEP='1', NVMTL_SURF_IMPORT_CACHE_IDLE_MS='10000')
    subprocess.run([str(binary)], check=True, env=dict(warm, TEST_PRESSURE_POLICY='1', NVMTL_SURF_IMPORT_CACHE_BUDGET_MB='8192'))
    subprocess.run([str(binary)], check=True, env=dict(warm, TEST_WARM_CAPACITY='1', NVMTL_SURF_IMPORT_CACHE_BUDGET_MB='256'))
    print('WindowServer 8 GiB default, 80% threshold, partial LRU trim and full-cache admission passed')
