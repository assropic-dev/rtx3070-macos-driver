import Darwin
import Foundation
import IOKit
import Metal

struct CoreReading: Identifiable {
    let id: Int
    let kind: String?
    let utilization: Double?
    let frequencyMHz: Double?
}

struct FrequencyReading: Identifiable {
    var id: String { name }
    let name: String
    let megahertz: Double?
}

struct PerformanceSample {
    let cpuUtilization: Double?
    let cores: [CoreReading]
    let cpuFrequencies: [FrequencyReading]
    let gpuUtilization: Double?
    let gpuFrequencyMHz: Double?
    let gpuTemperatureCelsius: Double?
    let gpuMemoryUsedBytes: UInt64?
    let gpuMemoryTotalBytes: UInt64?
    let windowServerGPUUtilization: Double?
    let windowServerVideoMemory: NVIDIAProcessVideoMemory?
    let memoryUsedBytes: UInt64?
    let memoryTotalBytes: UInt64
}

final class PerformanceMonitor {
    let cpuName: String
    let gpuName: String?
    let cpuNominalFrequencyMHz: Double?
    let logicalCPUCount: Int
    let physicalCPUCount: Int

    private let host: mach_port_t
    private let totalMemory = ProcessInfo.processInfo.physicalMemory
    private let pageSize = UInt64(getpagesize())
    private let gpu: GraphicsMonitor?
    private let amdClock: AMDClockMonitor?
    private var previousCPU: [CPUTicks]?
    private let silicon: SiliconMonitor?
    private let coreKinds: [String]

    init() {
        host = mach_host_self()
        logicalCPUCount = Int(sysctlInteger("hw.logicalcpu") ?? UInt64(ProcessInfo.processInfo.processorCount))
        physicalCPUCount = Int(sysctlInteger("hw.physicalcpu") ?? UInt64(logicalCPUCount))
        cpuName = sysctlString("machdep.cpu.brand_string") ?? "CPU"
        cpuNominalFrequencyMHz = sysctlInteger("hw.cpufrequency").flatMap {
            $0 > 0 ? Double($0) / 1_000_000 : nil
        }
        #if arch(x86_64)
        if sysctlString("machdep.cpu.vendor") == "AuthenticAMD",
           let referenceHz = sysctlInteger("machdep.tsc.frequency") ?? sysctlInteger("hw.cpufrequency") {
            amdClock = AMDClockMonitor(referenceHz: referenceHz)
        } else {
            amdClock = nil
        }
        #else
        amdClock = nil
        #endif
        gpu = GraphicsMonitor()
        gpuName = gpu?.name
        #if arch(arm64)
        coreKinds = Self.readCoreKinds()
        silicon = SiliconMonitor(coreKinds: coreKinds)
        #else
        silicon = nil
        coreKinds = []
        #endif
        previousCPU = readCPUTicks()
    }

    deinit {
        mach_port_deallocate(mach_task_self_, host)
    }

    func sample(windowServerPID: Int32? = nil) -> PerformanceSample {
        let chip = silicon?.sample()
        let gpuReading = gpu?.sample()
        let sampledAMDFrequency = amdClock?.sample()
        let currentCPU = readCPUTicks()
        defer { previousCPU = currentCPU }

        var totalBusy: UInt64 = 0
        var totalTicks: UInt64 = 0
        let count = currentCPU?.count ?? logicalCPUCount
        let cores = (0..<count).map { index in
            let kind = coreKinds.indices.contains(index) ? coreKinds[index] : nil
            let clock = chip?.coreMHz.indices.contains(index) == true ? chip?.coreMHz[index] : nil
            var utilization: Double?
            if let currentCPU, let previousCPU, currentCPU.count == previousCPU.count {
                let delta = currentCPU[index].delta(since: previousCPU[index])
                totalBusy += delta.busy
                totalTicks += delta.total
                if delta.total > 0 {
                    utilization = Double(delta.busy) / Double(delta.total)
                }
            }
            return CoreReading(id: index, kind: kind, utilization: utilization, frequencyMHz: clock)
        }
        let frequencyNames = coreKinds.reduce(into: [String]()) { names, kind in
            if !names.contains(kind) { names.append(kind) }
        }
        let frequencies: [FrequencyReading]
        if amdClock != nil {
            frequencies = [FrequencyReading(name: "采样核心", megahertz: sampledAMDFrequency)]
        } else {
            frequencies = (frequencyNames.isEmpty ? ["CPU"] : frequencyNames.reversed()).map { name in
                FrequencyReading(name: name, megahertz: chip?.clusterMHz[name])
            }
        }
        return PerformanceSample(
            cpuUtilization: totalTicks == 0 ? nil : Double(totalBusy) / Double(totalTicks),
            cores: cores,
            cpuFrequencies: frequencies,
            gpuUtilization: gpuReading?.utilization ?? chip?.gpuUtilization,
            gpuFrequencyMHz: gpuReading?.frequencyMHz ?? chip?.gpuMHz,
            gpuTemperatureCelsius: gpuReading?.temperatureCelsius,
            gpuMemoryUsedBytes: gpuReading?.memoryUsedBytes,
            gpuMemoryTotalBytes: gpu?.memoryTotalBytes,
            windowServerGPUUtilization: windowServerPID.flatMap { gpu?.gpuUtilization(for: $0) },
            windowServerVideoMemory: windowServerPID.flatMap { gpu?.videoMemoryUsage(for: $0) },
            memoryUsedBytes: readUsedMemory(),
            memoryTotalBytes: totalMemory
        )
    }

