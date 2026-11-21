#include "../vt/nvencvt_surface.h"
#include <IOSurface/IOSurface.h>
#include <string.h>
#include <unistd.h>

// The standalone vendor library is built separately from the kext. These are the
// v2 nvrm_surfshare_abi.h field layout and the checked whole-frame operation.
enum { SURF_VRAM = 0xe2, SURF_DIRTY = 0xe3, VIDEO_FRAME = 0x20000000u };
typedef struct {
    uint32_t version, surfaceID, plane, hClient, hSrcClient, hSrcMemory, status, pad;
    uint64_t size;
} SurfaceShare;
_Static_assert(sizeof(SurfaceShare) == 40, "surface share ABI");
typedef struct { uint32_t seed, reserved; uint64_t generation; } FrameTag;
_Static_assert(sizeof(FrameTag) == 16, "decoder publication tag");

static bool valid_output(CVPixelBufferRef pb, unsigned bps, unsigned w, unsigned h)
{
    OSType pf = CVPixelBufferGetPixelFormatType(pb);
    bool supported = (bps == 1 && (pf == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ||
                                  pf == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)) ||
                     (bps == 2 && (pf == kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange ||
                                  pf == kCVPixelFormatType_420YpCbCr10BiPlanarFullRange));
    return supported && w && h && CVPixelBufferGetPlaneCount(pb) == 2 &&
        CVPixelBufferGetWidthOfPlane(pb, 0) == w && CVPixelBufferGetHeightOfPlane(pb, 0) == h &&
        CVPixelBufferGetWidthOfPlane(pb, 1) == ((size_t)w + 1) / 2 && CVPixelBufferGetHeightOfPlane(pb, 1) == ((size_t)h + 1) / 2;
}

static uint64_t previous_frame(IOSurfaceRef s)
{
    FrameTag tag = {0};
    CFTypeRef v = IOSurfaceCopyValue(s, CFSTR("NVMTLDecodedFrameV1"));
    if (v && CFGetTypeID(v) == CFDataGetTypeID() && CFDataGetLength((CFDataRef)v) == sizeof tag)
        memcpy(&tag, CFDataGetBytePtr((CFDataRef)v), sizeof tag);
    if (v) {
        CFRelease(v);
        IOSurfaceRemoveValue(s, CFSTR("NVMTLDecodedFrameV1"));
    }
    return tag.seed == IOSurfaceGetSeed(s) && !tag.reserved ? tag.generation : 0;
}

static bool get_header(DecCopy *copy, IOSurfaceRef s, uint64_t gen, size_t n)
{
    if (!n) return true;
    if (n > copy->prefix.size) return false;
    for (unsigned i = 0; gen && i < 8; i++) {
        if (copy->header[i].sid == IOSurfaceGetID(s) && copy->header[i].generation == gen &&
            copy->header[i].bytes && (size_t)CFDataGetLength(copy->header[i].bytes) == n) {
            memcpy(copy->prefix.cpu, CFDataGetBytePtr(copy->header[i].bytes), n);
            return true;
        }
    }
    const IOSurfaceLockOptions options = kIOSurfaceLockReadOnly | kIOSurfaceLockAvoidSync;
    if (IOSurfaceLock(s, options, NULL) != kIOReturnSuccess) return false;
    memcpy(copy->prefix.cpu, IOSurfaceGetBaseAddress(s), n);
    return IOSurfaceUnlock(s, options, NULL) == kIOReturnSuccess;
}

static bool get_backing(Rm *rm, unsigned sid, unsigned plane, size_t n, SurfaceShare *e)
{
    *e = (SurfaceShare){ .version = 2, .surfaceID = sid, .plane = VIDEO_FRAME | plane,
                         .hClient = rm->client, .status = UINT32_MAX, .size = n };
    return !rm_esc(rm, rm->ctl, SURF_VRAM, e, sizeof *e) && !e->status && e->pad &&
           e->hSrcClient && e->hSrcMemory && e->size >= n && !(e->size & 4095);
}

