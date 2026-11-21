"""Test the actual borrowed/owned IOSurface transfer selection and pitched row copies."""
from pathlib import Path
import subprocess
import tempfile

repo = Path(__file__).resolve().parents[1]
code = (repo / 'plugin/nvmtl_vk.c').read_text()
logic = code[code.index('// Both pointers have the surface'):code.index('int nvmtl_vk_image_write_host(')]
upload = code[code.index('int nvmtl_vk_image_write_host('):code.index('// A CPU-written IOSurface is uploaded at bind time')]
read = code[code.index('int nvmtl_vk_image_read_host('):code.index('static int nvmtl_copy_has_image(')]
end_read = read[read.index('    if (!rc && !borrowed)'):read.index('    return rc;')]
logic += '''static void end_read(int rc,nvk_buffer *borrowed,void *base,nvk_buffer *hb,size_t row_bytes,nvk_image *img,size_t bpp) {
    nvmtl_host_page_cache *cache = NULL;
''' + end_read + '}\n'
stubs = r'''
#include <dispatch/dispatch.h>
#include <pthread.h>
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <string.h>
typedef struct { void *map, *buf; size_t size; uint64_t host_import_id,host_created_us; } nvk_buffer;
typedef struct { nvk_buffer buffer; } nvmtl_host_page_cache;
typedef struct { uint32_t bpp,w,h,bw,bh,mtl_type,layers,samples,layout,fmt; void *img; } nvk_image;
enum { VK_IMAGE_ASPECT_COLOR_BIT=1, VK_FORMAT_R8_UNORM, VK_FORMAT_R8G8_UNORM, VK_FORMAT_B8G8R8A8_UNORM,
       VK_FORMAT_B8G8R8A8_SRGB,VK_FORMAT_R8G8B8A8_UNORM,VK_FORMAT_R8G8B8A8_SRGB };
static unsigned nvmtl_copy_aspect(nvk_image *img) { (void)img; return VK_IMAGE_ASPECT_COLOR_BIT; }
static bool ws=true, stage_works=true, import_works=true;
static size_t g_host_align=4096, imported; static unsigned count,leases;
static unsigned char host[98304], stage[98304], destination[98304];
#define getprogname() (ws ? "WindowServer" : "app")
static void nvlog_every(const char *format, ...) { (void)format; }
static void nvmtl_stage_free(void *p) { (void)p; }
static int nvmtl_stage_for_key(pthread_key_t key, size_t n, nvk_buffer *b) {
    (void)key; assert(n <= sizeof stage); b->buf=b->map=stage; return stage_works ? 0 : -1;
}
static nvk_buffer *nvmtl_host_pages(nvmtl_host_page_cache *c,void *base,size_t n,nvk_buffer *tmp,uint8_t dir) {
    (void)c; (void)dir; imported=n; count++; if(!import_works) return NULL; tmp->buf=tmp->map=base; return tmp;
}
enum { NVMTL_HP_COLD=-2 };
static bool cold;
static const void *slice_seen;
static int nvmtl_hp_lease(void *base,size_t aligned,nvk_buffer *tmp,uint8_t direction,size_t bytes,const void *slice) {
    slice_seen=slice; assert(aligned<bytes); if(cold) return NVMTL_HP_COLD;
    return nvmtl_host_pages(NULL,base,aligned,tmp,direction) ? 1 : -1;
}
static void nvmtl_vk_host_pages_drop(nvmtl_host_page_cache *cache) { if (cache) memset(cache,0,sizeof *cache); }
static void nvmtl_host_pages_done(nvk_buffer *cache,nvk_buffer *borrowed) { assert(borrowed); if (borrowed!=cache) leases++; }
'''
cases = r'''
int main(void) {
    nvk_buffer a={0}, b={0}, *borrowed=NULL, *nested=NULL;
    nvk_image img={.bpp=4,.w=15,.h=4}; const size_t row=128, active=60;
    memset(host,0x14,sizeof host); memset(stage,0xc7,sizeof stage); memset(destination,0xff,sizeof destination);
    for(size_t y=0;y<img.h;y++) memset(host+y*row,0x21+y,active);
    for(unsigned kb=64;kb<=80;kb+=16) {
        assert(nvmtl_surface_copy_buffer(&img,host,row,kb*1024,NULL,&a,&borrowed,0,NULL)==&a);
        assert(!borrowed && a.map==stage && !count);
        for(size_t y=0;y<img.h;y++) for(size_t x=0;x<row;x++) assert(stage[y*row+x]==(x<active?0x21+y:0xc7));
        nvmtl_surface_copy_done(NULL,borrowed);
    }
    assert(nvmtl_surface_copy_buffer(&img,host,row,65536,NULL,&a,&borrowed,128,NULL)==&a && !borrowed);
    assert(nvmtl_surface_copy_buffer(&img,host,row,65536,NULL,&b,&nested,0,NULL)==&b && nested==&b && b.map==host);
    nvmtl_surface_copy_done(NULL,nested); assert(t_surface_stage_busy && leases==1);
    end_read(-1,NULL,destination,&a,row,&img,4);
    for(size_t i=0;i<sizeof destination;i++) assert(destination[i]==0xff);
    assert(nvmtl_surface_copy_buffer(&img,host,row,65536,NULL,&a,&borrowed,128,NULL)==&a && !borrowed);
    end_read(0,NULL,destination,&a,row,&img,4);
    for(size_t y=0;y<img.h;y++) for(size_t x=0;x<row;x++) assert(destination[y*row+x]==(x<active?0x21+y:0xff));
    assert(!t_surface_stage_busy);
    borrowed=NULL; assert(nvmtl_surface_copy_buffer(&img,host,row,81921,NULL,&a,&borrowed,0,NULL)==&a && borrowed && imported==81920);
    ws=false; borrowed=NULL; assert(nvmtl_surface_copy_buffer(&img,host,row,65536,NULL,&a,&borrowed,128,NULL) && borrowed);
    ws=true; stage_works=false; borrowed=NULL; assert(nvmtl_surface_copy_buffer(&img,host,row,65536,NULL,&a,&borrowed,128,NULL) && borrowed);
    import_works=false; borrowed=NULL; assert(!nvmtl_surface_copy_buffer(&img,host,row,65536,NULL,&a,&borrowed,0,NULL));
    stage_works=true; const unsigned n=count;
    assert(!nvmtl_surface_copy_buffer(&img,host,row,3*row+active-1,NULL,&a,&borrowed,0,NULL));
    assert(!nvmtl_surface_copy_buffer(&img,host,UINT64_MAX-3,65536,NULL,&a,&borrowed,0,NULL));
    assert(!nvmtl_surface_copy_buffer(&img,host,active-1,65536,NULL,&a,&borrowed,0,NULL));
    assert(count==n);
}
'''

