import AppKit
import SwiftUI
import UniformTypeIdentifiers

private let appTitle = "性能查看器"
private let refreshInterval: UInt64 = 1_000_000_000
private let gpuLogInterval: UInt64 = 100_000_000
private let historyDuration: TimeInterval = 60
private let bytesPerGibibyte: Double = 1_073_741_824

@main
@MainActor
struct PerformanceViewerApp: App {
    @NSApplicationDelegateAdaptor(ViewerDelegate.self) private var delegate
    @StateObject private var model = ViewerModel()
    @AppStorage("viewerFloatsAboveWindows") private var floatsAboveWindows = false

    var body: some Scene {
        Window(appTitle, id: "dashboard") {
            Dashboard(model: model, contentFrames: model.contentFrames, floatsAboveWindows: $floatsAboveWindows)
                .frame(minWidth: 820, minHeight: 555)
                .preferredColorScheme(.dark)
                .background {
                    ViewerWindowBinding(floatsAboveWindows: floatsAboveWindows, screenDidChange: model.bindScreen)
                        .allowsHitTesting(false)
                }
                .task(id: model.isPaused) { await model.collect() }
                .onReceive(model.contentFrames.$reading) { model.updateCaptureLog($0) }
                .onDisappear {
                    model.bindScreen(nil)
                    model.contentFrames.stop()
                    model.stopProcessLog()
                }
        }
        .defaultSize(width: 1010, height: 930)
        .windowResizability(.contentMinSize)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(after: .windowArrangement) {
                Toggle("窗口置顶", isOn: $floatsAboveWindows)
            }
            CommandMenu("采样") {
                Button(model.isPaused ? "继续采样" : "暂停采样") {
                    model.isPaused.toggle()
                }
                .keyboardShortcut("p", modifiers: [.command, .shift])
                Button(model.isRecordingProcesses ? "停止记录进程" : "开始记录进程…") {
                    model.toggleProcessLog()
                }
                .disabled(model.isChangingProcessLog)
            }
        }
    }
}

final class ViewerDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

private struct HardwareDetails: Sendable {
    let cpuName: String
    let gpuName: String?
    let physicalCPUCount: Int
    let logicalCPUCount: Int
    let cpuNominalFrequencyMHz: Double?
}

private actor Sampler {
    private lazy var monitor = PerformanceMonitor()
    private let windowServer = WindowServerMonitor()
    private var gpuLogSession: UUID?
    private var gpuSamples: [ProcessLogGPUSample] = []

    func hardware() -> HardwareDetails {
        HardwareDetails(
            cpuName: monitor.cpuName,
            gpuName: monitor.gpuName,
            physicalCPUCount: monitor.physicalCPUCount,
            logicalCPUCount: monitor.logicalCPUCount,
            cpuNominalFrequencyMHz: monitor.cpuNominalFrequencyMHz
        )
    }

    func resetWindowServer() { windowServer.reset() }

    func read() -> ViewerSample {
        let process = windowServer.sample()
        return ViewerSample(performance: monitor.sample(windowServerPID: process.pid), windowServer: process)
    }

    func recordGPU(in log: ProcessUsageLog, sessionID: UUID) async {
        guard !Task.isCancelled else { return }
        gpuLogSession = sessionID
        gpuSamples.removeAll(keepingCapacity: true)
        while !Task.isCancelled {
            let sample = monitor.sampleGPUForLog()
            gpuSamples.append(sample)
            if gpuSamples.count > 128 { gpuSamples.removeFirst() }
            await log.appendGPUSample(sample, sessionID: sessionID)
            do {
                try await Task.sleep(nanoseconds: gpuLogInterval)
            } catch {
                return
            }
        }
    }

    func gpuSample(atOrBefore uptimeNS: UInt64, sessionID: UUID) -> ProcessLogGPUSample? {
        guard gpuLogSession == sessionID,
              var index = gpuSamples.lastIndex(where: { $0.uptimeNS <= uptimeNS }),
              gpuSamples[index].percent != nil else { return nil }
        // Repeated reads of a firmware publication do not make it newer.
        if let timestamp = gpuSamples[index].sourceTimestamp {
            while index > 0, gpuSamples[index - 1].sourceTimestamp == timestamp { index -= 1 }
        }
        let sample = gpuSamples[index]
        return uptimeNS - sample.uptimeNS <= 1_250_000_000 ? sample : nil
    }
}

private struct ViewerSample {
    let performance: PerformanceSample
    let windowServer: WindowServerReading
}

private struct HistoryPoint {
    let uptime: TimeInterval
    let cpu: Double?
    let gpu: Double?
    let memory: Double?
    let peakThread: ThreadPeak?

    var peakThreadUtilization: Double? { peakThread?.utilization }
}

private struct ThreadPeak {
    let threadID: Int
    let utilization: Double

    init?(cores: [CoreReading]) {
        guard let busiest = cores.filter({ $0.utilization != nil }).max(by: {
            ($0.utilization ?? 0) < ($1.utilization ?? 0)
        }), let utilization = busiest.utilization else { return nil }
        threadID = busiest.id
        self.utilization = utilization
    }
}

private struct ObservedFrameIssue {
    let kind: FrameIssueKind
    let uptime: TimeInterval
    let source: FramePresentationSource
    let evidence: [String]
}

private enum FramePresentationSource: Equatable {
    case screen
    case sharedDisplay
    case sharedWindow

    var title: String {
        switch self {
        case .screen: return "当前屏幕"
        case .sharedDisplay: return "所选屏幕"
        case .sharedWindow: return "所选窗口"
        }
    }

    var logValue: String {
        switch self {
        case .screen: return "screen"
        case .sharedDisplay: return "captured_display"
        case .sharedWindow: return "captured_window"
        }
    }
}

private struct FrameTiming {
    let source: FramePresentationSource
    let fps: Double?
    let medianIntervalMS: Double?
    let p95IntervalMS: Double?
    let maxIntervalMS: Double?
    let sampleCount: Int
    let stutterCount: Int?
    let recentStutterCount: Int?
}

