import Foundation

/// One selectable voice: its provider name, a plain description of how it sounds, and whether a
/// bundled preview clip exists (`VoiceSamples/<id>.m4a` in the app's resources).
public struct VoiceOption: Equatable, Sendable, Identifiable, Hashable {
    public let id: String
    public let summary: String
    public let hasPreview: Bool
    public var name: String { id.prefix(1).uppercased() + id.dropFirst() }
    public init(id: String, summary: String, hasPreview: Bool) {
        self.id = id; self.summary = summary; self.hasPreview = hasPreview
    }
}

/// Voices each provider accepts. They differ: the ChatGPT-sign-in (Codex) voice model rejects the
/// API voice names and vice versa, so the picker offers only the list for the selected provider.
public enum VoiceCatalog {
    /// Descriptions come from the preview clips (measured pitch and pace for the same sentence),
    /// ordered low to high so people can browse by sound rather than by a label.
    public static let codex: [VoiceOption] = [
        .init(id: "arbor", summary: "Deepest and steadiest; unhurried.", hasPreview: true),
        .init(id: "ember", summary: "Low and quick.", hasPreview: true),
        .init(id: "cove", summary: "Low-mid, even pace. The default.", hasPreview: true),
        .init(id: "spruce", summary: "Low-mid and steady; the slowest pace.", hasPreview: true),
        .init(id: "breeze", summary: "Mid-range and quick.", hasPreview: true),
        .init(id: "sol", summary: "Mid-range, even pace.", hasPreview: true),
        .init(id: "vale", summary: "Mid-high and expressive; unhurried.", hasPreview: true),
        .init(id: "juniper", summary: "High and the most animated; unhurried.", hasPreview: true),
        .init(id: "maple", summary: "Highest and quickest.", hasPreview: true),
        // Added with GPT-Live 1.1 (Codex 0.163). Previews and pitch notes not recorded yet.
        .init(id: "bubbie", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "aube", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "rivage", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "haetsal", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "hanul", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "himari", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "miko", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "rindo", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "sonna", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "leher", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "neer", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "rio", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "tinta", summary: "New with GPT-Live 1.1.", hasPreview: false),
        .init(id: "viola", summary: "New with GPT-Live 1.1.", hasPreview: false),
    ]

    /// API-key voices. No previews are bundled for these yet.
    public static let openai: [VoiceOption] = ["alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse"]
        .map { VoiceOption(id: $0, summary: "", hasPreview: false) }

    /// Grok Voice via your X/SuperGrok sign-in. xAI's voice names, as its API spells them.
    public static let grok: [VoiceOption] = ["Ara", "Rex", "Sal", "Eve", "Leo"]
        .map { VoiceOption(id: $0, summary: "", hasPreview: false) }

    public static func voices(for provider: VoiceProvider) -> [VoiceOption] {
        switch provider {
        case .codex: return codex
        case .openai: return openai
        case .grok: return grok
        }
    }

    /// The voice a call uses when none is chosen (mirrors the server's defaults).
    public static func defaultVoice(for provider: VoiceProvider) -> String {
        switch provider {
        case .codex: return "cove"
        case .openai: return "marin"
        case .grok: return "Ara"
        }
    }

    public static func option(_ id: String, provider: VoiceProvider) -> VoiceOption? {
        voices(for: provider).first { $0.id == id }
    }

    /// A saved voice that the provider accepts; otherwise nil, meaning "use the provider's default".
    public static func compatible(_ voice: String?, provider: VoiceProvider) -> String? {
        guard let voice, !voice.isEmpty, option(voice, provider: provider) != nil else { return nil }
        return voice
    }
}
