import Foundation

// Local-only microphone controls: a configurable in-call mute shortcut with
// tap-to-toggle / hold-for-momentary semantics, plus transcript cleanup.
// Nothing here touches the api, the provider, or Hermes work.

// MARK: - Shortcut

/// A key + modifier combination expressed with Carbon virtual key codes and
/// Carbon modifier bits, parsed from a human string such as `ctrl+opt+m`.
public struct KeyShortcut: Equatable, Sendable {
    public static let command: UInt32 = 0x0100   // cmdKey
    public static let shift: UInt32 = 0x0200     // shiftKey
    public static let option: UInt32 = 0x0800    // optionKey
    public static let control: UInt32 = 0x1000   // controlKey

    public var keyCode: UInt32
    public var modifiers: UInt32
    public var keyName: String

    public init(keyCode: UInt32, modifiers: UInt32, keyName: String) {
        self.keyCode = keyCode; self.modifiers = modifiers; self.keyName = keyName
    }

    /// Existing call start/end shortcut (unchanged): Control–Option–Space.
    public static let call = KeyShortcut(keyCode: 0x31, modifiers: control | option, keyName: "Space")
    /// Default in-call mute shortcut: Control–Option–M. Registered only while a
    /// call is open, so it never occupies the key combination while idle.
    public static let defaultMute = KeyShortcut(keyCode: 0x2E, modifiers: control | option, keyName: "M")
    /// Default Pause/Resume shortcut: Control–Option–P, registered while a call
    /// is open or paused.
    public static let defaultPause = KeyShortcut(keyCode: 0x23, modifiers: control | option, keyName: "P")
    /// Suggested listening mode shortcut: Control–Option–L. Off until the user sets one (Settings ›
    /// Shortcuts); Delete in the recorder picks this.
    public static let suggestedListening = KeyShortcut(keyCode: 0x25, modifiers: control | option, keyName: "L")

    /// ANSI virtual key codes (kVK_ANSI_*), cross-checked against Carbon in tests.
    public static let keyCodes: [String: UInt32] = [
        "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07,
        "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10,
        "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19,
        "7": 0x1A, "8": 0x1C, "0": 0x1D, "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23, "l": 0x25,
        "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E, "space": 0x31,
        "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
        "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B, "f15": 0x71,
    ]

    public enum ParseError: Error, Equatable {
        case empty, unknownToken(String), missingKey, multipleKeys, tooFewModifiers, reservedForCall
    }

    /// Parse `ctrl+opt+m`, `control-option-command-f5`, `⌃⌥M`, etc.
    /// Rejects modifier-only bindings, single-modifier bindings (except F-keys
    /// with at least one modifier), and the call shortcut itself.
    public static func parse(_ raw: String, reserved: KeyShortcut? = KeyShortcut.call) -> Result<KeyShortcut, ParseError> {
        var text = raw.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return .failure(.empty) }
        for (symbol, word) in [("⌃", "ctrl+"), ("⌥", "opt+"), ("⌘", "cmd+"), ("⇧", "shift+")] {
            text = text.replacingOccurrences(of: symbol, with: word)
        }
        let tokens = text.split(whereSeparator: { $0 == "+" || $0 == "-" || $0 == " " }).map(String.init)
        var modifiers: UInt32 = 0
        var key: (String, UInt32)?
        for token in tokens {
            switch token {
            case "ctrl", "control", "ctl": modifiers |= control
            case "opt", "option", "alt": modifiers |= option
            case "cmd", "command": modifiers |= command
            case "shift": modifiers |= shift
            default:
                guard let code = keyCodes[token] else { return .failure(.unknownToken(token)) }
                if key != nil { return .failure(.multipleKeys) }
                key = (token, code)
            }
        }
        guard let (name, code) = key else { return .failure(.missingKey) }
        let primary = [control, option, command].filter { modifiers & $0 != 0 }.count
        let functionKey = name.hasPrefix("f") && name.count > 1
        let modifierCount = primary + (modifiers & shift != 0 ? 1 : 0)
        guard primary >= 1, functionKey || modifierCount >= 2 else { return .failure(.tooFewModifiers) }
        let shortcut = KeyShortcut(keyCode: code, modifiers: modifiers,
                                   keyName: name == "space" ? "Space" : name.uppercased())
        if let reserved, shortcut.keyCode == reserved.keyCode && shortcut.modifiers == reserved.modifiers { return .failure(.reservedForCall) }
        return .success(shortcut)
    }

    /// Stored form, e.g. `ctrl+opt+space`; `parse(storage, reserved: nil)` round-trips it.
    public var storage: String {
        var parts: [String] = []
        if modifiers & Self.control != 0 { parts.append("ctrl") }
        if modifiers & Self.option != 0 { parts.append("opt") }
        if modifiers & Self.shift != 0 { parts.append("shift") }
        if modifiers & Self.command != 0 { parts.append("cmd") }
        return (parts + [keyName.lowercased()]).joined(separator: "+")
    }

    /// A shortcut recorded from a key press (virtual key code + Carbon modifier bits),
    /// validated with the same rules as `parse`.
    public static func recorded(keyCode: UInt32, modifiers: UInt32, reserved: KeyShortcut? = nil) -> Result<KeyShortcut, ParseError> {
        guard let name = keyCodes.first(where: { $0.value == keyCode })?.key else { return .failure(.unknownToken("key \(keyCode)")) }
        let mods = modifiers & (control | option | command | shift)
        var words: [String] = []
        if mods & control != 0 { words.append("ctrl") }
        if mods & option != 0 { words.append("opt") }
        if mods & shift != 0 { words.append("shift") }
        if mods & command != 0 { words.append("cmd") }
        return parse((words + [name]).joined(separator: "+"), reserved: reserved)
    }

    /// Mac-style glyph string, e.g. `⌃⌥M`.
    public var display: String {
        var out = ""
        if modifiers & Self.control != 0 { out += "⌃" }
        if modifiers & Self.option != 0 { out += "⌥" }
        if modifiers & Self.shift != 0 { out += "⇧" }
        if modifiers & Self.command != 0 { out += "⌘" }
        return out + keyName
    }
}