private struct RecentDisplayGap: Identifiable {
    let gap: DisplayGap
    let capture: String
    var id: UInt64 { gap.endNS }
}

@MainActor
private final class ViewerModel: ObservableObject {
    @Published var isPaused = false
    @Published private(set) var hardware: HardwareDetails?
    @Published private(set) var sample: PerformanceSample?
    @Published private(set) var windowServer: WindowServerReading?
    @Published private(set) var history: [HistoryPoint] = []
    @Published private(set) var updatedAt: Date?
    @Published private var screenReading: ScreenFrameReading?
    @Published private(set) var recentFrameIssue: ObservedFrameIssue?
    @Published private(set) var processLogURL: URL?
    @Published private(set) var savedProcessLogURL: URL?
    @Published private(set) var processLogError: String?
    @Published private(set) var isChangingProcessLog = false
    @Published private(set) var recentDisplayGaps: [RecentDisplayGap] = []
    @Published private(set) var displayHandoffAvailable = false
    @Published var targetFrameRate = 0 {
        didSet { recentFrameIssue = nil }
    }
    let contentFrames = ContentFrameMonitor()
    private let sampler = Sampler()
    private let desktopFrames = DriverFrameCounter()
    private let screenFrames = ScreenFrameMonitor()
    private let handoffMonitor = DisplayHandoffMonitor()
    private let processLog = ProcessUsageLog()
    private var logSessionID: UUID?
    private var logWrite: Task<Void, Never>?
    private var gpuRecording: Task<Void, Never>?
    private var lastLoggedCapture: String?
    private var boundDisplayID: UInt32?
    private var displayMode: CGDisplayMode?
    private var previousStutterCount: (source: FramePresentationSource, count: Int)?

    var frameTiming: FrameTiming {
        frameTiming(for: contentFrames.reading)
    }

    private func frameTiming(for capture: ContentFrameReading) -> FrameTiming {
        if capture.state == .measuring, let source = capture.source {
            return FrameTiming(source: source == .window ? .sharedWindow : .sharedDisplay,
                               fps: capture.fps, medianIntervalMS: capture.medianIntervalMS,
                               p95IntervalMS: capture.p95IntervalMS, maxIntervalMS: capture.maxIntervalMS,
                               sampleCount: capture.sampleCount, stutterCount: capture.stutterCount,
                               recentStutterCount: capture.recentStutterCount)
        }
        return FrameTiming(source: .screen, fps: screenReading?.fps, medianIntervalMS: screenReading?.medianIntervalMS,
                           p95IntervalMS: screenReading?.p95IntervalMS, maxIntervalMS: screenReading?.maxIntervalMS,
                           sampleCount: screenReading?.sampleCount ?? 0, stutterCount: screenReading?.stutterCount,
                           recentStutterCount: screenReading?.recentStutterCount)
    }

