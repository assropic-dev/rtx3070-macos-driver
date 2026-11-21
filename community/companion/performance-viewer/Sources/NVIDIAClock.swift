import Foundation

struct NVIDIAProcessVideoMemory: Sendable {
    let privateBytes: UInt64
    let ownedSharedBytes: UInt64
    let referencedSharedBytes: UInt64

    var attributedBytes: UInt64? {
        let (bytes, overflow) = privateBytes.addingReportingOverflow(ownedSharedBytes)
        return overflow ? nil : bytes
    }
}

#if arch(x86_64)
import Darwin
import IOKit

// The NullMoth NVAccel IOUserClient transport and NVIDIA's read-only RUSD clock layout:
// https://github.com/nullmoth/nvidia-macos-driver/tree/main/tools/nvmtl-smi
// https://github.com/NVIDIA/open-gpu-kernel-modules/blob/620.70.06/src/common/sdk/nvidia/inc/class/cl00de.h
final class NVIDIAClockMonitor {
    // Labeled tuples preserve field order, including the padding in the driver's C ABI.
    private typealias DeviceInfo = (
        gpuID: UInt32, flags: UInt32, deviceInstance: UInt32, subdeviceInstance: UInt32,
        sliStatus: UInt32, boardID: UInt32, gpuInstance: UInt32, numaID: Int32
    )
    private typealias DeviceAllocation = (
        deviceInstance: UInt32, sharedClient: UInt32, targetClient: UInt32, targetDevice: UInt32,
        flags: UInt32, reserved: UInt32, addressSpaceSize: UInt64, startAddress: UInt64,
        limitAddress: UInt64, addressMode: UInt32, trailingPadding: UInt32
    )
    private typealias MapRequest = (
        client: UInt32, device: UInt32, memory: UInt32, reserved: UInt32, offset: UInt64,
        length: UInt64, returnedAddress: UInt64, status: UInt32, flags: UInt32,
        descriptor: Int32, trailingPadding: UInt32
    )
    private typealias AllocateRequest = (
        client: UInt32, parent: UInt32, resource: UInt32, resourceClass: UInt32,
        parameterAddress: UInt64, parameterCount: UInt32, status: UInt32
    )
    private typealias QueryRequest = (
        client: UInt32, resource: UInt32, command: UInt32, flags: UInt32,
        parameterAddress: UInt64, parameterCount: UInt32, status: UInt32
    )
    private typealias ClockSample = (timestamp: UInt64, targetMHz: UInt32, graphicsKHz: UInt32)

    private struct ProcessPoll {
        let pid: Int32
        let latestTimestamp: UInt64
        let observedAt: ContinuousClock.Instant
        let warmedUp: Bool
    }

    private static let sharedDataSize: UInt64 = 3184
    private static let clockOffset: UInt64 = 72
    // offsetof(NV00DE_SHARED_DATA, perfDevUtil) in the same 620-series layout.
    private static let deviceUtilizationOffset: UInt64 = 1992
    private static let processSampleCount = 5
    private static let processSampleSize = 104
    private static let processBlockSize = 8 + processSampleCount * processSampleSize
    private static let processOffset = sharedDataSize - UInt64(processBlockSize)

    private let connection: io_connect_t
    private let client: UInt32
    private let subdevice: UInt32
    private let descriptor: Int32
    private let address: mach_vm_address_t
    private let pollsProcessUtilization: Bool
    private let pollsDeviceUtilization: Bool
    private var lastTimestamp: UInt64 = 0
    private var lastChange = ContinuousClock.now
    private var processPoll: ProcessPoll?

