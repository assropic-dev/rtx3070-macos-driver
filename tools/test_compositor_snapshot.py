#!/usr/bin/env python3
"""Exercise the compositor's real snapshot allocation and fence-checked recycling with a fake device."""
from pathlib import Path
import subprocess
import tempfile

repo = Path(__file__).resolve().parents[1]
source = (repo / "plugin/nvmtl_vk.c").read_text()
pool = source[source.index("static pthread_mutex_t g_sample_pool_lock"):
              source.index("int nvmtl_vk_upload_sample_snapshot(")]

fixture = r'''
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef struct { int *fence; int observedReady; } nvmtl_inflight;
typedef struct { nvmtl_inflight infl; } nvmtl_asyncup;
typedef struct {
    void *img, *pool; unsigned w,h,fmt,sysmem; size_t snapshot_charge,alloc,poollen; uint64_t up_token;
} nvk_image;
static int g_state=1, g_dev=1, creates=0, retires=0, fail=0;
static _Atomic size_t g_ws_snapshot_reserved;
enum { VK_SUCCESS=0 };
static int pvkGetFenceStatus(int dev, int *fence) { assert(dev && fence); return *fence ? VK_SUCCESS : 1; }
static int nvmtl_vk_submit_finish(nvmtl_inflight *f) {
    assert(f->observedReady && *f->fence && "retiring this slot would wait at bind"); retires++; return 0;
}
static int nvmtl_vk_image_create_typed(unsigned w,unsigned h,unsigned fmt,int b,int a,int m,int s,int t,int l,nvk_image *i) {
    creates++; if (fail) return -1;
    *i=(nvk_image){.img=(void*)(uintptr_t)creates,.w=w,.h=h,.fmt=fmt,.alloc=(size_t)w*h*4}; return 0;
}
static void nvmtl_vk_image_destroy(nvk_image *i) {
    atomic_fetch_sub(&g_ws_snapshot_reserved,i->snapshot_charge); memset(i,0,sizeof *i);
}
@@POOL@@
int main(void) {
    nvk_image src={0}, other={0}, reuse={0}, batch[17]={0}; nvmtl_asyncup token={0}; int signaled=0;
    fail=1; assert(nvmtl_vk_create_sample_snapshot(64,64,44,&src)<0 && !g_ws_snapshot_reserved);
    fail=0; assert(!nvmtl_vk_create_sample_snapshot(64,64,44,&src)); void *original=src.img;
    token.infl.fence=&signaled; src.up_token=(uintptr_t)&token;
    nvmtl_vk_park_sample_snapshot(&src); assert(!src.img && g_sample_pool_bytes);
    assert(!nvmtl_vk_create_sample_snapshot(64,64,44,&other) && other.img!=original);
    assert(!retires && !nvmtl_ws_snapshot_recycles()); nvmtl_vk_image_destroy(&other);
    signaled=1; assert(!nvmtl_vk_create_sample_snapshot(64,64,44,&reuse));
    assert(reuse.img==original && reuse.up_token==(uintptr_t)&token && !token.infl.fence);
    assert(retires==1 && nvmtl_ws_snapshot_recycles()==1 && !g_sample_pool_bytes);
    nvmtl_vk_image_destroy(&reuse); assert(!g_ws_snapshot_reserved);
    assert(nvmtl_vk_create_sample_snapshot(1024,512,44,&src)<0 && !src.img);
    for (int i=0;i<16;i++) assert(!nvmtl_vk_create_sample_snapshot(512,512,44,&batch[i]));
    int before=creates;
    assert(nvmtl_vk_create_sample_snapshot(512,512,44,&batch[16])<0 && creates==before);
    nvmtl_vk_image_destroy(&batch[0]); assert(!nvmtl_vk_create_sample_snapshot(512,512,44,&batch[16]));
    for (int i=1;i<=16;i++) nvmtl_vk_image_destroy(&batch[i]);
    assert(!g_ws_snapshot_reserved);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    c, exe = Path(tmp) / "pool.c", Path(tmp) / "pool"
    for code, expected in [(pool, True), (pool.replace("!= VK_SUCCESS) continue;", "!= VK_SUCCESS) {}"), False)]:
        c.write_text(fixture.replace("@@POOL@@", code))
        subprocess.run(["cc", "-std=c11", str(c), "-o", str(exe)], check=True)
        result = subprocess.run([str(exe)], capture_output=True)
        assert (result.returncode == 0) == expected
print("PASS: allocation bound, admission failure, pending and completed slots; removing the fence check fails")