    func bindScreen(_ screen: NSScreen?) {
        screenFrames.bindScreen(screen)
        let id = (screen?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
        displayMode = id.flatMap(CGDisplayCopyDisplayMode)
        guard id != boundDisplayID else { return }
        boundDisplayID = id
        handoffMonitor.setEnabled(false, onGap: { _ in })
        recentDisplayGaps.removeAll()
        refreshHandoffMonitor()
    }

    var isRecordingProcesses: Bool { processLogURL != nil }

    func toggleProcessLog() {
        guard !isChangingProcessLog else { return }
        if isRecordingProcesses {
            stopProcessLog()
            return
        }

        isChangingProcessLog = true
        let panel = NSSavePanel()
        panel.title = "记录进程占用和上屏事件"
        panel.prompt = "开始记录"
        panel.allowedContentTypes = [.commaSeparatedText]
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd_HHmmss"
        panel.nameFieldStringValue = "性能日志-\(formatter.string(from: Date())).csv"

        let complete: (NSApplication.ModalResponse) -> Void = { [weak self] response in
            guard let self else { return }
            if response == .OK, let url = panel.url {
                self.startProcessLog(to: url)
            } else {
                self.isChangingProcessLog = false
            }
        }
        if let window = NSApp.keyWindow ?? NSApp.mainWindow {
            panel.beginSheetModal(for: window, completionHandler: complete)
        } else {
            panel.begin(completionHandler: complete)
        }
    }

    func stopProcessLog() {
        guard let url = processLogURL, !isChangingProcessLog else { return }
        isChangingProcessLog = true
        logSessionID = nil
        let gpuPending = gpuRecording
        refreshGPURecording()
        let pending = logWrite
        logWrite = nil
        Task {
            await gpuPending?.value
            await pending?.value
            do {
                try await processLog.stop()
                savedProcessLogURL = url
            } catch {
                processLogError = "关闭进程日志时出错：\(error.localizedDescription)"
            }
            processLogURL = nil
            isChangingProcessLog = false
        }
    }

    private func startProcessLog(to url: URL) {
        processLogError = nil
        Task {
            defer { isChangingProcessLog = false }
            do {
                let id = try await processLog.start(to: url, onFailure: { [weak self] error in
                    guard let self, self.processLogURL == url else { return }
                    self.logSessionID = nil
                    self.refreshGPURecording()
                    self.processLogURL = nil
                    self.savedProcessLogURL = url
                    self.processLogError = error
                }, context: { [weak self] in
                    self?.processLogContext()
                })
                logSessionID = id
                processLogURL = url
                lastLoggedCapture = nil
                refreshGPURecording()
                updateCaptureLog()
            } catch {
                processLogError = "无法记录进程：\(error.localizedDescription)"
            }
        }
    }

    private func refreshGPURecording() {
        guard !isPaused, let sessionID = logSessionID else {
            gpuRecording?.cancel()
            gpuRecording = nil
            return
        }
        guard gpuRecording == nil else { return }

        gpuRecording = Task(priority: .utility) { [sampler, processLog] in
            await sampler.recordGPU(in: processLog, sessionID: sessionID)
        }
    }

    private var captureLogValue: String {
        switch contentFrames.activeCaptureSource {
        case .window: return "window"
        case .display: return "display"
        case nil: return "off"
        }
    }

    private func processLogContext(_ capture: ContentFrameReading? = nil) -> ProcessLogContext {
        let counters = processLogWindowServerCounters()
        let valid = !isPaused && updatedAt.map { Date().timeIntervalSince($0) < 3 } == true
        let current = valid ? sample : nil
        let timing = capture.map { frameTiming(for: $0) } ?? frameTiming
        return ProcessLogContext(windowServer: counters, metricsAt: valid ? updatedAt : nil,
                                 windowServerCPUPercentage: valid ? windowServer?.cpuPercentage : nil,
                                 gpuPercentage: current?.gpuUtilization.map { $0 * 100 },
                                 gpuVRAMUsed: current?.gpuMemoryUsedBytes, gpuVRAMTotal: current?.gpuMemoryTotalBytes,
                                 windowServerSharedVRAM: current?.windowServerVideoMemory?.referencedSharedBytes,
                                 displayID: boundDisplayID, displayPixelWidth: displayMode?.pixelWidth,
                                 displayPixelHeight: displayMode?.pixelHeight,
                                 displayRefreshHz: displayMode.flatMap { $0.refreshRate > 0 ? $0.refreshRate : nil },
                                 viewerCapture: captureLogValue, frameSource: timing.source.logValue,
                                 screenFPS: isPaused ? nil : timing.fps, screenP95MS: isPaused ? nil : timing.p95IntervalMS,
                                 screenMaxMS: isPaused ? nil : timing.maxIntervalMS)
    }

    func updateCaptureLog(_ reading: ContentFrameReading? = nil) {
        let capture = captureLogValue
        guard let id = logSessionID, capture != lastLoggedCapture else { return }
        lastLoggedCapture = capture
        let context = processLogContext(reading)
        let date = Date()
        let prior = logWrite
        logWrite = Task { [processLog] in
            await prior?.value
            await processLog.appendCaptureChange(at: date, context: context, sessionID: id)
        }
    }

    private func refreshHandoffMonitor() {
        let enabled = !isPaused && boundDisplayID != nil && desktopFrames.activeDisplayID == boundDisplayID
        handoffMonitor.setEnabled(enabled) { [weak self] gap in self?.recordDisplayGap(gap) }
        if displayHandoffAvailable != handoffMonitor.isAvailable {
            displayHandoffAvailable = handoffMonitor.isAvailable
        }
    }

    private func recordDisplayGap(_ gap: DisplayGap) {
        let context = processLogContext()
        recentDisplayGaps = [RecentDisplayGap(gap: gap, capture: context.viewerCapture)] + recentDisplayGaps.prefix(2)
        guard let id = logSessionID else { return }
        let attachGPU = !isPaused
        let prior = logWrite
        logWrite = Task { [sampler, processLog] in
            await prior?.value
            let gpuSample = attachGPU ? await sampler.gpuSample(atOrBefore: gap.endNS, sessionID: id) : nil
            await processLog.appendDisplayGap(gap, context: context, gpuSample: gpuSample, sessionID: id)
        }
    }

    private func processLogWindowServerCounters() -> ProcessLogWindowServer? {
        guard !isPaused, let at = history.last?.uptime, ProcessInfo.processInfo.systemUptime - at < 3,
              let windowServer, let pid = windowServer.pid else { return nil }
        return ProcessLogWindowServer(
            pid: pid, diskReadBytesPerSecond: windowServer.diskReadBytesPerSecond,
            diskWriteBytesPerSecond: windowServer.diskWriteBytesPerSecond,
            gpuPercentage: sample?.windowServerGPUUtilization.map { $0 * 100 },
            vramBytes: sample?.windowServerVideoMemory?.attributedBytes
        )
    }

    func collect() async {
        refreshGPURecording()
        contentFrames.setPaused(isPaused)
        screenFrames.setPaused(isPaused || contentFrames.reading.state == .measuring)
        desktopFrames.reset()
        refreshHandoffMonitor()
        guard !isPaused else {
            return
        }
        if hardware == nil { hardware = await sampler.hardware() }
        guard !Task.isCancelled else { return }

        // A warm-up on resume keeps the next CPU reading scoped to a fresh interval.
        await sampler.resetWindowServer()
        let baseline = await sampler.read()
        guard !Task.isCancelled else { return }
        if sample == nil {
            record(baseline)
        } else {
            _ = desktopFrames.read(at: ProcessInfo.processInfo.systemUptime)
        }

        while !Task.isCancelled {
            do {
                try await Task.sleep(nanoseconds: refreshInterval)
            } catch {
                return
            }
            let next = await sampler.read()
            guard !Task.isCancelled else { return }
            record(next)
        }
    }

    private func record(_ reading: ViewerSample) {
        let next = reading.performance
        let now = ProcessInfo.processInfo.systemUptime
        screenFrames.setPaused(contentFrames.reading.state == .measuring)
        screenReading = screenFrames.sample()
        _ = desktopFrames.read(at: now)
        refreshHandoffMonitor()
        let point = HistoryPoint(
            uptime: now, cpu: next.cpuUtilization, gpu: next.gpuUtilization, memory: memoryFraction(next),
            peakThread: ThreadPeak(cores: next.cores)
        )
        recordFrameIssue(point, sample: next, windowServerCPU: reading.windowServer.cpuPercentage)
        sample = next
        windowServer = reading.windowServer
        history = history.filter { now - $0.uptime <= historyDuration } + [point]
        updatedAt = Date()
    }

    private func recordFrameIssue(_ point: HistoryPoint, sample: PerformanceSample, windowServerCPU: Double?) {
        let timing = frameTiming
        let driverSkips = timing.source == .sharedWindow ? 0 : desktopFrames.lateCount ?? 0
        let priorCount = previousStutterCount.flatMap { $0.source == timing.source ? $0.count : nil } ?? 0
        let observedStutter = timing.stutterCount.map { $0 > priorCount } ?? false
        if let count = timing.stutterCount { previousStutterCount = (timing.source, count) }
        let kind: FrameIssueKind?
        if driverSkips > 0 {
            kind = .displayPipelineBusy
        } else {
            kind = FrameDiagnostics.timing(fps: timing.fps, frameCount: timing.sampleCount,
                                           observedStutter: observedStutter, targetFPS: targetFrameRate)
        }

        if let kind {
            var evidence: [String] = []
            if kind == .displayPipelineBusy {
                evidence.append("上一帧尚未完成，跳过 \(driverSkips) 次更新")
            }
            if let peak = point.peakThread, peak.utilization >= 0.95, let load = percentage(peak.utilization) {
                evidence.append("\(threadName(peak.threadID)) \(load)，接近满载")
            }
            if let gpu = sample.gpuUtilization, gpu >= 0.95, let load = percentage(gpu) {
                evidence.append("GPU \(load)，接近满载")
            }
            if let vram = gpuMemoryFraction(sample), vram >= 0.95, let load = percentage(vram) {
                evidence.append("显存 \(load)")
            }
            if let windowServerCPU, windowServerCPU >= 80 {
                evidence.append("WindowServer CPU \(frameNumber(windowServerCPU))%")
            }
            if let load = sample.windowServerGPUUtilization, load >= 0.85, let formatted = percentage(load) {
                evidence.append("WindowServer GPU 采样 \(formatted)")
            }
            recentFrameIssue = ObservedFrameIssue(kind: kind, uptime: point.uptime, source: timing.source, evidence: evidence)
        } else if let issue = recentFrameIssue, issue.source != timing.source || point.uptime - issue.uptime > 10 {
            recentFrameIssue = nil
        }
    }
}

private enum Palette {
    static let background = Color(red: 0.052, green: 0.068, blue: 0.096)
    static let surface = Color(red: 0.081, green: 0.101, blue: 0.136)
    static let inset = Color(red: 0.105, green: 0.126, blue: 0.167)
    static let border = Color.white.opacity(0.09)
    static let primary = Color(red: 0.93, green: 0.95, blue: 0.98)
    static let secondary = Color(red: 0.66, green: 0.71, blue: 0.79)
    static let muted = Color(red: 0.53, green: 0.58, blue: 0.67)
    static let cpu = Color(red: 0.35, green: 0.88, blue: 0.75)
    static let gpu = Color(red: 0.75, green: 0.66, blue: 1.0)
    static let memory = Color(red: 0.46, green: 0.71, blue: 1.0)
    static let amber = Color(red: 1.0, green: 0.75, blue: 0.42)
}

private struct Dashboard: View {
    @ObservedObject var model: ViewerModel
    @ObservedObject var contentFrames: ContentFrameMonitor
    @Binding var floatsAboveWindows: Bool

