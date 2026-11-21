import Foundation

struct ProcessLogWindowServer: Sendable {
    let pid: Int32
    let diskReadBytesPerSecond: Double?
    let diskWriteBytesPerSecond: Double?
    let gpuPercentage: Double?
    let vramBytes: UInt64?
}

struct ProcessLogContext: Sendable {
    let windowServer: ProcessLogWindowServer?
    let metricsAt: Date?
    let windowServerCPUPercentage: Double?
    let gpuPercentage: Double?
    let gpuVRAMUsed: UInt64?
    let gpuVRAMTotal: UInt64?
    let windowServerSharedVRAM: UInt64?
    let displayID: UInt32?
    let displayPixelWidth: Int?
    let displayPixelHeight: Int?
    let displayRefreshHz: Double?
    let viewerCapture: String
    let frameSource: String
    let screenFPS: Double?
    let screenP95MS: Double?
    let screenMaxMS: Double?
}

struct ProcessLogGPUSample: Sendable {
    let uptimeNS: UInt64
    let wallTime: Date
    let percent: Double?
    let source: String?
    let sourceTimestamp: UInt64?
}

actor ProcessUsageLog {
    typealias FailureHandler = @MainActor @Sendable (String) -> Void
    typealias Context = @MainActor @Sendable () -> ProcessLogContext?

    private struct Session {
        let id: UUID
        let file: FileHandle
        let securityScopedURL: URL?
        let onFailure: FailureHandler?
        let context: Context?
        var baseline: Snapshot?
    }

    private struct Snapshot {
        let wallTime: Date
        let uptime: TimeInterval
        let processes: [Int32: ProcessReading]
    }

    private struct ProcessReading {
        let pid: Int32
        let start: String
        let name: String
        let cpuSeconds: TimeInterval
        let rssBytes: UInt64
        let state: Substring

        func usedCPU(since previous: ProcessReading) -> TimeInterval? {
            guard previous.start == start, previous.name == name, cpuSeconds >= previous.cpuSeconds else {
                return nil
            }
            return cpuSeconds - previous.cpuSeconds
        }
    }

    private enum LogError: LocalizedError {
        case alreadyRecording
        case invalidDestination
        case processSnapshotFailed

        var errorDescription: String? {
            switch self {
            case .alreadyRecording: "进程日志正在记录。"
            case .invalidDestination: "请选择本地 CSV 文件路径。"
            case .processSnapshotFailed: "读取本机进程统计失败。"
            }
        }
    }

    private static let processFields = [
        "sample_time_utc", "pid", "process_name", "process_start_utc", "process_state",
        "cpu_percent_one_logical_core", "rss_bytes",
        "windowserver_disk_read_bytes_per_second", "windowserver_disk_write_bytes_per_second",
        "windowserver_gpu_percent", "windowserver_vram_bytes"
    ]
    private static let contextFields = [
        "metrics_time_utc", "metrics_windowserver_cpu_percent_one_logical_core",
        "gpu_percent", "gpu_vram_used_bytes", "gpu_vram_total_bytes",
        "windowserver_referenced_shared_vram_bytes", "display_id", "display_pixel_width", "display_pixel_height",
        "display_refresh_hz", "viewer_capture", "frame_timing_source",
        "source_fps", "source_p95_frame_ms", "source_max_frame_ms"
    ]
    private static let counterNames = [
        "accepted", "successful_flips", "surface_marks", "surface_waits", "surface_timeouts",
        "async_drops", "async_refusals", "latch_timeouts", "late_swaps"
    ]
    private static let gapFields = [
        "gap_begin_utc", "gap_begin_uptime_ns", "gap_end_uptime_ns", "display_gap_ms",
        "first_accepted_after_gap_begin_ms", "longest_poll_gap_ms"
    ] + counterNames.map { "driver_" + $0 + "_before" }
      + counterNames.map { "driver_" + $0 + "_after" }
      + counterNames.map { "driver_" + $0 + "_delta" }
    private static let gpuFields = [
        "gpu_sample_uptime_ns", "gpu_sample_percent", "gpu_sample_age_ms", "gpu_sample_source",
        "gpu_sample_source_timestamp"
    ]
    private static let fields = processFields + ["record_type"] + contextFields + gapFields + gpuFields

    private var session: Session?
    private var recording: Task<Void, Never>?
    private let numberLocale = Locale(identifier: "en_US_POSIX")
    private let timestampFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    private let processStartParser: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "EEE MMM d HH:mm:ss yyyy"
        return formatter
    }()

    func start(
        to url: URL,
        onFailure: FailureHandler? = nil,
        context: Context? = nil
    ) throws -> UUID {
        guard session == nil else { throw LogError.alreadyRecording }
        guard url.isFileURL else { throw LogError.invalidDestination }

        let securityScopedURL = url.startAccessingSecurityScopedResource() ? url : nil
        let file: FileHandle
        do {
            let header = Self.fields.joined(separator: ",") + "\r\n"
            try Data(header.utf8).write(to: url, options: .atomic)
            file = try FileHandle(forWritingTo: url)
            _ = try file.seekToEnd()
        } catch {
            securityScopedURL?.stopAccessingSecurityScopedResource()
            throw error
        }

        let id = UUID()
        session = Session(
            id: id, file: file, securityScopedURL: securityScopedURL,
            onFailure: onFailure, context: context
        )
        recording = Task { [weak self] in
            while !Task.isCancelled {
                guard await self?.sample(sessionID: id) == true else { return }
                do {
                    try await Task.sleep(for: .seconds(2))
                } catch {
                    return
                }
            }
        }
        return id
    }

    func appendDisplayGap(_ gap: DisplayGap, context: ProcessLogContext,
                          gpuSample: ProcessLogGPUSample? = nil, sessionID: UUID) async {
        let fields = [
            timestampFormatter.string(from: gap.observedAt.addingTimeInterval(-gap.durationMS / 1_000)),
            String(gap.beginNS), String(gap.endNS), decimal(gap.durationMS),
            decimal(gap.firstAcceptedMS), decimal(gap.longestPollMS)
        ] + [gap.before.values, gap.after.values, gap.after.difference(from: gap.before)]
              .flatMap { $0.map { $0.map(String.init) ?? "" } }
        let gpuSample = gpuSample.flatMap { $0.uptimeNS <= gap.endNS ? $0 : nil }
        let gpuAgeMS = gpuSample.map { Double(gap.endNS - $0.uptimeNS) / 1_000_000 }
        await appendEvent("display_gap", at: gap.observedAt, context: context, fields: fields,
                          gpuSample: gpuSample, gpuAgeMS: gpuAgeMS, sessionID: sessionID)
    }

    func appendCaptureChange(at date: Date, context: ProcessLogContext, sessionID: UUID) async {
        await appendEvent("viewer_capture_state", at: date, context: context,
                          fields: Array(repeating: "", count: Self.gapFields.count), sessionID: sessionID)
    }

    func appendGPUSample(_ sample: ProcessLogGPUSample, sessionID: UUID) async {
        guard !Task.isCancelled else { return }
        await appendEvent("gpu_sample", at: sample.wallTime, context: nil,
                          fields: Array(repeating: "", count: Self.gapFields.count),
                          gpuSample: sample, sessionID: sessionID)
    }

    private func appendEvent(_ type: String, at date: Date, context: ProcessLogContext?,
                             fields: [String], gpuSample: ProcessLogGPUSample? = nil,
                             gpuAgeMS: Double? = nil, sessionID: UUID) async {
        guard let session, session.id == sessionID else { return }
        var row = Array(repeating: "", count: Self.processFields.count)
        row[0] = timestampFormatter.string(from: date)
        row[1] = context?.windowServer.map { String($0.pid) } ?? ""
        row[9] = decimal(context?.windowServer?.gpuPercentage)
        row[10] = context?.windowServer?.vramBytes.map(String.init) ?? ""
        row += [type] + contextValues(context) + fields + gpuValues(gpuSample, ageMS: gpuAgeMS)
        do {
            try session.file.write(contentsOf: Data((row.joined(separator: ",") + "\r\n").utf8))
        } catch {
            try? stop()
            await session.onFailure?(error.localizedDescription)
        }
    }

    func stop() throws {
        recording?.cancel()
        recording = nil
        guard let session else { return }
        self.session = nil
        defer { session.securityScopedURL?.stopAccessingSecurityScopedResource() }
        do {
            try session.file.synchronize()
        } catch {
            try? session.file.close()
            throw error
        }
        try session.file.close()
    }

    private func sample(sessionID: UUID) async -> Bool {
        guard let session, session.id == sessionID else { return false }
        do {
            let snapshot = try Self.readProcesses()
            if let baseline = session.baseline {
                let context = await session.context?()
                guard !Task.isCancelled, self.session?.id == sessionID else { return false }

                let lines = csvLines(snapshot: snapshot, since: baseline, context: context)
                if !lines.isEmpty {
                    try session.file.write(contentsOf: Data(lines.utf8))
                }
            }
            self.session?.baseline = snapshot
            return true
        } catch {
            try? stop()
            await session.onFailure?(error.localizedDescription)
            return false
        }
    }

    private static func readProcesses() throws -> Snapshot {
        let ps = Process()
        let pipe = Pipe()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps")
        ps.arguments = ["-A", "-c", "-ww", "-o", "pid=,lstart=,time=,rss=,stat=,comm="]
        ps.environment = ["LC_ALL": "C", "TZ": "UTC"]
        ps.standardOutput = pipe
        ps.standardError = FileHandle.nullDevice

        let startUptime = ProcessInfo.processInfo.systemUptime
        try ps.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        ps.waitUntilExit()
        let endUptime = ProcessInfo.processInfo.systemUptime
        guard ps.terminationStatus == 0 else { throw LogError.processSnapshotFailed }

        let output = String(decoding: data, as: UTF8.self)
        var readings: [Int32: ProcessReading] = [:]
        for line in output.split(whereSeparator: \.isNewline) {
            let fields = line.split(maxSplits: 9, whereSeparator: \.isWhitespace)
            guard fields.count == 10, let pid = Int32(fields[0]), pid != ps.processIdentifier,
                  let cpuSeconds = cpuSeconds(fields[6]),
                  let rssKiB = UInt64(fields[7]), rssKiB <= UInt64.max / 1024
            else { continue }

            let name = fields[9].trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else { continue }
            readings[pid] = ProcessReading(
                pid: pid, start: fields[1...5].joined(separator: " "), name: name,
                cpuSeconds: cpuSeconds, rssBytes: rssKiB * 1024, state: fields[8]
            )
        }
        guard !readings.isEmpty else { throw LogError.processSnapshotFailed }
        return Snapshot(wallTime: Date(), uptime: (startUptime + endUptime) / 2, processes: readings)
    }

    private static func cpuSeconds(_ value: Substring) -> TimeInterval? {
        let dayAndTime = value.split(separator: "-", maxSplits: 1)
        guard let time = dayAndTime.last else { return nil }
        let parts = time.split(separator: ":")
        guard (2...3).contains(parts.count) else { return nil }

        var seconds: TimeInterval = 0
        for part in parts {
            guard let value = Double(part), value.isFinite, value >= 0 else { return nil }
            seconds = 60 * seconds + value
        }
        if dayAndTime.count == 2 {
            guard let days = UInt64(dayAndTime[0]) else { return nil }
            seconds += Double(days) * 86_400
        }
        return seconds
    }

    private func csvLines(snapshot: Snapshot, since baseline: Snapshot, context: ProcessLogContext?) -> String {
        let elapsed = snapshot.uptime - baseline.uptime
        guard elapsed > 0 else { return "" }
        let timestamp = timestampFormatter.string(from: snapshot.wallTime)
        let contextColumns = contextValues(context)
        let noContext = Array(repeating: "", count: Self.contextFields.count)
        let noGapOrGPU = Array(repeating: "", count: Self.gapFields.count + Self.gpuFields.count)
        var lines = ""
        for process in snapshot.processes.values.sorted(by: { $0.pid < $1.pid }) {
            let previous = baseline.processes[process.pid]
            let cpuSeconds = previous.flatMap { process.usedCPU(since: $0) }
            let isWindowServer = process.name == "WindowServer"
            let activeSinceBaseline = cpuSeconds.map { $0 > 0 } ?? (process.cpuSeconds > 0)
            guard isWindowServer || activeSinceBaseline || process.state.first == "R" else { continue }

            let processStart = processStartParser.date(from: process.start)
                .map { timestampFormatter.string(from: $0) } ?? ""
            let windowServer = context?.windowServer
            let counters = isWindowServer && windowServer?.pid == process.pid ? windowServer : nil
            let firstCharacter = process.name.first
            let prefix = firstCharacter.map { "=+-@".contains($0) } == true ? "'" : ""
            let name = (prefix + process.name).replacingOccurrences(of: "\"", with: "\"\"")
            let fields = [
                timestamp, String(process.pid), "\"\(name)\"", processStart, String(process.state),
                decimal(cpuSeconds.map { $0 * 100 / elapsed }), String(process.rssBytes),
                decimal(counters?.diskReadBytesPerSecond), decimal(counters?.diskWriteBytesPerSecond),
                decimal(counters?.gpuPercentage), counters?.vramBytes.map { String($0) } ?? ""
            ] + ["process_sample"] + (isWindowServer ? contextColumns : noContext) + noGapOrGPU
            lines += fields.joined(separator: ",") + "\r\n"
        }
        return lines
    }

    private func contextValues(_ context: ProcessLogContext?) -> [String] {
        guard let context else { return Array(repeating: "", count: Self.contextFields.count) }
        return [
            context.metricsAt.map { timestampFormatter.string(from: $0) } ?? "",
            decimal(context.windowServerCPUPercentage), decimal(context.gpuPercentage),
            context.gpuVRAMUsed.map(String.init) ?? "",
            context.gpuVRAMTotal.map(String.init) ?? "", context.windowServerSharedVRAM.map(String.init) ?? "",
            context.displayID.map(String.init) ?? "", context.displayPixelWidth.map(String.init) ?? "",
            context.displayPixelHeight.map(String.init) ?? "", decimal(context.displayRefreshHz),
            context.viewerCapture, context.frameSource, decimal(context.screenFPS),
            decimal(context.screenP95MS), decimal(context.screenMaxMS)
        ]
    }

    private func gpuValues(_ sample: ProcessLogGPUSample?, ageMS: Double?) -> [String] {
        guard let sample else { return Array(repeating: "", count: Self.gpuFields.count) }
        return [String(sample.uptimeNS), decimal(sample.percent), decimal(ageMS),
                sample.source ?? "", sample.sourceTimestamp.map(String.init) ?? ""]
    }

    private func decimal(_ number: Double?) -> String {
        guard let number, number.isFinite, number >= 0 else { return "" }
        return String(format: "%.2f", locale: numberLocale, number)
    }
}
