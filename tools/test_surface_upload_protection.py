#!/usr/bin/env python3
"""Exercise the production adaptive upload gate with real VM areas and a counted GPU-import double."""
import os
from pathlib import Path
import subprocess
import tempfile

root=Path(__file__).resolve().parents[1]
def function(text,signature):
    start=text.index(signature)
    return text[start:text.index('\n}\n',start)+3]
vk=(root/'plugin/nvmtl_vk.c').read_text()
trace=(root/'plugin/nvmtl_import_trace.h').read_text()
gate=function(vk,'static int nvmtl_try_surface_import(')
nocopy_gate=function(vk,'int nvmtl_vk_buffer_import_nocopy(')
shared_gate=vk[vk.index('enum { NVMTL_HOST_IMPORT_DISABLED,'):vk.index(nocopy_gate)]
prot=function(trace,'static const char *nvmtl_import_protection(')
headers=r'''
#include <assert.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
typedef struct { uint64_t host_import_id; } nvk_buffer;
static unsigned calls, vm_queries, arms, skips; static int refusal;
static size_t g_host_align;
static int nvmtl_buffer_import_host(void *p, size_t n, nvk_buffer *b, bool *rejected) {
    assert(p && n && b); calls++;
    if (rejected) *rejected=(refusal==2);
    return refusal ? -1 : 0;
}
static int nvmtl_vk_buffer_import_host(void *p,size_t n,nvk_buffer *b) {
    return nvmtl_buffer_import_host(p,n,b,NULL);
}
static void nvmtl_import_decision(const char *action,size_t size,int rc,int rejected,int armed,uint64_t desc) {
    (void)size;(void)rc;(void)rejected;(void)desc;
    arms+=armed;skips+=!strcmp(action,"preflight_skip");
}
'''
test=r'''
static const char *counted_protection(void *p,size_t n) {
    vm_queries++;return nvmtl_import_protection(p,n);
}
#define nvmtl_import_protection counted_protection
'''
main=r'''
int main(int argc,char **argv) {
    assert(argc==3);
    const bool forced=!strcmp(argv[1],"1"), auto_mode=!strcmp(argv[1],"auto");
    const bool nocopy=!strcmp(argv[2],"nocopy");
    setprogname("WindowServer");
    const size_t page=getpagesize();
    g_host_align=page;
    unsigned char *p=mmap(NULL,2*page,PROT_READ|PROT_WRITE,MAP_ANON|MAP_PRIVATE,-1,0);
    assert(p!=MAP_FAILED);memset(p,0x5a,2*page);
    nvk_buffer b={0};
    if (nocopy) {
        assert(!nvmtl_vk_buffer_import_nocopy(p,page,&b) && calls==1);
        assert(!mprotect(p,page,PROT_READ));
        refusal=2;
        assert(nvmtl_vk_buffer_import_nocopy(p,page,&b)==-1 && calls==1+!forced);
        assert(nvmtl_vk_buffer_import_nocopy(p,page,&b)==-1 && calls==1+!forced+(!forced&&!auto_mode));
        assert(!mprotect(p,page,PROT_READ|PROT_WRITE));
        refusal=0;
        unsigned before=calls;
        assert(!nvmtl_vk_buffer_import_nocopy(p,page,&b) && calls==before+1);
        assert(!munmap(p,2*page));
        return 0;
    }
    assert(!nvmtl_try_surface_import(p,page,&b,0) && calls==1 && vm_queries==(unsigned)forced);
    refusal=2; // Real rejection on RW is insufficient to arm.
    assert(nvmtl_try_surface_import(p,page,&b,0)==-1 && arms==0 && skips==0);
    unsigned checked=vm_queries;
    refusal=0;
    assert(!nvmtl_try_surface_import(p,page,&b,0) && vm_queries==checked+(unsigned)forced);
    assert(!mprotect(p,page,PROT_READ));
    refusal=1; // A generic import failure does not identify an unsupported external handle.
    checked=vm_queries;
    assert(nvmtl_try_surface_import(p,page,&b,0)==-1 && arms==0 && vm_queries==checked+(unsigned)forced);
    refusal=2;
    unsigned before=calls;
    assert(nvmtl_try_surface_import(p,page,&b,0)==-1 && calls==before+!forced);
    assert(arms==(unsigned)auto_mode);
    before=calls;
    assert(nvmtl_try_surface_import(p,page,&b,0)==-1 && calls==before+(!forced&&!auto_mode));
    // A mixed VM range cannot prove that the whole source is read-only.
    refusal=0;before=calls;
    assert(!nvmtl_try_surface_import(p,2*page,&b,0) && calls==before+1);
    checked=vm_queries;before=calls;
    assert(!nvmtl_try_surface_import(p,page,&b,1) && calls==before+1 && vm_queries==checked);
    // Same address in its next, writable lifetime can import again.
    assert(!mprotect(p,page,PROT_READ|PROT_WRITE));before=calls;
    assert(!nvmtl_try_surface_import(p,page,&b,0) && calls==before+1);
    assert(!munmap(p,2*page));
}
'''
with tempfile.TemporaryDirectory() as temp:
    source=Path(temp)/'check.c'
    exe=Path(temp)/'check'
    source.write_text(headers+prot+test+shared_gate+nocopy_gate+gate+main)
    subprocess.run(['cc','-fblocks','-O2','-Wall','-Wextra','-Werror',source,'-o',exe],check=True)
    for value in (None,'0','1'):
        env=dict(os.environ)
        for knob in ('NVMTL_SURFIN_READONLY_CHECK','NVMTL_NOCOPY_READONLY_CHECK'):
            if value is None:env.pop(knob,None)
            else:env[knob]=value
        for api in ('surface','nocopy'):
            subprocess.run([exe,value if value is not None else 'auto',api],env=env,check=True)
print('PASS adaptive/forced/off for surface and no-copy imports, true allocator refusal, VM queries, readback and same-address recovery')
