#pragma once
#include <stddef.h>
#include <stdint.h>

enum {
    NVMTL_HOST_UNKNOWN, NVMTL_HOST_NOCOPY_DIRECT, NVMTL_HOST_NOCOPY_MANAGED, NVMTL_HOST_NOCOPY_ALIAS,
    NVMTL_HOST_SURFACE_LEASE, NVMTL_HOST_SURFACE_CACHE, NVMTL_HOST_SURFACE_TEMP, NVMTL_HOST_SURFACE_CACHE_ALIAS,
    NVMTL_HOST_ORIGIN_COUNT
};
#define NVMTL_HOST_SURFACE_READBACK 0x80u
#define NVMTL_WS_NOCOPY_ALIAS_BYTES (512ull << 20)

static inline int nvmtl_host_origin_is_alias(uint8_t origin)
{
    return origin == NVMTL_HOST_NOCOPY_ALIAS || origin == NVMTL_HOST_SURFACE_CACHE_ALIAS;
}

typedef struct {
    void *address;
    size_t bytes;
} nvmtl_host_alias;

typedef struct {
    void *context;
    void (*release)(void *);
} nvmtl_host_owner;
