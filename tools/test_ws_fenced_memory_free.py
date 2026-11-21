#!/usr/bin/env python3
"""Exercise the production fence reaper and physical free queue with a stalled RM free."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'plugin/nvmtl_vk.c').read_text()
def section(start, end):
    i = source.index(start)
    return source[i:source.index(end, i)]

queue = section('// Detached VkDeviceMemory', 'static void nvmtl_vk_buffer_destroy_body(')
budget = section('static _Atomic uint64_t g_budget_delta;', 'static _Atomic uint64_t g_vram_bytes')
destroy = section('static void nvmtl_vk_buffer_destroy_body(nvk_buffer *b, bool deferMemory, nvmtl_host_owner owner);', 'void nvmtl_vk_buffer_retire(')
image = (section('static uint64_t nvmtl_grace_seq_locked(void)', 'static void nvmtl_vk_texel_view_destroy_now') +
         section('static bool nvmtl_ws_upload_image_ready(', 'static bool nvmtl_ws_upload_image_retire_on(') +
         section('static void nvmtl_vk_image_destroy_now(nvk_image *i, bool deferUploadImage)\n{', 'static uint64_t g_res1_off'))
assert 'nvmtl_buffer_grace_reap(observer != NULL)' in source
fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <stddef.h>
#include <string.h>
#include <time.h>
#include <pthread.h>
#include <dispatch/dispatch.h>
#include "nvmtl_page_life.h"
#include "nvmtl_host_owner.h"
typedef void *VkDevice;
typedef void *VkDeviceMemory;
typedef void *VkBuffer;
typedef void *VkImage;
typedef void *VkImageView;
typedef struct {
    void *buf, *mem, *pool, *map, *lost_mem;
    size_t size, alloc; uint64_t host_import_id, host_created_us; uint8_t host_origin; bool bar1, imported, placed, bind_lost;
} nvk_buffer;
typedef struct {
    void *img, *mem, *view, *pool, *sparse;
    size_t alloc, snapshot_charge;
    uint64_t up_token;
    bool placed, sysmem, vfl;
} nvk_image;
typedef struct { nvk_buffer b; nvmtl_pl_node life; nvmtl_host_owner owner; } nvmtl_bgrace;
static nvmtl_pl_state g_bg;
static pthread_mutex_t g_bg_lock=PTHREAD_MUTEX_INITIALIZER;
static uint64_t g_cmd_finished_seq, g_cmd_submitted;
static uint64_t g_cmd_begun, g_cmd_abandoned;
static pthread_mutex_t g_grace_lock=PTHREAD_MUTEX_INITIALIZER;
static _Atomic size_t g_alloc_bytes;
static _Atomic uint64_t g_vram_bytes, g_img_align;
static _Atomic size_t g_ws_snapshot_reserved;
static uint64_t g_sys_bytes, g_sys_align;
static VkDevice g_dev=(void*)1;
static int g_state=1;
static bool nvmtl_buffer_grace_on(void) { return true; }
#define nvmtl_perf_now() 0
#define nvmtl_perf_note(...) ((void)0)
#define nvmtl_live_relabel(...) ((void)0)
static unsigned g_buf_gone, g_bar1_bytes;
static pthread_t producer;
static _Atomic unsigned calls[256], buffers[256], images[256], views[256], unmapped[256], pauses;
static dispatch_semaphore_t entered, resume;
#define nvlog(...) ((void)0)
#define nvlog_every(...) ((void)0)
#define nvmtl_import_descriptor_released(id,created) ((void)0)
#define nvmtl_fx_note_buffer_gone(b,where) ((void)0)
#define nvmtl_live_remove(b) ((void)0)
#define pool_buffer_release(b) ((b)->pool=NULL)
static void pvkDestroyBuffer(VkDevice d, VkBuffer b, void *a) {
    (void)a; assert(d==g_dev); assert(++buffers[(uintptr_t)b]==1); assert(pthread_equal(producer,pthread_self()));
}
static void pvkDestroyImage(VkDevice d, VkImage image, void *allocation) {
    (void)allocation;
    uintptr_t id = (uintptr_t)image;
    assert(d==g_dev && (id==113)==pthread_equal(producer,pthread_self()));
    assert(views[id]==1 && ++images[id]==1 && !calls[id]);
}
static void pvkDestroyImageView(VkDevice d, VkImageView view, void *allocation) {
    (void)allocation;
    assert(d==g_dev && pthread_equal(producer,pthread_self()) && ++views[(uintptr_t)view]==1);
}
static void nvmtl_vk_image_upload_barrier(nvk_image *i) { assert(!i->up_token); }
static void nvmtl_vw_note_died(void *view) { assert(view); }
static void nvmtl_dlss_views_drop(VkImage image) { assert(image); }
static void image_pool_release(nvk_image *i) { assert(!i->pool); }
static void unmap(VkDevice d,VkDeviceMemory m) { assert(d==g_dev); ++unmapped[(uintptr_t)m]; }
static void (*pvkUnmapMemory)(VkDevice,VkDeviceMemory)=unmap;
static void pvkFreeMemory(VkDevice d,VkDeviceMemory m,void *a) {
    uintptr_t id=(uintptr_t)m;
    (void)a; assert(d==g_dev && buffers[id]+images[id]==1);
    if (id==atomic_load(&pauses)) {
        assert(!pthread_equal(producer,pthread_self())); dispatch_semaphore_signal(entered);
        assert(!dispatch_semaphore_wait(resume,dispatch_time(DISPATCH_TIME_NOW,4*NSEC_PER_SEC)));
    }
    assert(++calls[id]==1);
}
/*BUDGET*/
/*QUEUE*/
/*DESTROY*/
/*IMAGE*/
static nvk_buffer allocation(unsigned id,size_t charged,size_t span) {
    g_alloc_bytes+=charged;
    if (charged) nvmtl_vk_budget_charge(charged);
    return (nvk_buffer){.buf=(void*)(uintptr_t)id,.mem=(void*)(uintptr_t)id,.alloc=charged,.size=span};
}
static void hold_until(nvk_buffer b, uint64_t seq) {
    nvmtl_bgrace *entry=calloc(1,sizeof *entry); assert(entry);
    entry->b=b;
    nvmtl_pl_made(&g_bg); nvmtl_pl_stamp(&entry->life,seq);
    assert(!nvmtl_pl_release(&g_bg,&entry->life,g_cmd_finished_seq,0));
}
static _Atomic unsigned callbacks[256];
static void complete_owner(void *context) {
    unsigned id=(unsigned)(uintptr_t)context;
    assert(calls[id]==1 && ++callbacks[id]==1);
    // A user callback may submit work: bookkeeping locks must already have been released.
    pthread_mutex_lock(&g_ws_free.lock);
    pthread_mutex_unlock(&g_ws_free.lock);
}
static void owned_import(unsigned id) {
    nvk_buffer b=allocation(id,65536,65536); nvmtl_vk_budget_discharge(65536);
    b.imported=true; b.map=(void*)1; b.host_import_id=id; b.host_origin=NVMTL_HOST_NOCOPY_DIRECT;
    void *reserved=nvmtl_vk_reserve_host_retirement(); assert(reserved);
    nvmtl_vk_destroy_owned_host_buffer(&b,(nvmtl_host_owner){(void*)(uintptr_t)id,complete_owner},reserved);
    assert(!b.buf && !b.mem);
}
static void expect_empty(void) {
    for (int i=0;i<1000;i++) {
        pthread_mutex_lock(&g_ws_free.lock); unsigned n=g_ws_free.count; size_t size=g_ws_free.bytes+g_ws_free.alias_bytes;
        pthread_mutex_unlock(&g_ws_free.lock);
        if (!n) { assert(!size); return; }
        struct timespec delay={0,1000000}; nanosleep(&delay,NULL);
    }
    assert(!"queue did not drain");
}
int main(int argc,char **argv) {
    producer=pthread_self(); entered=dispatch_semaphore_create(0); resume=dispatch_semaphore_create(0);
    if (argc>1) { assert(!nvmtl_ws_free_on()); return 0; }
    setenv("NVMTL_WS_DEFER_BUFFER_FREE","1",1);
    assert(nvmtl_ws_free_on());
    // First fence protects the buffer even when the worker is available.
    pauses=1; hold_until(allocation(1,65536,65536),1);
    nvmtl_buffer_grace_reap(true); assert(!buffers[1] && !calls[1]);
    g_cmd_finished_seq=1; nvmtl_buffer_grace_reap(true);
    assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
    assert(buffers[1] && !calls[1] && g_alloc_bytes==65536 && g_budget_delta==65536);
    // A second observed fence retires during the blocked free. Include alloc=0 imports in admission by span.
    nvk_buffer family=allocation(2,0,NVMTL_WS_FREE_BYTES-65536); family.imported=true;
    hold_until(family,2); g_cmd_finished_seq=2; nvmtl_buffer_grace_reap(true);
    assert(buffers[2] && !calls[2] && g_ws_free.bytes==NVMTL_WS_FREE_BYTES);
    nvk_buffer overflow=allocation(3,65536,65536); nvmtl_vk_buffer_destroy_body(&overflow,true,(nvmtl_host_owner){0});
    assert(calls[3] && !overflow.mem && g_alloc_bytes==65536 && g_budget_delta==65536);
    // A full 512 MiB alias fits alongside the ordinary budget, with both charges
    // retained while the worker is busy. A second alias cannot exceed that budget.
    nvmtl_ws_free_item bigAlias = {.device=g_dev, .memory=(void*)120,
        .span=512ull << 20, .origin=NVMTL_HOST_NOCOPY_ALIAS};
    buffers[120]=1; // detached handle, as supplied by the producer
    assert(nvmtl_ws_try_queue_free(bigAlias));
    assert(g_ws_free.alias_bytes==(512ull << 20));
    bigAlias.span=4096;
    assert(!nvmtl_ws_try_queue_free(bigAlias));
    bigAlias.span=(512ull << 20)+4096;
    assert(!nvmtl_ws_try_queue_free(bigAlias));
    dispatch_semaphore_signal(resume); expect_empty();
    assert(calls[120]==1);
    assert(calls[1] && calls[2] && !g_alloc_bytes && !g_budget_delta);
    // Fill the handle count with a blocked free in slot zero. The 65th goes through the producer.
    pauses=10;
    for (unsigned i=10;i<=10+NVMTL_WS_FREE_SLOTS;i++) {
        nvk_buffer b=allocation(i,65536,65536); nvmtl_vk_buffer_destroy_body(&b,true,(nvmtl_host_owner){0});
        if (i==10) assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
    }
    assert(g_ws_free.count==NVMTL_WS_FREE_SLOTS && calls[10+NVMTL_WS_FREE_SLOTS]);
    assert(g_alloc_bytes==65536ull*NVMTL_WS_FREE_SLOTS);
    nvk_buffer borrowed=allocation(101,1024,1024); borrowed.imported=true; borrowed.map=(void*)1;
    nvmtl_vk_budget_discharge(1024); // The host memory producer never charges the VRAM budget.
    nvmtl_vk_buffer_destroy_body(&borrowed,true,(nvmtl_host_owner){0}); assert(calls[101] && !unmapped[101]);
    nvk_buffer unobserved=allocation(102,1024,1024);
    nvmtl_vk_buffer_destroy_body(&unobserved,false,(nvmtl_host_owner){0}); assert(calls[102]);
    dispatch_semaphore_signal(resume); expect_empty();
    // A borrowed host import and an unobserved buffer still finish inline with an empty queue.
    borrowed=allocation(103,1024,1024); borrowed.imported=true; borrowed.map=(void*)1;
    nvmtl_vk_budget_discharge(1024);
    nvmtl_vk_buffer_destroy_body(&borrowed,true,(nvmtl_host_owner){0}); assert(calls[103] && !g_ws_free.count);
    unobserved=allocation(104,1024,1024); nvmtl_vk_buffer_destroy_body(&unobserved,false,(nvmtl_host_owner){0});
    assert(calls[104] && !g_ws_free.count);
    // The original callback follows its OWN import, independently of later fences.
    g_cmd_submitted=5; g_cmd_finished_seq=4; pauses=105;
    owned_import(105);
    nvmtl_buffer_grace_reap(true); assert(!calls[105] && !callbacks[105]);
    g_cmd_finished_seq=5; nvmtl_buffer_grace_reap(true);
    assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
    assert(!calls[105] && !callbacks[105] && g_alloc_bytes==65536);
    dispatch_semaphore_signal(resume); expect_empty(); assert(callbacks[105]);
    // A synchronous, unobserved fence still releases the callback after the actual free.
    g_cmd_submitted=6; owned_import(106);
    assert(!callbacks[106]); g_cmd_finished_seq=6; nvmtl_buffer_grace_reap(false);
    assert(callbacks[106] && !g_ws_free.count && !g_alloc_bytes);
    // An internal alias can go to the worker even if release itself sees the completed fence.
    pauses=108;
    nvk_buffer alias=allocation(108,65536,65536);
    alias.imported=true; alias.map=(void*)1; alias.host_import_id=108; alias.host_origin=NVMTL_HOST_NOCOPY_ALIAS;
    nvmtl_vk_budget_discharge(65536);
    nvmtl_host_owner aliasOwner={(void*)(uintptr_t)108, complete_owner};
    nvmtl_vk_destroy_owned_host_buffer(&alias,aliasOwner,nvmtl_vk_reserve_host_retirement());
    assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
    assert(!calls[108] && !callbacks[108]);
    dispatch_semaphore_signal(resume); expect_empty(); assert(callbacks[108]);
    // A large upload cache alias still waits for a known fence, then uses the
    // alias queue even when that fence is observed by a synchronous copy.
    const size_t surfaceBytes=81358848;
    g_cmd_submitted=7; pauses=109;
    nvk_buffer surface=allocation(109,surfaceBytes,surfaceBytes);
    surface.imported=true; surface.map=(void*)1;
    surface.host_import_id=109; surface.host_origin=NVMTL_HOST_SURFACE_CACHE_ALIAS;
    nvmtl_vk_budget_discharge(surfaceBytes);
    nvmtl_host_owner surfaceOwner={(void*)(uintptr_t)109, complete_owner};
    nvmtl_vk_destroy_owned_host_buffer(&surface,surfaceOwner,nvmtl_vk_reserve_host_retirement());
    nvmtl_buffer_grace_reap(false);
    assert(!calls[109] && !callbacks[109] && !g_ws_free.count);
    g_cmd_finished_seq=7; nvmtl_buffer_grace_reap(false);
    assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
    assert(!calls[109] && !callbacks[109] && g_ws_free.alias_bytes==surfaceBytes && !g_ws_free.bytes);
    dispatch_semaphore_signal(resume); expect_empty(); assert(callbacks[109]);
    // Before the final fence, device loss cannot turn borrowed pages over to the caller.
    g_cmd_submitted=8; g_state=-1; owned_import(107);
    assert(!calls[107] && !callbacks[107] && g_bg.ncool==1);
    g_cmd_finished_seq=8; nvmtl_buffer_grace_reap(false);
    assert(callbacks[107] && !g_bg.ncool); g_state=1;
    // A completed dedicated image consumes the ordinary byte budget. The worker
    // destroys the image before the allocation and keeps both charges until free returns.
    const size_t imageBytes=82575360;
    g_alloc_bytes=imageBytes; g_vram_bytes=imageBytes; g_img_align=imageBytes;
    nvmtl_vk_budget_charge(imageBytes);
    pauses=112;
    nvk_image image={.img=(void*)112,.mem=(void*)112,.view=(void*)112,.alloc=imageBytes};
    g_cmd_begun=9;
    assert(!nvmtl_ws_upload_image_ready(&image)); // begun but not submitted
    g_cmd_abandoned=1;
    image.up_token=1; assert(!nvmtl_ws_upload_image_ready(&image)); image.up_token=0;
    image.placed=true; assert(!nvmtl_ws_upload_image_ready(&image)); image.placed=false;
    g_state=-1; assert(!nvmtl_ws_upload_image_ready(&image)); g_state=1;
    assert(nvmtl_ws_upload_image_ready(&image));
    nvmtl_vk_image_destroy_now(&image, true);
    assert(!image.img && !image.mem);
    assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
    assert(images[112] && !calls[112] && g_alloc_bytes==imageBytes && g_vram_bytes==imageBytes);
    assert(g_img_align==imageBytes && g_budget_delta==imageBytes && g_ws_free.bytes==imageBytes);
    nvk_image pressured={.img=(void*)113,.mem=(void*)113,.view=(void*)113,.alloc=imageBytes};
    g_alloc_bytes+=imageBytes; g_vram_bytes+=imageBytes; g_img_align+=imageBytes;
    nvmtl_vk_budget_charge(imageBytes);
    nvmtl_vk_image_destroy_now(&pressured, true);
    assert(images[113]==1 && calls[113]==1 && !pressured.img && g_vram_bytes==imageBytes);
    g_state=-1;  // A device loss after the successful fence does not orphan the queue.
    dispatch_semaphore_signal(resume); expect_empty();
    assert(calls[112] && !g_alloc_bytes && !g_vram_bytes && !g_img_align && !g_budget_delta);
    g_state=1;
    // Pressure is temporary: a subsequent completed image can use the queue.
    nvk_image retry={.img=(void*)114,.mem=(void*)114,.view=(void*)114,.alloc=imageBytes};
    g_alloc_bytes=imageBytes; g_vram_bytes=imageBytes; g_img_align=imageBytes;
    nvmtl_vk_budget_charge(imageBytes);
    assert(nvmtl_ws_upload_image_ready(&retry));
    nvmtl_vk_image_destroy_now(&retry, true);
    expect_empty();
    assert(images[114]==1 && calls[114]==1 && !retry.img && !g_vram_bytes && !g_budget_delta);
    // A later device loss cannot orphan a known-retired allocation already given to the worker.
    pauses=110;
    nvk_buffer final=allocation(110,65536,65536); nvmtl_vk_buffer_destroy_body(&final,true,(nvmtl_host_owner){0});
    assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
    g_state=-1; dispatch_semaphore_signal(resume); nvmtl_ws_free_drain();
    assert(calls[110] && !g_ws_free.count && !g_ws_free.bytes && !g_alloc_bytes && !g_budget_delta);
    g_state=1; nvk_buffer after=allocation(111,65536,65536);
    nvmtl_vk_buffer_destroy_body(&after,true,(nvmtl_host_owner){0}); assert(calls[111] && !g_ws_free.started);
    puts("PASS: fence-only admission, concurrent progress, byte and entry pressure, ownership, accounting and shutdown");
}
'''
program = fixture.replace('/*QUEUE*/',queue).replace('/*BUDGET*/',budget).replace('/*DESTROY*/',destroy).replace('/*IMAGE*/',image)
with tempfile.TemporaryDirectory() as tmp:
    file, binary = Path(tmp)/'free.c', Path(tmp)/'free'
    file.write_text(program)
    subprocess.run(['xcrun','clang','-std=gnu11','-O1','-I',str(root/'plugin'),str(file),'-o',str(binary)],check=True)
    subprocess.run([str(binary),'default-other-process'],check=True,timeout=5)
    subprocess.run([str(binary)],check=True,timeout=10)
