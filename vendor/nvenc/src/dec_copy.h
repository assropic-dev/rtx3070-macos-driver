// Copies Ampere AV1/VP9 KBL pictures into pitch-linear device memory.
#pragma once
#include "channel.h"
#include <CoreFoundation/CoreFoundation.h>

typedef struct {
    Chan chan;
    RmMem scratch, sem, zero, prefix, readback;
    NvU32 seq;
    struct { uint32_t sid; uint64_t generation; CFDataRef bytes; } header[8];
    unsigned nextHeader;
} DecCopy;

typedef struct {
    const RmMem *src;
    size_t offset, stride, bytes, rows;    // destination plane, excluding padding
} DecCopyPlane;

bool dec_copy_open(DecCopy *copy, Rm *rm, size_t pitch, size_t rows);
void dec_copy_close(DecCopy *copy);
bool dec_picture_surfaces(DecCopy *copy, Rm *rm, size_t pitch, size_t rows,
                          RmMem *y, size_t ybytes, RmMem *uv, size_t uvbytes,
                          size_t count, bool zero);
// Both writes and the final system-visible release have completed on success.
bool dec_copy_picture(DecCopy *copy, size_t pitch, const DecCopyPlane planes[2], const RmMem *dst,
                      size_t frameSize, size_t prefixSize, const RmMem *mirror);