# Only the Vulkan command recorder is mocked. Use the production selection, copy commands,
# staging-slot ownership and fence path, including the early return on a rejected recording.
recorder = r"""
typedef struct { int unused; } nvk_queue;
static int nvmtl_vk_image_write_host_observed(nvk_queue *,nvk_image *,void *,size_t,size_t,nvmtl_host_page_cache *,uint64_t [2]);
typedef struct { void *cb; } nvk_cmdbuf;
typedef void *VkBuffer, *VkImage;
typedef struct { unsigned aspect, baseMip, levels, baseLayer, layers; } VkImageSubresourceRange;
typedef struct {
    unsigned kind; void *next; unsigned src, dst, oldLayout, newLayout, srcFamily, dstFamily;
    VkImage image; VkImageSubresourceRange range;
} VkImageMemoryBarrier;
typedef struct {
    uint64_t bufferOffset; uint32_t bufferRowLength, bufferImageHeight;
    struct { unsigned aspect, mip, base, count; } layer;
    struct { int32_t x,y,z; } imageOffset;
    struct { uint32_t width,height,depth; } imageExtent;
} VkBufferImageCopy;
enum { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER=1, VK_ACCESS_MEMORY_READ_BIT=1, VK_ACCESS_SHADER_READ_BIT=2,
       VK_ACCESS_TRANSFER_WRITE_BIT=4, VK_IMAGE_LAYOUT_UNDEFINED=0, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL=2,
       VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL=3, VK_QUEUE_FAMILY_IGNORED=0, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT=1,
       VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT=2, VK_PIPELINE_STAGE_TRANSFER_BIT=4, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT=8 };
static _Alignas(4096) unsigned char surface[29141504 + 4096];
static unsigned records, fences, barriers, begins;
static int rejectBegin, rejectSubmit;
static VkBufferImageCopy regions[3];
static void *buffers[3];
static int nvmtl_copy_into_multisample(const char *name, nvk_image *img) { (void)name; return img->samples > 1; }
static unsigned nvmtl_barrier_aspect(nvk_image *img) { return nvmtl_copy_aspect(img); }
static bool nvmtl_surfin_ordered_on(void) { return true; }
static int nvmtl_vk_cmd_begin(nvk_queue *q, nvk_cmdbuf *c) { (void)q; begins++; c->cb=c; return rejectBegin; }
static void pvkCmdPipelineBarrier(void *cb,unsigned src,unsigned dst,unsigned dep,unsigned a,void *pa,
                                  unsigned b,void *pb,unsigned n,const VkImageMemoryBarrier *bar) {
    (void)cb; (void)src; (void)dst; (void)dep; (void)a; (void)pa; (void)b; (void)pb;
    assert(n==1 && (barriers ? records>=2 && records<=3 && bar->newLayout==VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL :
                              !records && bar->newLayout==VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL));
    barriers++;
}
static void pvkCmdCopyBufferToImage(void *cb,VkBuffer buffer,VkImage img,unsigned layout,unsigned count,const VkBufferImageCopy *region) {
    (void)cb; (void)img;
    assert(records<3 && barriers==1 && count==1 && layout==VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    buffers[records]=buffer; regions[records++]=*region;
}
static int nvmtl_vk_submit_wait_at(nvk_cmdbuf *c,const char *who) {
    (void)c; assert(barriers==2 && (strstr(who,"host interior + staged edges") ? records>=2 && records<=3 :
                                 strstr(who,"host prefix + staged tail") && records==2));
    assert(t_surface_stage_busy && !nvmtl_surface_stage(8, &(nvk_buffer){0})); // nested upload cannot overwrite the tail
    fences++; return rejectSubmit;
}
"""
split_checks = r"""
static void run_split(uint32_t w,uint32_t h,size_t pitch,size_t bytes) {
    nvk_image img={.bpp=4,.w=w,.h=h,.mtl_type=2,.layers=1,.samples=1};
    const size_t visible=(size_t)w*4, aligned=bytes-bytes%4096;
    const uint32_t split=(uint32_t)(1+(aligned-visible)/pitch);
    assert(split>0 && split<h && bytes<=sizeof surface);
    assert((size_t)(h-1)*pitch+visible==bytes);
    memset(surface,0xed,bytes); memset(stage,0xb6,sizeof stage);
    for(uint32_t y=split-1;y<h;y++) memset(surface+(size_t)y*pitch,(unsigned char)y,visible);
    barriers=records=0; const unsigned before=leases, waited=fences;
    assert(!nvmtl_vk_image_write_host(NULL,&img,surface,pitch,bytes,NULL));
    assert(leases==before+1 && fences==waited+1 && !t_surface_stage_busy && imported==aligned);
    assert(buffers[0]==surface && buffers[1]==stage);
    assert(regions[0].imageOffset.y==0 && regions[0].imageExtent.height==split);
    assert(regions[1].imageOffset.y==(int32_t)split && regions[1].imageExtent.height==h-split);
    for(unsigned i=0;i<2;i++) {
        assert(!regions[i].bufferOffset && regions[i].imageExtent.width==w && regions[i].imageExtent.depth==1);
        assert(regions[i].bufferRowLength==pitch/4 && !regions[i].bufferImageHeight && regions[i].layer.count==1);
    }
    for(uint32_t y=0;y<h-split;y++) {
        for(size_t x=0;x<visible;x++) assert(stage[(size_t)y*pitch+x] == (unsigned char)(split+y));
        for(size_t x=visible;x<pitch;x++) assert(stage[(size_t)y*pitch+x]==0xb6);
    }
    assert((size_t)(split-1)*pitch+visible<=imported);
}
static void run_interior(uint32_t w, uint32_t h, unsigned bpp, unsigned fmt, size_t pitch, size_t offset, size_t supplied) {
    unsigned char *plane=surface+offset; const size_t width=(size_t)w*bpp, span=(h-1)*pitch+width;
    const size_t lead=4096-offset, high=lead+(span-lead)/4096*4096;
    assert(supplied>=span && supplied<=sizeof(surface)-offset);
    memset(surface,0xe3,offset+supplied); memset(stage,0xb6,sizeof stage);
    for(unsigned y=0;y<h;y++) for(size_t x=0;x<width;x++) plane[(size_t)y*pitch+x]=(unsigned char)(y*7+x*11);
    nvk_image img={.w=w,.h=h,.bpp=bpp,.mtl_type=2,.layers=1,.samples=1,.fmt=fmt};
    const unsigned before=leases, waited=fences;
    records=barriers=0;
    assert(nvmtl_vk_image_write_host(NULL,&img,plane,pitch,supplied,NULL)==0);
    assert(leases==before+1 && fences==waited+1 && !t_surface_stage_busy && slice_seen==plane);
    assert(imported==high-lead && (uintptr_t)buffers[1]%4096==0 && buffers[1]==plane+lead);
    assert(buffers[0]==stage && regions[0].imageExtent.height && !regions[0].bufferRowLength);
    assert(regions[1].bufferRowLength==pitch/bpp && regions[1].bufferOffset%4==0);
    if(records==3) assert(buffers[2]==stage && !regions[2].bufferRowLength && regions[2].bufferOffset%4==0);
    unsigned next=0;
    for(unsigned i=0;i<records;i++) {
        const VkBufferImageCopy *r=&regions[i];
        assert(r->imageOffset.y==(int32_t)next && r->imageExtent.width==w && r->imageExtent.depth==1);
        const size_t row=r->bufferRowLength ? (size_t)r->bufferRowLength*bpp : width;
        const size_t limit=i==1 ? imported : sizeof stage;
        assert(r->bufferOffset+(r->imageExtent.height-1)*row+width<=limit);
        for(unsigned y=0;y<r->imageExtent.height;y++) {
            const unsigned char *actual=(const unsigned char *)buffers[i]+r->bufferOffset+y*row;
            assert(!memcmp(actual,plane+(size_t)(next+y)*pitch,width));
        }
        next+=r->imageExtent.height;
    }
    assert(next==h);
    const size_t headBytes=regions[0].imageExtent.height*width;
    const size_t tailOff=records==3 ? regions[2].bufferOffset : headBytes;
    for(size_t k=headBytes;k<tailOff;k++) assert(stage[k]==0xb6);
    size_t used=records==3 ? tailOff+regions[2].imageExtent.height*width : headBytes;
    assert(stage[used]==0xb6); // transfer did not copy padding or read into the following plane
}
int main(void) {
    run_split(3008,2422,12032,29141504);
    // More than one tail row. Writes to staging must leave the source padding untouched.
    run_split(49,8199,256,8198u*256u+196u);
    // Last complete row ends within the imported prefix, with its padding outside it.
    run_split(32,4302,260,4301u*260u+128u);
    run_interior(3200,2103,4,VK_FORMAT_B8G8R8A8_UNORM,12864,0xbc0,27053888);
    run_interior(1920,1080,2,VK_FORMAT_R8G8_UNORM,12788,0xbc0,27053888);
    run_interior(351,2000,2,VK_FORMAT_R8G8_UNORM,706,3010,1414096); // advance rows to 4-byte source offset
    run_interior(512,1025,2,VK_FORMAT_R8G8_UNORM,1024,3072,1052672); // no tail after interior
    nvk_image bad={.w=3008,.h=2422,.bpp=4,.fmt=VK_FORMAT_B8G8R8A8_UNORM,.mtl_type=2,.layers=1,.samples=1};
    const unsigned prologue=begins, beforeImports=count;
    // A sparse R8 upload was consistently faster with the existing packed
    // transfer on the RTX 3070. Repeated visits must not promote it either.
    nvk_image r8={.w=3840,.h=2160,.bpp=1,.fmt=VK_FORMAT_R8_UNORM,.mtl_type=2,.layers=1,.samples=1};
    for (unsigned i=0;i<4;i++) {
        cold=(i==0);
        assert(nvmtl_vk_image_write_host(NULL,&r8,surface+0xbc0,12788,27626000,NULL)==-1);
    }
    cold=false;
    assert(nvmtl_vk_image_write_host(NULL,&bad,surface+3009,12032,29141504,NULL)==-1); // no aligned row offset
    assert(nvmtl_vk_image_write_host(NULL,&bad,(void *)(UINTPTR_MAX-32),12032,29141504,NULL)==-1);
    assert(begins==prologue && count==beforeImports && !t_surface_stage_busy);
    const size_t interiorBytes=27053888;
    nvk_image edges={.w=3200,.h=2103,.bpp=4,.fmt=VK_FORMAT_B8G8R8A8_SRGB,.mtl_type=2,.layers=1,.samples=1};
    unsigned returned=leases, observed=fences;
    cold=true;
    assert(nvmtl_vk_image_write_host(NULL,&edges,surface+0xbc0,12864,interiorBytes,NULL)==-1);
    assert(leases==returned && begins==prologue && !t_surface_stage_busy);
    cold=false; import_works=false;
    assert(nvmtl_vk_image_write_host(NULL,&edges,surface+0xbc0,12864,interiorBytes,NULL)==-1);
    assert(leases==returned && !t_surface_stage_busy);
    import_works=true; stage_works=false;
    assert(nvmtl_vk_image_write_host(NULL,&edges,surface+0xbc0,12864,interiorBytes,NULL)==-1);
    assert(leases==++returned && begins==prologue && !t_surface_stage_busy);
    stage_works=true; rejectBegin=1;
    assert(nvmtl_vk_image_write_host(NULL,&edges,surface+0xbc0,12864,interiorBytes,NULL)==-1);
    assert(leases==++returned && fences==observed && !t_surface_stage_busy);
    rejectBegin=0; rejectSubmit=-1; barriers=records=0;
    assert(nvmtl_vk_image_write_host(NULL,&edges,surface+0xbc0,12864,interiorBytes,NULL)==-1);
    assert(leases==++returned && fences==observed+1 && !t_surface_stage_busy);
    rejectSubmit=0;
    nvk_image img={.w=3008,.h=2422,.bpp=4,.mtl_type=2,.layers=1,.samples=1};
    unsigned before=leases, waited=fences, submitted=begins;
    size_t bytes=29141504;
    cold=true;
    assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1);
    assert(!t_surface_stage_busy && leases==before && begins==submitted);
    // A matching private mapping avoids the admission/pin cost.
    nvmtl_host_page_cache local={.buffer={.buf=surface,.map=surface,.size=bytes-bytes%4096}};
    records=barriers=0;
    assert(!nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,&local));
    assert(!t_surface_stage_busy && leases==before && begins==submitted+1);
    waited=fences; submitted=begins; cold=false;
    import_works=false;
    assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1);
    assert(!t_surface_stage_busy && leases==before && begins==submitted);
    import_works=true; stage_works=false;
    assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1 && begins==submitted);
    assert(leases==before+1); before=leases;
    stage_works=true;
    assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes-1,NULL)==-1);
    assert(nvmtl_vk_image_write_host(NULL,&img,surface+1,12032,bytes,NULL)==-1);
    ws=false; assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1);
    ws=true; img.layers=2; assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1);
    img.layers=1; img.samples=4; assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1);
    img.samples=1; assert(!t_surface_stage_busy && leases==before && begins==submitted);
    rejectBegin=1;
    assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1);
    assert(!t_surface_stage_busy && leases==before+1 && fences==waited);
    rejectBegin=0; rejectSubmit=-1; records=barriers=0;
    assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-1);
    assert(!t_surface_stage_busy && leases==before+2 && fences==waited+1);
    rejectSubmit=-2; records=barriers=0;
    assert(nvmtl_vk_image_write_host(NULL,&img,surface,12032,bytes,NULL)==-2);
    assert(t_surface_stage_busy && leases==before+2);
    assert(!nvmtl_surface_stage(8,&(nvk_buffer){0}));
}
"""