    func sampleGPUForLog() -> ProcessLogGPUSample {
        let reading = gpu?.logUtilization()
        return ProcessLogGPUSample(
            uptimeNS: DispatchTime.now().uptimeNanoseconds, wallTime: Date(),
            percent: reading?.percent, source: reading?.source, sourceTimestamp: reading?.timestamp
        )
    }

    private func readCPUTicks() -> [CPUTicks]? {
        var coreCount: natural_t = 0
        var valueCount: mach_msg_type_number_t = 0
        var values: processor_info_array_t?
        let result = host_processor_info(host, PROCESSOR_CPU_LOAD_INFO, &coreCount, &values, &valueCount)
        guard result == KERN_SUCCESS, let values else { return nil }
        defer {
            vm_deallocate(
                mach_task_self_,
                vm_address_t(UInt(bitPattern: values)),
                vm_size_t(valueCount) * vm_size_t(MemoryLayout<integer_t>.stride)
            )
        }
        guard coreCount > 0, Int(valueCount) >= Int(coreCount) * Int(CPU_STATE_MAX) else { return nil }
        return (0..<Int(coreCount)).map { core in
            let offset = core * Int(CPU_STATE_MAX)
            return CPUTicks(
                user: UInt32(bitPattern: values[offset + Int(CPU_STATE_USER)]),
                system: UInt32(bitPattern: values[offset + Int(CPU_STATE_SYSTEM)]),
                nice: UInt32(bitPattern: values[offset + Int(CPU_STATE_NICE)]),
                idle: UInt32(bitPattern: values[offset + Int(CPU_STATE_IDLE)])
            )
        }
    }

    private func readUsedMemory() -> UInt64? {
        var status = vm_statistics64()
        let fieldCount = MemoryLayout<vm_statistics64>.stride / MemoryLayout<integer_t>.stride
        var count = mach_msg_type_number_t(fieldCount)
        let result = withUnsafeMutablePointer(to: &status) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: fieldCount) {
                host_statistics64(host, HOST_VM_INFO64, $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return nil }
        let resident = UInt64(status.internal_page_count) + UInt64(status.wire_count)
            + UInt64(status.compressor_page_count)
        let purgeable = min(resident, UInt64(status.purgeable_count))
        return min(totalMemory, (resident - purgeable) * pageSize)
    }

    private static func readCoreKinds() -> [String] {
        guard let levels = sysctlInteger("hw.nperflevels"), levels > 1, levels <= 8 else { return [] }
        // On Apple Silicon, the Mach processor list starts with the lowest performance tier.
        return (0..<Int(levels)).reversed().flatMap { level -> [String] in
            let name = sysctlString("hw.perflevel\(level).name") ?? ""
            let nameMap = ["Efficiency": "能效核", "Performance": "性能核", "Super": "超大核"]
            guard let count = sysctlInteger("hw.perflevel\(level).logicalcpu") else { return [] }
            return Array(repeating: nameMap[name] ?? name, count: Int(count))
        }
    }
}

private struct CPUTicks {
    let user: UInt32
    let system: UInt32
    let nice: UInt32
    let idle: UInt32

