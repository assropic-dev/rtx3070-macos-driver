import Foundation

enum FrameIssueKind: Equatable {
    case unevenTiming
    case belowTarget
    case displayPipelineBusy

    var title: String {
        switch self {
        case .unevenTiming: return "检测到卡顿"
        case .belowTarget: return "低于目标帧率"
        case .displayPipelineBusy: return "呈现管线繁忙"
        }
    }
}

enum FrameDiagnostics {
    static func timing(fps: Double?, frameCount: Int, observedStutter: Bool, targetFPS: Int) -> FrameIssueKind? {
        if observedStutter { return .unevenTiming }
        guard let fps, fps >= 8, frameCount >= 8, targetFPS > 0 else { return nil }
        return fps < Double(targetFPS) * 0.9 ? .belowTarget : nil
    }
}
