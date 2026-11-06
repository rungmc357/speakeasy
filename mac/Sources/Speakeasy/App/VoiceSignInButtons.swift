import AppKit
import SpeakeasyCore
import SwiftUI

/// Codex isn't signed in and this plugin can't sign it in from the app yet: the update brings
/// Sign in to ChatGPT. Update starts the update as a job (plugins that run jobs), else the request
/// for Hermes is copied. Nothing when no update is offered.
struct PluginUpdateForSignInButton: View {
    @EnvironmentObject var app: AppModel
    @State private var copied = false

    var body: some View {
        if app.pluginUpdateAvailable != nil {
            if app.status?.capabilities?.startJobs == true, let start = app.startPluginUpdate {
                Button(CodexLoginCopy.update) { start() }
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
