import Darwin
import Foundation

struct WindowServerReading: Sendable {
    let pid: Int32?
    /// One saturated logical CPU is 100%. Work on multiple CPUs can exceed 100%.
    let cpuPercentage: Double?
    let residentMemoryBytes: UInt64?
    let physicalFootprintBytes: UInt64?
    let diskReadBytesPerSecond: Double?
    let diskWriteBytesPerSecond: Double?
}

/// Serial sampler for the compositor process. Keeps only one cumulative baseline.
final class WindowServerMonitor {
    private struct Identity: Equatable {
        let pid: pid_t
        let startedSeconds: Int
        let startedMicroseconds: Int32
    }

    private enum Source {
        case resourceUsage
        case ps
    }

    private struct Counters {
        let source: Source
        let cpuSeconds: Double
        let residentBytes: UInt64?
        let footprintBytes: UInt64?
        let diskReadBytes: UInt64?
        let diskWriteBytes: UInt64?
    }

    private struct Baseline {
        let process: Identity
        let counters: Counters
        let uptime: TimeInterval
    }

    private var trackedProcess: Identity?
    private var previous: Baseline?
    private var nextLookup: TimeInterval = 0

    func sample() -> WindowServerReading {
        let before = ProcessInfo.processInfo.systemUptime
        let process = locateWindowServer(at: before)
        let counters = process.flatMap { Self.nativeCounters(for: $0.pid) ?? Self.psCounters(for: $0.pid) }
        let after = ProcessInfo.processInfo.systemUptime
        guard let process, Self.identity(for: process.pid) == process else {
            reset()
            trackedProcess = nil
            return Self.reading()
        }
        guard let counters else {
            reset()
            return Self.reading(pid: process.pid)
        }

        let now = (before + after) / 2
        let baseline = previous
        previous = Baseline(process: process, counters: counters, uptime: now)
        guard let baseline, baseline.process == process, baseline.counters.source == counters.source else {
            return Self.reading(pid: process.pid, counters: counters)
        }
        let elapsed = now - baseline.uptime
        guard elapsed >= 0.1 else { return Self.reading(pid: process.pid, counters: counters) }

        let cpuDelta = counters.cpuSeconds - baseline.counters.cpuSeconds
        let cpuPercentage = cpuDelta >= 0 ? 100 * cpuDelta / elapsed : nil
        let read = Self.bytesPerSecond(counters.diskReadBytes, since: baseline.counters.diskReadBytes, over: elapsed)
        let write = Self.bytesPerSecond(counters.diskWriteBytes, since: baseline.counters.diskWriteBytes, over: elapsed)
        return Self.reading(pid: process.pid, counters: counters, cpu: cpuPercentage, read: read, write: write)
    }

    func reset() {
        previous = nil
    }

    private func locateWindowServer(at uptime: TimeInterval) -> Identity? {
        if let trackedProcess, Self.identity(for: trackedProcess.pid) == trackedProcess {
            return trackedProcess
        }
        trackedProcess = nil
        guard uptime >= nextLookup else { return nil }
        nextLookup = uptime + 2

        let uid = getpwnam("_windowserver")?.pointee.pw_uid
        if let uid, let process = Self.findProcess(in: UInt32(PROC_UID_ONLY), info: uid) {
            trackedProcess = process
        } else {
            trackedProcess = Self.findProcess(in: UInt32(PROC_ALL_PIDS), info: 0)
        }
        return trackedProcess
    }

    private static func findProcess(in kind: UInt32, info: UInt32) -> Identity? {
        let capacity = proc_listpids(kind, info, nil, 0) / Int32(MemoryLayout<pid_t>.stride)
        guard capacity > 0 else { return nil }
        var pids = [pid_t](repeating: 0, count: Int(capacity) + 32)
        let byteCount = pids.withUnsafeMutableBytes {
            proc_listpids(kind, info, $0.baseAddress, Int32($0.count))
        }
        guard byteCount > 0 else { return nil }
        for pid in pids.prefix(Int(byteCount) / MemoryLayout<pid_t>.stride) where pid > 0 {
            guard let before = identity(for: pid), isWindowServer(pid),
                  identity(for: pid) == before else { continue }
            return before
        }
        return nil
    }

    private static func isWindowServer(_ pid: pid_t) -> Bool {
        var path = [UInt8](repeating: 0, count: 4 * Int(MAXPATHLEN))
        let size = path.withUnsafeMutableBytes {
            proc_pidpath(pid, $0.baseAddress, UInt32($0.count))
        }
        guard size > 0 else { return false }
        let name = String(decoding: path.prefix(Int(size)).prefix { $0 != 0 }, as: UTF8.self)
        return name.hasPrefix("/System/Library/PrivateFrameworks/SkyLight.framework/")
            && name.hasSuffix("/Resources/WindowServer")
    }

