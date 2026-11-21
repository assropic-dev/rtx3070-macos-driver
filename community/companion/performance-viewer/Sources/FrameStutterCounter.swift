import Foundation

/// Counts episodes from consecutive presentation timestamps within one capture session.
struct FrameStutterCounter {
    private static let historyDuration: TimeInterval = 60
    private static let idleGap: TimeInterval = 1.25
    private static let maximumCadenceInterval: TimeInterval = 0.125
    private static let minimumStutterInterval: TimeInterval = 0.050
    private static let minimumCadenceSamples = 8
    private static let maximumCadenceSamples = 24

    private var lastFrame: TimeInterval?
    private var cadence: [TimeInterval] = []
    private var isInEpisode = false
    private var recoveredFrames = 0
    private var total = 0
    private var recentEvents: [TimeInterval] = []

    mutating func observe(at timestamp: TimeInterval) {
        guard timestamp.isFinite, lastFrame.map({ timestamp > $0 }) ?? true else { return }
        defer { lastFrame = timestamp }
        guard let lastFrame else { return }
        let gap = timestamp - lastFrame

        if gap >= Self.idleGap {
            interrupt()
            return
        }

        if let expected = recentCadence() {
            let slowThreshold = max(expected * 2, Self.minimumStutterInterval)
            if gap >= slowThreshold {
                recoveredFrames = 0
                if !isInEpisode {
                    total += 1
                    recentEvents.removeAll { timestamp - $0 >= Self.historyDuration }
                    recentEvents.append(timestamp)
                    isInEpisode = true
                }
            } else if isInEpisode {
                recoveredFrames += 1
                if recoveredFrames >= 2 { isInEpisode = false }
            }
        }

        if gap <= Self.maximumCadenceInterval {
            cadence.append(gap)
            if cadence.count > Self.maximumCadenceSamples { cadence.removeFirst() }
        }
    }

    mutating func interrupt() {
        lastFrame = nil
        cadence.removeAll(keepingCapacity: true)
        isInEpisode = false
        recoveredFrames = 0
    }

    func reading(at timestamp: TimeInterval) -> (total: Int, recent: Int) {
        let recentCount = recentEvents.lazy.filter { $0 <= timestamp && timestamp - $0 < Self.historyDuration }.count
        return (total, recentCount)
    }

    private func recentCadence() -> TimeInterval? {
        guard cadence.count >= Self.minimumCadenceSamples else { return nil }
        let sorted = cadence.sorted()
        let middle = sorted.count / 2
        if sorted.count.isMultiple(of: 2) { return (sorted[middle - 1] + sorted[middle]) / 2 }
        return sorted[middle]
    }
}
