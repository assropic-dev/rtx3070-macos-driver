#!/usr/bin/env python3
"""Compare eager and rate-limited sampler diagnostics using the production logger on the host."""
import re
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
plugin = root / "plugin"
source = (plugin / "NVMTLDevice.m").read_text()


def section(start, end):
    return source[source.index(start):source.index(end, source.index(start))]


sampler = section("- (id<MTLSamplerState>)newSamplerStateWithDescriptor:", "- (id<MTLComputePipelineState>)")
assert "nvlog_pixel_sampler(s->_pixelState)" in sampler
limiter = section("#define NVLOG_SITES", "const char *nvmtl_log_path(void)")
logger = section("static void nvlog_line(const char *t);", "#define NVMTL_OOM_STREAK")
fixture = r"""
#import <Foundation/Foundation.h>
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>
#include "nvmtl_log_failure.h"
static atomic_uint descriptions, notices, errors;
static const char *logPath;
static int optIn;
static const char *nvmtl_log_path(void) { return logPath; }
static int nvmtl_process_env_on(void) { return optIn; }
static void capture_syslog(int priority, const char *fmt, ...) {
    if (priority == LOG_ERR) ++errors;
    else { assert(priority == LOG_NOTICE); ++notices; }
}
#define syslog capture_syslog
""" + limiter + logger + r"""
@interface CountedState : NSObject
@property(nonatomic, copy) NSDictionary *fields;
@end
@implementation CountedState
- (NSString *)description {
    ++descriptions;
    return self.fields.description;
}
@end

int main(int argc, char **argv) { @autoreleasepool {
    assert(argc == 5);
    logPath = argv[1];
    optIn = atoi(argv[2]);
    const BOOL eager = atoi(argv[3]), full = atoi(argv[4]);
    CountedState *state = [CountedState new];
    state.fields = @{@"coordinates": @"Pixel", @"min_filter": @"Linear", @"max_anisotropy": @1,
                     @"lod_max_clamp": @(FLT_MAX), @"address_mode_s": @"ClampToZero"};
    static char sites[NVLOG_SITES];
    if (full) {
        // A saturated site table preserves every message.
        uint64_t repeat;
        for (unsigned i = 0; i < NVLOG_SITES; i++) nvlog_dropped(&sites[i], &repeat);
    }
    unsigned calls = full ? 16 : 8192;
    uint64_t start = clock_gettime_nsec_np(CLOCK_PROCESS_CPUTIME_ID);
    dispatch_apply(4, dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^(size_t worker) {
        for (unsigned i = 0; i < calls / 4; ++i) { @autoreleasepool {
            if (eager) nvlog("pixel sampler: %s", state.description.UTF8String);
            else nvlog_pixel_sampler((NSDictionary *)state);
        } }
    });
    uint64_t elapsed = clock_gettime_nsec_np(CLOCK_PROCESS_CPUTIME_ID) - start;
    nvlog("sample submission FAILED"); // failures still reach syslog in release builds
    printf("%u %u %u %.3f\n", atomic_load(&descriptions), atomic_load(&notices),
           atomic_load(&errors), elapsed / 1e6);
} }
"""


with tempfile.TemporaryDirectory(prefix="pixel-sampler-log-") as tmp:
    work = Path(tmp)
    src = work / "test.m"
    src.write_text(fixture)
    for release in (0, 1):
        exe = work / f"test-{release}"
        subprocess.run(["xcrun", "clang", "-fobjc-arc", "-O1", f"-DNVMTL_RELEASE={release}",
                        "-I", str(plugin), "-framework", "Foundation", str(src), "-o", str(exe)], check=True)
        for opted_in in ((0, 1) if release else (0,)):
            for full in (0, 1):
                visible = not release or opted_in
                samples = 16 if full else 10  # first eight, then occurrences 4096 and 8192
                results = []
                for eager in (1, 0):
                    path = work / f"log-{release}-{opted_in}-{full}-{eager}"
                    output = subprocess.check_output([str(exe), str(path), str(opted_in), str(eager),
                                                      str(full)], text=True, timeout=30)
                    descriptions, notices, errors, milliseconds = output.split()
                    assert int(descriptions) == ((16 if full else 8192) if eager else (samples if visible else 0))
                    assert int(notices) == (0 if release else samples + 1)
                    assert int(errors) == release
                    # Concurrent writers may finish in different order. Preserve every byte of each multiline entry.
                    entries = sorted(re.split(r"(?m)^pid \d+ NVMTL: ", path.read_text())[1:]) if path.exists() else []
                    assert len(entries) == (samples + 1 if visible else 0)
                    if visible and not full:
                        for repeat in ("4096", "8192"):
                            assert sum(f"[logged {repeat} times now]" in entry for entry in entries) == 1
                    results.append((entries, milliseconds))
                assert results[0][0] == results[1][0]
                if not full:
                    print(f"PASS release={release} opt-in={opted_in}: 8192 calls, "
                          f"descriptions 8192 -> {samples if visible else 0}, "
                          f"logger CPU {results[0][1]} -> {results[1][1]} ms")
