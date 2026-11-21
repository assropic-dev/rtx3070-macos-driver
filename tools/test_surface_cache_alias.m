// Isolated RTX hardware regression. Pass a private NVMTLDriver executable,
// "alias" or "direct", and the number of fresh IOSurfaces to exercise. An optional
// "cache" argument times cache detachment separately on a matching candidate.
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#import <IOSurface/IOSurface.h>
#import <Metal/Metal.h>
#import <objc/message.h>
#import <objc/runtime.h>
#include <assert.h>
#include <dlfcn.h>
#include <time.h>
#include "nvmtl_vk.h"

_Static_assert(offsetof(nvmtl_host_page_cache, buffer) == 0, "initial cache buffer moved");

static uint64_t uptime(void)
{
    return clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
}

static uint32_t pixel(size_t index, uint32_t seed)
{
    return (uint32_t)index * 2654435761u ^ seed;
}

static void write_surface(IOSurfaceRef surface, size_t bytes, uint32_t seed)
{
    assert(IOSurfaceLock(surface, 0, NULL) == kIOReturnSuccess);
    uint32_t *data = IOSurfaceGetBaseAddress(surface);
    assert(data);
    for (size_t i = 0; i < bytes / sizeof *data; i++) data[i] = pixel(i, seed);
    assert(IOSurfaceUnlock(surface, 0, NULL) == kIOReturnSuccess);
}

static void check_texture(id<MTLCommandQueue> queue, id<MTLTexture> texture, id<MTLBuffer> output,
                          NSUInteger row, NSUInteger bytes, uint32_t seed)
{
    @autoreleasepool {
        id<MTLCommandBuffer> command = [queue commandBuffer];
        id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
        assert(command && blit);
        [blit copyFromTexture:texture sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
                   sourceSize:MTLSizeMake(texture.width, texture.height, 1) toBuffer:output
            destinationOffset:0 destinationBytesPerRow:row destinationBytesPerImage:bytes];
        [blit endEncoding];
        [command commit];
        [command waitUntilCompleted];
        assert(command.status == MTLCommandBufferStatusCompleted);
        const uint32_t *data = output.contents;
        for (size_t i = 0; i < bytes / sizeof *data; i++) assert(data[i] == pixel(i, seed));
    }
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        const BOOL timeCache = argc == 5 && !strcmp(argv[4], "cache");
        assert(argc == 4 || timeCache);
        const BOOL aliases = !strcmp(argv[2], "alias");
        assert(aliases || !strcmp(argv[2], "direct"));
        const int iterations = atoi(argv[3]);
        assert(iterations > 0);
        assert(!setenv("NVMTL_WS_SURFACE_CACHE_ALIAS", aliases ? "1" : "0", 1));
        assert(!setenv("NVMTL_WS_DEFER_BUFFER_FREE", "1", 1));
        assert(!setenv("NVMTL_NO_SURFACE_SHARE", "1", 1));
        assert(!setenv("NVMTL_SURFIN_UPLOAD", "host", 1));
        void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        assert(library);
        void (*dropCache)(nvmtl_host_page_cache *) = timeCache ? dlsym(library, "nvmtl_vk_host_pages_drop") : NULL;
        assert(!timeCache || dropCache);
        Class cls = NSClassFromString(@"NVMTLDevice");
        char actual[PATH_MAX], expected[PATH_MAX];
        assert(cls && realpath(class_getImageName(cls), actual) && realpath(argv[1], expected) && !strcmp(actual, expected));

        io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
        assert(service);
        id<MTLDevice> device = ((id (*)(id, SEL, mach_port_t))objc_msgSend)(
            [cls alloc], NSSelectorFromString(@"initWithAcceleratorPort:"), service);
        IOObjectRelease(service);
        id<MTLCommandQueue> queue = [device newCommandQueue];
        const NSUInteger width = 4608, height = 4414;
        const NSUInteger row = width * sizeof(uint32_t), bytes = row * height;
        assert(bytes == 81358848);
        id<MTLBuffer> output = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
        assert(queue && output);
        MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                                                             width:width height:height mipmapped:NO];
        descriptor.storageMode = MTLStorageModePrivate;
        descriptor.usage = MTLTextureUsageShaderRead;
        printf("bytes,iteration,first_upload_ms,reused_upload_ms,wrapper_release_ms,import_id%s\n",
               timeCache ? ",cache_release_ms" : "");
        for (int i = 0; i < iterations; i++) @autoreleasepool {
            NSDictionary *properties = @{
                (id)kIOSurfaceWidth:@(width), (id)kIOSurfaceHeight:@(height), (id)kIOSurfaceBytesPerElement:@4,
                (id)kIOSurfaceBytesPerRow:@(row), (id)kIOSurfaceAllocSize:@(bytes), (id)kIOSurfacePixelFormat:@((uint32_t)'BGRA'),
            };
            IOSurfaceRef surface = IOSurfaceCreate((__bridge CFDictionaryRef)properties);
            assert(surface && IOSurfaceGetAllocSize(surface) == bytes && IOSurfaceGetBytesPerRow(surface) == row);
            __attribute__((objc_precise_lifetime)) id<MTLTexture> texture =
                [device newTextureWithDescriptor:descriptor iosurface:surface plane:0];
            assert(texture);
            Ivar field = class_getInstanceVariable(texture.class, "_hostPages");
            assert(field);
            NSUInteger cacheSize = 0;
            assert(NSGetSizeAndAlignment(ivar_getTypeEncoding(field), &cacheSize, NULL));
            assert(cacheSize == sizeof(nvmtl_host_page_cache) || (!timeCache && cacheSize == sizeof(nvk_buffer)));
            nvk_buffer *cache = (nvk_buffer *)((char *)(__bridge void *)texture + ivar_getOffset(field));
            SEL surfaceIn = NSSelectorFromString(@"nvmtlSurfaceIn");
            const uint32_t seed = (uint32_t)i * 17 + 1;
            write_surface(surface, bytes, seed);
            uint64_t firstStart = uptime();
            assert(((BOOL (*)(id, SEL))objc_msgSend)(texture, surfaceIn));
            uint64_t firstEnd = uptime();
            assert(cache->imported && cache->map == IOSurfaceGetBaseAddress(surface) && cache->size == bytes);
            assert(cache->host_origin == (aliases ? NVMTL_HOST_SURFACE_CACHE_ALIAS : NVMTL_HOST_SURFACE_CACHE));
            const uint64_t importID = cache->host_import_id;
            assert(importID);
            check_texture(queue, texture, output, row, bytes, seed);

            write_surface(surface, bytes, seed + 1);
            uint64_t reuseStart = uptime();
            assert(((BOOL (*)(id, SEL))objc_msgSend)(texture, surfaceIn));
            uint64_t reuseEnd = uptime();
            assert(cache->host_import_id == importID);
            check_texture(queue, texture, output, row, bytes, seed + 1);

            double cacheMS = 0;
            if (timeCache) {
                uint64_t cacheStart = uptime();
                dropCache((nvmtl_host_page_cache *)cache);
                cacheMS = (uptime() - cacheStart) / 1e6;
                assert(!cache->buf);
            }
            uint64_t releaseStart = uptime();
            texture = nil;
            uint64_t releaseEnd = uptime();
            CFRelease(surface);
            printf("%lu,%d,%.6f,%.6f,%.6f,%llu", (unsigned long)bytes, i, (firstEnd-firstStart)/1e6,
                (reuseEnd-reuseStart)/1e6, (releaseEnd-releaseStart)/1e6, (unsigned long long)importID);
            if (timeCache) printf(",%.6f", cacheMS);
            putchar('\n');
        }
    }
}
