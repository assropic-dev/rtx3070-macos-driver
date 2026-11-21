#!/usr/bin/env python3
"""Exercise the production timestamp-pool lifecycle and both pass diagnostics without a GPU."""
import os
from pathlib import Path
import re
import subprocess
import tempfile

src = (Path(__file__).resolve().parents[1] / "plugin/nvmtl_vk.c").read_text()
def section(start, end):
    a = src.index(start)
    return src[a:src.index(end, a)]

recorder = section("enum { NVMTL_PT_SLOTS", "void nvmtl_vk_cmd_end_render")
cleanup = section("    VkQueryPool completed_pass_pool = VK_NULL_HANDLE;", "    nvmtl_fcopy_release(c);")
publish = section("    // Publish only after the command buffer", "\n}")
fixture = r'''
#include <dispatch/dispatch.h>
#include <assert.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
typedef void *VkQueryPool;
typedef int VkResult;
typedef struct {int type; void *next; int flags, kind; unsigned queryCount; int pipelineStats;} VkQueryPoolCreateInfo;
enum { VK_SUCCESS=0, VK_NOT_READY=1, VK_ERROR_FEATURE_NOT_PRESENT=-8, VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO=1,
       VK_QUERY_TYPE_TIMESTAMP=2, VK_QUERY_RESULT_64_BIT=1, VK_QUERY_RESULT_WITH_AVAILABILITY_BIT=4,
       VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT=8192 };
#define VK_NULL_HANDLE NULL
typedef struct {void *pt_pool, *cb; unsigned pt_n, pt_open, nfb, wseq; uint64_t fx_seq;
    uint32_t pt_w[128], pt_h[128], pt_f[128], pt_fs[128][4];} nvk_cmdbuf;
static int g_dev, creates, destroys, writes, resets, g_keep_retired;
static _Atomic int g_state=1;
static unsigned g_timestamp_bits=8;
static struct {float timestampPeriod;} g_lim={2};
static int legacy_marker;
#define access(path, mode) (legacy_marker ? 0 : -1)
static int remove_marker(const char *path) { (void)path;legacy_marker=0;return 0; }
#define unlink(path) remove_marker(path)
#define nvlog(...) ((void)0)
#define nvlog_every(...) do { printf(__VA_ARGS__); putchar('\n'); } while (0)
static VkResult pvkCreateQueryPool(int d, const VkQueryPoolCreateInfo *q, void *a, VkQueryPool *p) {
    assert(q->queryCount==256); creates++;
    if (getenv("FAIL_CREATE")) return -3;
    *p=malloc(1); assert(*p); return 0;
}
static void pvkDestroyQueryPool(int d, VkQueryPool p, void *a) { assert(p); free(p); destroys++; }
static void pvkCmdResetQueryPool(void *cb, VkQueryPool p, unsigned first, unsigned n) {
    assert(p && !first && n==256); resets++;
}
static void pvkCmdWriteTimestamp(void *cb, unsigned stage, VkQueryPool p, unsigned index) {
    assert(p && stage==VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT && index<256); writes++;
}
static VkResult pvkGetQueryPoolResults(int d, VkQueryPool p, unsigned first, unsigned n,
        size_t bytes, void *output, size_t stride, unsigned flags) {
    uint64_t *values=output;
    assert(!first && n<=256 && bytes==n*16 && stride==16 && flags==5);
    for (unsigned i=0;i<n;i++) {
        values[i*2]=(250 + i/2*20 + (i%2)*11) & 255;
        values[i*2+1]=getenv("UNAVAILABLE") && i==1 ? 0 : 1;
    }
    return getenv("FAIL_READ") ? VK_NOT_READY : VK_SUCCESS;
}
''' + recorder + r'''
static uint64_t nvmtl_fence_trace_limit(void) {return getenv("NVMTL_FENCE_TRACE")!=NULL;}
static void retire(nvk_cmdbuf *c) {
    uint64_t marks[3]={0};
    uint64_t *pass_release=marks;
    const int keep=getenv("BAD_RETIRE") ? -1 : 1;
''' + cleanup + publish + r'''
    assert(marks[2] <= 1);
}
int main(int argc, char **argv) {
    if (getenv("CONCURRENT")) {
        static _Atomic unsigned granted;
        dispatch_apply(32, dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^(size_t worker) {
            for (unsigned i=0;i<64;i++) if (nvmtl_fence_pass_claim()) atomic_fetch_add(&granted, 1);
        });
        printf("granted=%u\n", atomic_load(&granted));
        return 0;
    }
    if (getenv("BURST")) {
        // Simultaneously outstanding buffers must have different pools. Merely
        // reading their results must not make a pool available to a new recorder.
        nvk_cmdbuf pending[17]={0};
        for (unsigned i=0;i<17;i++) {
            pending[i].nfb=1; pending[i].fx_seq=i+1;
            nvmtl_pass_time_begin(&pending[i],32,32,44);
            nvmtl_pass_time_end(&pending[i]);
            assert(!g_pass_cached);
            for(unsigned j=0;j<i;j++)
                assert(((nvmtl_pass_queries*)pending[i].pt_pool)->pool != ((nvmtl_pass_queries*)pending[j].pt_pool)->pool);
        }
        for(unsigned i=0;i<17;i++) nvmtl_pass_time_collect(&pending[i],true);
        assert(!g_pass_cached);
        for(unsigned i=0;i<17;i++) retire(&pending[i]);
        printf("burst_created=%u cached=%u destroyed=%u\n",creates,g_pass_cached,destroys);
        assert(creates==17 && creates==destroys+g_pass_cached && g_pass_cached<=8);
        return 0;
    }
    unsigned count=atoi(argv[1]); legacy_marker=getenv("LEGACY")!=NULL;
    for (unsigned seq=1;seq<=2;seq++) {
        nvk_cmdbuf c={.fx_seq=seq, .wseq=getenv("UNPROVEN") ? 1 : 0};
        for(unsigned j=0;j<count;j++) {
            c.nfb++; nvmtl_pass_time_begin(&c,5120,2880,44);
            nvmtl_vk_pt_note(&c,0xface);
            if (!getenv("ABANDON")) nvmtl_pass_time_end(&c);
            else break;
        }
        if (!getenv("ABANDON") && !getenv("NO_COMPLETION"))
            nvmtl_pass_time_collect(&c, seq!=2 || !getenv("NO_V1"));
        if (getenv("LOST")) g_state=-1;
        retire(&c); assert(!c.pt_pool && !c.pt_n && !c.pt_open);
    }
    assert(destroys+g_pass_cached==(getenv("FAIL_CREATE") ? 0 : creates));
    printf("pools=%d queries=%d cached=%u resets=%d\n",creates,writes,g_pass_cached,resets);
}
'''