    private var sample: PerformanceSample? { model.sample }
    private var hardware: HardwareDetails? { model.hardware }
    private var peakThread: ThreadPeak? { model.history.last?.peakThread }
    private var sampledCoreClock: FrequencyReading? {
        sample?.cpuFrequencies.first { $0.name == "采样核心" }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
                .padding(.horizontal, 26)
                .padding(.top, 37)
                .padding(.bottom, 17)

            ScrollView {
                VStack(alignment: .leading, spacing: 15) {
                    overview
                    framePanel
                    windowServerPanel
                    corePanel
                }
                .padding(.horizontal, 26)
                .padding(.top, 2)
                .padding(.bottom, 17)
            }

            footer
        }
        .foregroundStyle(Palette.primary)
        .background(Palette.background.ignoresSafeArea())
        .ignoresSafeArea(edges: .top)
    }

    private var header: some View {
        HStack(spacing: 13) {
            Image(systemName: "waveform.path.ecg")
                .font(.system(size: 21, weight: .semibold))
                .foregroundStyle(Palette.cpu)
                .frame(width: 42, height: 42)
                .background(Palette.cpu.opacity(0.11), in: RoundedRectangle(cornerRadius: 12))
                .overlay {
                    RoundedRectangle(cornerRadius: 12).strokeBorder(Palette.cpu.opacity(0.17))
                }
                .accessibilityHidden(true)

            Text(appTitle)
                .font(.system(size: 27, weight: .semibold))
                .accessibilityAddTraits(.isHeader)

            Spacer()

            HStack(spacing: 7) {
                Circle()
                    .fill(model.isPaused ? Palette.amber : Palette.cpu)
                    .frame(width: 6, height: 6)
                    .accessibilityHidden(true)
                Text(model.isPaused ? "已暂停" : "实时 · 每秒刷新")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Palette.secondary)
            }
            .padding(.trailing, 4)

            Button {
                model.toggleProcessLog()
            } label: {
                Label(model.isRecordingProcesses ? "停止记录" : "开始记录",
                      systemImage: model.isRecordingProcesses ? "stop.circle.fill" : "record.circle")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 88, height: 30)
                    .contentShape(Rectangle())
            }
            .buttonStyle(ControlButtonStyle(isSelected: model.isRecordingProcesses))
            .disabled(model.isChangingProcessLog)
            .help(model.isRecordingProcesses ? "停止写入进程日志" : "将活跃进程的占用记录到本地 CSV")

