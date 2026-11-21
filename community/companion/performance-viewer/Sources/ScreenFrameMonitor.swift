import AppKit
import QuartzCore

struct ScreenFrameReading {
    var fps: Double? = nil
    var medianIntervalMS: Double? = nil
    var p95IntervalMS: Double? = nil
    var maxIntervalMS: Double? = nil
    var stutterCount: Int? = nil
    var recentStutterCount: Int? = nil
    var sampleCount = 0
}

/// Counts changes to AppKit's per-display framebuffer update timestamp. The display's
/// refresh timing sets the polling cadence and is never counted as a picture update.
@MainActor
final class ScreenFrameMonitor {
    private struct Frame {
        let timestamp: TimeInterval
        let precedingTimestamp: TimeInterval?
    }

    private static let windowDuration: TimeInterval = 2
    private static let minimumDuration: TimeInterval = 0.5
    private var screen: NSScreen?
    private var displayID: UInt32?
    private var shortestRefresh: TimeInterval?
    private var isPaused = false
    private var timer: DispatchSourceTimer?
    private var generation = 0
    private var beganAt: TimeInterval?
    private var lastPolledAt: TimeInterval?
    private var lastSeenTimestamp: TimeInterval?
    private var lastTrackedFrame: TimeInterval?
    private var frames: [Frame] = []
    private var stutters = FrameStutterCounter()

    deinit { timer?.cancel() }

    func bindScreen(_ screen: NSScreen?) {
        let id = screen.flatMap {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
        }
        let refresh = screen.flatMap { screen -> TimeInterval? in
            let interval = screen.minimumRefreshInterval
            if (0.001...0.1).contains(interval) { return interval }
            let maximum = screen.maximumFramesPerSecond
            return (10...1_000).contains(maximum) ? 1 / Double(maximum) : nil
        }
        self.screen = screen
        guard displayID != id || shortestRefresh != refresh else { return }
        if displayID != id { stutters = FrameStutterCounter() }
        displayID = id
        shortestRefresh = refresh
        restartPolling()
    }

    func setPaused(_ paused: Bool) {
        guard paused != isPaused else { return }
        isPaused = paused
        restartPolling()
    }

    func sample() -> ScreenFrameReading {
        let now = CACurrentMediaTime()
        var reading = ScreenFrameReading()
        guard let beganAt, let lastPolledAt, now - lastPolledAt < 0.25 else { return reading }
        let cutoff = max(beganAt, now - Self.windowDuration)
        let duration = now - cutoff
        guard duration >= Self.minimumDuration else { return reading }

        let recent = frames.filter { $0.timestamp > cutoff && $0.timestamp <= now }
        reading.fps = Double(recent.count) / duration
        reading.sampleCount = recent.count
        let stutter = stutters.reading(at: now)
        reading.stutterCount = stutter.total
        reading.recentStutterCount = stutter.recent

        let intervals = recent.compactMap { frame -> Double? in
            guard let previous = frame.precedingTimestamp, previous >= cutoff else { return nil }
            return (frame.timestamp - previous) * 1_000
        }.sorted()
        guard !intervals.isEmpty else { return reading }
        let middle = intervals.count / 2
        reading.medianIntervalMS = intervals.count.isMultiple(of: 2)
            ? (intervals[middle - 1] + intervals[middle]) / 2
            : intervals[middle]
        reading.p95IntervalMS = intervals[Int(ceil(Double(intervals.count) * 0.95)) - 1]
        reading.maxIntervalMS = intervals.last
        return reading
    }

    private func restartPolling() {
        timer?.cancel()
        timer = nil
        generation &+= 1
        interruptObservation()
        guard !isPaused, screen != nil, displayID != nil, let shortestRefresh else { return }

        let interval = max(0.001, shortestRefresh * 0.4)
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now(), repeating: interval, leeway: .microseconds(200))
        let activeGeneration = generation
        timer.setEventHandler { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.generation == activeGeneration else { return }
                self.poll(shortestRefresh: shortestRefresh)
            }
        }
        self.timer = timer
        timer.resume()
    }

    private func interruptObservation(at now: TimeInterval? = nil, lastUpdate: TimeInterval? = nil) {
        beganAt = now
        lastPolledAt = now
        lastSeenTimestamp = lastUpdate
        lastTrackedFrame = nil
        frames.removeAll(keepingCapacity: true)
        stutters.interrupt()
    }

    private func poll(shortestRefresh: TimeInterval) {
        guard let screen else { return }
        let timestamp = screen.lastDisplayUpdateTimestamp
        let now = CACurrentMediaTime()
        guard timestamp.isFinite, timestamp > 0, timestamp <= now + shortestRefresh else {
            if beganAt != nil { interruptObservation() }
            return
        }
        guard let previousPoll = lastPolledAt, let previousTimestamp = lastSeenTimestamp,
              timestamp >= previousTimestamp else {
            interruptObservation(at: now, lastUpdate: timestamp)
            return
        }

        lastPolledAt = now
        guard timestamp > previousTimestamp else { return }
        lastSeenTimestamp = timestamp

        // A time gap in the observer is ambiguous if more than one update could
        // have occurred between its previous observation and the current image.
        // The poll can be delayed *after* a genuine slow frame is already visible.
        if now - previousPoll >= shortestRefresh * 0.95,
           timestamp - previousTimestamp >= shortestRefresh * 1.75,
           timestamp - previousPoll >= shortestRefresh * 0.95 {
            interruptObservation(at: now, lastUpdate: timestamp)
            return
        }

        frames.append(Frame(timestamp: timestamp, precedingTimestamp: lastTrackedFrame))
        let cutoff = timestamp - Self.windowDuration
        if let firstRecent = frames.firstIndex(where: { $0.timestamp >= cutoff }), firstRecent > 0 {
            frames.removeFirst(firstRecent)
        }
        lastTrackedFrame = timestamp
        stutters.observe(at: timestamp)
    }
}
