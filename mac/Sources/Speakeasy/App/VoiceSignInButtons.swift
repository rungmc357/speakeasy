import AppKit
import SpeakeasyCore
import SwiftUI

/// Codex isn't signed in and this plugin can't sign it in from the app yet: the update brings
/// Sign in to ChatGPT. Update starts the update as a job (plugins that run jobs), else the request
/// for Hermes is copied. Nothing when no update is offered.
struct PluginUpdateForSignInButton: View {
    @EnvironmentObject var app: AppModel
    @State private var copied = false
    /// `--settings-voice-snapshot` only: show Update without a paired server.
    static var previewOffered = false

    var body: some View {
        if app.pluginUpdateAvailable != nil || Self.previewOffered {
            if app.status?.capabilities?.startJobs == true {
                Button(CodexLoginCopy.update) { app.startPluginUpdate?() }
                    .accessibilityIdentifier("voice-plugin-update")
            } else {
                Button(copied ? "Copied" : "Copy update request") { app.copyPluginUpdateRequest(); copied = true }
                    .accessibilityIdentifier("voice-plugin-update")
            }
        }
    }
}

/// Grok Voice isn't signed in: copy the request that asks Hermes to sign in to xAI (X / SuperGrok).
/// The app can't start that sign-in, so the agent does it and sends the link.
struct GrokSignInRequestButton: View {
    @State private var copied = false

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            Button(copied ? "Copied" : GrokSignInCopy.copyButton) {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(GrokSignInCopy.request(device: "Mac"), forType: .string)
                copied = true
            }
            .accessibilityIdentifier("grok-sign-in-request")
            if copied {
                Text(GrokSignInCopy.copied).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// `--settings-voice-snapshot <dir>`: the Voice tab's sign-in rows with invented statuses (an older
/// plugin's Codex row with Update, a signed-out Grok row) as PNGs, in the current appearance
/// (`SPEAKEASY_APPEARANCE=light|dark`). Development only; no server.
@MainActor
enum VoiceSignInSnapshot {
    static func run(app: AppModel, dir: String) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        if let look = ProcessInfo.processInfo.environment["SPEAKEASY_APPEARANCE"] {
            NSApp.appearance = NSAppearance(named: look == "light" ? .aqua : .darkAqua)
        }
        PluginUpdateForSignInButton.previewOffered = true
        let look = ProcessInfo.processInfo.environment["SPEAKEASY_APPEARANCE"] ?? "light"
        func json(_ s: String) -> ServerStatus? { try? JSONDecoder().decode(ServerStatus.self, from: Data(s.utf8)) }
        let shots: [(String, VoiceProvider, ServerStatus?)] = [
            ("mac-voice-codex-old-\(look)", .codex,
             json(#"{"provider":"codex","codex_signed_in":false,"codex_sign_in":"none","capabilities":{"start_jobs":true}}"#)),
            ("mac-voice-grok-\(look)", .grok,
             json(#"{"provider":"grok","grok_signed_in":false,"grok_ready":false,"grok_available":true,"beta":true}"#)),
        ]
        Task { @MainActor in
            for (name, provider, status) in shots {
                app.status = status
                var draft = ServerSettings(); draft.voice = .init(provider: provider.rawValue)
                let host = NSHostingController(rootView: Form { VoiceProviderStatus(draft: .constant(draft)) }
                    .formStyle(.grouped).environmentObject(app).frame(width: 560, height: 170))
                let window = NSWindow(contentViewController: host)
                window.setContentSize(NSSize(width: 560, height: 170))
                window.makeKeyAndOrderFront(nil)
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                if let view = window.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
                    print("snapshot \(dir)/\(name).png")
                }
                window.orderOut(nil)
            }
            exit(0)
        }
    }
}