            Button {
                floatsAboveWindows.toggle()
            } label: {
                Label(floatsAboveWindows ? "已置顶" : "置顶", systemImage: floatsAboveWindows ? "pin.fill" : "pin")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 78, height: 30)
                    .contentShape(Rectangle())
            }
            .buttonStyle(ControlButtonStyle(isSelected: floatsAboveWindows))
            .help(floatsAboveWindows ? "取消窗口置顶" : "让查看器保持在普通窗口上方")

            Button {
                model.isPaused.toggle()
            } label: {
                Label(model.isPaused ? "继续" : "暂停", systemImage: model.isPaused ? "play.fill" : "pause.fill")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 64, height: 30)
                    .contentShape(Rectangle())
            }
            .buttonStyle(ControlButtonStyle())
            .help(model.isPaused ? "继续采样" : "暂停采样")
        }
    }

    private var overview: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("资源概览")
                    .font(.system(size: 13, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Label("最近 60 秒", systemImage: "clock")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Palette.muted)
            }

            HStack(spacing: 12) {
                ResourceCard(
                    name: "CPU", detail: hardware?.cpuName ?? "处理器",
                    symbol: "cpu", tint: Palette.cpu, value: sample?.cpuUtilization,
                    history: model.history, metric: \.cpu, valueLabel: "总负载",
                    metrics: cpuMetrics + [peakThreadMetric],
                    overlaySeries: .init(metric: \.peakThreadUtilization, tint: Palette.amber)
                )
                ResourceCard(
                    name: "GPU", detail: hardware?.gpuName ?? "图形处理器",
                    symbol: "display", tint: Palette.gpu, value: sample?.gpuUtilization,
                    history: model.history, metric: \.gpu, valueLabel: "负载",
                    metrics: [
                        .init(name: "显存占用", value: gpuMemoryDisplay),
                        .init(name: "实时频率", value: frequency(sample?.gpuFrequencyMHz)),
                        .init(name: "核心温度", value: temperature(sample?.gpuTemperatureCelsius))
                    ]
                )
                ResourceCard(
                    name: "内存", detail: byteString(sample?.memoryTotalBytes).map { "\($0) 物理内存" } ?? "物理内存",
                    symbol: "memorychip", tint: Palette.memory, value: memoryFraction(sample),
                    history: model.history, metric: \.memory, valueLabel: "使用率",
                    metrics: [
                        .init(name: "已用", value: byteString(sample?.memoryUsedBytes)),
                        .init(name: "可用", value: byteString(memoryAvailable(sample)))
                    ]
                )
            }
        }
    }

    private var framePanel: some View {
        let capture = contentFrames.reading
        let timing = model.frameTiming
        let isConnecting = capture.state == .choosing || capture.state == .starting

        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Text("帧率与卡顿")
                    .font(.system(size: 15, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Text(timing.source.title)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Palette.memory)
                Spacer()
                if #available(macOS 14.0, *) {
                    if capture.state == .measuring || isConnecting {
                        Button(isConnecting ? "取消选择" : "返回当前屏幕", action: contentFrames.stop)
                            .buttonStyle(.plain)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(Palette.secondary)
                    }
                    Menu {
                        Button("选择屏幕…") { contentFrames.chooseSource(.display) }
                        Button("选择窗口…") { contentFrames.chooseSource(.window) }
                    } label: {
                        Text("测量来源")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    .disabled(model.isPaused || isConnecting)
                    .help("使用 macOS 系统选择器测量指定的屏幕或窗口")
                }
            }

            HStack(spacing: 14) {
                frameValue(timing.source == .sharedWindow ? "窗口帧率" : "上屏帧率", timing.fps, unit: "FPS")
                frameValue("典型帧间隔", timing.medianIntervalMS, unit: "ms")
                    .help("连续两次画面更新之间的时间")
                frameValue("P95 帧间隔", timing.p95IntervalMS, unit: "ms")
                    .help("95% 的已测帧间隔小于或等于此数值")
                frameValue("最大帧间隔", timing.maxIntervalMS, unit: "ms")
                panelReadout("卡顿次数", value: timing.stutterCount.map { String($0) }, unit: "次", missing: "—")
                    .frame(minWidth: 80, alignment: .leading)
                    .help("帧间隔达到 50 ms 且至少为近期典型帧间隔的两倍时开始计数。连续慢帧合计一次，长时间静止后重新建立帧节奏。")
                Spacer(minLength: 0)
                targetFrameRateMenu
            }

            frameIssueStatus(capture, timing: timing)
            displayHandoffRows
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .panelBackground(radius: 15)
    }

    private var displayHandoffRows: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("近期上屏空档")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Palette.secondary)
            if !model.displayHandoffAvailable {
                Text(model.isPaused ? "采样已暂停" : "当前屏幕没有可用的驱动翻转计数")
                    .font(.system(size: 11))
                    .foregroundStyle(Palette.muted)
            } else if model.recentDisplayGaps.isEmpty {
                Text("等待画面连续更新，记录约 33 ms 以上的空档")
                    .font(.system(size: 11))
                    .foregroundStyle(Palette.muted)
            } else {
                ForEach(model.recentDisplayGaps) { row in
                    let gap = row.gap
                    let first = gap.firstAcceptedMS.map { "首次受理约 +\(frameNumber($0)) ms" } ?? "区间内无新增受理"
                    let capture = row.capture == "off" ? "查看器捕获关闭" : "查看器捕获开启"
                    Text("\(gap.observedAt.formatted(date: .omitted, time: .standard)) · \(frameNumber(gap.durationMS)) ms · \(first) · \(capture)")
                        .font(.system(size: 11))
                        .monospacedDigit()
                        .foregroundStyle(Palette.secondary)
                }
            }
        }
        .help("根据驱动计数轮询估算空档。首次受理是空档里的计数变化，无法关联到某一张表面。")
    }

    private func frameValue(_ title: String, _ number: Double?, unit: String) -> some View {
        panelReadout(title, value: number.map(frameNumber), unit: unit, missing: "—")
            .frame(minWidth: 90, alignment: .leading)
    }

    private func panelReadout(_ title: String, value: String?, unit: String?, missing: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Palette.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(value ?? missing)
                    .font(.system(size: value == nil && missing != "—" ? 13 : 21, weight: .semibold))
                    .monospacedDigit()
                    .foregroundStyle(value == nil && missing != "—" ? Palette.muted : Palette.primary)
                if let unit {
                    Text(unit)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Palette.muted)
                }
            }
            .frame(minHeight: 25, alignment: .leading)
        }
        .accessibilityElement(children: .combine)
    }

    private var windowServerPanel: some View {
        let process = model.windowServer
        let cpu = process?.cpuPercentage.map(frameNumber)
        let gpu = sample?.windowServerGPUUtilization.map { frameNumber($0 * 100) }
        let memory = process?.physicalFootprintBytes ?? process?.residentMemoryBytes
        let video = sample?.windowServerVideoMemory

        return VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("WindowServer")
                    .font(.system(size: 15, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                if let pid = process?.pid {
                    Text("PID \(pid)")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Palette.secondary)
                }
                Spacer()
                if let referenced = video?.referencedSharedBytes, referenced > 0 {
                    let display = byteReadout(Double(referenced))
                    Text("引用共享显存 \(display.value) \(display.unit)")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Palette.secondary)
                }
            }

            HStack(spacing: 12) {
                panelReadout("CPU", value: cpu, unit: cpu == nil ? nil : "%", missing: "未提供")
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .help("WindowServer 进程 CPU 占用。100% 相当于占满一个逻辑处理器。")
                panelReadout("GPU（采样）", value: gpu, unit: gpu == nil ? nil : "%", missing: "未提供")
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .help("根据 NVIDIA 驱动按 WindowServer PID 标记的 3D 引擎样本估算。")
                windowServerByteReadout(process?.physicalFootprintBytes != nil ? "内存占用" : "常驻内存",
                                        bytes: memory.map { Double($0) })
                windowServerByteReadout("归属显存", bytes: video?.attributedBytes.map { Double($0) })
                    .help("由驱动归属于 WindowServer 的显存，包含私有显存和由它持有的共享分配。")
                windowServerByteReadout("磁盘读取", bytes: process?.diskReadBytesPerSecond, rate: true)
                windowServerByteReadout("磁盘写入", bytes: process?.diskWriteBytesPerSecond, rate: true)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .panelBackground(radius: 15)
    }

    private func windowServerByteReadout(_ title: String, bytes: Double?, rate: Bool = false) -> some View {
        let display = bytes.map { byteReadout($0, rate: rate) }
        return panelReadout(title, value: display?.value, unit: display?.unit, missing: "未提供")
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var targetFrameRateMenu: some View {
        Menu {
            Picker("目标帧率", selection: $model.targetFrameRate) {
                Text("自动检测").tag(0)
                ForEach([24, 30, 45, 60, 90, 120, 144, 160, 165, 240], id: \.self) { rate in
                    Text("\(rate) FPS").tag(rate)
                }
            }
        } label: {
            Text(model.targetFrameRate == 0 ? "目标 · 自动" : "目标 · \(model.targetFrameRate) FPS")
                .font(.system(size: 11, weight: .medium))
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("自动识别帧间隔波动，也可选择目标帧率")
    }

    private func frameIssueStatus(_ capture: ContentFrameReading, timing: FrameTiming) -> some View {
        let issue = model.recentFrameIssue.flatMap { $0.source == timing.source ? $0 : nil }
        let text: String
        let shouldHighlight: Bool
        switch capture.state {
        case .choosing:
            text = "请在 macOS 中选择要测量的屏幕或窗口。"
            shouldHighlight = false
        case .starting:
            text = "正在连接并等待画面更新…"
            shouldHighlight = false
        case .failed(let message):
            text = message
            shouldHighlight = true
        default:
            if let issue {
                let evidence = issue.evidence.isEmpty ? "原因待确认" : "同期 " + issue.evidence.joined(separator: " · ")
                text = "近 10 秒 · \(issue.kind.title) · \(evidence)"
                shouldHighlight = true
            } else if let count = timing.recentStutterCount {
                text = "近 60 秒 \(count) 次卡顿"
                shouldHighlight = false
            } else {
                text = "等待可靠的画面帧时间。"
                shouldHighlight = false
            }
        }
        return Text(text)
            .font(.system(size: 11, weight: shouldHighlight ? .medium : .regular))
            .foregroundStyle(shouldHighlight ? Palette.amber : Palette.secondary)
            .lineLimit(2)
            .help("慢帧线索来自同一采样时段的系统负载")
    }

    private var gpuMemoryDisplay: String? {
        let total = sample?.gpuMemoryTotalBytes
        guard let used = sample?.gpuMemoryUsedBytes else {
            return byteString(total).map { "总计 \($0)" }
        }
        guard let total, total > 0 else { return byteString(used) }
        let usage = String(format: "%.1f / %.1f GiB", Double(used) / bytesPerGibibyte, Double(total) / bytesPerGibibyte)
        let percent = percentage(gpuMemoryFraction(sample))
        return percent.map { "\(usage) · \($0)" } ?? usage
    }

    private var peakThreadMetric: MetricLine {
        let value = peakThread.flatMap { peak in
            percentage(peak.utilization).map { "\(threadName(peak.threadID)) · \($0)" }
        }
        return MetricLine(name: "最高单线程负载", value: value, tint: Palette.amber)
    }

    private var cpuMetrics: [MetricLine] {
        let nominal = MetricLine(name: "标称频率", value: frequency(hardware?.cpuNominalFrequencyMHz))
        if let sampledCoreClock {
            return [.init(name: "采样核心频率", value: frequency(sampledCoreClock.megahertz)), nominal]
        }

        let frequencies = sample?.cpuFrequencies.filter { $0.megahertz != nil } ?? []
        let liveMetrics = frequencies.map {
            MetricLine(name: $0.name == "CPU" ? "实时频率" : "\($0.name)频率", value: frequency($0.megahertz))
        }
        if liveMetrics.isEmpty {
            return [.init(name: "实时频率", value: nil), nominal]
        }
        return liveMetrics.count == 1 ? liveMetrics + [nominal] : liveMetrics
    }

    private var corePanel: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .firstTextBaseline) {
                Text("CPU 逻辑线程负载")
                    .font(.system(size: 15, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 12)
                if let hardware {
                    Text("\(hardware.physicalCPUCount) 个物理核心 · \(hardware.logicalCPUCount) 个逻辑线程")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Palette.secondary)
                }
            }

            if let cores = sample?.cores, !cores.isEmpty {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 9), count: 4), spacing: 9) {
                    ForEach(cores) { core in
                        CoreTile(core: core, isPeak: peakThread?.threadID == core.id && (core.utilization ?? 0) > 0)
                    }
                }
            } else {
                Text("正在读取核心数据…")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.secondary)
                    .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .panelBackground(radius: 15)
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Image(systemName: "info.circle")
                .accessibilityHidden(true)
            Text(sampledCoreClock == nil ? "频率读数取决于系统和驱动。" : "CPU 频率每次采自一个逻辑核心。")
            Spacer()
            if let message = model.processLogError {
                Text(message)
                    .lineLimit(1)
                    .foregroundStyle(Palette.amber)
                    .help(message)
            } else if let recording = model.processLogURL {
                Text("记录中 · \(recording.lastPathComponent)")
                    .lineLimit(1)
                    .foregroundStyle(Palette.cpu)
                    .help(recording.path)
            } else if let saved = model.savedProcessLogURL {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([saved])
                } label: {
                    Text("已保存 · \(saved.lastPathComponent)")
                        .lineLimit(1)
                        .foregroundStyle(Palette.memory)
                }
                .buttonStyle(.plain)
                .help("在访达中显示日志")
            }
            if let updatedAt = model.updatedAt {
                Text("更新于 \(updatedAt.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits).second(.twoDigits)))")
                    .monospacedDigit()
            }
        }
        .font(.system(size: 11, weight: .medium))
        .foregroundStyle(Palette.muted)
        .padding(.horizontal, 26)
        .frame(height: 35)
        .overlay(alignment: .top) { Rectangle().fill(Palette.border).frame(height: 1) }
    }
}

