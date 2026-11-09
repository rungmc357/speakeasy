import Foundation

/// Words on the last setup screen ("You're ready to talk"), shared by the Mac and iPhone.
/// PLACEHOLDER: this mirrors the iPhone onboarding redo, which is still moving (SPK-171).
/// Change the wording here only, never in the views.
public enum ReadyCopy {
    public static let title = "You\u{2019}re ready to talk"
    /// Follows the connection line ("Connected to … .") in the subtitle.
    public static let subtitleTail = "Here\u{2019}s how your calls are set up. Press the shortcut any time to talk."
    public static let footnote = "You can change any of this later in Settings. Your first call starts with a one-minute tour; say \u{201C}skip\u{201D} to jump straight in."
    public static let microphone = "Microphone"
    public static let userName = "Your name"
    public static let userNamePrompt = "Optional"
    public static let assistantName = "Assistant\u{2019}s name"
    public static let homeToggle = "Control your home by voice"
    /// Mac only.
    public static let showInDock = "Show in the Dock"
    public static let callShortcut = "Call shortcut"
    /// The picker's "Nowhere" choice, said short for the folded row.
    public static let thisMacOnly = "This Mac only"

    /// The folded row holding where finished work goes and, when Hermes has Home Assistant,
    /// home control. It names what's inside, so nothing in it is hidden by a vague label.
    public static func moreTitle(homeOffered: Bool) -> String {
        homeOffered ? "Finished work and home control" : "Where finished work goes"
    }

    /// What the folded row has picked, shown while it's closed: these act for the user when they
    /// press Start a call, so they're always on screen. `destination` is the label of where
    /// finished work goes (nil: this Mac only); `homeOn` is nil when home control isn't offered.
    public static func moreSummary(destination: String?, homeOn: Bool?) -> String {
        let work = destination ?? thisMacOnly
        guard let homeOn else { return work }
        return work + ", home control " + (homeOn ? "on" : "off")
    }

    /// The label the delivery picker shows for `target`: the destination's name, or the raw target
    /// when the server no longer lists it. nil means nowhere (this Mac only).
    public static func destinationLabel(target: String?, destinations: [Destination]) -> String? {
        guard let target else { return nil }
        return destinations.first { $0.target == target }?.label ?? target
    }

    /// The home toggle's second line.
    public static func homeDetail(devices n: Int) -> String {
        let devices = n == 0 ? "No devices picked yet" : "\(n) device\(n == 1 ? "" : "s")"
        return "\(devices) \u{00B7} choose which in Settings \u{203A} Home"
    }
}