    func delta(since previous: Self) -> (busy: UInt64, total: UInt64) {
        let busy = UInt64(user &- previous.user) + UInt64(system &- previous.system)
            + UInt64(nice &- previous.nice)
        return (busy, busy + UInt64(idle &- previous.idle))
    }
}

private func sysctlInteger(_ key: String) -> UInt64? {
    var value: UInt64 = 0
    var size = MemoryLayout<UInt64>.size
    guard sysctlbyname(key, &value, &size, nil, 0) == 0 else { return nil }
    if size == MemoryLayout<UInt32>.size {
        return UInt64(UInt32(truncatingIfNeeded: value))
    }
    return size == MemoryLayout<UInt64>.size ? value : nil
}

private func sysctlString(_ key: String) -> String? {
    var size = 0
    guard sysctlbyname(key, nil, &size, nil, 0) == 0, size > 1 else { return nil }
    var bytes = [UInt8](repeating: 0, count: size)
    let result = bytes.withUnsafeMutableBytes { sysctlbyname(key, $0.baseAddress, &size, nil, 0) }
    guard result == 0 else { return nil }
    return String(decoding: bytes.prefix { $0 != 0 }, as: UTF8.self)
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

private final class AMDClockMonitor {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private var pending = Data()
    private var mostRecent: (time: UInt64, megahertz: Double)?

    init?(referenceHz: UInt64) {
        guard let executable = Bundle.main.executableURL?.deletingLastPathComponent()
            .appendingPathComponent("amd-clock"),
            FileManager.default.isExecutableFile(atPath: executable.path) else { return nil }
        let readFD = output.fileHandleForReading.fileDescriptor
        let writeFD = input.fileHandleForWriting.fileDescriptor
        guard fcntl(readFD, F_SETFL, O_NONBLOCK) != -1,
              fcntl(writeFD, F_SETFL, O_NONBLOCK) != -1,
              fcntl(writeFD, F_SETNOSIGPIPE, 1) != -1 else { return nil }

        process.executableURL = executable
        process.arguments = [String(referenceHz)]
        process.standardInput = input
        process.standardOutput = output
        do {
            try process.run()
        } catch {
            return nil
        }
        requestReading()
    }

    deinit {
        try? input.fileHandleForWriting.close()
        try? output.fileHandleForReading.close()
    }

    func sample() -> Double? {
        guard process.isRunning else { return nil }
        let readFD = output.fileHandleForReading.fileDescriptor
        var buffer = [UInt8](repeating: 0, count: 256)
        while true {
            let length = buffer.withUnsafeMutableBytes { Darwin.read(readFD, $0.baseAddress, $0.count) }
            if length < 0 && errno == EINTR { continue }
            guard length > 0 else { break }
            pending.append(contentsOf: buffer.prefix(length))
        }
        if let end = pending.lastIndex(of: 10) {
            let lines = String(decoding: pending[...end], as: UTF8.self).split(separator: "\n")
            pending = Data(pending.dropFirst(end + 1))
            if let line = lines.last {
                let fields = line.split(separator: " ")
                if fields.count == 2,
                   let time = UInt64(fields[0]), let megahertz = Double(fields[1]),
                   (50...10_000).contains(megahertz) {
                    mostRecent = (time, megahertz)
                }
            }
        }
        requestReading()
        let now = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
        guard let mostRecent, now >= mostRecent.time, now - mostRecent.time < 2_500_000_000 else { return nil }
        return mostRecent.megahertz
    }

    private func requestReading() {
        var request: UInt8 = 1
        _ = Darwin.write(input.fileHandleForWriting.fileDescriptor, &request, 1)
    }
}

private final class GraphicsMonitor {
    struct Reading {
        let utilization: Double?
        let frequencyMHz: Double?
        let temperatureCelsius: Double?
        let memoryUsedBytes: UInt64?
    }

    let name: String?
    let memoryTotalBytes: UInt64?
    private let hasDedicatedMemory: Bool
    private let accelerator: io_service_t
    private let nvidiaClock: NVIDIAClockMonitor?

    init?() {
        let device = MTLCreateSystemDefaultDevice()
        accelerator = Self.findAccelerator(registryID: device?.registryID)
        guard device != nil || accelerator != 0 else { return nil }
        name = device?.name.components(separatedBy: " (NVMTL over ").first
        memoryTotalBytes = device?.hasUnifiedMemory == true ? nil : Self.dedicatedMemoryCapacity(for: accelerator)
        hasDedicatedMemory = device?.hasUnifiedMemory == false || memoryTotalBytes != nil
        nvidiaClock = name?.localizedCaseInsensitiveContains("NVIDIA") == true ? NVIDIAClockMonitor() : nil
    }

    deinit {
        if accelerator != 0 { IOObjectRelease(accelerator) }
    }

    func sample() -> Reading {
        let statistics = Self.statistics(for: accelerator)
        let utilization = Self.utilizationPercent(in: statistics)
        let frequencyNames = ["GPU Core Clock (MHz)", "Core Clock(MHz)", "GPU Core Clock MHz"]
        let frequency = frequencyNames.lazy.compactMap { statistics?[$0]?.doubleValue }
            .first { (1...10_000).contains($0) }
        let temperature = statistics?["Temperature(C)"]?.doubleValue
        let memoryUsed = hasDedicatedMemory ? Self.usedDedicatedMemory(for: accelerator, statistics: statistics) : nil
        return Reading(
            utilization: utilization.map { $0 / 100 },
            frequencyMHz: frequency ?? nvidiaClock?.frequencyMHz(),
            temperatureCelsius: temperature.flatMap { (1...150).contains($0) ? $0 : nil },
            memoryUsedBytes: memoryUsed
        )
    }

    func logUtilization() -> (percent: Double, source: String, timestamp: UInt64?)? {
        if let reading = nvidiaClock?.deviceUtilizationSample() {
            return (reading.percent, "nvidia_rusd", reading.timestamp)
        }
        guard let percent = Self.utilizationPercent(in: Self.statistics(for: accelerator)) else { return nil }
        return (percent, "ioregistry", nil)
    }

    private static func utilizationPercent(in statistics: [String: NSNumber]?) -> Double? {
        let keys = ["GPU Core Utilization %", "Device Utilization %", "GPU Activity(%)"]
        return keys.lazy.compactMap { statistics?[$0]?.doubleValue }.first { (0...100).contains($0) }
    }

    func videoMemoryUsage(for pid: Int32) -> NVIDIAProcessVideoMemory? {
        nvidiaClock?.videoMemoryUsage(for: pid)
    }

    func gpuUtilization(for pid: Int32) -> Double? {
        nvidiaClock?.gpuUtilization(for: pid)
    }

    private static func usedDedicatedMemory(for accelerator: io_service_t,
                                            statistics: [String: NSNumber]?) -> UInt64? {
        let key = "inUseVidMemoryBytes"
        if let bytes = byteCount(statistics?[key]) { return bytes }
        // NVAccel publishes the current memory gauge in the broader accumulator dictionary.
        return byteCount(Self.statistics(for: accelerator, property: "PerformanceStatisticsAccum")?[key])
    }

    private static func dedicatedMemoryCapacity(for accelerator: io_service_t) -> UInt64? {
        guard accelerator != 0 else { return nil }
        // Search the selected accelerator and its providers, including its GPU's PCI entry.
        func property(_ key: String) -> UInt64? {
            let options = IOOptionBits(kIORegistryIterateRecursively | kIORegistryIterateParents)
            let value = IORegistryEntrySearchCFProperty(accelerator, kIOServicePlane, key as CFString,
                                                        kCFAllocatorDefault, options)
            return byteCount(value as? NSNumber)
        }
        if let bytes = property("VRAM,totalsize"), bytes > 0 { return bytes }
        let bytesPerMiB: UInt64 = 1_048_576
        guard let mebibytes = property("VRAM,totalMB"), (1...(UInt64.max / bytesPerMiB)).contains(mebibytes) else { return nil }
        return mebibytes * bytesPerMiB
    }

    private static func byteCount(_ number: NSNumber?) -> UInt64? {
        guard let number, number.int64Value >= 0 else { return nil }
        return number.uint64Value
    }

    private static func findAccelerator(registryID: UInt64?) -> io_service_t {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOAccelerator"), &iterator) == KERN_SUCCESS else {
            return 0
        }
        defer { IOObjectRelease(iterator) }
        var candidates: [io_service_t] = []
        while case let entry = IOIteratorNext(iterator), entry != 0 {
            var entryID: UInt64 = 0
            if let registryID, IORegistryEntryGetRegistryEntryID(entry, &entryID) == KERN_SUCCESS, registryID == entryID {
                candidates.forEach { _ = IOObjectRelease($0) }
                return entry
            }
            if statistics(for: entry) != nil {
                candidates.append(entry)
            } else {
                IOObjectRelease(entry)
            }
        }
        if candidates.count == 1 { return candidates[0] }
        candidates.forEach { _ = IOObjectRelease($0) }
        return 0
    }

    private static func statistics(for entry: io_service_t, property: String = "PerformanceStatistics") -> [String: NSNumber]? {
        guard entry != 0 else { return nil }
        return IORegistryEntryCreateCFProperty(entry, property as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? [String: NSNumber]
    }
}

// Apple Silicon exposes DVFS residency through an optional, non-public interface.
// The app still samples CPU, memory and the accelerator if the interface changes.
private final class SiliconMonitor {
    struct Reading {
        var coreMHz: [Double?] = []
        var clusterMHz: [String: Double] = [:]
        var gpuUtilization: Double?
        var gpuMHz: Double?
    }

    private enum CoreKind: String, CaseIterable {
        case efficiency = "ECPU"
        case middle = "MCPU"
        case performance = "PCPU"

        func title(hasMiddleTier: Bool) -> String {
            switch self {
            case .efficiency: return "能效核"
            case .middle: return "性能核"
            case .performance: return hasMiddleTier ? "超大核" : "性能核"
            }
        }
    }

    private struct Residency {
        let active: Double
        let total: Double
        let weightedMHz: Double?
    }

    private let reports: IOReportMonitor
    private let deviceFrequencies: [String: [Double]]
    private let coreKinds: [String]

    init?(coreKinds: [String]) {
        guard let reports = IOReportMonitor() else { return nil }
        self.reports = reports
        self.coreKinds = coreKinds
        deviceFrequencies = Self.readFrequencyTables()
    }

    func sample() -> Reading {
        var result = Reading()
        var cpu: [CoreKind: [(name: String, residency: Residency?)]] = [:]
        var gpu: [Residency] = []

        reports.forEachChannel { group, name, states in
            switch group {
            case "CPU Stats":
                guard let kind = CoreKind.allCases.first(where: { name.contains($0.rawValue) }) else { return }
                let table = deviceFrequencies[kind.rawValue] ?? []
                cpu[kind, default: []].append((name, Self.decode(states: states, frequencies: table)))
            case "GPU Stats" where name.hasSuffix("GPUPH"):
                let table = deviceFrequencies["GPU"] ?? []
                if let residency = Self.decode(states: states, frequencies: table) { gpu.append(residency) }
            default: break
            }
        }

        let hasMiddleTier = cpu[.middle] != nil
        var channelKinds: [String] = []
        for kind in CoreKind.allCases {
            let channels = cpu[kind, default: []].sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
            let title = kind.title(hasMiddleTier: hasMiddleTier)
            let residences = channels.compactMap(\.residency)
            result.clusterMHz[title] = Self.averageFrequency(residences)
            channelKinds.append(contentsOf: Array(repeating: title, count: channels.count))
            result.coreMHz.append(contentsOf: channels.map { channel in
                guard let r = channel.residency, let weighted = r.weightedMHz, r.active > 0 else { return nil }
                return weighted / r.active
            })
        }
        // Pair frequencies with Mach core rows only if both sources agree on the CPU layout.
        if channelKinds != coreKinds { result.coreMHz = [] }
        let gpuTotal = gpu.reduce(0) { $0 + $1.total }
        if gpuTotal > 0 {
            result.gpuUtilization = gpu.reduce(0) { $0 + $1.active } / gpuTotal
        }
        result.gpuMHz = Self.averageFrequency(gpu)
        return result
    }

    private static func decode(states: [(String, Int64)], frequencies: [Double]) -> Residency? {
        guard !states.isEmpty, states.allSatisfy({ $0.1 >= 0 }) else { return nil }
        let inactive = Set(["IDLE", "DOWN", "OFF"])
        let activeStates = states.filter { !inactive.contains($0.0) }.map { Double($0.1) }
        let active = activeStates.reduce(0, +)
        let total = states.reduce(0.0) { $0 + Double($1.1) }
        let weighted = activeStates.count == frequencies.count && !frequencies.isEmpty
            ? zip(activeStates, frequencies).reduce(0.0) { $0 + $1.0 * $1.1 }
            : nil
        return total > 0 ? Residency(active: active, total: total, weightedMHz: weighted) : nil
    }

    private static func averageFrequency(_ residences: [Residency]) -> Double? {
        let valid = residences.filter { $0.weightedMHz != nil }
        let active = valid.reduce(0) { $0 + $1.active }
        let weighted = valid.compactMap(\.weightedMHz).reduce(0, +)
        return active > 0 ? weighted / active : nil
    }

    private static func readFrequencyTables() -> [String: [Double]] {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("AppleARMIODevice"), &iterator) == KERN_SUCCESS else {
            return [:]
        }
        defer { IOObjectRelease(iterator) }
        var tables: [String: [Double]] = [:]
        while case let entry = IOIteratorNext(iterator), entry != 0 {
            defer { IOObjectRelease(entry) }
            var rawName = [CChar](repeating: 0, count: 128)
            guard IORegistryEntryGetName(entry, &rawName) == KERN_SUCCESS else { continue }
            let name = String(decoding: rawName.prefix { $0 != 0 }.map(UInt8.init(bitPattern:)), as: UTF8.self)
            guard name == "pmgr" || name == "pmgr-child" else { continue }

            func data(_ key: String) -> Data? {
                IORegistryEntryCreateCFProperty(entry, key as CFString, kCFAllocatorDefault, 0)?
                    .takeRetainedValue() as? Data
            }

            func table(_ key: String) -> [Double]? {
                guard let raw = data(key), raw.count >= 8, raw.count.isMultiple(of: 8) else { return nil }
                let values = raw.withUnsafeBytes { bytes in
                    stride(from: 0, to: raw.count, by: 8).map { offset in
                        UInt32(littleEndian: bytes.loadUnaligned(fromByteOffset: offset, as: UInt32.self))
                    }
                }.filter { $0 > 0 }
                guard let maximum = values.max() else { return nil }
                // Older devices encode Hz, and newer CPU tables may use kHz.
                let scale: Double = maximum >= 100_000_000 ? 1_000_000 : 1_000
                let valuesMHz = values.map { Double($0) / scale }
                guard valuesMHz.allSatisfy({ (50...10_000).contains($0) }) else { return nil }
                return valuesMHz
            }

            if let clusters = data("acc-clusters") {
                let prefixByTier = [0: "ECPU", 1: "MCPU", 2: "PCPU"]
                let offsets = stride(from: 0, to: max(0, clusters.count - 7), by: 8)
                let tiers = Dictionary(grouping: offsets, by: { Int(clusters[$0 + 1]) })
                for (tier, group) in tiers {
                    // Older E/P machines label the top tier 1. Newer three-tier machines use 2.
                    let key = tier == 1 && tiers[2] == nil ? "PCPU" : prefixByTier[tier]
                    guard let key, tables[key] == nil, let first = group.first else { continue }
                    tables[key] = table("voltage-states\(clusters[first])-sram")
                }
            }
            tables["ECPU"] = tables["ECPU"] ?? table("voltage-states1-sram")
            tables["PCPU"] = tables["PCPU"] ?? table("voltage-states5-sram")
            tables["GPU"] = tables["GPU"] ?? table("voltage-states9") ?? table("voltage-states9-sram")
        }
        return tables
    }
}

