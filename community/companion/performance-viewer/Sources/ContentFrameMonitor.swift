import Combine
import CoreMedia
import Darwin
import Foundation
import ScreenCaptureKit

enum ContentFrameState: Equatable {
    case idle
    case choosing
    case starting
    case measuring
    case unavailable
    case failed(String)
}

enum ContentFrameSource: Equatable {
    case display
    case window
}

struct ContentFrameReading {
    let state: ContentFrameState
    var source: ContentFrameSource? = nil
    var fps: Double? = nil
    var medianIntervalMS: Double? = nil
    var p95IntervalMS: Double? = nil
    var maxIntervalMS: Double? = nil
    var sampleCount: Int = 0
    var stutterCount: Int? = nil
    var recentStutterCount: Int? = nil
}

@MainActor
final class ContentFrameMonitor: ObservableObject {
    @Published private(set) var reading: ContentFrameReading
    private var session: (any ContentCaptureSession)?
    private var isPaused = false

    var activeCaptureSource: ContentFrameSource? { session?.activeCaptureSource }

    init() {
        reading = ContentFrameReading(state: Self.initialState)
    }

    func chooseSource(_ source: ContentFrameSource = .display) {
        guard #available(macOS 14.0, *) else { return }
        if session == nil {
            session = ScreenCaptureSession(monitor: self, isPaused: isPaused)
        }
        session?.present(source)
    }

    func stop() {
        session?.stop()
        session = nil
        reading = ContentFrameReading(state: Self.initialState)
    }

    func setPaused(_ paused: Bool) {
        guard paused != isPaused else { return }
        isPaused = paused
        session?.setPaused(paused)
    }

    fileprivate func publish(_ reading: ContentFrameReading, from session: any ContentCaptureSession) {
        guard self.session === session else { return }
        self.reading = reading
    }

    fileprivate func end(_ state: ContentFrameState, from session: any ContentCaptureSession) {
        guard self.session === session else { return }
        session.stop()
        self.session = nil
        reading = ContentFrameReading(state: state)
    }

    private static var initialState: ContentFrameState {
        if #available(macOS 14.0, *) { return .idle }
        return .unavailable
    }
}

/// A trailing two-second window of display timestamps. FPS includes idle time
/// within that window. Interval statistics require two distinct complete frames.
struct ContentFrameSamples {
    private static let window = 2.0
    private let startedAt: Double
    private var timestamps: [Double] = []

    init(startedAt: Double) {
        self.startedAt = startedAt
    }

    mutating func insert(_ timestamp: Double) {
        guard timestamp.isFinite, timestamp >= startedAt,
              timestamps.last.map({ timestamp > $0 }) ?? true else { return }
        timestamps.append(timestamp)
        let oldest = timestamp - Self.window
        if let first = timestamps.firstIndex(where: { $0 >= oldest }), first > 0 {
            timestamps.removeFirst(first)
        }
        if timestamps.count > 1024 {
            timestamps.removeFirst(timestamps.count - 1024)
        }
    }

    func snapshot(at now: Double) -> ContentFrameReading {
        let cutoff = max(startedAt, now - Self.window)
        let duration = now - cutoff
        var reading = ContentFrameReading(state: .measuring)
        guard duration >= 0.5 else { return reading }

        let recent = timestamps.filter { $0 > cutoff && $0 <= now }
        reading.sampleCount = recent.count
        reading.fps = Double(recent.count) / duration
        let intervals = zip(recent, recent.dropFirst()).map { ($1 - $0) * 1_000 }.sorted()
        guard !intervals.isEmpty else { return reading }
        let middle = intervals.count / 2
        reading.medianIntervalMS = intervals.count.isMultiple(of: 2)
            ? (intervals[middle - 1] + intervals[middle]) / 2
            : intervals[middle]
        reading.p95IntervalMS = intervals[Int(ceil(Double(intervals.count) * 0.95)) - 1]
        reading.maxIntervalMS = intervals.last
        return reading
    }
}

@MainActor
private protocol ContentCaptureSession: AnyObject {
    var activeCaptureSource: ContentFrameSource? { get }
    func present(_ source: ContentFrameSource)
    func stop()
    func setPaused(_ paused: Bool)
}

@available(macOS 14.0, *)
private extension ContentFrameSource {
    var style: SCShareableContentStyle {
        switch self {
        case .display: return .display
        case .window: return .window
        }
    }

    var pickerConfiguration: SCContentSharingPickerConfiguration {
        var configuration = SCContentSharingPickerConfiguration()
        switch self {
        case .display:
            configuration.allowedPickerModes = .singleDisplay
        case .window:
            configuration.allowedPickerModes = .singleWindow
            if let identifier = Bundle.main.bundleIdentifier {
                configuration.excludedBundleIDs = [identifier]
            }
        }
        return configuration
    }
}

