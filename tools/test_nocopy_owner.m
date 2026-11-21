// Isolated hardware check. Pass the NVMTLDriver executable to test.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <IOKit/IOKitLib.h>
#import <objc/message.h>
#import <objc/runtime.h>
#include <assert.h>
#include <dlfcn.h>
#include <mach/mach_vm.h>
#include <sys/mman.h>
#include "nvmtl_vk.h"

static void observed(void *context) { ++*(unsigned *)context; }

static nvk_buffer *buffer_backing(id<MTLBuffer> buffer, const char *name) {
    Ivar field = class_getInstanceVariable(buffer.class, name);
    assert(field);
    return (nvk_buffer *)((char *)(__bridge void *)buffer + ivar_getOffset(field));
}

static void *borrowed_mapping(size_t length) {
    void *address = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    assert(address != MAP_FAILED);
    return address;
}

static id<MTLBuffer> import_borrowed(id<MTLDevice> device, void *address, size_t size, BOOL expectAlias) {
    uint64_t start = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    id<MTLBuffer> buffer = [device newBufferWithBytesNoCopy:address length:size
        options:MTLResourceStorageModeShared deallocator:nil];
    double elapsedMS = (clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - start) / 1e6;
    assert(buffer && buffer.contents == address);
    assert((buffer_backing(buffer, "_b")->host_origin == NVMTL_HOST_NOCOPY_ALIAS) == expectAlias);
    printf("PASS: %zu-byte borrow (%s), import %.3f ms\n", size, expectAlias ? "alias" : "fallback", elapsedMS);
    return buffer;
}

static void check_large_alias(id<MTLDevice> device, id<MTLBuffer> buffer) {
    const NSUInteger page = getpagesize();
    const NSUInteger last = buffer.length - page;
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLBuffer> output = [device newBufferWithLength:page * 2 options:MTLResourceStorageModeShared];
    uint8_t *bytes = buffer.contents;
    memset(bytes, 0x37, page);
    memset(bytes + last, 0x61, page);
    id<MTLCommandBuffer> command = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
    [blit copyFromBuffer:buffer sourceOffset:0 toBuffer:output destinationOffset:0 size:page];
    [blit copyFromBuffer:buffer sourceOffset:last toBuffer:output destinationOffset:page size:page];
    [blit fillBuffer:buffer range:NSMakeRange(0, page) value:0x59];
    [blit fillBuffer:buffer range:NSMakeRange(last, page) value:0x63];
    [blit endEncoding]; [command commit]; [command waitUntilCompleted]; assert(!command.error);
    const uint8_t *copied = output.contents;
    for (NSUInteger i = 0; i < page; i++) {
        assert(copied[i] == 0x37 && copied[page + i] == 0x61);
        assert(bytes[i] == 0x59 && bytes[last + i] == 0x63);
    }
}

static void wait_for_alias_budget(id<MTLDevice> device, void *address) {
    uint64_t end = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) + 5 * NSEC_PER_SEC;
    do {
        @autoreleasepool {
            id<MTLBuffer> probe = [device newBufferWithBytesNoCopy:address length:getpagesize()
                options:MTLResourceStorageModeShared deallocator:nil];
            assert(probe);
            if (buffer_backing(probe, "_b")->host_origin == NVMTL_HOST_NOCOPY_ALIAS) return;
        }
        usleep(1000);
    } while (clock_gettime_nsec_np(CLOCK_UPTIME_RAW) < end);
    assert(!"alias quota was not reclaimed");
}

