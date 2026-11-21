// Process-local, explicitly enabled observation of public no-copy construction
// and synchronous IOSurface upload. IDs never encode addresses. No bytes from
// caller allocations or submitted images enter the log.
#pragma once
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

void nvlog_every(const char *, ...);
typedef struct {
    uint64_t id, begin, refused, fallback, thread, descriptor_created_ns;
    const char *source;
} nvmtl_import_trace;

static uint64_t nvmtl_import_now(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }

static unsigned nvmtl_import_trace_limit(void)
{
    static unsigned limit;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        const char *on = getenv("NVMTL_HOST_IMPORT_TRACE");
        if (!on || strcmp(on, "1")) return;
        limit = 128; // per source; reserve room for both APIs
        const char *value = getenv("NVMTL_HOST_IMPORT_TRACE_LIMIT");
        if (value && *value >= '0' && *value <= '9') {
            char *end;
            unsigned long n = strtoul(value, &end, 10);
            if (!*end && n <= 4096) limit = (unsigned)n;
        }
    });
    return limit;
}

static nvmtl_import_trace nvmtl_import_begin(unsigned source, size_t bytes, int owner,
                                            uint32_t sid, size_t plane, uint32_t seed, uint64_t cookie)
{
    unsigned limit = nvmtl_import_trace_limit();
    nvmtl_import_trace t = {0};
    if (!limit) return t;
    static _Atomic unsigned counts[2];
    if (atomic_load_explicit(&counts[source], memory_order_relaxed) >= limit) return t;
    unsigned serial = atomic_fetch_add_explicit(&counts[source], 1, memory_order_relaxed);
    if (serial >= limit) return t;
    t.id = (uint64_t)serial * 2 + 1 + source;
    t.source = source ? "IOSurfaceUpload" : "newBufferWithBytesNoCopy";
    t.begin = nvmtl_import_now();
    pthread_threadid_np(NULL, &t.thread);
    nvlog_every("host import trace v1 pid=%d tid=%llu id=%llu event=request source=%s bytes=%zu "
                "request_ns=%llu callback_owner=%d sid=%u plane=%zu seed=%u cookie=%llu backing_release=unknown",
                getpid(), t.thread, t.id, t.source, bytes, t.begin, owner, sid, plane, seed, cookie);
    return t;
}

static const char *nvmtl_import_protection(const void *ptr, size_t bytes)
{
    const mach_vm_address_t p = (uintptr_t)ptr;
    mach_vm_address_t start = p;
    mach_vm_size_t size = 0;
    vm_region_basic_info_data_64_t info = {0};
    mach_msg_type_number_t count = VM_REGION_BASIC_INFO_COUNT_64;
    mach_port_t object = MACH_PORT_NULL;
    kern_return_t kr = mach_vm_region(mach_task_self(), &start, &size, VM_REGION_BASIC_INFO_64,
                                      (vm_region_info_t)&info, &count, &object);
    if (object != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), object);
    if (kr != KERN_SUCCESS || start > p || bytes > size || p-start > size-bytes) return "unknown";
    if (!(info.protection & VM_PROT_READ)) return "not_readable";
    return (info.protection & VM_PROT_WRITE) ? "rw" : "read_only";
}

static void nvmtl_import_refused(nvmtl_import_trace *t, const char *step, int rc,
                                 const void *ptr, size_t bytes)
{
    if (!t->id) return;
    t->refused = nvmtl_import_now();
    const char *protection = nvmtl_import_protection(ptr, bytes);
    nvlog_every("host import trace v1 pid=%d tid=%llu id=%llu event=declined source=%s step=%s "
                "api_rc=%d refused_ns=%llu vm_access=%s rm_status=unknown",
                getpid(), t->thread, t->id, t->source, step, rc, t->refused, protection);
}

static void nvmtl_import_done(const nvmtl_import_trace *t, const char *path, int rc, uint64_t descriptor)
{
    if (!t->id) return;
    uint64_t end = nvmtl_import_now();
    nvlog_every("host import trace v1 pid=%d tid=%llu id=%llu event=result source=%s path=%s rc=%d "
                "request_ns=%llu refused_ns=%llu fallback_begin_ns=%llu fallback_end_ns=%llu "
                "result_ns=%llu known_import_id=%llu descriptor_created_ns=%llu descriptor_release_trace=%s",
                getpid(), t->thread, t->id, t->source, path, rc, t->begin, t->refused, t->fallback,
                t->fallback ? end : 0, end, descriptor, t->descriptor_created_ns,
                descriptor && descriptor <= nvmtl_import_trace_limit() ? "eligible" : "unknown");
}

static void nvmtl_import_release(uint64_t id, const char *kind)
{
    if (id) nvlog_every("host import trace v1 pid=%d id=%llu event=%s at_ns=%llu",
                        getpid(), id, kind, nvmtl_import_now());
}

static void nvmtl_import_descriptor_released(uint64_t descriptor, uint64_t created_us)
{
    if (descriptor && descriptor <= nvmtl_import_trace_limit())
        nvlog_every("host import trace v1 pid=%d event=descriptor_free_returned known_import_id=%llu "
                    "descriptor_created_ns=%llu at_ns=%llu",
                    getpid(), descriptor, created_us*1000, nvmtl_import_now());
}

// This event occurs during an upload request's host-transfer interval on the
// same thread. Direct driver callers may have no surrounding Metal API record.
static void nvmtl_import_decision(const char *action, size_t size, int rc,
                                  int external_refused, int armed_now, uint64_t descriptor)
{
    unsigned limit = nvmtl_import_trace_limit();
    if (!limit) return;
    static _Atomic unsigned count;
    if (atomic_load_explicit(&count, memory_order_relaxed) >= limit) return;
    unsigned i = atomic_fetch_add_explicit(&count, 1, memory_order_relaxed);
    if (i >= limit) return;
    uint64_t tid = 0;
    pthread_threadid_np(NULL, &tid);
    nvlog_every("host import trace v1 pid=%d tid=%llu event=surface_import_miss attempt_id=%u "
                "action=%s bytes=%zu rc=%d invalid_external_handle=%d armed_now=%d known_import_id=%llu at_ns=%llu",
                getpid(), tid, i+1, action, size, rc, external_refused, armed_now, descriptor, nvmtl_import_now());
}
