#include <cpuid.h>
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#define NANOS_PER_MILLISECOND UINT64_C(1000000)
#define NANOS_PER_SECOND UINT64_C(1000000000)

// APERF counts actual cycles and MPERF counts reference cycles while a core
// is active. AMD documents both as user-mode RDPRU readings on supported CPUs.
// https://www.amd.com/content/dam/amd/en/documents/processor-tech-docs/programmer-references/24594.pdf
struct counter_snapshot {
    uint64_t time_ns;
    uint64_t actual;
    uint64_t reference;
};

static uint64_t read_rdpru(unsigned selector) {
    uint32_t low, high;
    __asm__ volatile("lfence\n\t.byte 0x0f, 0x01, 0xfd\n\tlfence"
                     : "=a"(low), "=d"(high) : "c"(selector) : "memory");
    return ((uint64_t)high << 32) | low;
}

static unsigned local_apic_id(void) {
    unsigned eax, ebx, ecx, edx;
    __asm__ volatile("cpuid" : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx)
                     : "0"(1), "2"(0) : "memory");
    return ebx >> 24;
}

static void instruction_fault(int signal_number) {
    _exit(128 + signal_number);
}

static int supports_rdpru(void) {
    unsigned eax, ebx, ecx, edx;
    if (!__get_cpuid(0, &eax, &ebx, &ecx, &edx)
        || ebx != 0x68747541 || edx != 0x69746e65 || ecx != 0x444d4163) {
        return 0;
    }
    return __get_cpuid(0x80000008, &eax, &ebx, &ecx, &edx) && (ebx & (1u << 4)) != 0;
}

static int sample(double reference_hz, struct counter_snapshot baseline[256]) {
    const uint64_t now = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    const unsigned cpu = local_apic_id();
    const uint64_t reference_cycles = read_rdpru(0);
    const uint64_t actual_cycles = read_rdpru(1);
    if (cpu != local_apic_id() || reference_cycles == 0 || actual_cycles == 0) return 0;

    const struct counter_snapshot previous = baseline[cpu];
    if (previous.time_ns != 0 && now - previous.time_ns < 60 * NANOS_PER_MILLISECOND) return 0;
    baseline[cpu] = (struct counter_snapshot){now, actual_cycles, reference_cycles};
    if (previous.time_ns == 0 || now - previous.time_ns > 3 * NANOS_PER_SECOND
        || reference_cycles <= previous.reference || actual_cycles <= previous.actual) {
        return 0;
    }

    const uint64_t elapsed_ns = now - previous.time_ns;
    const uint64_t reference_delta = reference_cycles - previous.reference;
    const double maximum_reference_delta = 1.25 * reference_hz * ((double)elapsed_ns / NANOS_PER_SECOND);
    if (reference_delta < 250000 || reference_delta > maximum_reference_delta) return 0;
    const double ratio = (double)(actual_cycles - previous.actual) / (double)reference_delta;
    if (ratio < 0.15 || ratio > 2.5) return 0;
    printf("%" PRIu64 " %.0f\n", now, ratio * reference_hz / 1000000.0);
    fflush(stdout);
    return 1;
}

int main(int argc, const char *argv[]) {
    if (argc != 2 || !supports_rdpru()) return 0;
    char *end = NULL;
    const double reference_hz = strtod(argv[1], &end);
    if (*end || !(reference_hz >= 100000000.0 && reference_hz <= 10000000000.0)) return 0;
    signal(SIGILL, instruction_fault);
    signal(SIGSEGV, instruction_fault);
    signal(SIGBUS, instruction_fault);

    struct counter_snapshot baseline[256] = {0};
    for (;;) {
        char request;
        const ssize_t size = read(STDIN_FILENO, &request, sizeof(request));
        if (size < 0 && errno == EINTR) continue;
        if (size != 1) break;
        // A short sleep supplies a same-core interval when possible, with zero
        // busy waiting. Cached per-APIC baselines also tolerate migration.
        if (!sample(reference_hz, baseline)) {
            nanosleep(&(struct timespec){.tv_nsec = 100 * NANOS_PER_MILLISECOND}, NULL);
            sample(reference_hz, baseline);
        }
    }
    return 0;
}
