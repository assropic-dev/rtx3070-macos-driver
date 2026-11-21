// Run on a Mac with NVAccel. This process loads the specified complete driver bundle and opts in to the same
// upload policy as WindowServer. It creates private test surfaces and does not need an install or a restart.
// clang -O2 -fobjc-arc tools/test_ws_surface_upload.m -framework Foundation -framework Metal -framework IOKit -framework IOSurface -o /tmp/test_ws_surface_upload
// /tmp/test_ws_surface_upload /path/to/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver
#import <Foundation/Foundation.h>
#import <IOSurface/IOSurface.h>
#import <IOKit/IOKitLib.h>
#import <Metal/Metal.h>
#import <objc/message.h>
#import <objc/runtime.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void (*SnapshotStats)(uint64_t [3]);
typedef struct {
    id<MTLCommandBuffer> __strong command;
    id<MTLTexture> __strong target;
    id<MTLBuffer> __strong output;
    unsigned expected;
} Frame;

typedef struct {
    id __strong surface;
    id<MTLTexture> __strong textures[2];
    unsigned version;
} Input;

static uint32_t pixel(unsigned version, unsigned x, unsigned y)
{
    return 0xff000000u | ((113 * version + 27 * x + 3 * y) & 255u) << 16 |
                          ((91 * version + 33 * x + 5 * y) & 255u) << 8 |
                           ((51 * version + 7 * x + 113 * y) & 255u);
}

static bool writePattern(IOSurfaceRef s, unsigned version, unsigned width, unsigned height)
{
    if (IOSurfaceLock(s, 0, NULL) != kIOReturnSuccess) return false;
    uint8_t *base = IOSurfaceGetBaseAddress(s);
    size_t stride = IOSurfaceGetBytesPerRow(s);
    for (unsigned y = 0; y < height; y++) {
        uint32_t *row = (uint32_t *)(base + stride * y);
        for (unsigned x = 0; x < width; x++) row[x] = pixel(version, x, y);
        memset((uint8_t *)row + width * 4, 0xd3, stride - width * 4);
    }
    return IOSurfaceUnlock(s, 0, NULL) == kIOReturnSuccess;
}

static bool verify(Frame *frame, size_t stride, unsigned width, unsigned height)
{
    if (!frame->command) return true;
    [frame->command waitUntilCompleted];
    if (frame->command.status != MTLCommandBufferStatusCompleted) {
        fprintf(stderr, "GPU command failed: %s\n", frame->command.error.description.UTF8String);
        return false;
    }
    const uint8_t *pixels = frame->output.contents;
    for (unsigned y = 0; y < height; y++) {
        const uint32_t *row = (const uint32_t *)(pixels + y * stride);
        for (unsigned x = 0; x < width; x++) {
            uint32_t want = pixel(frame->expected, x, y);
            if (row[x] != want) {
                fprintf(stderr, "generation %u pixel (%u,%u) returned %08x, expected %08x\n",
                        frame->expected, x, y, row[x], want);
                return false;
            }
        }
    }
    frame->command = nil;
    return true;
}

static id<MTLRenderPipelineState> pipeline(id<MTLDevice> device)
{
    NSString *source =
        @"#include <metal_stdlib>\nusing namespace metal;\n"
         "vertex float4 v(uint i [[vertex_id]]) { float2 xy = float2((i << 1) & 2, i & 2); return float4(xy * 2 - 1, 0, 1); }\n"
         "fragment float4 f(float4 pos [[position]], texture2d<float,access::read> t [[texture(0)]]) { return t.read(uint2(pos.xy)); }\n";
    NSError *error = nil;
    id<MTLLibrary> lib = [device newLibraryWithSource:source options:nil error:&error];
    if (!lib) { fprintf(stderr, "test shaders: %s\n", error.description.UTF8String); return nil; }
    MTLRenderPipelineDescriptor *d = [MTLRenderPipelineDescriptor new];
    d.vertexFunction = [lib newFunctionWithName:@"v"];
    d.fragmentFunction = [lib newFunctionWithName:@"f"];
    d.colorAttachments[0].pixelFormat = MTLPixelFormatBGRA8Unorm;
    id<MTLRenderPipelineState> p = [device newRenderPipelineStateWithDescriptor:d error:&error];
    if (!p) fprintf(stderr, "test pipeline: %s\n", error.description.UTF8String);
    return p;
}

static void encodeRead(id<MTLCommandBuffer> command, id<MTLRenderPipelineState> p, id<MTLTexture> source,
                       id<MTLTexture> target, id<MTLBuffer> out, size_t stride, unsigned width, unsigned height)
{
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = target;
    pass.colorAttachments[0].loadAction = MTLLoadActionDontCare;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    id<MTLRenderCommandEncoder> render = [command renderCommandEncoderWithDescriptor:pass];
    [render setRenderPipelineState:p];
    [render setFragmentTexture:source atIndex:0];
    [render drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [render endEncoding];
    id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
    [blit copyFromTexture:target sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0)
               sourceSize:MTLSizeMake(width,height,1) toBuffer:out destinationOffset:0
               destinationBytesPerRow:stride destinationBytesPerImage:stride * height];
    [blit endEncoding];
}