private final class IOReportMonitor {
    private typealias CopyGroup = @convention(c) (CFString, CFString, UInt64, UInt64, UInt64) -> Unmanaged<CFDictionary>?
    private typealias Merge = @convention(c) (CFMutableDictionary, CFDictionary, CFTypeRef?) -> Void
    private typealias Subscribe = @convention(c) (UnsafeRawPointer?, CFMutableDictionary, UnsafeMutablePointer<Unmanaged<CFMutableDictionary>?>, UInt64, CFTypeRef?) -> Unmanaged<CFTypeRef>?
    private typealias Sample = @convention(c) (CFTypeRef, CFMutableDictionary, CFTypeRef?) -> Unmanaged<CFDictionary>?
    private typealias Delta = @convention(c) (CFDictionary, CFDictionary, CFTypeRef?) -> Unmanaged<CFDictionary>?
    private typealias ChannelLabel = @convention(c) (CFDictionary) -> Unmanaged<CFString>?
    private typealias StateCount = @convention(c) (CFDictionary) -> Int32
    private typealias StateName = @convention(c) (CFDictionary, Int32) -> Unmanaged<CFString>?
    private typealias Residency = @convention(c) (CFDictionary, Int32) -> Int64

    private let library: UnsafeMutableRawPointer
    private let subscription: CFTypeRef
    private let channels: CFMutableDictionary
    private let makeSample: Sample
    private let makeDelta: Delta
    private let groupName: ChannelLabel
    private let channelName: ChannelLabel
    private let stateCount: StateCount
    private let stateName: StateName
    private let stateResidency: Residency
    private var previous: CFDictionary?

