// Optional GA10x output in the accelerator family's two IOSurface device caches.
// The producer waits for both complete copies before making either cache current.
#pragma once
#include "dec_copy.h"
#include <CoreVideo/CoreVideo.h>
bool nvdec_output_vram(DecCopy *copy, CVPixelBufferRef pb, size_t pitch,
                       const RmMem *y, const RmMem *uv, unsigned bps, unsigned w, unsigned h);
// The caller holds a write lock on pb. This is used for software clients or when the cache refuses sharing.
bool nvdec_output_readback(DecCopy *copy, CVPixelBufferRef pb, size_t pitch,
                           const RmMem *y, const RmMem *uv, unsigned bps, unsigned w, unsigned h);
