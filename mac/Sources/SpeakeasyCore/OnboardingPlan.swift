import Foundation

/// The Mac first run, kept to the screens a first call actually needs:
/// connect (only until paired) → voice sign-in (only while the voice isn't ready) → ready.
/// Everything else (names, where finished work goes, home control, Dock, shortcut) sits on the
/// ready screen with a default already picked; routing and the voice brief are left to Settings.
public enum OnboardingStep: String, CaseIterable, Sendable {
    case connect, voice, ready

    public var title: String {
        switch self {
        case .connect: return "Connect to Hermes"
        case .voice: return "Voice sign-in"
        case .ready: return "Ready"
        }
    }
}

public struct OnboardingFacts: Equatable, Sendable {
    public var paired: Bool
    /// The server says the voice can start (signed in to the voice provider); nil when its
    /// status hasn't loaded. Unknown keeps the sign-in screen, which shows the checks and a
    /// Check again button, so a broken voice is never discovered on the first call instead.
    public var voiceReady: Bool?
    public init(paired: Bool, voiceReady: Bool?) { self.paired = paired; self.voiceReady = voiceReady }

    public func isDone(_ step: OnboardingStep) -> Bool {
        switch step {
        case .connect: return paired
        case .voice: return voiceReady == true
        case .ready: return false
        }
    }
}

public enum OnboardingPlan {
    /// First screen that still needs the user.
    public static func first(_ facts: OnboardingFacts) -> OnboardingStep {
        OnboardingStep.allCases.first { !facts.isDone($0) } ?? .ready
    }

    /// The screen after `step`, skipping ones already done; nil after the last.
    public static func next(after step: OnboardingStep, _ facts: OnboardingFacts) -> OnboardingStep? {
        let all = OnboardingStep.allCases
        guard let i = all.firstIndex(of: step) else { return nil }
        return all[(i + 1)...].first { !facts.isDone($0) }
    }

    /// The screen before `step` the user can still change; nil when there is none. Connect is
    /// never offered once paired (re-pairing lives in Settings).
    public static func previous(before step: OnboardingStep, _ facts: OnboardingFacts) -> OnboardingStep? {
        let all = OnboardingStep.allCases
        guard let i = all.firstIndex(of: step) else { return nil }
        return all[..<i].reversed().first { !facts.isDone($0) }
    }

    /// Every screen this setup would show from the start, for the step count in tests and the
    /// accessibility label.
    public static func screens(_ facts: OnboardingFacts) -> [OnboardingStep] {
        OnboardingStep.allCases.filter { !facts.isDone($0) }
    }
}
