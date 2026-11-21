"""Exercise the kernel surface-cookie query and guarded export with a synthetic family cache."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'kexts/NVRM/accel/nvrm-accel.cpp').read_text()
start = source.index('static IOReturn nvAccelSurfVram(')
dispatch = source[start:source.index('\n}\n', start) + 3]

stubs = r'''
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include "nvrm_surfshare_abi.h"
using UInt64 = uint64_t;
using IOReturn = int;
using task_t = unsigned;
constexpr int kIOReturnSuccess = 0;
struct OSObject { void release() {} };
struct OSDictionary: OSObject {};
struct IOService: OSObject {
    static OSDictionary *serviceMatching(const char *) { return nullptr; }
    static IOService *waitForMatchingService(OSDictionary *, UInt64) { return nullptr; }
};
struct NVAccel { static NVAccel *resolve(NVAccel *a) { return a; } };
struct NVVidMemory { bool fRmHandle = true, fScanout = false; UInt64 fAllocationCookie = 42, fBytes = 4096; };
struct NVResource: OSObject {};
struct Cache: OSObject { char reserved[0x20]{}; NVResource *resource; };
static_assert(offsetof(Cache, resource) == 0x20);
struct NVRMRmHandles {};
static IOService root;
static IOService *gNVSVRoot = &root;
static NVAccel accel;
static NVResource surfaceResource;
static NVVidMemory backing;
static Cache cache;
static bool locked, busy;
static unsigned planeSeen, grants, wires, rmLookups, gNVSVRefused, gNVSVDirty, gNVSVDirtyRefused, gNVSVOk;
static unsigned nvSurfGpuEscape(NVRMSurfShareEsc *) { return 0; }
static void nvSVZero(void *p, size_t n) { memset(p, 0, n); }
static bool nvAccelRmHandles(NVAccel *, NVRMRmHandles *) { rmLookups++; return true; }
static void *_ZN13IOSurfaceRoot13lookupSurfaceEjP4task(IOService *, unsigned id, task_t task) {
    return id == 7 && task == 1 ? &surfaceResource : nullptr;
}
static bool nvFamLock(NVAccel *, bool tryOnly) {
    assert(!locked); if (busy && tryOnly) return false; return locked = true;
}
static void nvFamUnlock(NVAccel *) { assert(locked); locked = false; }
static void *_ZN22IOGraphicsAccelerator223deviceCacheForIOSurfaceEP9IOSurfacej(void *, void *, unsigned plane) {
    assert(locked); planeSeen = plane; return &cache;
}
#define OSDynamicCast(type, obj) static_cast<type *>(obj)
static NVVidMemory *nvres_vidmem(NVResource *) { return &backing; }
static bool nvAccelRestoreParkedForDirty(NVResource *, NVVidMemory *, unsigned) { return true; }
static unsigned nvAccelSurfDirty(void *, NVResource *, unsigned) { return 0; }
static bool nvSVDiag() { return false; }
#define ALOG(...) ((void)0)
static void _ZN16IOAccelVidMemory4wireEv(NVVidMemory *vm) { assert(locked); wires++; vm->fRmHandle = true; }
static void nvExportSurfaceMemory(NVAccel *, NVRMRmHandles &, NVVidMemory *vm, NVRMSurfShareEsc *p) {
    assert(locked && vm->fRmHandle && p->size <= vm->fBytes);
    p->status = 0; p->hSrcMemory = ++grants; p->hSrcClient = 1;
}
'''

cases = r'''
static unsigned call(unsigned selector, unsigned cmd, unsigned task = 1, unsigned bytes = 4096, unsigned sid = 7) {
    NVRMSurfShareEsc request{};
    request.version = NVRM_SURFSHARE_VERSION; request.surfaceID = sid; request.plane = selector;
    request.hSrcClient = request.hSrcMemory = 1234; request.size = bytes;
    assert(nvAccelSurfVram(&accel, &request, task, cmd) == kIOReturnSuccess && !locked);
    if (request.status || cmd != NVRM_ESC_SURF_VRAM) assert(!request.hSrcMemory && !request.hSrcClient);
    return request.status;
}
int main() {
    cache.resource = &surfaceResource;
    const unsigned q = NVRM_SS_PLANE_COOKIE, read = NVRM_ESC_SURF_DIRTY, share = NVRM_ESC_SURF_VRAM;
    assert(call(q, read, 1, 0, 0) == q);
    assert(call(q, read, 2) == NVRM_SS_NO_SURFACE);
    assert(call(q, read) == 42 && !planeSeen && !rmLookups && !gNVSVDirty && !grants && !wires);
    busy = true; assert(call(q, read) == NVRM_SS_BUSY); busy = false;
    assert(call(q | 42, share, 2) == NVRM_SS_NO_SURFACE);
    assert(call(q | 42, share, 1, 4097) == NVRM_SS_TOO_SMALL && !grants);
    assert(call(q | 42, share) == 0 && grants == 1);
    backing.fAllocationCookie = 43;
    assert(call(q | 42, share) == NVRM_SS_NO_VRAM && grants == 1);
    assert(call(q, read) == 43);
    backing.fAllocationCookie = (UInt64(1) << 32) + 42;
    assert(call(q, read) == NVRM_SS_NO_VRAM && call(q | 42, share) == NVRM_SS_NO_VRAM);
    backing.fAllocationCookie = NVRM_SS_COOKIE_MAX;
    assert(call(q, read) == NVRM_SS_COOKIE_MAX && !call(q | NVRM_SS_COOKIE_MAX, share));
    backing.fRmHandle = false;
    assert(call(q, read) == NVRM_SS_NO_VRAM && call(q | NVRM_SS_COOKIE_MAX, share) == NVRM_SS_NO_VRAM && !wires);
    assert(!call(1, share) && planeSeen == 1 && wires == 1); // ordinary plane keeps normal export semantics
    assert(call(q, share) == NVRM_SS_NO_CACHE && call(q | 42, read) == NVRM_SS_NO_CACHE);
}
'''

with tempfile.TemporaryDirectory() as folder:
    harness, binary = Path(folder) / 'cookie.cpp', Path(folder) / 'cookie'
    harness.write_text(stubs + dispatch + cases)
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Werror', '-I', str(root / 'kexts/NVRM'),
                    str(harness), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('PASS family cache cookie query, task rights, locked export, bounds, overflow and old plane semantics')
