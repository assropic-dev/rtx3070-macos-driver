/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#define NVRM_ESC_SURF_VRAM     0xE2u
#define NVRM_ESC_SURF_DIRTY    0xE3u
#define NVRM_SURFSHARE_VERSION 2u
/* NVRM_ESC_SURF_DIRTY with this plane bit is a WindowServer completion mark (bit 0: 1 pending, 0 ready), not a dirty plane */
#define NVRM_SS_PLANE_GPU_MARK 0x80000000u
/* NVRM_ESC_SURF_DIRTY with this plane bit gives up with NVRM_SS_BUSY instead of waiting for the accelerator lock
 * (accelerators that publish NVAccelSurfaceWait) */
#define NVRM_SS_PLANE_TRYLOCK  0x40000000u
/* GPU-decoded bi-planar frame. VRAM: low bit selects the plane, READ requires a published
 * frame, pad returns its backing cookie. DIRTY: size packs the two 32-bit cookies (Y low).
 * The old driver rejects these values as unsupported plane indices. */
#define NVRM_SS_PLANE_VIDEO_FRAME 0x20000000u
#define NVRM_SS_VIDEO_READ 2u
/* Single-plane compositor surfaces. DIRTY with COOKIE looks up the calling task's
 * plane-0 backing and returns its cookie as status (SID 0 probes the selector).
 * VRAM with COOKIE | cookie exports only that allocation. The 28-bit cookie must
 * fit in full; larger allocation serials use the unguarded, uncached path. */
#define NVRM_SS_PLANE_COOKIE 0x10000000u
#define NVRM_SS_COOKIE_MAX   0x0fffffffu
#define NVRM_SS_BAD_VERSION   0x80000001u
#define NVRM_SS_OFF           0x80000002u
#define NVRM_SS_NO_SURFACE    0x80000003u
#define NVRM_SS_NO_CACHE      0x80000004u
#define NVRM_SS_NO_VRAM       0x80000005u
#define NVRM_SS_TOO_SMALL     0x80000006u
#define NVRM_SS_BUSY          0x80000007u
struct NVRMSurfShareEsc {
    unsigned int version;
    unsigned int surfaceID;
    unsigned int plane;
    unsigned int hClient;
    unsigned int hSrcClient;
    unsigned int hSrcMemory;
    unsigned int status;
    unsigned int pad;
    unsigned long long size;
};

#ifdef NVRM_SS_KERNEL_ABI
#define NVACCEL_SS_FN   "nvSurfVram"
#define NVRM_SS_FN_PAGEOFF "nvSurfPageoff"
#define NVRM_SS_FN_RM   "nvSurfShareRm"
#define NVRM_SS_RM_VERSION 2u
enum { NVRM_SS_RM_SHARE_KAPI = 5 };
struct NVRMSurfShareRm {
    unsigned int version, op;
    void *kapiDevice, *kapiMemory;
    unsigned int target;
    unsigned int hClient, hObject;
    unsigned int status;
};
struct NVRMSurfPageoff {
    unsigned int version;
    unsigned int status;
    void *kapiDevice, *kapiMemory;
    const unsigned long long *pages;
    unsigned long long pageCount;
    unsigned long long length;
    unsigned long long usec;
};
#endif