private struct ControlButtonStyle: ButtonStyle {
    var isSelected = false

    func makeBody(configuration: Configuration) -> some View {
        let fill = isSelected ? Palette.cpu.opacity(configuration.isPressed ? 0.22 : 0.12) : Palette.inset
        return configuration.label
            .foregroundStyle(isSelected ? Palette.cpu : Palette.primary)
            .background(configuration.isPressed && !isSelected ? Palette.border : fill, in: RoundedRectangle(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8).strokeBorder(isSelected ? Palette.cpu.opacity(0.30) : Palette.border)
            }
    }
}

private struct MetricLine: Identifiable {
    var id: String { name }
    let name: String
    let value: String?
    var tint: Color? = nil
}

private struct HistorySeries {
    let metric: KeyPath<HistoryPoint, Double?>
    let tint: Color
}

private struct ResourceCard: View {
    let name: String
    let detail: String
    let symbol: String
    let tint: Color
    let value: Double?
    let history: [HistoryPoint]
    let metric: KeyPath<HistoryPoint, Double?>
    let valueLabel: String
    let metrics: [MetricLine]
    var overlaySeries: HistorySeries? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 9) {
                Image(systemName: symbol)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(tint)
                    .frame(width: 30, height: 30)
                    .background(tint.opacity(0.10), in: RoundedRectangle(cornerRadius: 8))
                    .accessibilityHidden(true)

                VStack(alignment: .leading, spacing: 2) {
                    Text(name)
                        .font(.system(size: 14, weight: .semibold))
                        .accessibilityAddTraits(.isHeader)
                    Text(detail)
                        .font(.system(size: 11))
                        .foregroundStyle(Palette.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .help(detail)
                }
                Spacer(minLength: 0)
            }

            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Text(percentage(value) ?? "—")
                    .font(.system(size: 39, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(value == nil ? Palette.muted : Palette.primary)
                Text(valueLabel)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Palette.secondary)
            }
            .frame(height: 49, alignment: .bottomLeading)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(Text(valueLabel))
            .accessibilityValue(Text(percentage(value) ?? "不可用"))

            HistoryChart(history: history, series: .init(metric: metric, tint: tint), overlaySeries: overlaySeries)
                .frame(height: 59)
                .padding(.top, 7)
                .padding(.bottom, 10)
                .accessibilityHidden(true)

            Rectangle().fill(Palette.border).frame(height: 1)

            VStack(spacing: 7) {
                ForEach(metrics) { metric in
                    HStack(spacing: 8) {
                        if let tint = metric.tint {
                            Capsule()
                                .fill(tint)
                                .frame(width: 9, height: 2)
                                .accessibilityHidden(true)
                        }
                        Text(metric.name)
                            .foregroundStyle(Palette.secondary)
                            .fixedSize(horizontal: true, vertical: false)
                        Spacer(minLength: 4)
                        Text(metric.value ?? "未提供")
                            .fontWeight(metric.value == nil ? .regular : .medium)
                            .monospacedDigit()
                            .foregroundStyle(metric.value == nil ? Palette.muted : metric.tint ?? Palette.primary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.82)
                    }
                    .font(.system(size: 11))
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(.top, 9)
            .frame(minHeight: 58, alignment: .top)
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .panelBackground(radius: 15)
        .accessibilityElement(children: .contain)
    }
}

private struct CoreTile: View {
    let core: CoreReading
    let isPeak: Bool

    private var title: String { threadName(core.id) }
    private var tint: Color { isPeak ? Palette.amber : Palette.cpu }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(title)
                    .font(.system(size: 11, weight: .semibold))
                    .monospacedDigit()
                if let kind = core.kind {
                    Text(kind)
                        .font(.system(size: 10))
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                Text(percentage(core.utilization) ?? "—")
                    .font(.system(size: 12, weight: .semibold))
                    .monospacedDigit()
                    .foregroundStyle(core.utilization == nil ? Palette.muted : tint)
            }

            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule().fill(Palette.border)
                    if let utilization = core.utilization, utilization > 0 {
                        Capsule()
                            .fill(tint.opacity(0.83))
                            .frame(width: max(3, geometry.size.width * min(utilization, 1)))
                    }
                }
            }
            .frame(height: 4)

            if let clock = frequency(core.frequencyMHz) {
                Text(clock)
                    .font(.system(size: 10, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(Palette.secondary)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 12)
        .background(Palette.inset, in: RoundedRectangle(cornerRadius: 8))
        .overlay {
            if isPeak { RoundedRectangle(cornerRadius: 8).strokeBorder(tint.opacity(0.40)) }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text([title, core.kind].compactMap { $0 }.joined(separator: "，")))
        .accessibilityValue(Text([
            percentage(core.utilization) ?? "负载未提供",
            isPeak ? "当前最高负载" : nil,
            frequency(core.frequencyMHz)
        ].compactMap { $0 }.joined(separator: "，")))
    }
}

