#ifndef NV_FENCE_TRACE_H
#define NV_FENCE_TRACE_H
#include <stdint.h>

// Private optional NVMTL/NVK diagnostic ABI. Returned to the caller of the same single-fence
// wait, never a process-global "latest event". All host timestamps are in uptime nanoseconds.
// The graphics event counter belongs to a device connection; it is not this semaphore target.
struct nv_fence_trace_v1 {
    uint64_t target, ready_ns, reap_done_ns, event_enter_ns, event_return_ns, shared_post_ns;
    int64_t event_returned;
    int32_t kernel_status;
    uint32_t ambiguous_target;
    uint32_t matched, target_valid, slot, gen, event_calls, changed_calls, event_seen, shared_post_valid;
};
#endif
