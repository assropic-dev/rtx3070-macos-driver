#include "dec_copy.h"
#include "blocklinear.h"
#include "nv_floor.h"
#include <stdlib.h>
#include "class/clc6b5.h"
#include "class/clc7b5.h"
#include "class/clc6b5sw.h"
#include "class/clc56f.h"
#include "class/cl2080_notification.h"
#include "ctrl/ctrl2080/ctrl2080gpu.h"
#include "ctrl/ctrl2080/ctrl2080ce.h"
#include <string.h>

static NvU32 copy_engine(Rm *rm)
{
    NV2080_CTRL_GPU_GET_ENGINES_V2_PARAMS list = {0};
    if (rm_ctrl(rm, rm->subdevice, NV2080_CTRL_CMD_GPU_GET_ENGINES_V2, &list, sizeof list)) return 0;
    for (unsigned i = 0; i < list.engineCount && i < NV2080_GPU_MAX_ENGINES_LIST_SIZE; i++) {
        NvU32 eng = list.engineList[i];
        if (!NV2080_ENGINE_TYPE_IS_COPY(eng)) continue;
        NV2080_CTRL_CE_GET_CAPS_V2_PARAMS caps = { .ceEngineType = eng };
        if (!rm_ctrl(rm, rm->subdevice, NV2080_CTRL_CMD_CE_GET_CAPS_V2, &caps, sizeof caps) &&
            !NV2080_CTRL_CE_GET_CAP(caps.capsTbl, NV2080_CTRL_CE_CAPS_CE_GRCE)) return eng;
    }
    return 0;
}

bool dec_copy_open(DecCopy *copy, Rm *rm, size_t pitch, size_t rows)
{
    memset(copy, 0, sizeof *copy);
    if (!pitch || !rows || pitch > 32768 || rows > 16384 || (pitch & 63) ||
        rm_class(rm, RM_CLS_NVDEC) != 0xc7b0) return false;
    NvU32 engine = copy_engine(rm);
    if (!engine) return false;
    NVC6B5_ALLOCATION_PARAMETERS params = { .version = NVC6B5_ALLOCATION_PARAMETERS_VERSION_1, .engineType = engine };
    if (chan_create(rm, &copy->chan, engine, AMPERE_DMA_COPY_B, &params) ||
        rm_mem_alloc(rm, &copy->scratch, pitch * ((rows + 15) / 16), 4096, false, false, true) ||
        rm_mem_alloc(rm, &copy->sem, 4096, 4096, true, true, true) ||
        rm_mem_alloc(rm, &copy->zero, 4096, 4096, false, true, true) ||
        rm_mem_alloc(rm, &copy->prefix, 4096, 4096, true, true, true)) {
        dec_copy_close(copy);
        return false;
    }
    memset(copy->zero.cpu, 0, 4096);
    *(volatile NvU32 *)copy->sem.cpu = 0;
    return true;
}

void dec_copy_close(DecCopy *copy)
{
    Rm *rm = copy->chan.rm;
    if (!rm) return;
    for (unsigned i = 0; i < 8; i++)
        if (copy->header[i].bytes) CFRelease(copy->header[i].bytes);
    chan_destroy(&copy->chan);   // stop any faulted or timed-out commands before releasing their memory
    rm_mem_free(rm, &copy->scratch);
    rm_mem_free(rm, &copy->sem);
    rm_mem_free(rm, &copy->zero);
    rm_mem_free(rm, &copy->prefix);
    rm_mem_free(rm, &copy->readback);
    memset(copy, 0, sizeof *copy);
}

// The new producer and consumer path is enabled together. Older NVAccel versions
// cannot publish both planes of one decode atomically.
static bool video_planes_available(void)
{
    const char *enabled = getenv("NVMTL_NVDEC_VRAM");
    if (!enabled || strcmp(enabled, "1")) return false;
    io_service_t service = IOServiceGetMatchingService(NV_IO_MAIN_PORT, IOServiceMatching("NVAccel"));
    if (!service) return false;
    CFTypeRef prop = IORegistryEntryCreateCFProperty(service, CFSTR("NVAccelVideoPlanes"), NULL, 0);
    IOObjectRelease(service);
    int version = 0;
    if (prop && CFGetTypeID(prop) == CFNumberGetTypeID())
        CFNumberGetValue((CFNumberRef)prop, kCFNumberIntType, &version);
    if (prop) CFRelease(prop);
    return version == 1;
}

bool dec_picture_surfaces(DecCopy *copy, Rm *rm, size_t pitch, size_t rows,
                          RmMem *y, size_t ybytes, RmMem *uv, size_t uvbytes,
                          size_t count, bool zero)
{
    bool vram = video_planes_available() && dec_copy_open(copy, rm, pitch, rows);
    for (;;) {
        for (size_t i = 0; i < count; i++) {
            if (rm_mem_alloc(rm, &y[i], ybytes, 4096, !vram, true, true) ||
                rm_mem_alloc(rm, &uv[i], uvbytes, 4096, !vram, true, true)) {
                for (size_t j = 0; j <= i; j++) {
                    rm_mem_free(rm, &y[j]); rm_mem_free(rm, &uv[j]);
                }
                if (!vram) return false;
                dec_copy_close(copy); vram = false;
                break;
            }
            if (zero) { memset(y[i].cpu, 0, y[i].size); memset(uv[i].cpu, 0, uv[i].size); }
            if (i + 1 == count) return true;
        }
    }
}

