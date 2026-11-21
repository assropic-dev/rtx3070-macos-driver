/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 */
#pragma once
#include "nvrm_vram_abi.h"

// The display gate protects this inventory. It contains only fully zeroed,
// never-published allocations whose BAR1 bytes were already charged.
class NVRMPreparedVramPool {
public:
    static constexpr unsigned burstDepth = 3;
    static constexpr unsigned capacity = 2 * burstDepth;
    static constexpr unsigned long long limit = 448ull << 20;
    static constexpr unsigned long long minimumHeadroom = 128ull << 20;
    static bool eligible(unsigned long long bytes) { return bytes >= (32ull << 20) && bytes <= (96ull << 20); }

    unsigned count(unsigned long long bytes) const {
        unsigned matching = 0;
        for (const auto &entry : entries) if (entry.handle && entry.actualSize == bytes) matching++;
        return matching;
    }

    bool wantsMore(unsigned long long bytes, unsigned long long primary, unsigned long long previous) const {
        return count(bytes) < target(bytes, primary, previous);
    }

    unsigned long long nextSize(unsigned long long primary, unsigned long long previous) const {
        if (!eligible(primary)) return 0;
        const unsigned ready = count(primary);
        if (ready == 0) return primary;
        const unsigned priorTarget = target(previous, primary, previous);
        const unsigned priorReady = count(previous);
        if (priorTarget && !priorReady) return previous;
        if (ready < burstDepth) return primary;
        return priorReady < priorTarget ? previous : 0;
    }

    bool hasRoom(unsigned long long bytes) const {
        if (!eligible(bytes)) return false;
        unsigned count = 0;
        unsigned long long used = 0;
        for (const auto &entry : entries) {
            if (entry.handle) { count++; used += entry.actualSize; }
        }
        return count < capacity && bytes <= limit - used;
    }

    bool add(const NVRMVramRequest &entry) {
        if (!entry.handle || !entry.kva || !entry.phys || entry.allocationCookie || !hasRoom(entry.actualSize)) return false;
        for (auto &slot : entries) {
            if (!slot.handle) { slot = entry; return true; }
        }
        return false;
    }

    NVRMVramRequest take(unsigned long long bytes) {
        for (auto &entry : entries) {
            if (entry.handle && entry.actualSize == bytes) return remove(entry);
        }
        return {};
    }

    NVRMVramRequest evictOther(unsigned long long bytes) {
        for (auto &entry : entries) {
            if (entry.handle && entry.actualSize != bytes) return remove(entry);
        }
        return {};
    }

    NVRMVramRequest makeRoom(unsigned long long wanted, unsigned long long primary, unsigned long long previous) {
        for (auto &entry : entries) {
            if (entry.handle && entry.actualSize != primary && entry.actualSize != previous) return remove(entry);
        }
        // A size that was primary can have more spares than its new quota.
        if (wanted == primary && previous != primary && count(previous) > target(previous, primary, previous))
            return take(previous);
        return {};
    }

private:
    NVRMVramRequest entries[capacity] = {};
    static unsigned target(unsigned long long bytes, unsigned long long primary, unsigned long long previous) {
        if (!eligible(bytes) || !eligible(primary)) return 0;
        if (bytes == primary) return burstDepth;
        if (bytes != previous) return 0;
        const auto fits = (limit - burstDepth * primary) / previous;
        return fits < burstDepth ? static_cast<unsigned>(fits) : burstDepth;
    }
    static NVRMVramRequest remove(NVRMVramRequest &entry) {
        NVRMVramRequest removed = entry;
        entry = {};
        return removed;
    }
};