bool nvdec_output_vram(DecCopy *copy, CVPixelBufferRef pb, size_t pitch,
                       const RmMem *y, const RmMem *uv, unsigned bps, unsigned w, unsigned h)
{
    IOSurfaceRef s = CVPixelBufferGetIOSurface(pb);
    if (!s) return false;
    uint64_t prev = previous_frame(s);
    Rm *rm = copy->chan.rm;
    if (!rm || !valid_output(pb, bps, w, h) || IOSurfaceGetPlaneCount(s) != 2) return false;
    size_t n = IOSurfaceGetAllocSize(s);
    uintptr_t base = (uintptr_t)IOSurfaceGetBaseAddress(s);
    uintptr_t py = (uintptr_t)IOSurfaceGetBaseAddressOfPlane(s, 0), puv = (uintptr_t)IOSurfaceGetBaseAddressOfPlane(s, 1);
    if (!base || py < base || puv < py || puv - base >= n || !n || n > UINT32_MAX) return false;
    size_t yoff = py - base, uvoff = puv - base;
    size_t ys = IOSurfaceGetBytesPerRowOfPlane(s, 0), uvs = IOSurfaceGetBytesPerRowOfPlane(s, 1);
    size_t ybytes = (size_t)w * bps, cbytes = (size_t)((w + 1ULL) & ~1ULL) * bps;
    size_t crows = ((size_t)h + 1) / 2;
    if (ys != CVPixelBufferGetBytesPerRowOfPlane(pb, 0) || uvs != CVPixelBufferGetBytesPerRowOfPlane(pb, 1) ||
        ys < ybytes || uvs < cbytes || h > (uvoff - yoff) / ys || crows > (n - uvoff) / uvs ||
        !get_header(copy, s, prev, yoff)) return false;

    const unsigned sid = IOSurfaceGetID(s);
    SurfaceShare e[2];
    if (!get_backing(rm, sid, 0, n, &e[0]) || !get_backing(rm, sid, 1, n, &e[1])) return false;
    bool same = e[0].pad == e[1].pad;
    RmMem out[2] = {0};
    if (rm_mem_dup(rm, out, e[0].hSrcClient, e[0].hSrcMemory, e[0].size)) return false;
    bool copied = false;
    if (same || !rm_mem_dup(rm, out + 1, e[1].hSrcClient, e[1].hSrcMemory, e[1].size)) {
        const DecCopyPlane planes[2] = {{y, yoff, ys, ybytes, h}, {uv, uvoff, uvs, cbytes, crows}};
        copied = dec_copy_picture(copy, pitch, planes, out, n, yoff, same ? NULL : out + 1);
    }
    bool published = false;
    if (copied) {
        SurfaceShare dirty = { .version = 2, .surfaceID = sid, .plane = VIDEO_FRAME, .hClient = rm->client,
                               .status = UINT32_MAX, .size = ((uint64_t)e[1].pad << 32) | e[0].pad };
        published = !rm_esc(rm, rm->ctl, SURF_DIRTY, &dirty, sizeof dirty) && !dirty.status;
    }
    rm_mem_free(rm, out + 1); rm_mem_free(rm, out);
    if (!published) return false;
    static uint32_t sequence;
    uint32_t next = __atomic_add_fetch(&sequence, 1, __ATOMIC_RELAXED);
    if (!next) next = __atomic_add_fetch(&sequence, 1, __ATOMIC_RELAXED);
    FrameTag tag = { IOSurfaceGetSeed(s), 0, ((uint64_t)(uint32_t)getpid() << 32) | next };
    CFDataRef data = CFDataCreate(NULL, (const UInt8 *)&tag, sizeof tag);
    if (!data) return false;
    IOSurfaceSetValue(s, CFSTR("NVMTLDecodedFrameV1"), data);
    CFRelease(data);
    if (yoff) {
        unsigned i = copy->nextHeader++ % 8;
        if (copy->header[i].bytes) CFRelease(copy->header[i].bytes);
        copy->header[i].sid = sid; copy->header[i].generation = tag.generation;
        copy->header[i].bytes = CFDataCreate(NULL, copy->prefix.cpu, yoff);
    }
    return true;
}

bool nvdec_output_readback(DecCopy *copy, CVPixelBufferRef pb, size_t pitch,
                           const RmMem *y, const RmMem *uv, unsigned bps, unsigned w, unsigned h)
{
    if (!copy->chan.rm || !valid_output(pb, bps, w, h)) return false;
    unsigned char *d[2] = { CVPixelBufferGetBaseAddressOfPlane(pb, 0), CVPixelBufferGetBaseAddressOfPlane(pb, 1) };
    size_t stride[2] = { CVPixelBufferGetBytesPerRowOfPlane(pb, 0), CVPixelBufferGetBytesPerRowOfPlane(pb, 1) };
    size_t bytes = (size_t)w * bps, cb = ((w + 1ULL) & ~1ULL) * bps, ch = ((size_t)h + 1) / 2;
    if (!d[0] || !d[1] || stride[0] < bytes || stride[1] < cb || bytes > pitch || cb > pitch ||
        pitch > 32768 || h > 16384) return false;
    size_t need = pitch * (h + ch);
    Rm *rm = copy->chan.rm;
    if (copy->readback.size < need) {
        rm_mem_free(rm, &copy->readback);
        if (rm_mem_alloc(rm, &copy->readback, need, 4096, true, true, true)) {
            rm_mem_free(rm, &copy->readback); return false;
        }
    }
    DecCopyPlane p[2] = {{y, 0, pitch, bytes, h}, {uv, pitch * h, pitch, cb, ch}};
    if (!dec_copy_picture(copy, pitch, p, &copy->readback, 0, 0, NULL)) return false;
    for (unsigned i = 0; i < 2; i++)
        for (size_t row = 0; row < p[i].rows; row++)
            memcpy(d[i] + row * stride[i], (const uint8_t *)copy->readback.cpu + p[i].offset + row * pitch, p[i].bytes);
    return true;
}
