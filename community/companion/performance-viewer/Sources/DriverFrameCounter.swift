import CoreGraphics
import Darwin
import Foundation
import IOKit

/// Counts successful NullMoth zero-copy scan-out presents on a single NVIDIA display.
final class DriverFrameCounter {
    private struct Counters {
        let shown: Int64
        let submitted: Int64
        let discarded: Int64
        let busySkips: Int64
    }

    private var baseline: (displayID: CGDirectDisplayID, uptime: TimeInterval, counters: Counters)?
    /// New flips skipped in the latest valid interval because the previous flip was still in flight.
    private(set) var lateCount: Int?
    private(set) var activeDisplayID: CGDirectDisplayID?

    static var isAvailable: Bool {
        supportedDisplay() != nil && readCounters() != nil
    }

    func reset() {
        baseline = nil
        lateCount = nil
        activeDisplayID = nil
    }

    func read(at uptime: TimeInterval) -> Double? {
        lateCount = nil
        guard uptime.isFinite, let displayID = Self.supportedDisplay(), let counters = Self.readCounters() else {
            reset()
            return nil
        }
        activeDisplayID = displayID

        let previous = baseline
        baseline = (displayID, uptime, counters)
        guard let previous, previous.displayID == displayID else { return nil }
        let elapsed = uptime - previous.uptime
        guard elapsed > 0, elapsed <= 5, counters.shown >= previous.counters.shown,
              counters.submitted >= previous.counters.submitted,
              counters.discarded >= previous.counters.discarded,
              counters.busySkips >= previous.counters.busySkips else { return nil }

        let shown = counters.shown - previous.counters.shown
        let submitted = counters.submitted - previous.counters.submitted
        let discarded = counters.discarded - previous.counters.discarded
        // A couple of transactions may straddle sample boundaries. A wider gap can mean the
        // compositor switched to copies, which this successful-flip counter does not include.
        let unmatchedSubmissions = max(0, submitted - shown)
        guard unmatchedSubmissions - discarded <= 2 else { return nil }
        lateCount = Int(exactly: counters.busySkips - previous.counters.busySkips)
        return Double(shown) / elapsed
    }

    private static func supportedDisplay() -> CGDirectDisplayID? {
        // NVAccel exposes a global flip counter. It identifies a specific screen only
        // when Core Graphics and the driver each report exactly one display/framebuffer.
        var displayCount: UInt32 = 0
        guard CGGetActiveDisplayList(0, nil, &displayCount) == .success, displayCount == 1 else { return nil }
        let displayID = CGMainDisplayID()
        guard CGDisplayIsAsleep(displayID) == 0,
              sysctl("debug.nvaccel_iop", as: Int32.self) == 1,
              sysctl("debug.nvaccel_iop_flip", as: Int32.self) == 1 else { return nil }

        var framebufferIterator: io_iterator_t = IO_OBJECT_NULL
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("NVRMNVDAFramebuffer"),
                                           &framebufferIterator) == KERN_SUCCESS else { return nil }
        defer { IOObjectRelease(framebufferIterator) }
        let framebuffer = IOIteratorNext(framebufferIterator)
        guard framebuffer != IO_OBJECT_NULL else { return nil }
        defer { IOObjectRelease(framebuffer) }
        let secondFramebuffer = IOIteratorNext(framebufferIterator)
        if secondFramebuffer != IO_OBJECT_NULL {
            IOObjectRelease(secondFramebuffer)
            return nil
        }

        let identifier = IORegistryEntryCreateCFProperty(framebuffer, "CFBundleIdentifier" as CFString,
                                                        kCFAllocatorDefault, 0)?.takeRetainedValue()
        return (identifier as? String) == "com.nullmoth.NVRMFB" ? displayID : nil
    }

    private static func readCounters() -> Counters? {
        // Published as read-only QUADs by NVAccel. Successful flips count after scan-out
        // accepts a new surface. iop_ok also counts queued and copied submissions.
        // https://github.com/nullmoth/nvidia-macos-driver/blob/v3.0/kexts/NVRM/accel/nvrm-accel.cpp
        guard let shown = sysctl("debug.nvaccel_iop_flips", as: Int64.self), shown >= 0,
              let submitted = sysctl("debug.nvaccel_iop_ok", as: Int64.self), submitted >= 0,
              let drops = sysctl("debug.nvaccel_iop_async_drop", as: Int64.self), drops >= 0,
              let busySkips = sysctl("debug.nvaccel_iop_late", as: Int64.self), busySkips >= 0,
              let refused = sysctl("debug.nvaccel_iop_async_refused", as: Int64.self), refused >= 0 else { return nil }
        let (partiallyDiscarded, firstOverflow) = drops.addingReportingOverflow(busySkips)
        let (discarded, secondOverflow) = partiallyDiscarded.addingReportingOverflow(refused)
        guard !firstOverflow, !secondOverflow else { return nil }
        return Counters(shown: shown, submitted: submitted, discarded: discarded, busySkips: busySkips)
    }

    private static func sysctl<Integer: FixedWidthInteger>(_ key: String, as _: Integer.Type) -> Integer? {
        var value: Integer = 0
        var size = MemoryLayout<Integer>.size
        guard sysctlbyname(key, &value, &size, nil, 0) == 0, size == MemoryLayout<Integer>.size else { return nil }
        return value
    }
}