static bool run(id<MTLDevice> device, id<MTLRenderPipelineState> p, SnapshotStats stats,
                unsigned width, unsigned height, unsigned updateEvery, bool rewriteAfterBind, bool eligible, bool waits)
{
    const unsigned count = 24;
    size_t stride = ((size_t)width * 4 + 255) & ~(size_t)255;
    NSDictionary *properties = @{
        (id)kIOSurfaceWidth:@(width), (id)kIOSurfaceHeight:@(height), (id)kIOSurfaceBytesPerElement:@4,
        (id)kIOSurfaceBytesPerRow:@(stride + 64), (id)kIOSurfacePixelFormat:@((uint32_t)'BGRA')
    };
    MTLTextureDescriptor *input = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:width height:height mipmapped:NO];
    input.storageMode = MTLStorageModeShared;
    input.usage = MTLTextureUsageShaderRead;
    // Snapshot reads can all reuse the same surface as soon as it is bound.
    // For live reads each in-flight frame owns its source until verify() completes it.
    enum { depth = 4 };
    unsigned inputCount = eligible && !waits ? 1 : depth;
    Input inputs[depth] = {};
    for (unsigned i = 0; i < inputCount; i++) {
        IOSurfaceRef surface = IOSurfaceCreate((__bridge CFDictionaryRef)properties);
        if (!surface) return false;
        inputs[i].surface = CFBridgingRelease(surface);
        inputs[i].version = UINT_MAX;
        for (unsigned j = 0; j < 2; j++) {
            inputs[i].textures[j] = [device newTextureWithDescriptor:input iosurface:surface plane:0];
            if (!inputs[i].textures[j]) return false;
        }
    }
    id<MTLCommandQueue> queue[2] = { [device newCommandQueue], [device newCommandQueue] };
    id<MTLSharedEvent> timeline = [device newSharedEvent];
    if (!queue[0] || !queue[1] || !timeline) return false;
    MTLTextureDescriptor *target = [input copy];
    target.usage = MTLTextureUsageRenderTarget;
    target.storageMode = MTLStorageModePrivate;
    Frame frames[depth] = {};
    for (unsigned i = 0; i < depth; i++) {
        frames[i].target = [device newTextureWithDescriptor:target];
        frames[i].output = [device newBufferWithLength:stride * height options:MTLResourceStorageModeShared];
        if (!frames[i].target || !frames[i].output) return false;
    }
    uint64_t before[3], after[3];
    stats(before);
    for (unsigned i = 0; i < count; i++) @autoreleasepool {
        Frame *frame = &frames[i % depth];
        if (!verify(frame, stride, width, height)) return false;
        Input *source = &inputs[i % inputCount];
        IOSurfaceRef surface = (__bridge IOSurfaceRef)source->surface;
        unsigned version = i / updateEvery;
        if (source->version != version) {
            if (!writePattern(surface, version, width, height)) return false;
            source->version = version;
        }
        frame->expected = version;
        frame->command = [queue[waits ? i % 2 : 0] commandBuffer];
        if (waits && i) [frame->command encodeWaitForEvent:timeline value:i];
        encodeRead(frame->command, p, source->textures[(i / inputCount) % 2], frame->target, frame->output, stride, width, height);
        if (waits) [frame->command encodeSignalEvent:timeline value:i + 1];
        // After an explicit wait the command reads the live input. Only the snapshot path
        // promises to preserve the bound version across a CPU rewrite before commit.
        if (rewriteAfterBind && eligible && !(waits && i)) {
            if (!writePattern(surface, version + 97, width, height)) return false;
            source->version = version + 97;
        }
        [frame->command commit];
    }
    for (unsigned i = 0; i < depth; i++) if (!verify(&frames[i], stride, width, height)) return false;
    stats(after);
    uint64_t uploads = after[0] - before[0], hits = after[1] - before[1];
    uint64_t bytes = after[2] - before[2];
    // An explicit wait can order a future GPU producer. Consumers recorded after such a wait must use the live resource.
    unsigned expectedFast = eligible ? (waits ? 1 : count) : 0;
    bool ok = uploads + hits == expectedFast && bytes == uploads * width * height * 4 &&
              (updateEvery == 1 ? uploads == expectedFast : uploads && hits);
    printf("%ux%u update every %u, rewrite %d, wait %d: %llu uploads, %llu reuse, %s\n",
           width, height, updateEvery, rewriteAfterBind, waits, uploads, hits, ok ? "pass" : "FAIL");
    return ok;
}

