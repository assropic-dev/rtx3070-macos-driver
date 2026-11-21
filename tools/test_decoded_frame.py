"""Exercise the actual paired-plane validation, publication and CPU page-off rules."""
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
code = (root / "kexts/NVRM/accel/nvrm-accel.cpp").read_text()
methods = code[code.index("static bool nvVideoSurfaceLayout"):code.index("\n#endif", code.index("static bool nvReadVideoFrame"))]
harness = r'''
#include <cassert>
#include <cstdint>
#include <cstring>
#include "nvrm_surfshare_abi.h"
using UInt64 = uint64_t;
struct IOMemoryDescriptor {
    unsigned char data[24]{};
    size_t getLength() const { return sizeof data; }
    size_t writeBytes(size_t off, const void *src, size_t n) {
        assert(off + n <= sizeof data); memcpy(data + off, src, n); return n;
    }
};
struct IOSurface {
    IOMemoryDescriptor md;
    unsigned count = 2, fmt = 0x34323076;
    UInt64 offsets[2]{4,16}, sizes[2]{12,8};
    unsigned getPixelFormat() const { return fmt; }
    unsigned getPlaneCount() const { return count; }
    UInt64 getPlaneOffset(unsigned i) const { return offsets[i]; }
    UInt64 getPlaneSize(unsigned i) const { return sizes[i]; }
    UInt64 getAllocSize() const { return 24; }
    IOMemoryDescriptor *getMemoryDescriptor() { return &md; }
};
struct NVAccel {};
struct R { UInt64 fVideoCookie = 0; };
struct V { unsigned char *fKva; UInt64 fAllocationCookie; };
struct TestPlane { unsigned marked; R r; V v; unsigned short bits[7]{}; };
static unsigned char y[24], uv[24];
static TestPlane backing[2]{{0,{}, {y,7},{}}, {0,{}, {uv,13},{}}};
struct NVVideoPlane {
    TestPlane *cache; R *res; V *vm; unsigned short *valid;
    bool resolve(NVAccel *, IOSurface *, unsigned i, UInt64 n, bool) {
        cache = &backing[i]; res = &cache->r; vm = &cache->v; valid = cache->bits; return n == 24;
    }
};
static void _ZN20IOSurfaceDeviceCache10setCurrentEv(TestPlane *p) { ++p->marked; }
'''
cases = r'''
int main() {
    NVAccel accel; IOSurface s; UInt64 size;
    assert(nvVideoSurfaceLayout(&s, &size) && size == 24);
    s.count = 1; assert(!nvVideoSurfaceLayout(&s, &size)); s.count = 2;
    s.fmt = 0; assert(!nvVideoSurfaceLayout(&s, &size)); s.fmt = 0x78343230;
    assert(nvPublishVideoFrame(&accel, &s, size, (UInt64(99)<<32)|7) != 0);
    assert(!backing[0].marked && !backing[1].marked && !backing[0].bits[6]);
    assert(nvPublishVideoFrame(&accel, &s, size, (UInt64(13)<<32)|7) == 0);
    memset(y, 0xa1, sizeof y); memset(uv, 0xb2, sizeof uv); memset(s.md.data, 0xcc, sizeof s.md.data);
    assert(nvReadVideoFrame(&accel, &s, &s.md));
    for (unsigned i = 0; i < 24; ++i) assert(s.md.data[i] == (i<4 ? 0xcc : i<16 ? 0xa1 : 0xb2));
    assert((backing[0].bits[0]&1) && (backing[1].bits[0]&2));
    s.md.data[17] = 0xe3; assert(nvReadVideoFrame(&accel, &s, &s.md) && s.md.data[17]==0xe3);
    s.sizes[0] = 13; assert(!nvReadVideoFrame(&accel, &s, &s.md)); s.sizes[0] = 12;
    backing[1].bits[0] = 0; ++backing[1].v.fAllocationCookie;
    assert(!nvReadVideoFrame(&accel, &s, &s.md));
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = pathlib.Path(tmp) / "frame.cpp"
    exe = pathlib.Path(tmp) / "frame"
    src.write_text(harness + methods + cases)
    subprocess.run(["c++", "-std=c++17", "-Wall", "-Werror", "-I", str(root / "kexts/NVRM"), str(src), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print("PASS paired publication, CPU prefix and untouched planes, layout bounds and stale cookies")
