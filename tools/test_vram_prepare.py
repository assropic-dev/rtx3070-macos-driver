#!/usr/bin/env python3
"""Exercise the production spare inventory, chunk clear and quiesce with host synchronization stubs."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
fb = root / 'kexts/NVRMFB/fb'
source = (fb / 'nvrm-fb.cpp').read_text()


def extract(first, last):
    start = source.index(first)
    return source[start:source.index(last, start)]


fixture = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>
#include "nvrm-vram-prepare.h"
#include "nvrm-allocation-table.h"
using NvU64 = unsigned long long;
using SInt32 = int;
using UInt8 = unsigned char;
using thread_call_param_t = void *;
constexpr int kIOReturnSuccess = 0, kMillisecondScale = 1, THREAD_UNINT = 0;
struct IOLock { std::mutex mutex; std::condition_variable waiter; };
static void IOLockLock(IOLock *s) { s->mutex.lock(); }
static void IOLockUnlock(IOLock *s) { s->mutex.unlock(); }
static void IOLockSleep(IOLock *s, void *, int) {
    std::unique_lock<std::mutex> lock(s->mutex, std::adopt_lock);
    s->waiter.wait(lock);
    lock.release();
}
static void IOLockWakeup(IOLock *s, void *, bool) { s->waiter.notify_all(); }
static int OSAddAtomic(int n, volatile int *p) { return __atomic_fetch_add(p, n, __ATOMIC_ACQ_REL); }
static bool OSCompareAndSwap(int old, int value, volatile int *p) {
    return __atomic_compare_exchange_n(p, &old, value, false, __ATOMIC_ACQ_REL, __ATOMIC_RELAXED);
}
static void clock_interval_to_deadline(int interval, int, uint64_t *deadline) { *deadline = interval; }
static void IOSleep(int ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }
static std::recursive_mutex display;
class NVRMDisplayGate {
    std::unique_lock<std::recursive_mutex> lock;
public:
    explicit NVRMDisplayGate(bool wait = true) : lock(display, std::defer_lock) {
        if (wait) lock.lock(); else lock.try_lock();
    }
    bool valid() const { return lock.owns_lock(); }
};
struct NVRMCallReference { explicit NVRMCallReference(void *) {} };
static size_t largestWrite, clearedBytes;
static void nvu_zero_wide(void *p, size_t bytes) {
    largestWrite = std::max(largestWrite, bytes);
    clearedBytes += bytes;
    std::memset(p, 0, bytes);
}
class NVRMNVDAFramebuffer;
static NVRMNVDAFramebuffer *gFB;
static std::mutex simulation;
static std::condition_variable barriers;
static bool preparing, finish, changeApertureOnFirstChunk;
static std::atomic<unsigned> freed, calls;
static std::atomic<bool> pausedFinished;
static NVRMVramRequest fresh(unsigned id, NvU64 size);

class NVRMNVDAFramebuffer {
public:
    IOLock lock;
    IOLock *fVramPrepareState = &lock;
    void *fVramPrepareCall = this;
    NvU64 fVramPrepareSize = 0, fVramPreparePreviousSize = 0;
    bool fVramPrepareActive = false, fHeadSlept = false, fConsoleAperture = false, fModeSet = true;
    bool refillImmediately = false;
    unsigned nextAllocation = 100;
    volatile int fVramPreparePaused = 0, fStopping = 0, fVramPrepareQueued = 0, fVramPrepareRetries = 0;
    NVRMPreparedVramPool fVramPrepared;
    NVRMVramRequest fVramPrepareDiscard = {};
    void scheduleCall(void *, uint64_t) { calls++; }
    int rmGpuReadiness() { return kIOReturnSuccess; }
    bool isInactive() { return false; }
    bool preparedVramOverlaps(NvU64, NvU64) const {
        return changeApertureOnFirstChunk && clearedBytes > 0;
    }
    void discardPreparedVram(NVRMVramRequest block) { if (block.handle) freed++; }
    bool vramGrant(NVRMVramRequest *request, bool preparation) {
        assert(preparation);
        if (refillImmediately) {
            assert(fVramPrepared.wantsMore(request->size, fVramPrepareSize, fVramPreparePreviousSize));
            assert(fVramPrepared.add(fresh(nextAllocation++, request->size)));
            if (fVramPrepared.nextSize(fVramPrepareSize, fVramPreparePreviousSize)) armVramPreparation();
            return true;
        }
        std::unique_lock<std::mutex> lock(simulation);
        preparing = true;
        barriers.notify_all();
        barriers.wait(lock, [] { return finish; });
        return false;
    }
    void discardPreparedWhenIdle(NVRMVramRequest);
    void queueVramPreparation(NvU64);
    void armVramPreparation();
    void retryVramPreparation();
    void pauseVramPreparation();
    static void vramPreparationFire(thread_call_param_t, thread_call_param_t);
    bool clearPreparedVram(const NVRMVramRequest &);
};
'''

checks = r'''
static NVRMVramRequest fresh(unsigned id, NvU64 size) {
    NVRMVramRequest r = {};
    r.handle = reinterpret_cast<void *>(static_cast<uintptr_t>(id));
    r.kva = r.handle;
    r.phys = 4096ull * id;
    r.actualSize = size;
    return r;
}

int main() {
    constexpr NvU64 large = 0x4da0000, small = 0x3840000;
    NVRMPreparedVramPool pool;
    assert(!pool.take(large).handle);
    for (unsigned i = 1; i <= 3; i++) assert(pool.add(fresh(i, large)));
    assert(pool.nextSize(large, 0) == 0 && !pool.take(small).handle);
    assert(pool.nextSize(large, small) == small);
    for (unsigned i = 4; i <= 6; i++) assert(pool.add(fresh(i, small)));
    assert(pool.nextSize(large, small) == 0 && pool.nextSize(small, large) == 0);
    assert(!pool.add(fresh(7, small)) && !pool.makeRoom(small, small, large).handle);
    for (unsigned i = 0; i < 3; i++) assert(pool.take(small).handle && pool.take(large).handle);
    assert(!pool.take(small).handle && !pool.take(large).handle);
    auto alreadyPublished = fresh(5, small);
    alreadyPublished.allocationCookie = 7;
    assert(!pool.add(alreadyPublished));
    NVRMAllocationTable<int, 1> allocations;
    int deviceMemory = 0;
    assert(!allocations.size(&deviceMemory, 0));
    const auto firstCookie = allocations.insert(&deviceMemory, large);
    assert(firstCookie && allocations.size(&deviceMemory, firstCookie) == large);
    assert(!allocations.insert(&deviceMemory, small) && allocations.remove(&deviceMemory, firstCookie));
    const auto nextCookie = allocations.insert(&deviceMemory, small);
    assert(nextCookie != firstCookie && !allocations.size(&deviceMemory, firstCookie));
    assert(!allocations.remove(&deviceMemory, firstCookie) && allocations.size(&deviceMemory, nextCookie) == small);

    NVRMNVDAFramebuffer fb;
    gFB = &fb;
    constexpr size_t span = (512u << 10) + 37;
    std::vector<UInt8> bytes(span + 2, 0xa5);
    auto work = fresh(1, span);
    work.kva = bytes.data() + 1;
    assert(fb.clearPreparedVram(work));
    assert(clearedBytes == span && largestWrite == (256u << 10));
    assert(bytes.front() == 0xa5 && bytes.back() == 0xa5);
    assert(std::all_of(bytes.begin() + 1, bytes.end() - 1, [](UInt8 byte) { return byte == 0; }));
    std::fill(bytes.begin(), bytes.end(), 0xa5);
    changeApertureOnFirstChunk = true;
    clearedBytes = 0;
    assert(!fb.clearPreparedVram(work));
    assert(clearedBytes == (256u << 10) && bytes[(256u << 10) + 1] == 0xa5);
    changeApertureOnFirstChunk = false;

    assert(fb.fVramPrepared.add(fresh(1, large)));
    assert(fb.fVramPrepared.add(fresh(2, large)));
    fb.fVramPrepareDiscard = fresh(3, large);
    fb.queueVramPreparation(large);
    fb.queueVramPreparation(large);
    assert(calls == 1);
    std::thread worker([&] { NVRMNVDAFramebuffer::vramPreparationFire(&fb, nullptr); });
    {
        std::unique_lock<std::mutex> lock(simulation);
        assert(barriers.wait_for(lock, std::chrono::seconds(2), [] { return preparing; }));
    }
    {
        NVRMDisplayGate gate;
        assert(fb.fVramPrepared.take(large).handle);  // claim does not wait for the worker
    }
    std::thread sleeper([&] { fb.pauseVramPreparation(); pausedFinished = true; });
    for (int i = 0; i < 200 && !__atomic_load_n(&fb.fVramPreparePaused, __ATOMIC_ACQUIRE); i++) IOSleep(1);
    assert(__atomic_load_n(&fb.fVramPreparePaused, __ATOMIC_ACQUIRE));
    assert(!pausedFinished);
    {
        std::lock_guard<std::mutex> lock(simulation);
        finish = true;
    }
    barriers.notify_all();
    worker.join();
    sleeper.join();
    assert(pausedFinished && freed == 2 && !fb.fVramPrepared.take(large).handle);
    fb.queueVramPreparation(large);
    assert(calls == 1);

    NVRMNVDAFramebuffer busy;
    gFB = &busy;
    busy.queueVramPreparation(large);
    {
        NVRMDisplayGate gate;
        std::thread blocked([&] { NVRMNVDAFramebuffer::vramPreparationFire(&busy, nullptr); });
        blocked.join();
        assert(calls == 3 && busy.fVramPrepareQueued && busy.fVramPrepareRetries == 3);
        std::thread discard([&] { busy.discardPreparedWhenIdle(fresh(7, large)); });
        discard.join();
        assert(busy.fVramPrepareDiscard.handle && freed == 2);
    }
    busy.pauseVramPreparation();
    assert(!busy.fVramPrepareDiscard.handle && freed == 3);

    NVRMNVDAFramebuffer refill;
    gFB = &refill;
    refill.refillImmediately = true;
    const auto drain = [&] {
        for (unsigned i = 0; refill.fVramPrepareQueued; i++) {
            assert(i < 12);
            NVRMNVDAFramebuffer::vramPreparationFire(&refill, nullptr);
        }
    };
    refill.queueVramPreparation(small);
    drain();
    assert(refill.fVramPrepared.count(small) == 3);
    refill.queueVramPreparation(large);
    drain();
    assert(refill.fVramPrepared.count(large) == 3 && refill.fVramPrepared.count(small) == 3);

    // Switching preference to a 95 MiB size needs more than one eviction
    // across several calls. It can keep only two 77.625 MiB prior spares.
    constexpr NvU64 widest = 95ull << 20;
    const unsigned firstFrees = freed;
    refill.queueVramPreparation(widest);
    drain();
    assert(freed == firstFrees + 4);
    assert(refill.fVramPrepared.count(widest) == 3 && refill.fVramPrepared.count(large) == 2);
    assert(refill.fVramPrepared.count(small) == 0 && !refill.fVramPrepared.hasRoom(small));

    // Switching back evicts one excess previous spare even though no room
    // is available before the worker runs, then replenishes after a burst.
    refill.queueVramPreparation(large);
    drain();
    assert(freed == firstFrees + 5);
    assert(refill.fVramPrepared.count(large) == 3 && refill.fVramPrepared.count(widest) == 2);
    for (unsigned i = 0; i < 3; i++) assert(refill.fVramPrepared.take(large).handle);
    assert(!refill.fVramPrepared.take(large).handle);
    refill.queueVramPreparation(large);
    drain();
    assert(refill.fVramPrepared.count(large) == 3 && refill.fVramPrepared.count(widest) == 2);
}
'''

grant = extract('bool NVRMNVDAFramebuffer::vramGrant(', 'bool NVRMNVDAFramebuffer::vramRelease(')
prepare = grant[grant.index('if (!clearPreparedVram(block))'):grant.index('// A cold size')]
assert prepare.index('probe()') < prepare.index('fVramPrepared.add(block)')
assert 'fVramPrepared.wantsMore(want, fVramPrepareSize, fVramPreparePreviousSize)' in prepare
assert 'if (fVramPrepared.nextSize(fVramPrepareSize, fVramPreparePreviousSize)) armVramPreparation();' in prepare
assert grant.index('gAllocations.insert(mem, want)') > grant.index('return publishVramGrant(r, block, before')
assert 'want > (NvU64)budget' in grant and 'gFB->fVramPrepared.evictOther(0)' in grant
assert 'nv_xnu_pm_enter' not in source, 'the preparer must never park on the RM PM gate'
release = extract('bool NVRMNVDAFramebuffer::vramRelease(', 'IOReturn NVRMNVDAFramebuffer::callPlatformFunction(')
assert 'gAllocations.remove' in release and 'fVramPrepared' not in release, 'a returned grant never becomes a spare'
power = extract('if (functionName && functionName->isEqualTo("NVRMGpuPower"))', 'if (fProxy && functionName')
assert power.index('pauseVramPreparation()') < power.index('fSysSlept = true')

with tempfile.TemporaryDirectory() as directory:
    test = Path(directory) / 'prepare.cpp'
    test.write_text(fixture + extract('void NVRMNVDAFramebuffer::discardPreparedWhenIdle(', 'static bool nvrmVramOverlaps(') +
                    extract('bool NVRMNVDAFramebuffer::clearPreparedVram(', 'bool NVRMNVDAFramebuffer::vramGrant(') + checks)
    binary = test.with_suffix('')
    subprocess.run(['xcrun', 'clang++', '-std=c++17', '-pthread', '-I', str(fb.parent), '-I', str(fb),
                    str(test), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=8)
print('PASS: exact bounded reserves, alternate sizes, full chunk coverage, overlap cancellation and concurrent claim/standby')