    init?() {
        guard let library = dlopen("/usr/lib/libIOReport.dylib", RTLD_LAZY | RTLD_LOCAL | RTLD_NODELETE) else { return nil }
        func symbol<T>(_ name: String, as type: T.Type) -> T? {
            dlsym(library, name).map { unsafeBitCast($0, to: type) }
        }
        guard let copy = symbol("IOReportCopyChannelsInGroup", as: CopyGroup.self),
              let merge = symbol("IOReportMergeChannels", as: Merge.self),
              let subscribe = symbol("IOReportCreateSubscription", as: Subscribe.self),
              let sample = symbol("IOReportCreateSamples", as: Sample.self),
              let delta = symbol("IOReportCreateSamplesDelta", as: Delta.self),
              let group = symbol("IOReportChannelGetGroup", as: ChannelLabel.self),
              let channel = symbol("IOReportChannelGetChannelName", as: ChannelLabel.self),
              let count = symbol("IOReportStateGetCount", as: StateCount.self),
              let name = symbol("IOReportStateGetNameForIndex", as: StateName.self),
              let residency = symbol("IOReportStateGetResidency", as: Residency.self) else {
            dlclose(library)
            return nil
        }

        let cpu = copy("CPU Stats" as CFString, "CPU Core Performance States" as CFString, 0, 0, 0)?.takeRetainedValue()
        let gpu = copy("GPU Stats" as CFString, "GPU Performance States" as CFString, 0, 0, 0)?.takeRetainedValue()
        guard let available = cpu ?? gpu, let mutable = CFDictionaryCreateMutableCopy(nil, 0, available) else {
            dlclose(library)
            return nil
        }
        if cpu != nil, let gpu { merge(mutable, gpu, nil) }
        var subscribed: Unmanaged<CFMutableDictionary>?
        let subscription = subscribe(nil, mutable, &subscribed, 0, nil)?.takeRetainedValue()
        guard let subscription, let channels = subscribed?.takeRetainedValue() else {
            subscribed?.release()
            dlclose(library)
            return nil
        }

        self.library = library
        self.subscription = subscription
        self.channels = channels
        makeSample = sample
        makeDelta = delta
        groupName = group
        channelName = channel
        stateCount = count
        stateName = name
        stateResidency = residency
        previous = sample(subscription, channels, nil)?.takeRetainedValue()
    }

