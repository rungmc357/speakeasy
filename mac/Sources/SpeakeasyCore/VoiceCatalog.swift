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
        .init(id: "rivage", summary: "The deepest; calm and unhurried.", hasPreview: true),
        .init(id: "hanul", summary: "Deep and very level; the flattest delivery.", hasPreview: true),
        .init(id: "rio", summary: "Deep and brisk.", hasPreview: true),
        .init(id: "arbor", summary: "Deep and steady; unhurried.", hasPreview: true),
        .init(id: "miko", summary: "Low and brisk.", hasPreview: true),
        .init(id: "ember", summary: "Low and quick.", hasPreview: true),
        .init(id: "neer", summary: "Low and level, even pace.", hasPreview: true),
        .init(id: "cove", summary: "Low-mid, even pace. The default.", hasPreview: true),
        .init(id: "spruce", summary: "Low-mid and steady; slow pace.", hasPreview: true),
        .init(id: "rindo", summary: "Low-mid with lots of lift; even pace.", hasPreview: true),
        .init(id: "breeze", summary: "Mid-range and quick.", hasPreview: true),
        .init(id: "sol", summary: "Mid-range, even pace.", hasPreview: true),
        .init(id: "haetsal", summary: "Mid-high, even pace.", hasPreview: true),
        .init(id: "leher", summary: "Mid-high and lively; even pace.", hasPreview: true),
        .init(id: "tinta", summary: "Mid-high and unhurried.", hasPreview: true),
        .init(id: "vale", summary: "Mid-high and expressive; unhurried.", hasPreview: true),
        .init(id: "aube", summary: "High and smooth; measured and even.", hasPreview: true),
        .init(id: "sonna", summary: "High and very expressive.", hasPreview: true),
        .init(id: "himari", summary: "High and bright; even pace.", hasPreview: true),
        .init(id: "bubbie", summary: "High and the most animated; the slowest pace.", hasPreview: true),
        .init(id: "juniper", summary: "High and very animated; unhurried.", hasPreview: true),
        .init(id: "maple", summary: "Very high and the quickest.", hasPreview: true),
        .init(id: "viola", summary: "The highest; even pace.", hasPreview: true),
    ]

    /// API-key voices. No previews are bundled for these yet.
    public static let openai: [VoiceOption] = ["alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse"]
        .map { VoiceOption(id: $0, summary: "", hasPreview: false) }

    public static func voices(for provider: VoiceProvider) -> [VoiceOption] {
        provider == .codex ? codex : openai
    }

    /// The voice a call uses when none is chosen (mirrors the server's defaults).
    public static func defaultVoice(for provider: VoiceProvider) -> String {
        provider == .codex ? "cove" : "marin"
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
