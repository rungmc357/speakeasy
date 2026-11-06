import Foundation

/// The OpenAI API key for the API-key voice, added from the app (Settings › Voice › Add key) on
/// plugins with `capabilities.openai_key`: the secure key box sends it to `POST /voice/openai-key`,
/// which saves it on the Hermes computer. Nobody is sent to a terminal.
public enum OpenAIKeyCopy {
    public static let notSet = "No key added yet"
    public static let set = "Key added on your Hermes"
    public static let addButton = "Add OpenAI API key"
    public static let replaceButton = "Replace key"
    public static let saved = "Key saved. Calls use it from now on."
    /// Footer under the button.
    public static let footer = "Calls bill that OpenAI account per minute. The key is saved only on your Hermes computer, never on this device."
    /// Older plugins can't take the key from the app: they get the plugin update first (Settings shows it).
    public static let oldPluginNote = "No key added yet — update Speakeasy on your Hermes to add it here"

    /// The setup checklist's line for a missing key.
    public static func fix(canAddKey: Bool) -> String {
        canAddKey ? "Add your OpenAI API key in Settings › Voice"
                  : "Update Speakeasy on your Hermes, then add your OpenAI API key in Settings › Voice"
    }
}

public extension ServerStatus {
    /// The server takes the OpenAI API key from the app (`capabilities.openai_key`).
    var canAddOpenAIKey: Bool { capabilities?.openaiKey == true }
}

public extension SecureInputRequest {
    /// The env var the plugin saves the key as (`settings.OPENAI_KEY_NAME`).
    static let openAIKeyEnvVar = "SPEAKEASY_OPENAI_API_KEY"

    /// The secure key box for the OpenAI API key, opened by the user (no countdown, no "needs you").
    static func openAIKey(replacing: Bool = false) -> SecureInputRequest {
        SecureInputRequest(
            id: "sec_local_openai_api_key_0", kind: .secret, job: "", envVar: openAIKeyEnvVar,
            label: "OpenAI API key", prompt: "OpenAI API key",
            help: replacing ? "Paste a new key to replace the one on your Hermes. You can create one at platform.openai.com/api-keys."
                            : "Paste a key from platform.openai.com/api-keys. Calls then use GPT-Live through the OpenAI API and bill that account per minute.",
            storedWhere: "Saved only on your Hermes computer, in its private settings. The AI never sees it, and it never stays on this device.",
            expiresAt: .distantFuture, userStarted: true)
    }

    /// Rough check before sending (the plugin checks again): OpenAI keys start with `sk-`.
    static func looksLikeOpenAIKey(_ value: String) -> Bool {
        let v = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return v.hasPrefix("sk-") && v.count >= 20 && !v.contains(where: { $0.isWhitespace })
    }
}