// Record the compositor first. Before submitting it, submit and complete a later-recorded GPU writer to the same
// IOSurface through a different wrapper. Reading the early CPU snapshot here would return the pattern, not the clear.
static bool reorderedWriter(id<MTLDevice> d, id<MTLRenderPipelineState> p, bool crossQueue, bool red)
{
    const unsigned size = 32;
    const size_t row = 256;
    IOSurfaceRef s = IOSurfaceCreate((__bridge CFDictionaryRef)@{
        (id)kIOSurfaceWidth:@(size), (id)kIOSurfaceHeight:@(size), (id)kIOSurfaceBytesPerElement:@4,
        (id)kIOSurfacePixelFormat:@((uint32_t)'BGRA') });
    if (!s) return false;
    __attribute__((objc_precise_lifetime)) id hold = CFBridgingRelease(s);
    (void)hold;
    if (!writePattern(s, 5, size, size)) return false;
    MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:size height:size mipmapped:NO];
    td.storageMode = MTLStorageModeShared;
    td.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> src = [d newTextureWithDescriptor:td iosurface:s plane:0];
    td.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    id<MTLTexture> writable = [d newTextureWithDescriptor:td iosurface:s plane:0];
    td.storageMode = MTLStorageModePrivate;
    id<MTLTexture> target = [d newTextureWithDescriptor:td];
    id<MTLBuffer> output = [d newBufferWithLength:row * size options:MTLResourceStorageModeShared];
    id<MTLCommandQueue> readerQueue = [d newCommandQueue], writerQueue = crossQueue ? [d newCommandQueue] : readerQueue;
    id<MTLEvent> event = crossQueue ? [d newEvent] : nil;
    if (!src || !writable || !target || !output || !readerQueue || !writerQueue || (crossQueue && !event)) return false;

    id<MTLCommandBuffer> reader = [readerQueue commandBuffer];
    if (event) [reader encodeWaitForEvent:event value:1];
    encodeRead(reader, p, src, target, output, row, size, size);

    id<MTLCommandBuffer> writer = [writerQueue commandBuffer];
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = writable;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(red ? 1 : 0, 0, red ? 0 : 1, 1);
    [[writer renderCommandEncoderWithDescriptor:pass] endEncoding];
    if (event) [writer encodeSignalEvent:event value:1];
    [writer commit]; [writer waitUntilCompleted];
    if (writer.status != MTLCommandBufferStatusCompleted) return false;
    [reader commit]; [reader waitUntilCompleted];
    if (reader.status != MTLCommandBufferStatusCompleted) return false;

    uint32_t want = red ? 0xffff0000 : 0xff0000ff;
    for (unsigned y = 0; y < size; y++) {
        const uint32_t *pixels = (const uint32_t *)((const uint8_t *)output.contents + row * y);
        for (unsigned x = 0; x < size; x++) if (pixels[x] != want) {
            fprintf(stderr, "late writer, %s: pixel (%u,%u) was %08x, expected %08x\n",
                    crossQueue ? "different queues" : "same queue", x, y, pixels[x], want);
            return false;
        }
    }
    printf("reader recorded before %s writer, GPU color %08x: pass\n", crossQueue ? "cross-queue" : "same-queue", want);
    return true;
}

int main(int argc, char **argv)
{
    if (argc != 2) { fprintf(stderr, "usage: %s /path/to/driver/binary\n", argv[0]); return 2; }
    @autoreleasepool {
        setenv("NVMTL_PROCESS_ENV", "1", 1);
        setenv("NVMTL_WS_ASYNC_SURFIN", "1", 1);
        void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        if (!lib) { fprintf(stderr, "%s\n", dlerror()); return 1; }
        Class cls = NSClassFromString(@"NVMTLDevice");
        char wanted[PATH_MAX], actual[PATH_MAX];
        if (!cls || !realpath(class_getImageName(cls), actual) || !realpath(argv[1], wanted) || strcmp(actual, wanted)) {
            fprintf(stderr, "the requested NVMTLDevice class is not loaded\n");
            return 1;
        }
        SnapshotStats stats = (SnapshotStats)dlsym(lib, "nvmtl_ws_snapshot_stats");
        io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
        if (!svc || !stats) return 1;
        id<MTLDevice> device = ((id (*)(id, SEL, mach_port_t))objc_msgSend)(
            [cls alloc], NSSelectorFromString(@"initWithAcceleratorPort:"), svc);
        IOObjectRelease(svc);
        id<MTLRenderPipelineState> p = pipeline(device);
        if (!p) return 1;
        if (!run(device, p, stats, 257, 193, 1, true, true, false) ||
            !run(device, p, stats, 257, 193, 4, false, true, false) ||
            !run(device, p, stats, 257, 193, 1, true, true, true) ||
            !run(device, p, stats, 1024, 512, 1, false, false, false)) return 1;
        for (int cross = 0; cross < 2; cross++)
            for (int red = 0; red < 2; red++)
                if (!reorderedWriter(device, p, cross, red)) return 1;
        return 0;
    }
}