static void check_alias_limit(id<MTLDevice> device) {
    const size_t cap = NVMTL_WS_NOCOPY_ALIAS_BYTES;
    const size_t page = getpagesize();
    void *oversized = borrowed_mapping(cap + page);
    @autoreleasepool {
        (void)import_borrowed(device, oversized, cap + page, NO);
    }
    assert(!munmap(oversized, cap + page));

    void *full = borrowed_mapping(cap);
    void *small = borrowed_mapping(page);
    uint64_t releaseStart;
    @autoreleasepool {
        __attribute__((objc_precise_lifetime)) id<MTLBuffer> exact = import_borrowed(device, full, cap, YES);
        check_large_alias(device, exact);
        (void)import_borrowed(device, small, page, NO);
        releaseStart = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    }
    uint64_t wrapperReturned = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    assert(!munmap(full, cap));
    wait_for_alias_budget(device, small);
    double wrapperMS = (wrapperReturned - releaseStart) / 1e6;
    double quotaMS = (clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - releaseStart) / 1e6;
    printf("PASS: 512 MiB wrapper release %.3f ms, alias quota reusable within %.3f ms\n", wrapperMS, quotaMS);
    assert(!munmap(small, page));
}

static void check_alias_pressure(id<MTLDevice> device) {
    const size_t half = NVMTL_WS_NOCOPY_ALIAS_BYTES / 2;
    const size_t page = getpagesize();
    void *first = borrowed_mapping(half), *second = borrowed_mapping(half), *over = borrowed_mapping(page);
    @autoreleasepool {
        __attribute__((objc_precise_lifetime)) id<MTLBuffer> one = import_borrowed(device, first, half, YES);
        __attribute__((objc_precise_lifetime)) id<MTLBuffer> two = import_borrowed(device, second, half, YES);
        (void)import_borrowed(device, over, page, NO);
    }
    assert(!munmap(first, half) && !munmap(second, half));
    wait_for_alias_budget(device, over);
    assert(!munmap(over, page));
}