private struct HistoryChart: View {
    let history: [HistoryPoint]
    let series: HistorySeries
    let overlaySeries: HistorySeries?

    var body: some View {
        Canvas { context, size in
            let bounds = CGRect(x: 2, y: 3, width: max(0, size.width - 4), height: max(0, size.height - 6))
            var grid = Path()
            for fraction in [0.0, 0.5, 1.0] {
                let y = bounds.minY + bounds.height * fraction
                grid.move(to: CGPoint(x: bounds.minX, y: y))
                grid.addLine(to: CGPoint(x: bounds.maxX, y: y))
            }
            context.stroke(grid, with: .color(Palette.border), style: StrokeStyle(lineWidth: 0.65, dash: [3, 4]))

            draw(series, in: context, bounds: bounds, filled: true)
            if let overlaySeries {
                draw(overlaySeries, in: context, bounds: bounds, filled: false)
            }
        }
    }

    private func draw(_ series: HistorySeries, in context: GraphicsContext, bounds: CGRect, filled: Bool) {
        guard let end = history.last?.uptime else { return }
        var runs: [[CGPoint]] = []
        var current: [CGPoint] = []
        var previousTime: TimeInterval?
        for point in history {
            let hasGap = previousTime.map { point.uptime - $0 > 2.5 } ?? false
            let value = point[keyPath: series.metric]
            if (hasGap || value == nil), !current.isEmpty {
                runs.append(current)
                current.removeAll(keepingCapacity: true)
            }
            previousTime = point.uptime
            guard let value else { continue }
            let x = bounds.maxX - bounds.width * (end - point.uptime) / historyDuration
            let y = bounds.maxY - bounds.height * min(max(value, 0), 1)
            current.append(CGPoint(x: x, y: y))
        }
        if !current.isEmpty { runs.append(current) }

        for run in runs {
            guard let first = run.first, let last = run.last else { continue }
            var stroke = Path()
            stroke.move(to: first)
            for point in run.dropFirst() { stroke.addLine(to: point) }

            if filled {
                var area = stroke
                area.addLine(to: CGPoint(x: last.x, y: bounds.maxY))
                area.addLine(to: CGPoint(x: first.x, y: bounds.maxY))
                area.closeSubpath()
                context.fill(area, with: .linearGradient(
                    Gradient(colors: [series.tint.opacity(0.22), series.tint.opacity(0.02)]),
                    startPoint: CGPoint(x: 0, y: bounds.minY), endPoint: CGPoint(x: 0, y: bounds.maxY)
                ))
            }
            context.stroke(stroke, with: .color(series.tint), style: StrokeStyle(
                lineWidth: 1.4, lineCap: .round, lineJoin: .round, dash: filled ? [] : [3, 3]
            ))
        }

        if let last = current.last {
            let dot = CGRect(x: last.x - 2, y: last.y - 2, width: 4, height: 4)
            context.fill(Path(ellipseIn: dot), with: .color(series.tint))
        }
    }
}

