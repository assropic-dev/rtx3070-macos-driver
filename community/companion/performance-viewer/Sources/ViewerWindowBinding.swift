import AppKit
import SwiftUI

struct ViewerWindowBinding: NSViewRepresentable {
    let floatsAboveWindows: Bool
    let screenDidChange: (NSScreen?) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> AttachmentView {
        let view = AttachmentView(frame: .zero)
        let coordinator = context.coordinator
        view.windowDidChange = { [weak coordinator] window in
            coordinator?.attach(to: window)
        }
        coordinator.configure(floatsAboveWindows: floatsAboveWindows, screenDidChange: screenDidChange)
        return view
    }

    func updateNSView(_ view: AttachmentView, context: Context) {
        context.coordinator.configure(floatsAboveWindows: floatsAboveWindows, screenDidChange: screenDidChange)
        context.coordinator.attach(to: view.window)
    }

    static func dismantleNSView(_ view: AttachmentView, coordinator: Coordinator) {
        view.windowDidChange = nil
        coordinator.detach()
    }

    final class AttachmentView: NSView {
        var windowDidChange: ((NSWindow?) -> Void)?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            windowDidChange?(window)
        }
    }

    @MainActor
    final class Coordinator: NSObject {
        private weak var window: NSWindow?
        private var originalLevel: NSWindow.Level?
        private var floatsAboveWindows = false
        private var screenDidChange: ((NSScreen?) -> Void)?
        private var isScreenReportPending = false

        func configure(floatsAboveWindows: Bool, screenDidChange: @escaping (NSScreen?) -> Void) {
            self.floatsAboveWindows = floatsAboveWindows
            self.screenDidChange = screenDidChange
            updateLevel()
        }

        func attach(to newWindow: NSWindow?) {
            guard window !== newWindow else { return }
            restoreLevel()
            NotificationCenter.default.removeObserver(self)
            window = newWindow
            if let newWindow {
                NotificationCenter.default.addObserver(self, selector: #selector(windowScreenChanged),
                    name: NSWindow.didChangeScreenNotification, object: newWindow)
                NotificationCenter.default.addObserver(self, selector: #selector(windowScreenChanged),
                    name: NSApplication.didChangeScreenParametersNotification, object: nil)
                updateLevel()
            }
            queueScreenReport()
        }

        func detach() {
            attach(to: nil)
            screenDidChange = nil
        }

        private func updateLevel() {
            guard let window else { return }
            if floatsAboveWindows {
                if originalLevel == nil { originalLevel = window.level }
                if window.level != .floating { window.level = .floating }
            } else {
                restoreLevel()
            }
        }

        private func restoreLevel() {
            if let originalLevel { window?.level = originalLevel }
            originalLevel = nil
        }

        @objc private func windowScreenChanged(_ notification: Notification) {
            queueScreenReport()
        }

        private func queueScreenReport() {
            guard !isScreenReportPending else { return }
            isScreenReportPending = true
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.isScreenReportPending = false
                self.screenDidChange?(self.window?.screen)
            }
        }
    }
}