@available(macOS 14.0, *)
@MainActor
private final class ScreenCaptureSession: NSObject, ContentCaptureSession, SCContentSharingPickerObserver, SCStreamDelegate {
    private weak var monitor: ContentFrameMonitor?
    private let picker = SCContentSharingPicker.shared
    private let output = CaptureFrameOutput()
    private let captureQueue = DispatchQueue(label: "local.performanceviewer.content-frames", qos: .userInitiated)
    private var stream: SCStream?
    private var activeSource: ContentFrameSource?
    var activeCaptureSource: ContentFrameSource? { isRunning ? activeSource : nil }
    private var requestedSource: ContentFrameSource?
    private var isObservingPicker = false
    private var isChoosing: Bool { requestedSource != nil }
    private var isChanging = false
    private var isRunning = false
    private var isPaused: Bool
    private var publication: Task<Void, Never>?

    init(monitor: ContentFrameMonitor, isPaused: Bool) {
        self.monitor = monitor
        self.isPaused = isPaused
        super.init()
        output.setPaused(isPaused)
    }

    func present(_ source: ContentFrameSource) {
        guard !isChoosing, !isChanging else { return }
        picker.defaultConfiguration = source.pickerConfiguration
        if !isObservingPicker {
            picker.maximumStreamCount = 1
            picker.add(self)
            picker.isActive = true
            isObservingPicker = true
        }
        requestedSource = source
        stopPublishing()
        publish(ContentFrameReading(state: .choosing, source: source))
        if let stream {
            picker.setConfiguration(source.pickerConfiguration, for: stream)
            picker.present(for: stream, using: source.style)
        } else {
            picker.present(using: source.style)
        }
    }

    func stop() {
        stopPublishing()
        requestedSource = nil
        activeSource = nil
        isChanging = false
        output.selectSource(nil)
        if isObservingPicker {
            picker.remove(self)
            picker.isActive = false
            isObservingPicker = false
        }
        let previous = stream
        stream = nil
        if let previous, isRunning {
            try? previous.removeStreamOutput(output, type: .screen)
            Task { try? await previous.stopCapture() }
        }
        isRunning = false
    }

    func setPaused(_ paused: Bool) {
        isPaused = paused
        output.setPaused(paused)
        if paused {
            stopPublishing()
        } else if isRunning, !isChoosing, !isChanging {
            startPublishing()
        }
    }

    private func selectionChanged(_ filter: SCContentFilter, for selectedStream: SCStream?) {
        guard isObservingPicker, !isChanging, selectedStream === stream,
              let source = requestedSource ?? activeSource else { return }
        guard filter.style == source.style else {
            cancelSelection()
            return
        }
        requestedSource = nil
        isChanging = true
        stopPublishing()
        publish(ContentFrameReading(state: .starting, source: source))
        if let stream {
            output.selectSource(nil)
            Task { @MainActor [weak self] in
                do {
                    try await stream.updateContentFilter(filter)
                    guard let self, self.stream === stream else { return }
                    self.output.selectSource(source)
                    self.captureReady(stream, source: source)
                } catch {
                    self?.captureFailed(error, stream: stream)
                }
            }
            return
        }

        let configuration = SCStreamConfiguration()
        configuration.width = 96
        configuration.height = 64
        configuration.minimumFrameInterval = .zero
        configuration.queueDepth = 5
        configuration.showsCursor = false
        configuration.capturesAudio = false
        configuration.streamName = "性能查看器 · 画面帧率"
        if #available(macOS 14.2, *) { configuration.includeChildWindows = false }
        let newStream = SCStream(filter: filter, configuration: configuration, delegate: self)
        stream = newStream
        output.selectSource(source)
        do {
            try newStream.addStreamOutput(output, type: .screen, sampleHandlerQueue: captureQueue)
        } catch {
            captureFailed(error, stream: newStream)
            return
        }
        Task { @MainActor [weak self, output] in
            do {
                try await newStream.startCapture()
                guard let self, self.stream === newStream else {
                    try? newStream.removeStreamOutput(output, type: .screen)
                    try? await newStream.stopCapture()
                    return
                }
                self.captureReady(newStream, source: source)
            } catch {
                self?.captureFailed(error, stream: newStream)
            }
        }
    }

    private func cancelSelection() {
        guard isChoosing else { return }
        requestedSource = nil
        if isRunning, let stream, let activeSource {
            picker.defaultConfiguration = activeSource.pickerConfiguration
            picker.setConfiguration(activeSource.pickerConfiguration, for: stream)
            publish(output.currentReading())
            startPublishing()
        } else {
            monitor?.end(.idle, from: self)
        }
    }

    private func captureReady(_ stream: SCStream, source: ContentFrameSource) {
        guard self.stream === stream else { return }
        activeSource = source
        isChanging = false
        isRunning = true
        publish(output.currentReading())
        startPublishing()
    }