int main(int argc, char **argv) { @autoreleasepool {
    const BOOL aliases = argc == 3 && !strcmp(argv[2], "--aliases");
    const BOOL limit = argc == 3 && !strcmp(argv[2], "--alias-limit");
    const BOOL pressure = argc == 3 && !strcmp(argv[2], "--alias-pressure");
    assert(argc == 2 || aliases || limit || pressure);
    const BOOL enableAliases = aliases || limit || pressure;
    assert(!setenv("NVMTL_WS_NOCOPY_ALIAS", enableAliases ? "1" : "0", 1));
    if (enableAliases) assert(!setenv("NVMTL_WS_DEFER_BUFFER_FREE", "1", 1));
    void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    assert(lib);
    Class deviceClass = NSClassFromString(@"NVMTLDevice");
    char actual[PATH_MAX], expected[PATH_MAX];
    assert(deviceClass && realpath(class_getImageName(deviceClass), actual) &&
           realpath(argv[1], expected) && !strcmp(actual, expected));
    io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
    assert(service);
    id<MTLDevice> device = ((id (*)(id, SEL, mach_port_t))objc_msgSend)(
        [deviceClass alloc], NSSelectorFromString(@"initWithAcceleratorPort:"), service);
    IOObjectRelease(service); assert(device);
#define LOAD(name) __typeof__(&nvmtl_vk_##name) name = dlsym(lib, "nvmtl_vk_" #name); assert(name)
    LOAD(queue_create); LOAD(queue_destroy); LOAD(buffer_create); LOAD(buffer_destroy);
    LOAD(cmd_begin); LOAD(cmd_fill_buffer); LOAD(inflight_new); LOAD(inflight_free);
    LOAD(submit_begin_at); LOAD(submit_finish_observed);
    nvk_queue raw = {0}; nvk_buffer scratch = {0};
    assert(!queue_create(&raw) && !buffer_create(4096, 1, &scratch));
    if (limit) check_alias_limit(device);
    if (pressure) check_alias_pressure(device);
    const unsigned smallCases = limit || pressure ? 0 : (aliases ? 5 : 4);
    for (unsigned arm = 0; arm < smallCases; arm++) {
        const BOOL managed = arm == 1, fallback = arm == 2, owned = arm < 3;
        const BOOL alias = aliases && !owned;
        const NSUInteger length = (arm == 4 ? 20 : 64) * 1024;
        void *allocation = NULL;
        if (owned) assert(!posix_memalign(&allocation, 65536, 131072));
        else allocation = borrowed_mapping(length);
        uint8_t *ptr = (uint8_t *)allocation + fallback;
        memset(ptr, 0x11, length);
        dispatch_semaphore_t returned = dispatch_semaphore_create(0);
        __block unsigned callbacks = 0;
        nvk_cmdbuf marker = {0};
        nvmtl_inflight *fence = inflight_new(); assert(fence);
        @autoreleasepool {
            void (^release)(void *, NSUInteger) = ^(void *p, NSUInteger n) {
                assert(p == ptr && n == length && ++callbacks == 1);
                dispatch_semaphore_signal(returned);
            };
            id<MTLBuffer> source = [device newBufferWithBytesNoCopy:ptr length:length
                options:managed ? MTLResourceStorageModeManaged : MTLResourceStorageModeShared
                deallocator:owned ? release : nil];
            assert(source && source.contents == ptr);
            nvk_buffer *backing = buffer_backing(source, managed ? "_shadow" : "_b");
            assert(!!backing->imported == !fallback);
            if (!fallback) {
                unsigned origin = managed ? NVMTL_HOST_NOCOPY_MANAGED : NVMTL_HOST_NOCOPY_DIRECT;
                if (alias) origin = NVMTL_HOST_NOCOPY_ALIAS;
                assert(backing->host_origin == origin && backing->host_import_id);
            }
            id<MTLCommandQueue> q = [device newCommandQueue];
            id<MTLBuffer> dst = [device newBufferWithLength:length options:MTLResourceStorageModeShared];
            memset(ptr, 0x37, length);
            if (managed) [source didModifyRange:NSMakeRange(0, length)];
            id<MTLCommandBuffer> cb = [q commandBuffer];
            id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
            [blit copyFromBuffer:source sourceOffset:0 toBuffer:dst destinationOffset:0 size:length];
            [blit fillBuffer:source range:NSMakeRange(0, length) value:0x59];
            if (managed) [blit synchronizeResource:source];
            [blit endEncoding]; [cb commit]; [cb waitUntilCompleted]; assert(!cb.error);
            const uint8_t *copied = dst.contents;
            for (NSUInteger i=0; i<length; i++) {
                assert(copied[i]==0x37);
                assert(ptr[i]==0x59);
            }
            // Hold a real submission's fence observation while the last Metal reference is released.
            assert(!cmd_begin(&raw, &marker) && !cmd_fill_buffer(&marker, &scratch, 0, 4096, 0x21));
            assert(!submit_begin_at(&marker, fence, "owned host lifetime probe"));
        }
        if (!fallback) assert(dispatch_semaphore_wait(returned, DISPATCH_TIME_NOW) != 0);
        if (alias) {
            // The caller can release and reuse the original virtual address while the driver owns the alias.
            assert(!munmap(ptr, length));
            mach_vm_address_t replacement = (uintptr_t)ptr;
            assert(mach_vm_allocate(mach_task_self(), &replacement, length, VM_FLAGS_FIXED) == KERN_SUCCESS);
            memset(ptr, 0xa8, length);
        }
        unsigned fences=0;
        assert(!submit_finish_observed(fence, observed, &fences) && fences==1);
        inflight_free(fence);
        if (owned) assert(!dispatch_semaphore_wait(returned,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)));
        assert(callbacks==(unsigned)owned);
        if (alias) for (NSUInteger i=0; i<length; i++) assert(ptr[i] == 0xa8);
        if (owned) free(allocation);
        else assert(!munmap(ptr, length));
        printf("PASS: %s (%lu bytes), CPU and GPU visibility, caller ownership and release\n",
               managed ? "managed host shadow" : fallback ? "unaligned copy fallback" :
               owned ? "shared host import" : alias ? "caller-managed shared alias" : "caller-managed direct import",
               (unsigned long)length);
    }
    buffer_destroy(&scratch); queue_destroy(&raw);
} }