    private static func identity(for pid: pid_t) -> Identity? {
        var query = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        var process = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.size
        let result = sysctl(&query, UInt32(query.count), &process, &size, nil, 0)
        guard result == 0, size == MemoryLayout<kinfo_proc>.size,
              process.kp_proc.p_pid == pid, process.kp_proc.p_stat != Int8(SZOMB) else { return nil }
        let started = process.kp_proc.p_un.__p_starttime
        guard started.tv_sec > 0 else { return nil }
        return Identity(pid: pid, startedSeconds: started.tv_sec, startedMicroseconds: started.tv_usec)
    }

    private static func nativeCounters(for pid: pid_t) -> Counters? {
        var usage = rusage_info_v2()
        let result = withUnsafeMutablePointer(to: &usage) { pointer in
            pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_V2, $0)
            }
        }
        guard result == 0, usage.ri_proc_exit_abstime == 0 else { return nil }
        return Counters(
            source: .resourceUsage,
            cpuSeconds: (Double(usage.ri_user_time) + Double(usage.ri_system_time)) / 1_000_000_000,
            residentBytes: usage.ri_resident_size,
            footprintBytes: usage.ri_phys_footprint,
            diskReadBytes: usage.ri_diskio_bytesread,
            diskWriteBytes: usage.ri_diskio_byteswritten
        )
    }

    private static func psCounters(for pid: pid_t) -> Counters? {
        // macOS's stock ps has read access across user IDs, which ordinary processes
        // lack. Its cumulative TIME and RSS provide a low-overhead fallback.
        guard let data = readPS(pid: pid) else { return nil }
        let fields = String(decoding: data, as: UTF8.self).split(whereSeparator: \.isWhitespace)
        guard fields.count == 2, let cpuSeconds = cpuTime(fields[0]),
              let residentKB = UInt64(fields[1]), residentKB > 0, residentKB <= UInt64.max / 1024 else { return nil }
        return Counters(source: .ps, cpuSeconds: cpuSeconds, residentBytes: residentKB * 1024,
                        footprintBytes: nil, diskReadBytes: nil, diskWriteBytes: nil)
    }

    private static func readPS(pid: pid_t) -> Data? {
        var descriptors = [Int32](repeating: -1, count: 2)
        guard pipe(&descriptors) == 0 else { return nil }
        let reader = FileHandle(fileDescriptor: descriptors[0], closeOnDealloc: true)
        let writer = FileHandle(fileDescriptor: descriptors[1], closeOnDealloc: true)
        guard fcntl(reader.fileDescriptor, F_SETFD, FD_CLOEXEC) == 0,
              fcntl(writer.fileDescriptor, F_SETFD, FD_CLOEXEC) == 0 else { return nil }

        var actions: posix_spawn_file_actions_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else { return nil }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawn_file_actions_adddup2(&actions, writer.fileDescriptor, STDOUT_FILENO) == 0,
              posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0) == 0
        else { return nil }

        let program = "/bin/ps"
        let parameters = [program, "-p", String(pid), "-o", "time=,rss="].map { value in
            value.withCString { strdup($0) }
        }
        let locale = strdup("LC_ALL=C")
        defer {
            parameters.forEach { free($0) }
            free(locale)
        }
        guard parameters.allSatisfy({ $0 != nil }), let locale else { return nil }
        var args = parameters + [nil]
        var environment: [UnsafeMutablePointer<CChar>?] = [locale, nil]
        var child: pid_t = 0
        guard posix_spawn(&child, program, &actions, nil, &args, &environment) == 0 else { return nil }

        try? writer.close()
        let output = try? reader.readToEnd()
        var status: Int32 = -1
        var waited: pid_t
        repeat { waited = waitpid(child, &status, 0) } while waited == -1 && errno == EINTR
        return waited == child && status == 0 ? output : nil
    }

    private static func cpuTime(_ value: Substring) -> Double? {
        let parts = value.split(separator: ":")
        guard parts.count == 2,
              let minutes = UInt64(parts[0]), let seconds = Double(parts[1]), (0..<60).contains(seconds) else { return nil }
        return 60 * Double(minutes) + seconds
    }

    private static func bytesPerSecond(_ bytes: UInt64?, since baseline: UInt64?, over elapsed: TimeInterval) -> Double? {
        guard let bytes, let baseline, bytes >= baseline else { return nil }
        return Double(bytes - baseline) / elapsed
    }

    private static func reading(pid: Int32? = nil, counters: Counters? = nil, cpu: Double? = nil,
                                read: Double? = nil, write: Double? = nil) -> WindowServerReading {
        WindowServerReading(pid: pid, cpuPercentage: cpu, residentMemoryBytes: counters?.residentBytes,
                            physicalFootprintBytes: counters?.footprintBytes,
                            diskReadBytesPerSecond: read, diskWriteBytesPerSecond: write)
    }
}