private extension View {
    func panelBackground(radius: CGFloat) -> some View {
        background(Palette.surface, in: RoundedRectangle(cornerRadius: radius))
            .overlay { RoundedRectangle(cornerRadius: radius).strokeBorder(Palette.border) }
    }
}

private func percentage(_ value: Double?) -> String? {
    guard let value, value.isFinite, (0...1).contains(value) else { return nil }
    return "\(Int((100 * value).rounded()))%"
}

private func frameNumber(_ value: Double) -> String {
    value.formatted(.number.precision(.fractionLength(0...1)))
}

private func byteReadout(_ bytes: Double, rate: Bool = false) -> (value: String, unit: String) {
    let units: [(scale: Double, suffix: String)] = [
        (bytesPerGibibyte, "GiB"), (1_048_576, "MiB"), (1_024, "KiB")
    ]
    let unit = units.first { bytes >= $0.scale } ?? (1, "B")
    return (frameNumber(bytes / unit.scale), rate ? "\(unit.suffix)/s" : unit.suffix)
}

private func threadName(_ id: Int) -> String {
    "线程 " + String(format: "%02d", id + 1)
}

private func frequency(_ megahertz: Double?) -> String? {
    guard let megahertz, megahertz.isFinite, megahertz > 0 else { return nil }
    if megahertz >= 1000 { return String(format: "%.2f GHz", megahertz / 1000) }
    return String(format: "%.0f MHz", megahertz)
}

private func temperature(_ celsius: Double?) -> String? {
    guard let celsius, celsius.isFinite else { return nil }
    return String(format: "%.0f °C", celsius)
}

private func byteString(_ bytes: UInt64?) -> String? {
    guard let bytes else { return nil }
    let gibibytes = Double(bytes) / bytesPerGibibyte
    return String(format: "%.1f GiB", gibibytes)
}

private func memoryFraction(_ sample: PerformanceSample?) -> Double? {
    guard let sample, let used = sample.memoryUsedBytes, sample.memoryTotalBytes > 0 else { return nil }
    return Double(min(used, sample.memoryTotalBytes)) / Double(sample.memoryTotalBytes)
}

private func gpuMemoryFraction(_ sample: PerformanceSample?) -> Double? {
    guard let sample, let used = sample.gpuMemoryUsedBytes, let total = sample.gpuMemoryTotalBytes,
          total > 0, used <= total else { return nil }
    return Double(used) / Double(total)
}

private func memoryAvailable(_ sample: PerformanceSample?) -> UInt64? {
    guard let sample, let used = sample.memoryUsedBytes else { return nil }
    return sample.memoryTotalBytes - min(used, sample.memoryTotalBytes)
}