with tempfile.TemporaryDirectory() as folder:
    source, exe = Path(folder)/'staging.c', Path(folder)/'staging'
    for implementation, expected in [(logic,True), (logic.replace('bytes <= 81920','bytes < 65536'),False),
                                     (logic.replace('!valid || t_surface_stage_busy','!valid'),False),
                                     (logic.replace('if (!rc && !borrowed)','if (!borrowed)'),False)]:
        source.write_text(stubs+implementation+cases)
        subprocess.run(['xcrun','clang','-std=gnu11','-fblocks','-Wall','-Werror',str(source),'-o',str(exe)],check=True)
        result=subprocess.run([str(exe)],capture_output=True)
        assert (result.returncode==0)==expected
    source.write_text(stubs + logic + recorder + upload + split_checks)
    subprocess.run(['xcrun','clang','-std=gnu11','-fblocks','-Wall','-Werror','-Wno-unused-function',
                    '-Wno-unused-variable',str(source),'-o',str(exe)],check=True)
    subprocess.run([str(exe)], check=True)
assert read.index('nvmtl_vk_submit_wait_at(') < read.index('if (!rc && !borrowed) nvmtl_surface_copy_rows')
print('PASS: 64/80 KiB staging, readback fence ordering, pitch padding, nested lease, larger controls, failed imports, bounds, R8 fallback and one fence for RG8/BGRA split uploads')