// MARK: - Mute key gesture

/// Tap = toggle and stay; hold = momentary. Pressing always flips the mic
/// immediately (so the first spoken word is not lost when talking); releasing
/// after a hold of at least `holdThreshold` flips it back. So when muted,
/// holding the key is push-to-talk; when live, holding it is a cough button.
public struct MuteKeyGesture: Equatable, Sendable {
    public static let defaultHoldThreshold: TimeInterval = 0.35
    public let holdThreshold: TimeInterval
    public private(set) var pressedAt: Date?
    public private(set) var stateBeforePress: MicState?

    public init(holdThreshold: TimeInterval = MuteKeyGesture.defaultHoldThreshold) {
        self.holdThreshold = holdThreshold
    }

    public var isHeld: Bool { pressedAt != nil }

    /// Returns the mic state to apply on key-down, or nil for a repeat.
    public mutating func press(current: MicState, now: Date) -> MicState? {
        guard pressedAt == nil else { return nil }   // auto-repeat / duplicate down
        pressedAt = now
        stateBeforePress = current
        return current == .live ? .muted : .live
    }

    /// Returns the mic state to restore on key-up (a hold), or nil (a tap).
    public mutating func release(now: Date) -> MicState? {
        guard let start = pressedAt, let before = stateBeforePress else { return nil }
        pressedAt = nil
        stateBeforePress = nil
        return now.timeIntervalSince(start) >= holdThreshold ? before : nil
    }

    /// Forget an in-flight press (call ended, shortcut unregistered).
    public mutating func cancel() { pressedAt = nil; stateBeforePress = nil }
}

// MARK: - Transcript cleanup

/// Removes bracketed non-speech annotations such as `[clear throat]`,
/// `[cough]`, `[laughs]` while keeping every real word. An unclosed trailing
/// `[…` (a tag still streaming in) is hidden until it closes or grows too long
/// to be a tag. Whitespace left behind is collapsed; no other text changes.
public func cleanTranscript(_ text: String) -> String {
    guard text.contains("[") else { return text }
    var out = text.replacingOccurrences(of: #"\[[A-Za-z][A-Za-z '_-]{0,39}\]"#, with: " ", options: .regularExpression)
    out = out.replacingOccurrences(of: #"\[(?:[A-Za-z][A-Za-z '_-]{0,39})?$"#, with: "", options: .regularExpression)
    out = out.replacingOccurrences(of: #"[ \t]{2,}"#, with: " ", options: .regularExpression)
    out = out.replacingOccurrences(of: #" +([,.!?;:])"#, with: "$1", options: .regularExpression)
    return out.trimmingCharacters(in: .whitespaces)
}
