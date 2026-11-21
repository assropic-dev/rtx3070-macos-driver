#!/usr/bin/env python3
"""Check the exact WindowServer fence publication path with an in-memory mark transport."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'plugin/NVMTLEventSync.m').read_text()

def excerpt(start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]

fixture = r'''
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#include <assert.h>
#include <stdatomic.h>
#include <stdarg.h>
typedef struct { uint64_t host_before_ns, host_after_ns, host_wait_begin_ns; } nvmtl_gpu_interval;
typedef struct { nvmtl_gpu_interval timing; uint64_t fx_seq; } Commands;
static uint64_t testGpuEnd;
static char statsLog[512], releaseLog[512];
static int nvmtl_vk_interval_seconds(const nvmtl_gpu_interval *time, double *start, double *end) {
    *start = 0; *end = testGpuEnd / 1e9; return 0;
}
static void nvlog(const char *fmt, ...) { (void)fmt; }
static void nvlog_every(const char *fmt, ...) {
    char *out = strstr(fmt, "fence stats:") ? statsLog : releaseLog;
    va_list args; va_start(args, fmt); vsnprintf(out, 512, fmt, args); va_end(args);
}
static uint32_t IOSurfaceGetID(const uint32_t *surface) { return *surface; }
static NSMutableArray<NSString *> *events;
static BOOL nvmtl_ws_mark(uint32_t sid, BOOL pending) {
    assert(!pending);
    [events addObject:[NSString stringWithFormat:@"ready:%u", sid]];
    return YES;
}
static const void *const nvmtl_ws_marks_key = &nvmtl_ws_marks_key;
@interface NVMTLTexture : NSObject {
@public
    const uint32_t *_surf;
    NVMTLTexture *_parent;
    BOOL _vramOn;
    NSString *label;
}
- (BOOL)nvmtlSurfaceOut;
- (BOOL)nvmtlSurfaceOutDirtied:(BOOL)dirtied;
@end
@implementation NVMTLTexture
- (BOOL)nvmtlSurfaceOut { assert(!"private publication is not allowed before retirement"); return NO; }
- (BOOL)nvmtlSurfaceOutDirtied:(BOOL)dirtied { assert(dirtied); [events addObject:label]; return YES; }
@end
@interface NVMTLSubmissionPart : NSObject { @public NSSet<NVMTLTexture *> *surfaces; Commands commands; } @end
@implementation NVMTLSubmissionPart @end
@interface NVMTLCommandBuffer : NSObject { @public NSArray * _parts; } @end
@implementation NVMTLCommandBuffer @end
'''

checks = r'''
static NVMTLTexture *texture(const uint32_t *sid, NSString *label) {
    NVMTLTexture *t = [NVMTLTexture new]; t->_surf = sid; t->_vramOn = YES; t->label = label; return t;
}
static NVMTLSubmissionPart *partWithTexture(NVMTLTexture *t) {
    NVMTLSubmissionPart *part = [NVMTLSubmissionPart new]; part->surfaces = [NSSet setWithObject:t]; return part;
}
static NVMTLSurfaceHolds *hold(NVMTLCommandBuffer *cb, uint32_t sid, NVMTLTexture *t) {
    NVMTLSurfaceHolds *hold = [NVMTLSurfaceHolds new];
    hold->ids = [NSMutableSet setWithObject:@(sid)]; hold->dirtied = [NSSet setWithObject:t]; hold->early = YES;
    hold->markedAt = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    objc_setAssociatedObject(cb, nvmtl_ws_marks_key, hold, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    nvmtl_ws_pending_holds = [NSCountedSet setWithObject:@(sid)];
    nvmtl_ws_deferred = nil;
    return hold;
}
int main(void) { @autoreleasepool {
    setenv("NVMTL_WS_FENCE_STATS", "1", 1);
    events = [NSMutableArray new];
    uint32_t sid = 17;
    NVMTLTexture *old = texture(&sid, @"old");
    NVMTLTexture *current = texture(&sid, @"current");
    NVMTLCommandBuffer *older = [NVMTLCommandBuffer new], *cb = [NVMTLCommandBuffer new];
    NVMTLSubmissionPart *part = partWithTexture(current);
    cb->_parts = @[part];

    NVMTLSurfaceHolds *holds = hold(cb, sid, current);
    nvmtl_ws_defer(older, current, YES, sid);
    nvmtl_ws_defer(older, old, YES, sid);
    nvmtl_ws_fence_part fence = { .cb = cb };
    nvmtl_ws_fence_landed(&fence);
    assert(fence.landed && fence.published && !strcmp(fence.path, "at-fence-deferred"));
    assert([events isEqualToArray:(@[@"ready:17", @"old", @"current"])]);
    assert(!holds->ids.count && !nvmtl_ws_pending_holds.count && !nvmtl_ws_deferred.count);
    assert(fence.slow.length == sizeof(nvmtl_ws_ready_time));

    [events removeAllObjects];
    holds = hold(cb, sid, current);
    nvmtl_ws_defer(older, old, NO, sid);
    fence = (nvmtl_ws_fence_part){ .cb = cb };
    nvmtl_ws_fence_landed(&fence);
    assert(!fence.landed && !strcmp(fence.path, "deferred-publication"));
    assert(!events.count && holds->ids.count == 1 && nvmtl_ws_deferred[@(sid)].count == 1);

    holds = hold(cb, sid, current);
    [nvmtl_ws_pending_holds addObject:@(sid)];
    fence = (nvmtl_ws_fence_part){ .cb = cb };
    nvmtl_ws_fence_landed(&fence);
    assert(!fence.landed && !strcmp(fence.path, "shared-ownership"));
    assert(!events.count && [nvmtl_ws_pending_holds countForObject:@(sid)] == 2);

    holds = hold(cb, sid, current);
    cb->_parts = @[part, partWithTexture(old)];
    fence = (nvmtl_ws_fence_part){ .cb = cb };
    nvmtl_ws_fence_landed(&fence);
    assert(!fence.landed && !strcmp(fence.path, "later-write") && !events.count);

    testGpuEnd = 3000000000ull;
    part->commands.timing = (nvmtl_gpu_interval){ .host_wait_begin_ns = testGpuEnd + 12000000ull,
        .host_after_ns = testGpuEnd + 25000000ull };
    nvmtl_ws_log_slow_release(sid, testGpuEnd - 1000000ull, testGpuEnd + 26000000ull, testGpuEnd + 27000000ull,
        part, 0, 0, 0, "test", YES);
    assert(strstr(statsLog, "releases=1 calibrated=1 over_10ms=1 max_gpu_end_to_ready_us=27000"));
    assert(strstr(statsLog, "late_wait_entry=1 late_wait_return=1 missing_wait_entry=0"));
    assert(strstr(releaseLog, "fence_wait_enter_us=3012000"));
    puts("PASS: true final GPU fence releases shared VRAM before retirement, preserves deferred ordering and fallback, and reports split wait timing");
} }
'''

parts = [fixture,
    excerpt('static BOOL nvmtl_ws_fence_stats_on', '@interface NVMTLPendingSurfaceWrite'),
    excerpt('@interface NVMTLSurfaceHolds', '// Commit, OUTSIDE'),
    excerpt('// A fence is sufficient', '// The command buffer has finished (or failed)'),
    checks]
with tempfile.TemporaryDirectory() as directory:
    test = Path(directory) / 'verify.m'
    binary = test.with_suffix('')
    test.write_text('\n'.join(parts))
    subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-framework', 'Foundation', str(test), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=5)
