#!/usr/bin/env python3
"""Run the producer and consumer in separate processes against one real IOSurface and Darwin notify."""
import os
import selectors
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = (root / "plugin/NVMTLDevice.m").read_text()
busy = source[source.index("typedef struct { uint32_t magic, done;"):
              source.index("// Shared family-VRAM imports")]

fixture = r'''
#import <Foundation/Foundation.h>
#import <IOSurface/IOSurface.h>
#include <notify.h>
#include <poll.h>
#include <stdatomic.h>
#include <unistd.h>
#include <assert.h>
static const char *mode;
static void pause_test(const char *where) {
    puts(where); fflush(stdout); assert(getchar() != EOF);
}
static uint32_t register_test(const char *name, int *fd, int flags, int *token) {
    uint32_t rc = notify_register_file_descriptor(name, fd, flags, token);
    assert(rc == NOTIFY_STATUS_OK);
    if (!strcmp(mode, "gap")) pause_test("registered");
    return rc;
}
static int poll_test(struct pollfd *fd, nfds_t n, int ms) {
    assert(ms > 0);
    pause_test("waiting");
    int rc = poll(fd, n, 2000);
    assert(rc == 1 && (fd->revents & POLLIN));  // the writer in the other process actually woke this fd
    return rc;
}
static void sleep_test(useconds_t us) {
    assert(!strcmp(mode, "legacy"));
    pause_test("polling");
}
#define notify_register_file_descriptor register_test
#define poll poll_test
#define usleep sleep_test
// Pause the clock for the orchestrated handshake. The timeout arm starts with a known
// unfinished generation that has been waiting for the full watchdog interval.
#define clock_gettime_nsec_np(clock) (!strcmp(mode, "timeout") ? UINT64_C(1100000000) : UINT64_C(1000000000))
@@BUSY@@
int main(int argc, char **argv) {
    @autoreleasepool {
        mode = argv[1];
        if (!strcmp(mode, "writer")) {
            IOSurfaceRef s = IOSurfaceCreate((__bridge CFDictionaryRef)@{
                (__bridge NSString *)kIOSurfaceIsGlobal: @YES, (__bridge NSString *)kIOSurfaceWidth: @8,
                (__bridge NSString *)kIOSurfaceHeight: @8, (__bridge NSString *)kIOSurfaceBytesPerElement: @4});
            assert(s);
            nvmtl_busy_write(s, 0, 3, 0);
            printf("%u\n", IOSurfaceGetID(s)); fflush(stdout);
            int c;
            while ((c = getchar()) != EOF) {
                if (c == 'd') nvmtl_busy_write(s, 0, 3, 1);
                nvmtl_busy_notify_done();
                puts("posted"); fflush(stdout);
            }
            CFRelease(s);
        } else {
            assert(argc == 3);
            IOSurfaceRef s = IOSurfaceLookup((uint32_t)strtoul(argv[2], NULL, 10));
            assert(s);
            if (!strcmp(mode, "timeout")) {
                assert(!nvmtl_busy_wait(s, 0));
                CFRelease(s); puts("done"); fflush(stdout); return 0;
            }
            assert(nvmtl_busy_wait(s, 0));
            nvmtl_busy_tag tag;
            assert(nvmtl_busy_read(s, 0, &tag) && tag.done && tag.seq == 3);
            CFRelease(s); puts("done"); fflush(stdout);
        }
    }
}
'''

def line(proc):
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        if not sel.select(5):
            raise AssertionError("notification test timed out")
    return proc.stdout.readline().strip()


def send(proc, cmd):
    proc.stdin.write(cmd)
    proc.stdin.flush()


def check(exe, mode):
    env = dict(os.environ, NVMTL_SURFACE_NOTIFY="0" if mode == "legacy" else "1")
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    writer = subprocess.Popen([exe, "writer"], env=env, **options)
    reader = None
    try:
        sid = line(writer)
        assert sid.isdigit(), sid
        reader = subprocess.Popen([exe, mode, sid], env=dict(os.environ, NVMTL_SURFACE_NOTIFY="1"), **options)
        events = {"gap": [("registered", "d")], "event": [("waiting", "w"), ("waiting", "d")],
                  "legacy": [("polling", "d")], "timeout": []}[mode]
        for signal, command in events:
            assert line(reader) == signal
            send(writer, command)
            assert line(writer) == "posted"
            send(reader, "x")
        assert line(reader) == "done" and reader.wait(timeout=5) == 0
    finally:
        if reader and reader.poll() is None:
            reader.kill()
            reader.wait()
        writer.stdin.close()
        writer.wait(timeout=5)


with tempfile.TemporaryDirectory() as tmp:
    src, exe = Path(tmp) / "busy.m", Path(tmp) / "busy"
    src.write_text(fixture.replace("@@BUSY@@", busy))
    subprocess.run(["xcrun", "clang", "-fobjc-arc", "-Wno-deprecated-declarations", str(src),
                    "-framework", "Foundation", "-framework", "IOSurface", "-o", str(exe)], check=True)
    for mode in ("gap", "event", "legacy", "timeout"):
        check(exe, mode)
print("PASS: completion around registration, spurious cross-process wake, legacy writer, and unresolved timeout")