    private func captureFailed(_ error: Error, stream: SCStream) {
        guard self.stream === stream else { return }
        let state: ContentFrameState
        switch (error as? SCStreamError)?.code {
        case .userStopped: state = .idle
        case .userDeclined: state = .failed("画面共享未获授权")
        default: state = .failed("画面共享已结束，请重新选择")
        }
        monitor?.end(state, from: self)
    }

    private func publish(_ reading: ContentFrameReading) {
        monitor?.publish(reading, from: self)
    }

    private func startPublishing() {
        guard !isPaused else { return }
        stopPublishing()
        publish(output.currentReading())
        publication = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .milliseconds(500)) } catch { break }
                guard !Task.isCancelled, let self else { break }
                self.publish(self.output.currentReading())
            }
        }
    }

    private func stopPublishing() {
        publication?.cancel()
        publication = nil
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        Task { @MainActor [weak self] in
            guard let self, stream === self.stream else { return }
            self.cancelSelection()
        }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter, for stream: SCStream?) {
        Task { @MainActor [weak self] in self?.selectionChanged(filter, for: stream) }
    }

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        Task { @MainActor [weak self] in
            guard let self, self.isChoosing else { return }
            if self.isRunning {
                self.cancelSelection()
            } else {
                self.monitor?.end(.failed("无法打开系统画面选择器"), from: self)
            }
        }
    }

    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        Task { @MainActor [weak self] in self?.captureFailed(error, stream: stream) }
    }
}

@available(macOS 14.0, *)
private final class CaptureFrameOutput: NSObject, SCStreamOutput {
    private let lock = NSLock()
    private var samples = ContentFrameSamples(startedAt: FrameHostClock.now)
    private var stutters = FrameStutterCounter()
    private var source: ContentFrameSource?
    private var sourceStartedAt = FrameHostClock.now
    private var latestDisplayTime: Double?
    private var interrupted = false
    private var paused = false

    func selectSource(_ source: ContentFrameSource?) {
        lock.lock()
        self.source = source
        sourceStartedAt = FrameHostClock.now
        samples = ContentFrameSamples(startedAt: sourceStartedAt)
        stutters = FrameStutterCounter()
        latestDisplayTime = nil
        interrupted = false
        lock.unlock()
    }

    func setPaused(_ paused: Bool) {
        lock.lock()
        if self.paused != paused {
            self.paused = paused
            stutters.interrupt()
        }
        lock.unlock()
    }

    func currentReading() -> ContentFrameReading {
        lock.lock()
        let now = FrameHostClock.now
        let source = self.source
        let copy = samples
        let observedDisplayClock = latestDisplayTime != nil
        let hasRate = observedDisplayClock && !interrupted
        let counts = observedDisplayClock ? stutters.reading(at: now) : nil
        lock.unlock()

        var result = hasRate ? copy.snapshot(at: now) : ContentFrameReading(state: .measuring)
        result.source = source
        result.stutterCount = counts?.total
        result.recentStutterCount = counts?.recent
        return result
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let metadata = attachments.first,
              let rawStatus = metadata[.status] as? Int,
              let status = SCFrameStatus(rawValue: rawStatus) else { return }

        lock.lock()
        defer { lock.unlock() }
        guard source != nil else { return }
        switch status {
        case .blank, .suspended, .stopped:
            interruptTiming()
        case .started, .complete:
            // Only WindowServer's display timestamp tracks a presentation event.
            // The sample buffer clock and callback arrival are capture timing.
            // The image buffer is never retrieved or retained.
            guard let ticks = metadata[.displayTime] as? UInt64, ticks > 0 else {
                if status == .complete { interruptTiming() }
                return
            }
            let timestamp = FrameHostClock.seconds(for: ticks)
            let now = FrameHostClock.now
            guard timestamp.isFinite, timestamp <= now + 0.025, timestamp >= now - 5 else {
                if status == .complete { interruptTiming() }
                return
            }
            guard timestamp >= sourceStartedAt, latestDisplayTime.map({ timestamp > $0 }) ?? true else { return }
            if interrupted {
                samples = ContentFrameSamples(startedAt: timestamp)
                interrupted = false
            }
            latestDisplayTime = timestamp
            // The initial frame anchors the clock without counting a start event as a new update.
            if status == .complete { samples.insert(timestamp) }
            if !paused { stutters.observe(at: timestamp) }
        case .idle:
            break
        @unknown default:
            break
        }
    }

    private func interruptTiming() {
        interrupted = true
        stutters.interrupt()
    }
}

private enum FrameHostClock {
    private static let secondsPerTick: Double = {
        var timebase = mach_timebase_info_data_t()
        mach_timebase_info(&timebase)
        return Double(timebase.numer) / Double(timebase.denom) / 1_000_000_000
    }()

    static var now: Double { seconds(for: mach_absolute_time()) }
    static func seconds(for ticks: UInt64) -> Double { Double(ticks) * secondsPerTick }
}
