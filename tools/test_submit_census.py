#!/usr/bin/env python3
"""Concurrent submissions must be accounted for exactly once in driver diagnostics."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
vk = (root / "plugin/nvmtl_vk.c").read_text()
start = vk.index("static pthread_mutex_t g_subwho_lock")
end = vk.index("int nvmtl_vk_submit_wait_at(", start)
fixture = r'''
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static _Atomic unsigned reports;
static void nvlog(const char *fmt, unsigned long long total, const char *line) {
    (void)fmt;
    assert(total && total % 4096 == 0);
    assert(strstr(line, "shared="));
    atomic_fetch_add(&reports, 1);
}

/* COUNTERS */

enum { workers = 8, iterations = 8192 };
static const char shared[] = "shared";
static void *record(void *label) {
    for (unsigned i = 0; i < iterations; ++i) {
        nvmtl_subwho_note(shared);
        nvmtl_subwho_note(label);
    }
    return NULL;
}
int main(void) {
    pthread_t threads[workers];
    char labels[workers][20];
    for (unsigned i = 0; i < workers; ++i) {
        snprintf(labels[i], sizeof labels[i], "queue%u", i);
        assert(!pthread_create(&threads[i], NULL, record, labels[i]));
    }
    for (unsigned i = 0; i < workers; ++i) assert(!pthread_join(threads[i], NULL));
    assert(g_subwho_n == workers + 1);
    for (unsigned i = 0; i < g_subwho_n; ++i) {
        uint64_t expected = g_subwho[i].who == shared ? workers * iterations : iterations;
        assert(g_subwho[i].n == expected);
    }
    assert(g_subwho_total == 2 * workers * iterations);
    assert(atomic_load(&reports) == g_subwho_total / 4096);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    source, binary = Path(tmp) / "census.c", Path(tmp) / "census"
    source.write_text(fixture.replace("/* COUNTERS */", vk[start:end]))
    subprocess.run(["cc", "-std=c11", "-O2", "-pthread", "-Wall", "-Werror",
                    str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], timeout=15, check=True)
print("PASS: concurrent calls from eight submitters preserve counts and reporting intervals")