    deinit {
        dlclose(library)
    }

    func forEachChannel(_ observe: (String, String, [(String, Int64)]) -> Void) {
        guard let current = makeSample(subscription, channels, nil)?.takeRetainedValue() else {
            previous = nil
            return
        }
        defer { previous = current }
        guard let previous, let delta = makeDelta(previous, current, nil)?.takeRetainedValue() else { return }
        let key = "IOReportChannels" as CFString
        guard let value = CFDictionaryGetValue(delta, Unmanaged.passUnretained(key).toOpaque()) else { return }
        let entries = Unmanaged<CFArray>.fromOpaque(value).takeUnretainedValue()
        for index in 0..<CFArrayGetCount(entries) {
            guard let pointer = CFArrayGetValueAtIndex(entries, index) else { continue }
            let entry = Unmanaged<CFDictionary>.fromOpaque(pointer).takeUnretainedValue()
            guard let group = groupName(entry)?.takeUnretainedValue(),
                  let channel = channelName(entry)?.takeUnretainedValue() else { continue }
            let count = stateCount(entry)
            guard (1...256).contains(count) else { continue }
            let states = (0..<count).compactMap { index -> (String, Int64)? in
                guard let name = stateName(entry, index)?.takeUnretainedValue() else { return nil }
                return (name as String, stateResidency(entry, index))
            }
            guard states.count == count else { continue }
            observe(group as String, channel as String, states)
        }
    }
}