    init?() {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"))
        guard service != IO_OBJECT_NULL else { return nil }
        defer { IOObjectRelease(service) }
        let publisher = IORegistryEntryCreateCFProperty(service, "CFBundleIdentifier" as CFString, kCFAllocatorDefault, 0)
        guard publisher?.takeRetainedValue() as? String == "com.nullmoth.NVAccel" else { return nil }

        var connection: io_connect_t = IO_OBJECT_NULL
        let resourceManagerClient: UInt32 = 0x4E56_0000
        guard IOServiceOpen(service, mach_task_self_, resourceManagerClient, &connection) == KERN_SUCCESS else { return nil }

        let descriptor = Darwin.open("/dev/null", O_RDONLY | O_CLOEXEC)
        var mappedAddress: mach_vm_address_t = 0
        var didInitialize = false
        defer {
            if !didInitialize {
                if mappedAddress != 0 {
                    IOConnectUnmapMemory64(connection, 0, mach_task_self_, mappedAddress)
                }
                IOServiceClose(connection)
                if descriptor >= 0 { Darwin.close(descriptor) }
            }
        }
        guard descriptor >= 0 else { return nil }
        var descriptorInput = UInt64(descriptor)
        guard IOConnectCallScalarMethod(connection, 3, &descriptorInput, 1, nil, nil) == KERN_SUCCESS else { return nil }

        guard let client = Self.allocate(on: connection, client: 0, parent: 0, resourceClass: 0x41) else { return nil }

        // RM's attached device list has 32 IDs followed by invalid entries.
        // With multiple NVIDIA cards, the app cannot associate this clock with the displayed GPU.
        var gpuIDs = [UInt32](repeating: .max, count: 32)
        let queriedIDs = gpuIDs.withUnsafeMutableBytes {
            Self.query(on: connection, client: client, command: 0x0201, data: $0)
        }
        guard queriedIDs, gpuIDs[0] != .max, gpuIDs.dropFirst().allSatisfy({ $0 == .max }) else { return nil }

        // NV0000_CTRL_GPU_GET_ID_INFO_V2_PARAMS consists of eight 32-bit fields.
        var deviceInfo: DeviceInfo = (gpuIDs[0], 0, 0, 0, 0, 0, 0, 0)
        guard withUnsafeMutableBytes(of: &deviceInfo, {
            Self.query(on: connection, client: client, command: 0x0205, data: $0)
        }) else { return nil }

        // NV0080_ALLOC_PARAMETERS is zeroed except for the selected device and shared client.
        var deviceParameters: DeviceAllocation = (deviceInfo.deviceInstance, client, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        let device = withUnsafePointer(to: &deviceParameters) {
            Self.allocate(on: connection, client: client, parent: client, resourceClass: 0x80, parameters: UnsafeRawPointer($0))
        }
        guard let device else { return nil }

        var subdeviceInstance = deviceInfo.subdeviceInstance
        let subdevice = withUnsafePointer(to: &subdeviceInstance) {
            Self.allocate(on: connection, client: client, parent: device, resourceClass: 0x2080, parameters: UnsafeRawPointer($0))
        }
        guard let subdevice else { return nil }

        // Prefer CLOCK | PERF | PROC_UTIL. Preserve the clock and per-process
        // readings on drivers that support only a subset of these groups.
        let masks: [UInt64] = [0x83, 0x81, 0x03, 0x01]
        guard let (sharedData, mask) = masks.lazy.compactMap({ mask in
            Self.allocateSharedData(on: connection, client: client, subdevice: subdevice,
                                    pollingMask: mask).map { ($0, mask) }
        }).first else { return nil }

        // nv_ioctl_nvos33_parameters_with_fd, including alignment padding.
        // The RUSD system-memory object requires NVOS33_FLAGS_ACCESS_READ_ONLY.
        var map: MapRequest = (client, device, sharedData, 0, 0, Self.sharedDataSize, 0, .max, 1, descriptor, 0)
        guard Self.escape(0x4E, on: connection, arguments: &map), map.status == 0 else { return nil }

        var mappedSize: mach_vm_size_t = 0
        let options = IOOptionBits(kIOMapAnywhere | kIOMapReadOnly)
        guard IOConnectMapMemory64(connection, 0, mach_task_self_, &mappedAddress, &mappedSize, options) == KERN_SUCCESS,
              mappedAddress != 0, mappedSize >= Self.sharedDataSize else { return nil }

        didInitialize = true
        self.connection = connection
        self.client = client
        self.subdevice = subdevice
        self.descriptor = descriptor
        self.address = mappedAddress
        self.pollsProcessUtilization = mask & 0x80 != 0
        self.pollsDeviceUtilization = mask & 0x02 != 0
    }

    deinit {
        IOConnectUnmapMemory64(connection, 0, mach_task_self_, address)
        // The driver frees this connection's RM client and resource handles on close.
        IOServiceClose(connection)
        Darwin.close(descriptor)
    }

    func frequencyMHz() -> Double? {
        // offsetof(NV00DE_SHARED_DATA, clkPublicDomainInfos) for the NVIDIA 620-series ABI.
        let clockAddress = address + Self.clockOffset
        var sample: ClockSample = (0, 0, 0)
        var currentTimestamp: UInt64 = 0
        guard Self.read(from: clockAddress, into: &sample),
              Self.read(from: clockAddress, into: &currentTimestamp),
              sample.timestamp == currentTimestamp,
              currentTimestamp != 0, currentTimestamp < 0xFF00_0000_0000_0000,
              (1...5_000_000).contains(sample.graphicsKHz) else { return nil }

        if lastTimestamp != currentTimestamp {
            lastTimestamp = currentTimestamp
            lastChange = .now
        }
        guard lastChange.duration(to: .now) < .seconds(5) else { return nil }
        return Double(sample.graphicsKHz) / 1_000
    }

    /// Board-wide busy percentage paired with its original firmware publication timestamp.
    func deviceUtilizationSample() -> (timestamp: UInt64, percent: Double)? {
        guard pollsDeviceUtilization else { return nil }
        let field = address + Self.deviceUtilizationOffset
        var before: UInt64 = 0
        var busy: UInt32 = 0
        var after: UInt64 = 0
        guard Self.read(from: field, into: &before), before > 0, before < 0xFF00_0000_0000_0000,
              Self.read(from: field + 8, into: &busy), busy <= 100,
              Self.read(from: field, into: &after), before == after else { return nil }
        return (before, Double(busy))
    }

    /// Driver-sampled graphics engine utilization attributed to a process, in 0...1.
    func gpuUtilization(for pid: Int32) -> Double? {
        guard pid > 0, pollsProcessUtilization, let sample = processUtilizationSample(for: UInt32(pid)) else { return nil }

        let now = ContinuousClock.now
        guard let previous = processPoll, previous.pid == pid, sample.timestamp >= previous.latestTimestamp else {
            processPoll = ProcessPoll(pid: pid, latestTimestamp: sample.timestamp, observedAt: now, warmedUp: false)
            return nil
        }
        if sample.timestamp > previous.latestTimestamp {
            processPoll = ProcessPoll(pid: pid, latestTimestamp: sample.timestamp, observedAt: now, warmedUp: true)
        } else if !previous.warmedUp || previous.observedAt.duration(to: now) >= .seconds(3) {
            return nil
        }
        return sample.utilization
    }

    private func processUtilizationSample(for pid: UInt32) -> (timestamp: UInt64, utilization: Double)? {
        // NVIDIA 620 RUSD_PROC_UTIL has a publication timestamp and five 104-byte records.
        // NVIDIA's GPUACCT likewise divides the matching PID's GR samples by the total sample count.
        // https://github.com/NVIDIA/open-gpu-kernel-modules/blob/620.70.06/src/nvidia/src/kernel/diagnostics/gpu_acct.c
        let processAddress = address + Self.processOffset
        var storage = Array(repeating: UInt64(0), count: Self.processBlockSize / MemoryLayout<UInt64>.stride)
        return storage.withUnsafeMutableBytes { bytes in
            var publicationTimestamp: UInt64 = 0
            var timestampAfterCopy: UInt64 = 0
            guard Self.read(from: processAddress, into: &publicationTimestamp),
                  publicationTimestamp != 0, publicationTimestamp < 0xFF00_0000_0000_0000,
                  Self.read(from: processAddress, into: bytes),
                  Self.read(from: processAddress, into: &timestampAfterCopy),
                  publicationTimestamp == timestampAfterCopy,
                  publicationTimestamp == bytes.load(as: UInt64.self) else { return nil }

            var earliest = UInt64.max
            var latest: UInt64 = 0
            var timestamps = Set<UInt64>()
            var utilization: UInt32 = 0
            for index in 0..<Self.processSampleCount {
                let sample = 8 + index * Self.processSampleSize
                let graphics = sample + 24
                let timestamp = bytes.load(fromByteOffset: sample, as: UInt64.self)
                let load = bytes.load(fromByteOffset: graphics, as: UInt32.self)
                let owner = bytes.load(fromByteOffset: graphics + 8, as: UInt32.self)
                let subprocess = bytes.load(fromByteOffset: graphics + 12, as: UInt32.self)
                guard timestamp > 0, timestamps.insert(timestamp).inserted, load <= 10_000 else { return nil }
                // Busy periods with no valid owner cannot safely be attributed to any process.
                guard load == 0 || (owner != 0 && owner != .max && subprocess != .max) else { return nil }
                earliest = min(earliest, timestamp)
                latest = max(latest, timestamp)
                if owner == pid { utilization += load }
            }
            guard latest - earliest <= 1_500_000_000, publicationTimestamp >= latest,
                  publicationTimestamp - latest <= 2_000_000_000 else { return nil }
            return (latest, Double(utilization) / Double(10_000 * Self.processSampleCount))
        }
    }

    func videoMemoryUsage(for pid: Int32) -> NVIDIAProcessVideoMemory? {
        guard pid > 0 else { return nil }

        // GET_PID_INFO queries the RM's per-PID allocation accounting. The ABI has an
        // 8-byte header followed by capacity for 200 aligned 72-byte entries.
        // https://github.com/NVIDIA/open-gpu-kernel-modules/blob/620.70.06/src/common/sdk/nvidia/inc/ctrl/ctrl2080/ctrl2080gpu.h
        let entryOffset = 8
        let resultOffset = entryOffset + 8
        let memoryOffset = entryOffset + 16
        var storage = Array(repeating: UInt64(0), count: (entryOffset + 200 * 72) / 8)
        return storage.withUnsafeMutableBytes { bytes in
            bytes.storeBytes(of: UInt32(1), as: UInt32.self)
            bytes.storeBytes(of: UInt32(pid), toByteOffset: entryOffset, as: UInt32.self)
            bytes.storeBytes(of: UInt32.max, toByteOffset: resultOffset, as: UInt32.self)
            let command: UInt32 = 0x2080_018E
            guard Self.query(on: connection, client: client, resource: subdevice, command: command, data: bytes),
                  bytes.load(fromByteOffset: resultOffset, as: UInt32.self) == 0 else { return nil }

            return NVIDIAProcessVideoMemory(
                privateBytes: bytes.load(fromByteOffset: memoryOffset, as: UInt64.self),
                ownedSharedBytes: bytes.load(fromByteOffset: memoryOffset + 8, as: UInt64.self),
                referencedSharedBytes: bytes.load(fromByteOffset: memoryOffset + 16, as: UInt64.self)
            )
        }
    }

    private static func read<T>(from address: mach_vm_address_t, into value: inout T) -> Bool {
        withUnsafeMutableBytes(of: &value) { bytes in
            read(from: address, into: bytes)
        }
    }

    private static func read(from address: mach_vm_address_t, into bytes: UnsafeMutableRawBufferPointer) -> Bool {
        guard let destination = bytes.baseAddress else { return false }
        let length = mach_vm_size_t(bytes.count)
        var bytesCopied: mach_vm_size_t = 0
        return mach_vm_read_overwrite(mach_task_self_, address, length,
                                      mach_vm_address_t(UInt(bitPattern: destination)), &bytesCopied) == KERN_SUCCESS
            && bytesCopied == length
    }

    private static func allocateSharedData(on connection: io_connect_t, client: UInt32, subdevice: UInt32,
                                           pollingMask: UInt64) -> UInt32? {
        withUnsafePointer(to: pollingMask) {
            allocate(on: connection, client: client, parent: subdevice, resourceClass: 0xDE,
                     parameters: UnsafeRawPointer($0))
        }
    }

    private static func allocate(on connection: io_connect_t, client: UInt32, parent: UInt32,
                                 resourceClass: UInt32, parameters: UnsafeRawPointer? = nil) -> UInt32? {
        // NVOS21_PARAMETERS
        var request: AllocateRequest =
            (client, parent, 0, resourceClass, UInt64(UInt(bitPattern: parameters)), 0, .max)
        guard escape(0x2B, on: connection, arguments: &request), request.status == 0, request.resource != 0 else { return nil }
        return request.resource
    }

    private static func query(on connection: io_connect_t, client: UInt32, resource: UInt32? = nil, command: UInt32,
                              data: UnsafeMutableRawBufferPointer) -> Bool {
        // NVOS54_PARAMETERS. Callers query device identity or process video-memory accounting.
        var request: QueryRequest =
            (client, resource ?? client, command, 0, UInt64(UInt(bitPattern: data.baseAddress)), UInt32(data.count), .max)
        return escape(0x2A, on: connection, arguments: &request) && request.status == 0
    }

    private static func escape<T>(_ command: UInt64, on connection: io_connect_t, arguments: inout T) -> Bool {
        withUnsafeMutableBytes(of: &arguments) { bytes in
            var selectorInput = command
            var resultSize = bytes.count
            let status = IOConnectCallMethod(connection, 1, &selectorInput, 1, bytes.baseAddress, bytes.count,
                                             nil, nil, bytes.baseAddress, &resultSize)
            return status == KERN_SUCCESS && resultSize == bytes.count
        }
    }
}
#else
final class NVIDIAClockMonitor {
    init?() { return nil }
    func frequencyMHz() -> Double? { nil }
    func deviceUtilizationSample() -> (timestamp: UInt64, percent: Double)? { nil }
    func gpuUtilization(for pid: Int32) -> Double? { nil }
    func videoMemoryUsage(for pid: Int32) -> NVIDIAProcessVideoMemory? { nil }
}
#endif
