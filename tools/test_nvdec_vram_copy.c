// GA104 hardware test. Build with the vendor/nvenc/src includes and RM headers used
// by build_nvencvt.sh, linking dec_copy.c, blocklinear.c, channel.c and rm.c.
#include "dec_copy.h"
#include "blocklinear.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint8_t pixel(int plane, size_t x, size_t y)
{
    return (uint8_t)(plane * 67 + x * 13 + y * 91 + 5);
}

static bool check_frame(Rm *rm, size_t width, size_t height, unsigned bytesPerSample)
{
    size_t chromaWidth = ((width + 1) & ~(size_t)1) * bytesPerSample;
    size_t pitch = (chromaWidth + 63) & ~(size_t)63;
    size_t stride = pitch + 64, chromaRows = (height + 1) / 2;
    size_t first = 128, second = first + stride * height + 128;
    size_t size = second + stride * chromaRows + 48;
    RmMem src[2] = {0}, out[2] = {0};
    DecCopy copy;
    if (!dec_copy_open(&copy, rm, pitch, height)) return false;
    uint8_t *expected = calloc(1, size);
    bool match = false;
    if (!expected) goto done;
    DecCopyPlane planes[2] = {
        {&src[0], first, stride, width * bytesPerSample, height},
        {&src[1], second, stride, chromaWidth, chromaRows}
    };
    for (size_t i = 0; i < first; i++) expected[i] = (uint8_t)(0xe0 + i);
    memcpy(copy.prefix.cpu, expected, first);
    for (int plane = 0; plane < 2; plane++) {
        const DecCopyPlane *p = &planes[plane];
        size_t n = nvenc_in_plane_size(pitch, p->rows);
        if (rm_mem_alloc(rm, &src[plane], n, 4096, false, true, true) ||
            rm_mem_alloc(rm, &out[plane], size, 4096, false, true, true)) goto done;
        uint8_t *tiled = calloc(1, n);
        if (!tiled) goto done;
        for (size_t y = 0; y < p->rows; y++)
            for (size_t x = 0; x < p->bytes; x++) {
                uint8_t v = pixel(plane, x, y);
                tiled[nvenc_in_offset(x, y, pitch)] = v;
                expected[p->offset + y * stride + x] = v;
            }
        memcpy(src[plane].cpu, tiled, n);
        free(tiled);
        memset(out[plane].cpu, plane ? 0xa5 : 0x5a, size);
    }
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    match = dec_copy_picture(&copy, pitch, planes, out, size, first, out + 1) &&
            !memcmp(expected, out[0].cpu, size) && !memcmp(expected, out[1].cpu, size);
done:
    dec_copy_close(&copy);
    for (int i = 0; i < 2; i++) { rm_mem_free(rm, &out[i]); rm_mem_free(rm, &src[i]); }
    free(expected);
    printf("%zux%zu, %u byte samples: %s\n", width, height, bytesPerSample, match ? "pass" : "FAIL");
    return match;
}

int main(void)
{
    Rm rm = {0};
    bool success = !rm_open(&rm) && !rm_va_init(&rm) &&
        check_frame(&rm, 257, 129, 1) && check_frame(&rm, 1919, 1079, 1) && check_frame(&rm, 257, 129, 2);
    rm_close(&rm);
    return success ? 0 : 1;
}
