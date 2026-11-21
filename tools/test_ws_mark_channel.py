#!/usr/bin/env python3
"""Exercise the production completion-mark transport with deterministic simulated gates.

--live-probe opens a normal NVAccel control user client as the invoking process
and sends the nonmutating SID 0 probe only. The kernel must refuse it.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

repo = Path(__file__).resolve().parents[1]
source = (repo / 'plugin/nvmtl_vk.c').read_text()
dirty = source[source.index('static uint32_t nvmtl_ws_try_dirty(uint32_t sid, uint32_t plane);'):source.index('// A capture page-off holds')]
chunk = source[source.index('// A capture page-off holds'):source.index('/* 1 when NVAccel')]
headers = """
#include <IOKit/IOKitLib.h>
#include <os/log.h>
#include <libproc.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <assert.h>
#include <string.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include "../kexts/NVRM/nvrm_surfshare_abi.h"
"""
stubs = r'''
#define NVMTL_IO_MAIN_PORT kIOMainPortDefault
static int g_dev = 1;
static uint64_t nvmtl_card_registry_id;
static io_service_t accepted = 10;
static io_connect_t opened = 100, last_call;
static unsigned open_count, closed, old_calls, new_calls, reports, short_reply;
static bool deny, failed, fake_ws, false_name;
static bool expects_pending, ready_announced;
static pthread_mutex_t ordinary_gate = PTHREAD_MUTEX_INITIALIZER;
static uint32_t old(int dev, uint32_t sid, uint32_t plane) {
    assert(dev && plane == (NVRM_SS_PLANE_GPU_MARK | (expects_pending ? 1u : 0u)));
    pthread_mutex_lock(&ordinary_gate); old_calls++; pthread_mutex_unlock(&ordinary_gate);
    return deny ? NVRM_SS_OFF : 0;
}
static uint32_t (*pnvk_surface_dirty)(int, uint32_t, uint32_t) = old;
static bool nvmtl_gpu_headless(void) { return false; }
#define nvlog(...) ((void)0)
static kern_return_t call(io_connect_t conn, uint32_t selector, const uint64_t *esc, unsigned esc_n,
    const void *in, size_t in_len, void *scalar, void *scalar_len, void *out, size_t *len) {
    assert(conn > 100 && conn <= opened && selector == 1 && *esc == NVRM_ESC_SURF_DIRTY && esc_n == 1);
    last_call = conn;
    const struct NVRMSurfShareEsc *r = in;
    assert(in_len == sizeof *r && *len == in_len && r->version == NVRM_SURFSHARE_VERSION);
    assert(!r->hClient && !r->hSrcClient && !r->hSrcMemory && !r->size && !r->pad);
    if (r->plane & NVRM_SS_PLANE_TRYLOCK) {
        assert(r->plane == (NVRM_SS_PLANE_TRYLOCK | 2) && r->surfaceID == 17);
    } else assert(r->plane == (NVRM_SS_PLANE_GPU_MARK | (r->surfaceID && expects_pending ? 1u : 0u)));
    if (failed) return kIOReturnNotAttached;
    if (short_reply) { *len = sizeof *r - 1; return KERN_SUCCESS; }
    new_calls++; ((struct NVRMSurfShareEsc *)out)->status = deny ? NVRM_SS_OFF : 0;
    if (r->surfaceID && !expects_pending && !deny) ready_announced = true;
    return KERN_SUCCESS;
}
static int pidpath(int pid, char *buf, size_t n) {
    return snprintf(buf, n, "%s", fake_ws && !false_name ?
        "/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/Resources/WindowServer" : "/private/tmp/qa");
}
static kern_return_t open_client(io_service_t port, mach_port_t task, uint32_t type, io_connect_t *out) {
    assert(port == accepted && task == mach_task_self() && type == 0x4e560000u);
    open_count++; *out = ++opened; return KERN_SUCCESS;
}
#define IOConnectCallMethod call
#define IOObjectConformsTo(p, name) ((p) == accepted && !strcmp(name, "NVAccel"))
#define IORegistryEntryGetRegistryEntryID(p, id) (*(id) = (uint64_t)(p), KERN_SUCCESS)
#define IOServiceGetMatchingService(port, match) (accepted)
#define IOObjectRelease(port) ((void)0)
#define IOObjectRetain(port) KERN_SUCCESS
#define IOServiceOpen open_client
#define IOServiceClose(conn) ((void)(++closed))
#define getprogname() (fake_ws ? "WindowServer" : "qa")
#define proc_pidpath pidpath
#undef os_log
#define os_log(log, msg) ((void)(++reports))
'''
checks = r'''
int main(void) {
    nvmtl_ws_open_mark_channel(accepted); assert(!open_count);
    fake_ws = true; false_name = true;
    nvmtl_ws_open_mark_channel(accepted); assert(!open_count);
    false_name = false;
    nvmtl_card_registry_id = 12; nvmtl_ws_open_mark_channel(accepted); assert(!open_count);
    nvmtl_card_registry_id = 0;
    deny = true; nvmtl_ws_open_mark_channel(accepted); assert(open_count == 2 && closed == 2 && !g_ws_mark_connection);
    assert(nvmtl_vk_surface_gpu_mark(0, 0) == NVRM_SS_OFF);
    deny = false; nvmtl_ws_open_mark_channel(accepted); assert(open_count == 4 && g_ws_mark_connection);
    nvmtl_ws_open_mark_channel(accepted); assert(open_count == 4);
    assert(g_ws_remark_connection && g_ws_remark_connection != g_ws_mark_connection);
    // GPU ready can now pass while normal dirty/import is holding its own connection.
    pthread_mutex_lock(&ordinary_gate);
    pthread_mutex_lock(&g_ws_mark_lock);
    assert(!nvmtl_vk_surface_dirty_try(17, 2) && last_call == g_ws_remark_connection);
    assert(nvmtl_vk_surface_dirty_try(17, 16) == -1);
    pthread_mutex_unlock(&g_ws_mark_lock);
    pthread_mutex_lock(&g_ws_remark_lock);
    assert(nvmtl_vk_surface_dirty_try(17, 2) == 1);
    expects_pending = true;
    ready_announced = false;
    assert(!nvmtl_vk_surface_gpu_mark(17, 1) && !ready_announced && last_call == g_ws_mark_connection);
    pthread_mutex_unlock(&g_ws_remark_lock);
    // Represent two command-buffer holders; release after the final genuine fence.
    unsigned holders = 2;
    if (--holders == 0) nvmtl_vk_surface_gpu_mark(17, 0);
    assert(!ready_announced);
    expects_pending = false;
    if (--holders == 0) assert(!nvmtl_vk_surface_gpu_mark(17, 0));
    assert(ready_announced && !old_calls);
    deny = true; assert(nvmtl_ws_try_dirty(17, 2) == NVRM_SS_OFF && !old_calls);
    deny = false;
    // A transport fault returns busy to the completion lane; only the deferred lane reopens it.
    const io_connect_t broken = g_ws_remark_connection;
    short_reply = 1;
    assert(nvmtl_ws_try_dirty(17, 2) == NVRM_SS_BUSY && !atomic_load(&g_ws_remark_usable));
    assert(closed == 2 && !old_calls && nvmtl_vk_surface_gpu_mark(0, 0) == NVRM_SS_OFF);
    short_reply = 0;
    assert(!nvmtl_vk_surface_gpu_mark(17, 0) && last_call == g_ws_mark_connection);
    nvmtl_vk_prepare_surface_dirty_retry();
    assert(g_ws_remark_connection != broken && closed == 3);
    assert(!nvmtl_ws_try_dirty(17, 2) && last_call == g_ws_remark_connection && !old_calls);
    pthread_mutex_unlock(&ordinary_gate);
    deny = true; assert(nvmtl_vk_surface_gpu_mark(17, 0) == NVRM_SS_OFF && !old_calls && g_ws_mark_connection);
    deny = false; short_reply = 1;
    assert(!nvmtl_vk_surface_gpu_mark(17, 0) && old_calls == 1 && !g_ws_mark_connection && closed == 4);
    short_reply = 0;
    nvmtl_ws_open_mark_channel(accepted); failed = true;
    expects_pending = true; assert(!nvmtl_vk_surface_gpu_mark(17, 1) && old_calls == 2 && closed == 5);
    failed = false; nvmtl_ws_open_mark_channel(accepted);
    nvmtl_ws_close_mark_channel(); nvmtl_ws_close_mark_channel();
    assert(closed == open_count && !g_ws_mark_connection);
    puts("PASS: authenticated probe, real device identity, independent gate, final holder, explicit refusal, transport fallback, closure");
}
'''
real = r'''
int main(void) {
    io_service_t a = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
    assert(a);
    io_connect_t conn = MACH_PORT_NULL;
    assert(IOServiceOpen(a, mach_task_self(), 0x4e560000u, &conn) == KERN_SUCCESS);
    IOObjectRelease(a);
    uint32_t status;
    kern_return_t kr = nvmtl_ws_mark_call(conn, 0, false, &status);
    IOServiceClose(conn);
    assert(kr == KERN_SUCCESS && status == NVRM_SS_OFF);
    printf("PASS: ordinary process, actual NVAccel probe returned %#x, SID 0 only\n", status);
}
'''

opts = argparse.ArgumentParser()
opts.add_argument('--live-probe', action='store_true')
args = opts.parse_args()
with tempfile.TemporaryDirectory() as temp:
    src, binary = Path(temp) / 'test.c', Path(temp) / 'test'
    if args.live_probe:
        call = chunk[chunk.index('static kern_return_t nvmtl_ws_dirty_call'):chunk.index('static io_connect_t nvmtl_ws_verified_connection')]
        src.write_text(headers + call + real)
    else:
        src.write_text(headers + stubs + dirty + chunk + checks)
    subprocess.run(['xcrun', 'clang', '-std=gnu11', '-Wall', '-Werror', '-I', str(repo/'plugin'), str(src),
                    '-framework', 'IOKit', '-framework', 'CoreFoundation', '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=5)
