#!/usr/bin/env python3
"""Exercise the patched NVK single-fence wait, including an unrelated notification and old-kext fallback."""
import pathlib
import subprocess
import tempfile

repo = pathlib.Path(__file__).resolve().parents[1]
folder = pathlib.Path('src/nouveau/vulkan/nvkmd/nvrm')
targets = ['nvkmd_nvrm_sync.c', 'nvkmd_nvrm_ctx.c', 'nvkmd_nvrm.h', 'nvrm_xnu.c']

def cut(text, first, following):
    a = text.index(first)
    return text[a:text.index(following, a)]

stubs = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
#include "nv_fence_trace.h"
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
enum { VK_SUCCESS, VK_TIMEOUT, VK_TRUE=1, NVRM_GS_WAITS=1 };
enum vk_sync_wait_flags { VK_SYNC_WAIT_ANY=1, VK_SYNC_WAIT_PENDING=2, VK_SYNC_WAIT_COMPLETE=0 };
typedef int VkResult;
typedef void *VkDevice;
struct vk_sync;
struct vk_fence { struct vk_sync *sync; };
typedef struct vk_fence *VkFence;
typedef VkResult (*PFN_vkWaitForFences)(VkDevice,uint32_t,const VkFence*,unsigned,uint64_t);
#define vk_fence_from_handle(h) (h)
static struct vk_sync *vk_fence_get_active_sync(struct vk_fence *f) { return f->sync; }
struct vk_sync { unsigned dummy; };
struct vk_sync_wait { struct vk_sync *sync; uint64_t wait_value; };
struct vk_object_base { unsigned dummy; };
struct nvkmd_nvrm_sync_pend { uint32_t slot,gen; uint64_t seq,value; };
struct nvkmd_nvrm_sync {
    struct vk_sync base; uint64_t value,pending; unsigned npend;
    struct nvkmd_nvrm_sync_pend pend[1];
};
struct nvkmd_nvrm_slot { void *ctx; uint32_t gen; uint64_t *sem; };
static uint64_t sem, now; static struct nvkmd_nvrm_slot nvkmd_nvrm_slots[1];
static pthread_mutex_t nvkmd_nvrm_gsub_mtx = PTHREAD_MUTEX_INITIALIZER;
static unsigned event_seq,returned_seq,posts,reaps,waits;
static bool kernel, race, nested, sameCounter;
static struct nvkmd_nvrm_sync target, foreign;
static int64_t os_time_get_nano(void){ now+=25000; return now; }
#define clock_gettime_nsec_np(clock) ((uint64_t)os_time_get_nano())
static struct nvkmd_nvrm_sync *vk_sync_as_nvkmd_nvrm_sync(struct vk_sync *s) {return (void*)s;}
static bool nvrm_gsub_is_binary(struct nvkmd_nvrm_sync *s){(void)s;return true;}
static int64_t nvkmd_nvrm_flush_timeout_ms(void){return 5000;}
static void nvkmd_nvrm_gsub_count(int n){(void)n;}
static bool nvkmd_nvrm_diag_on(void){return false;}
static const char *nvmtl_cfg_getenv(const char *k){(void)k;return "1";}
static const char *getprogname(void){return "test";}
static void nvkmd_nvrm_wait_backoff(int64_t ns){(void)ns;now+=100000;}
static VkResult nvkmd_nvrm_slot_health(uint32_t s,uint32_t g,struct vk_object_base*l) { (void)s;(void)g;(void)l;return VK_SUCCESS; }
static VkResult nvkmd_nvrm_slot_stalled(uint32_t s,uint32_t g,uint64_t seq,long us,struct vk_object_base*l) { (void)s;(void)g;(void)seq;(void)us;(void)l;return VK_SUCCESS; }
static void nvkmd_nvrm_diag_dump(const char *a,const char *b){(void)a;(void)b;}
static bool nvrm_evwait_pending(unsigned n,const uint32_t *s,const uint32_t *g,const uint64_t *w,int *fd){
   assert(n==1 && !*s && *g==7 && *w==88); *fd=17; return sem<*w;
}
static unsigned armed;
static int nvrm_xnu_trace_event(int fd,uint64_t begin,uint32_t *seq,uint64_t *ns) {
   if(kernel){if(begin)armed++;else {assert(armed);armed--;}}
   assert(fd==17); *seq=event_seq+(race?1:0); *ns=posts?now:0; return kernel;
}
static int64_t nvrm_xnu_wait_event(int fd,uint32_t seen,uint32_t us) {
   assert(fd==17); if (!us) return event_seq; assert(us==2000 && seen==event_seq);
   if (!sameCounter) event_seq++;
   returned_seq=event_seq; posts++; sem=88;return event_seq;
}
static void nvrm_gsub_reap(struct nvkmd_nvrm_sync *s){
   if(s->npend && sem>=s->pend[0].seq){s->value=1;s->npend=0;}
}
static void nvkmd_nvrm_reap_deferred(void){now+=90000;reaps++;}
'''
checks = r'''
static VkResult real_wait(VkDevice device,uint32_t count,const VkFence *f,unsigned all,uint64_t timeout) {
   assert(count==1 && all==VK_TRUE && timeout==UINT64_MAX);waits++;
   // A nested unrelated sync wait on the same thread must not stamp this trace.
   if(nested){nested=false;struct vk_sync_wait other={&foreign.base,0};
       assert(!nvrm_gsub_wait(1,&other,VK_SYNC_WAIT_COMPLETE,UINT64_MAX,NULL));
       assert(!trace_result->matched && !trace_result->ready_ns);
   }
   struct vk_sync_wait one={(*f)->sync,0};
   (void)device;return nvrm_gsub_wait(1,&one,VK_SYNC_WAIT_COMPLETE,timeout,NULL);
}
static int scenario(bool instrument) {
   memset(&target,0,sizeof target);target.npend=1;
   target.pend[0]=(struct nvkmd_nvrm_sync_pend){0,7,88,1};
   foreign.value=1;sem=0;posts=0;event_seq=19;
   nvkmd_nvrm_slots[0]=(struct nvkmd_nvrm_slot){&target,7,&sem};
   struct nv_fence_trace_v1 t={0};struct vk_fence f={&target.base};
   unsigned before=waits;
   if(nvkmd_nvrm_trace_wait_v1(NULL,&f,UINT64_MAX,real_wait,&t,instrument?sizeof t:0)!=VK_SUCCESS) return 1;
   assert(waits==before+1 && !trace_sync && !trace_result && !armed);
   if(!instrument) {assert(!t.matched && !t.ready_ns);return 0;}
   assert(t.matched && t.target_valid && t.slot==0 && t.gen==7 && t.target==88);
   assert(t.ready_ns && t.reap_done_ns>=t.ready_ns+90000);
   assert(t.event_calls==1 && t.changed_calls==!sameCounter);
   assert(t.event_seen==19 && t.event_returned==returned_seq && t.event_enter_ns<=t.event_return_ns);
   assert(t.kernel_status==(kernel?1:-1));
   assert(!!t.shared_post_valid==(kernel && !race && !sameCounter));
   assert(!!t.shared_post_ns==!!t.shared_post_valid);return 0;
}
int main(void){
   kernel=true;nested=true;assert(!scenario(true));
   race=true;assert(!scenario(true));race=false;
   sameCounter=true;assert(!scenario(true));sameCounter=false;
   kernel=false;assert(!scenario(true));assert(!scenario(false));
   struct nv_fence_trace_v1 t={0};struct vk_fence f={&foreign.base};
   assert(nvkmd_nvrm_trace_wait_v1(NULL,&f,UINT64_MAX,real_wait,&t,sizeof t)==VK_SUCCESS);
   assert(t.matched && !t.target_valid && !t.event_calls && !t.shared_post_valid);
}
'''
with tempfile.TemporaryDirectory() as temp:
    tmp = pathlib.Path(temp)
    # Apply the same ordered release patches to the NVRM files in isolation.
    build = (repo/'build/build_nvk.sh').read_text()
    chain = cut(build, '  3.0)', '  2.8)').split('CHAIN="',1)[1].split('"',1)[0]
    for patch in chain.replace('@FIXES','nvk-fixes-after-def16').split():
        subprocess.run(['git','apply','--whitespace=nowarn',*[f'--include={folder/name}' for name in targets],
                        str(repo/'nvk'/f'{patch}.patch')], cwd=tmp,check=True)
    sync=(tmp/folder/'nvkmd_nvrm_sync.c').read_text()
    ctx=(tmp/folder/'nvkmd_nvrm_ctx.c').read_text()
    block=cut(ctx, 'void\nnvkmd_nvrm_wait_block_observed(', '/* b73 (patch_evflush.py)')
    bridge=cut(sync,'static _Thread_local struct vk_sync *trace_sync;', 'static VkResult\nnvrm_gsub_sync_wait_many(')
    c=tmp/'trace.c'; exe=tmp/'trace'
    c.write_text(stubs+block+bridge+checks)
    subprocess.run(['cc','-std=c11','-Wall','-Werror','-I',str(repo/'nvk'),str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True,timeout=5)
print('PASS: single-fence sync identity, no nested/superseded event attribution, no-event/old-kext/old-ABI paths and delayed NVK reap')

kernel = (repo/'kexts/NVRM/NVRM.cpp').read_text()
assert 'class IOAccelNVRMBridge : public NVRMUserClient' in kernel
assert 'externalMethod' not in cut(kernel,'class IOAccelNVRMBridge :','OSDefineMetaClassAndStructors(IOAccelNVRMBridge')
assert '{ sTraceEvent, 1, 0, 2, 0 }' in kernel and '{ sWaitEvent, 2, 0, 1, 0 }' in kernel
kernel_fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <mutex>
using UInt32=uint32_t; using UInt64=uint64_t; using SInt32=int32_t; using SInt64=int64_t;
using NvHandle=unsigned; using NvU32=unsigned; using NvU16=unsigned short; using NvBool=bool;
using IOReturn=int; using IOInterruptState=int; using IOSimpleLock=std::mutex;
using event_t=const volatile void *;
struct OSObject{};
struct IOExternalMethodArguments{uint64_t scalarInput[1],scalarOutput[2];};
struct NVRMUserClient:OSObject {
    UInt32 fEvtSeq=0,fTraceArmed=0,fTracePostedSeq=0; UInt64 fTracePostedTicks=0; IOSimpleLock *fTraceEventLock=nullptr;
    static NVRMUserClient* fromNvfp(NVRMUserClient* c){return c;}
    static IOReturn sTraceEvent(OSObject*,void*,IOExternalMethodArguments*);
    ~NVRMUserClient(){delete fTraceEventLock;}
};
struct nv_event_t{NVRMUserClient *nvfp;};
enum { kIOReturnNotAttached=1,kIOReturnBadArgument,kIOReturnNotReady,kIOReturnNoMemory,kIOReturnSuccess=0,kNvEvtMaxSleepers=256 };
#define OSDynamicCast(T,x) static_cast<T*>(x)
#define NV_API_CALL
#define OSReportWithBacktrace(...) ((void)0)
#define kprintf(...) ((void)0)
static SInt64 gNvEvtPosts,gNvEvtMatched; static unsigned stampCalls,wakes;
static int64_t OSIncrementAtomic64(int64_t *n){return (*n)++;}
static int32_t OSIncrementAtomic(volatile int32_t *n){return (*n)++;}
static IOSimpleLock *IOSimpleLockAlloc(){return new IOSimpleLock;}
static void IOSimpleLockFree(IOSimpleLock*l){delete l;}
static int IOSimpleLockLockDisableInterrupt(IOSimpleLock*l){l->lock();return 1;}
static void IOSimpleLockUnlockEnableInterrupt(IOSimpleLock*l,int state){assert(state==1);l->unlock();}
static UInt64 mach_absolute_time(){return ++stampCalls;}
static void absolutetime_to_nanoseconds(UInt64 t,UInt64 *out){*out=t*100;}
static void thread_wakeup(event_t){wakes++;}
'''
kernel_fixture += cut(kernel,'extern "C" void NV_API_CALL nv_post_event(nv_event_t *event, NvHandle hEvent,',
                      'IOReturn NVRMUserClient::sWaitEvent(')