with tempfile.TemporaryDirectory(prefix="fence-passes-") as tmp:
    work = Path(tmp)
    file, exe = work / "test.m", work / "test"
    file.write_text(fixture)
    subprocess.run(["xcrun", "clang", "-fblocks", "-O1", str(file), "-o", str(exe)], check=True)

    def run(count=3, budget="4", trace=True, **knobs):
        env = {"PATH": os.environ["PATH"], "TMPDIR": tmp, "NVMTL_FENCE_PASSES": budget, **knobs}
        if trace:
            env["NVMTL_FENCE_TRACE"] = "1"
        output = subprocess.check_output([str(exe), str(count)], env=env, text=True)
        return [dict(re.findall(r"(\w+)=([^\s]+)", line)) for line in output.splitlines()
                if line.startswith("fence pass v1")], output

    for budget, trace in [("0", True), ("65537", True), ("1x", True), ("4", False)]:
        rows, out = run(budget=budget, trace=trace)
        assert not rows and "pools=0 queries=0" in out
    rows, out = run()
    assert "pools=1 queries=8 cached=1 resets=2" in out and rows[-1]["pool_reused"]=="1"
    off, detail = run(NVMTL_FENCE_PASS_POOL="0")
    assert "pools=2 queries=8 cached=0 resets=2" in detail and all(v["pool_reused"]=="0" for v in off)
    fields = ["submit_id","pass_index","width","height","gpu_span_ns","gpu_valid","query_result"]
    assert [[v[k] for k in fields] for v in off] == [[v[k] for k in fields] for v in rows]
    for flag in ["UNPROVEN","LOST","BAD_RETIRE","NO_COMPLETION","FAIL_READ","UNAVAILABLE"]:
        rejected, detail = run(**{flag:"1"})
        assert "cached=0" in detail and all(v["pool_reused"]=="0" for v in rejected)
    for cap in ["0","1","8","200"]:
        _, detail = run(budget="32", BURST="1", NVMTL_FENCE_PASS_POOL=cap)
        assert f"burst_created=17 cached={min(8,int(cap))}" in detail
    assert [(v["submit_id"], v["pass_index"]) for v in rows] == [("1","0"),("1","1"),("1","2"),("2","0")]
    assert all(v["render_pass_count"]=="3" and v["gpu_valid"]=="1" and v["gpu_span_ns"]=="22" and
               v["width"]=="5120" and v["height"]=="2880" and v["fs"].startswith("0000face/") for v in rows)
    rows, _ = run(count=129, budget="130")
    assert len(rows) == 130 and rows[127]["pass_index"] == "127" and rows[128]["submit_id"]=="2"
    assert "granted=33" in run(budget="33", CONCURRENT="1")[1]
    assert not list(work.glob("nvmtl-passtime-*.log"))
    for option in ("FAIL_CREATE", "FAIL_READ"):
        rows, _ = run(**{option: "1"})
        assert len(rows)==4 and all(v["gpu_valid"]=="0" and v["query_result"]!="0" for v in rows)
    rows, _ = run(UNAVAILABLE="1")
    assert [v["gpu_valid"] for v in rows]==["0","1","1","0"]
    assert len(run(NO_V1="1")[0])==3
    rows, out = run(ABANDON="1")
    assert not rows and "pools=2 queries=2" in out
    rows, _ = run(LEGACY="1")
    legacy, = work.glob("nvmtl-passtime-*.log")
    lines = legacy.read_text().splitlines()
    assert len(rows)==4 and len(lines)==6
    assert lines[0].startswith("cb 1 pass 0 5120x2880 fmt 44") and lines[-1].startswith("cb 2 pass 2 ")
print("PASS: pass/submission IDs, opt-in budgets, capacity, timestamp wrap, unavailable/error, abandon and legacy output")