static void copy_rows(Chan *c, NvU64 src, NvU64 dst, NvU32 srcPitch, NvU32 dstPitch,
                      NvU32 width, NvU32 rows, bool release)
{
    const NvU32 launch = DRF_DEF(C6B5, _LAUNCH_DMA, _DATA_TRANSFER_TYPE, _NON_PIPELINED) |
        DRF_DEF(C6B5, _LAUNCH_DMA, _SRC_MEMORY_LAYOUT, _PITCH) |
        DRF_DEF(C6B5, _LAUNCH_DMA, _DST_MEMORY_LAYOUT, _PITCH) |
        DRF_DEF(C6B5, _LAUNCH_DMA, _MULTI_LINE_ENABLE, _TRUE);
    chan_mthd(c, 0, NVC6B5_OFFSET_IN_UPPER, 8, (NvU32)(src >> 32), (NvU32)src,
              (NvU32)(dst >> 32), (NvU32)dst, srcPitch, dstPitch, width, rows);
    const NvU32 fence = DRF_DEF(C6B5, _LAUNCH_DMA, _FLUSH_ENABLE, _TRUE) |
                        DRF_DEF(C6B5, _LAUNCH_DMA, _SEMAPHORE_TYPE, _RELEASE_ONE_WORD_SEMAPHORE);
    chan_mthd(c, 0, NVC6B5_LAUNCH_DMA, 1, launch | (release ? fence : 0));
}

static void copy_linear(Chan *c, NvU64 src, NvU64 dst, size_t n, bool release)
{
    size_t blocks = n / 4096, tail = n % 4096;
    if (blocks) copy_rows(c, src, dst, 4096, 4096, 4096, (NvU32)blocks, release && !tail);
    if (tail) copy_rows(c, src + blocks * 4096, dst + blocks * 4096, 4096, 4096, (NvU32)tail, 1, release);
}

static bool plane_fits(const DecCopyPlane *p, size_t pitch, NvU64 destSize, NvU64 scratchSize)
{
    if (!p->src || !p->src->gpu || !p->bytes || !p->rows || p->rows > 16384 || p->bytes > pitch ||
        p->stride < p->bytes || p->stride > UINT32_MAX / 16 || p->offset > destSize || p->bytes > destSize - p->offset)
        return false;
    size_t bands = (p->rows + 15) / 16;
    return (p->rows - 1) <= (destSize - p->offset - p->bytes) / p->stride &&
           bands * pitch <= scratchSize && bands * pitch * 16 <= p->src->size;
}

bool dec_copy_picture(DecCopy *copy, size_t pitch, const DecCopyPlane planes[2], const RmMem *dst,
                      size_t frameSize, size_t prefixSize, const RmMem *mirror)
{
    Chan *c = &copy->chan;
    if (!c->rm || !dst->gpu || !pitch || pitch > 32768 || (pitch & 63) ||
        !plane_fits(&planes[0], pitch, dst->size, copy->scratch.size) ||
        !plane_fits(&planes[1], pitch, dst->size, copy->scratch.size) || !chan_rewind(c)) return false;
    if (frameSize > UINT32_MAX || frameSize > dst->size || prefixSize > 4096 || prefixSize > frameSize ||
        (mirror && (!mirror->gpu || !frameSize || mirror->size < frameSize))) return false;
    chan_mthd(c, 0, NVC56F_SET_OBJECT, 1, (NvU32)AMPERE_DMA_COPY_B);
    const NvU32 seq = ++copy->seq;
    chan_mthd(c, 0, NVC6B5_SET_SEMAPHORE_A, 3, (NvU32)(copy->sem.gpu >> 32), (NvU32)copy->sem.gpu, seq);
    // Family page-off can expose the entire IOSurface allocation, including padding.
    // Initialize it from one device-local zero page; only the small planar header comes from the CPU.
    if (frameSize) {
        size_t filled = frameSize < 4096 ? frameSize : 4096;
        copy_linear(c, copy->zero.gpu, dst->gpu, filled, false);
        while (filled < frameSize) {
            size_t next = frameSize - filled < filled ? frameSize - filled : filled;
            copy_linear(c, dst->gpu, dst->gpu + filled, next, false);
            filled += next;
        }
    }
    if (prefixSize) copy_linear(c, copy->prefix.gpu, dst->gpu, prefixSize, false);
    for (unsigned pl = 0; pl < 2; pl++) {
        const DecCopyPlane *p = &planes[pl];
        size_t phases = p->rows < 16 ? p->rows : 16;
        for (size_t row = 0; row < phases; row++) {
            // For each row phase, gather its four 16-byte runs from every 64x16 tile into
            // the scratch rows. The byte order is NVDEC KBL, not a generic Fermi GOB.
            size_t bands = (p->rows + 15 - row) / 16;
            for (unsigned x = 0; x < 64; x += 16) {
                size_t off = nvenc_in_offset(x, row, pitch);
                copy_rows(c, p->src->gpu + off, copy->scratch.gpu + x, 1024, 64,
                          16, (NvU32)(bands * (pitch / 64)), false);
            }
            // Each scratch row is now one complete scanline. Skip the destination's
            // padding and interleave this phase with the other fifteen scanlines.
            copy_rows(c, copy->scratch.gpu, dst->gpu + p->offset + row * p->stride,
                      (NvU32)pitch, (NvU32)(16 * p->stride), (NvU32)p->bytes, (NvU32)bands,
                      !mirror && pl == 1 && row + 1 == phases);
        }
    }
    if (mirror)
        copy_linear(c, dst->gpu, mirror->gpu, frameSize, true);
    if (chan_submit(c)) return false;
    if (!chan_wait_release(c, &copy->sem, seq)) {
        dec_copy_close(copy);
        return false;
    }
    return true;
}
