import Darwin
import Foundation

struct DisplayCounters: Sendable {
    let accepted: Int64
    let flips: Int64
    let surfaceMarks: Int64?
    let surfaceWaits: Int64?
    let surfaceTimeouts: Int64?
    let asyncDrops: Int64?
    let asyncRefusals: Int64?
    let latchTimeouts: Int64?
    let lateSwaps: Int64?

    func difference(from old: Self) -> [Int64?] {
        zip(values, old.values).map { current, previous in
            guard let current, let previous, current >= previous else { return nil }
            return current - previous
        }
    }

    var values: [Int64?] {
        [accepted, flips, surfaceMarks, surfaceWaits, surfaceTimeouts,
         asyncDrops, asyncRefusals, latchTimeouts, lateSwaps]
    }
}

struct DisplayGap: Sendable {
    let beginNS: UInt64
    let endNS: UInt64
    let observedAt: Date
    let firstAcceptedNS: UInt64?
    let longestPollNS: UInt64
    let before: DisplayCounters
    let after: DisplayCounters

    var durationMS: Double { Double(endNS - beginNS) / 1_000_000 }
    var firstAcceptedMS: Double? { firstAcceptedNS.map { Double($0 - beginNS) / 1_000_000 } }
    var longestPollMS: Double { Double(longestPollNS) / 1_000_000 }
}

/// Reports gaps in successful driver flips following steady activity. All times
/// are poll observations, so the longest poll is a bound on timing resolution.
struct DisplayGapTracker {
    private var lastPoll: UInt64?
    private var lastFlip: (time: UInt64, counters: DisplayCounters)?
    private var firstAccepted: UInt64?
    private var longestPoll: UInt64 = 0
    private var fastFlips = 0
    private var followUpGap = false

    mutating func observe(_ counters: DisplayCounters, at now: UInt64) -> DisplayGap? {
        guard let previous = lastPoll, previous < now, now - previous < 1_250_000_000,
              let flip = lastFlip, counters.flips >= flip.counters.flips,
              counters.accepted >= flip.counters.accepted else {
            self = DisplayGapTracker(lastPoll: now, lastFlip: (now, counters))
            return nil
        }
        lastPoll = now
        longestPoll = max(longestPoll, now - previous)
        if firstAccepted == nil, counters.accepted > flip.counters.accepted {
            firstAccepted = now
        }
        guard counters.flips != flip.counters.flips else { return nil }

        let duration = now - flip.time
        let consecutive = counters.flips - flip.counters.flips == 1
        let deltas = counters.difference(from: flip.counters)
        // Queue entries, copied presentations, and late flips can all increase
        // the accepted counter. Skip ambiguous intervals as the FPS reader does.
        let discarded = [deltas[5], deltas[6], deltas[8]].compactMap { $0 }.reduce(0.0) { $0 + Double($1) }
        let followsFlipPath = Double(counters.accepted - flip.counters.accepted) - discarded <= 3
        let result: DisplayGap?
        if consecutive, followsFlipPath, fastFlips >= 4, (33_000_000..<1_250_000_000).contains(duration) {
            result = DisplayGap(beginNS: flip.time, endNS: now, observedAt: Date(),
                                firstAcceptedNS: firstAccepted, longestPollNS: longestPoll,
                                before: flip.counters, after: counters)
        } else {
            result = nil
        }

        if consecutive, followsFlipPath, duration < 33_000_000 {
            fastFlips = min(4, fastFlips + 1)
            followUpGap = false
        } else if result != nil, !followUpGap {
            // Preserve consecutive stalls once, then require renewed activity
            // so slow desktop updates do not keep producing events in idle.
            followUpGap = true
        } else {
            fastFlips = 0
            followUpGap = false
        }
        lastFlip = (now, counters)
        firstAccepted = nil
        longestPoll = 0
        return result
    }
}

@MainActor
final class DisplayHandoffMonitor {
    private var timer: DispatchSourceTimer?
    private var generation = 0
    private(set) var isAvailable = false

    deinit { timer?.cancel() }

    func setEnabled(_ enabled: Bool, onGap: @escaping @MainActor (DisplayGap) -> Void) {
        if !enabled {
            if timer != nil { generation &+= 1 }
            timer?.cancel()
            timer = nil
            isAvailable = false
            return
        }
        guard timer == nil, let reader = CounterReader() else { return }
        generation &+= 1
        let current = generation
        let source = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "performanceviewer.flip-timing", qos: .utility))
        source.schedule(deadline: .now(), repeating: .milliseconds(4), leeway: .microseconds(500))
        source.setEventHandler { [weak self] in
            guard let gap = reader.poll() else { return }
            DispatchQueue.main.async {
                guard let self, self.generation == current else { return }
                onGap(gap)
            }
        }
        timer = source
        isAvailable = true
        source.resume()
    }
}

// A reader is owned by one serial timer queue. Resolving the numeric MIBs once
// avoids repeated name lookups on the sampling path.
private final class CounterReader: @unchecked Sendable {
    private struct Key {
        let mib: [Int32]
        init?(_ name: String) {
            var path = [Int32](repeating: 0, count: Int(CTL_MAXNAME))
            var count = path.count
            guard sysctlnametomib("debug." + name, &path, &count) == 0 else { return nil }
            mib = Array(path.prefix(count))
        }
        func read<Integer: FixedWidthInteger>(_: Integer.Type = Int64.self) -> Int64? {
            var value: Integer = 0
            var size = MemoryLayout<Integer>.size
            // Darwin's sysctl takes a mutable pointer to the input MIB.
            let status = mib.withUnsafeBufferPointer { path -> Int32 in
                guard let base = path.baseAddress else { return -1 }
                return sysctl(UnsafeMutablePointer(mutating: base), UInt32(path.count), &value, &size, nil, 0)
            }
            guard status == 0, size == MemoryLayout<Integer>.size, value >= 0 else { return nil }
            return Int64(exactly: value)
        }
    }

    private let accepted: Key
    private let flips: Key
    private let marks = Key("nvaccel_surface_gpu_marks")
    private let waits = Key("nvaccel_surface_gpu_waits")
    private let timeouts = Key("nvaccel_surface_gpu_timeouts")
    private let drops = Key("nvaccel_iop_async_drop")
    private let refusals = Key("nvaccel_iop_async_refused")
    private let latch = Key("nvrmfb_flip_latch_timeouts")
    private let late = Key("nvaccel_iop_late")
    private var tracker = DisplayGapTracker()

    init?() {
        guard let accepted = Key("nvaccel_iop_ok"), let flips = Key("nvaccel_iop_flips") else { return nil }
        self.accepted = accepted
        self.flips = flips
    }

    func poll() -> DisplayGap? {
        guard let first = flips.read(), let queued = accepted.read() else {
            tracker = DisplayGapTracker()
            return nil
        }
        let counters = DisplayCounters(accepted: queued, flips: first, surfaceMarks: marks?.read(),
                                       surfaceWaits: waits?.read(), surfaceTimeouts: timeouts?.read(),
                                       asyncDrops: drops?.read(), asyncRefusals: refusals?.read(),
                                       latchTimeouts: latch?.read(Int32.self), lateSwaps: late?.read())
        // Skip this observation if a flip crossed the reads; the following
        // sample measures how long the observer actually went without a poll.
        guard flips.read() == first else { return nil }
        return tracker.observe(counters, at: DispatchTime.now().uptimeNanoseconds)
    }
}
