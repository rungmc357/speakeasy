import Foundation

/// What a call's own audio engine does about an audio-session interruption (a phone call, Siri,
/// an alarm). The system stops an `AVAudioEngine` when one begins and never restarts it, so a
/// relayed call (Grok Voice) went silent both ways after the user took a phone call. Raw values
/// so this stays testable off iOS: `AVAudioSession.InterruptionType` (began 1, ended 0) and
/// `AVAudioSession.InterruptionOptions.shouldResume` (1).
public enum AudioInterruption {
    public enum Action: Equatable { case none, restart }

    public static func action(typeRaw: UInt?, optionsRaw: UInt?, audioRunning: Bool, closed: Bool) -> Action {
        guard !closed, audioRunning, typeRaw == 0 else { return .none }   // only an end, on a live call
        // Without shouldResume another app took the audio over (it is playing now): leave it be;
        // the call's mic check notices the quiet mic and reconnects once the session is free.
        return (optionsRaw ?? 0) & 1 == 1 ? .restart : .none
    }
}
