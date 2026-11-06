import SwiftUI
import SpeakeasyClient
import SpeakeasyCore

/// Add key (Settings › Voice and setup): opens the secure key box for the OpenAI API key and sends it
/// to the Hermes computer (`POST /voice/openai-key`). Shown only on plugins that take the key from the
/// app (`capabilities.openai_key`). `useAfterSaving` also switches calls to the API-key voice (setup).
struct OpenAIKeyButton: View {
    @EnvironmentObject var app: AppModel
    var useAfterSaving = false
    @State private var showing = false

    var body: some View {
        if let status = app.status, status.canAddOpenAIKey {
            let replacing = status.apiKeySet == true
            Button(replacing ? OpenAIKeyCopy.replaceButton : OpenAIKeyCopy.addButton) { showing = true }
                .accessibilityIdentifier("openai-key-add")
                .sheet(isPresented: $showing) {
                    SecureInputCard(request: .openAIKey(replacing: replacing),
                                    onSubmit: { await save($0) },
                                    onSkip: { showing = false })
                        .frame(width: 480).fixedSize(horizontal: false, vertical: true)
                }
        }
    }

    /// nil = saved (the sheet closes), else the line to show on the card.
    private func save(_ value: String) async -> String? {
        guard SecureInputRequest.looksLikeOpenAIKey(value) else {
            return "That doesn't look like an OpenAI API key. They start with sk-."
        }
        guard let api = app.api else { return "Not connected to your Hermes." }
        do {
            try await api.saveOpenAIKey(value)
        } catch let error as ServerClient.HTTPError {
            return error.message
        } catch {
            return "Couldn't reach your Hermes. Try again."
        }
        if useAfterSaving {
            var s = app.settings
            var v = s.voice ?? .init()
            v.provider = VoiceProvider.openai.rawValue
            v.voice = VoiceCatalog.compatible(v.voice, provider: .openai)
            s.voice = v
            _ = await app.save(s)
        }
        await app.refresh()
        showing = false
        return nil
    }
}