kernel_fixture += r'''
int main(){
 NVRMUserClient a,b; nv_event_t e{&a}; IOExternalMethodArguments p={};
 auto post=[&](){nv_post_event(&e,0,0,0,0,false);};
 auto selector=[&](NVRMUserClient *c,uint64_t arm){p.scalarInput[0]=arm;return NVRMUserClient::sTraceEvent(c,nullptr,&p);};
 post(); assert(a.fEvtSeq==1 && !stampCalls && wakes==1);
 assert(selector(&a,0)==kIOReturnNotReady && !a.fTraceEventLock);
 assert(selector(&a,2)==kIOReturnBadArgument);
 assert(!selector(&a,1) && !p.scalarOutput[1]);
 assert(!selector(&a,1) && a.fTraceArmed==2);
 assert(!selector(&b,1)); post();
 assert(!selector(&b,0) && !p.scalarOutput[1]);
 assert(!selector(&a,0) && p.scalarOutput[0]==2 && p.scalarOutput[1]==100 && a.fTraceArmed==1);
 post(); assert(!selector(&a,0) && p.scalarOutput[0]==3 && p.scalarOutput[1]==200);
 post(); assert(a.fEvtSeq==4 && wakes==4 && stampCalls==2 && !a.fTraceArmed);
 assert(!selector(&a,1) && !p.scalarOutput[1]); assert(!selector(&a,0));
 for(unsigned i=0;i<kNvEvtMaxSleepers;i++) assert(!selector(&a,1));
 assert(selector(&a,1)==kIOReturnBadArgument);
 for(unsigned i=0;i<kNvEvtMaxSleepers;i++) assert(!selector(&a,0));
 assert(selector(&a,0)==kIOReturnBadArgument);
}
'''
with tempfile.TemporaryDirectory() as temp:
    source=pathlib.Path(temp)/'posts.cc'; exe=source.with_suffix('')
    source.write_text(kernel_fixture)
    subprocess.run(['c++','-std=c++17','-Wall','-Werror',str(source),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
print('PASS: bridge inherits selector dispatch, posts always wake normally; timestamp capture is connection-local and disarms after the last overlapping traced wait')
