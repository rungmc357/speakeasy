import AppKit
import SpeakeasyClient
import SpeakeasyCore
import SwiftUI

/// "Sign in to ChatGPT": the code Codex on the Hermes computer is waiting for, a link to open, and a
/// status that flips to Signed in ✓ by itself (CodexLoginModel polls the server).
struct CodexLoginSheet: View {
    @StateObject private var model: CodexLoginModel
    let switchFromAPIKey: Bool
    var onAlternative: () -> Void
    var onDone: () -> Void
    @Environment(\.openURL) private var openURL

    init(model: @autoclosure @escaping () -> CodexLoginModel, switchFromAPIKey: Bool = false,
         onAlternative: @escaping () -> Void, onDone: @escaping () -> Void) {
        _model = StateObject(wrappedValue: model())
        self.switchFromAPIKey = switchFromAPIKey
        self.onAlternative = onAlternative
        self.onDone = onDone
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(CodexLoginCopy.title).font(.title2.weight(.semibold))
            Text(model.state.phase == .signedIn ? CodexLoginCopy.done
                 : switchFromAPIKey ? CodexLoginCopy.apiKeyNote : CodexLoginCopy.intro)
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            codeBox
            HStack(spacing: 8) {
                statusIcon
                Text(model.state.statusLine)
                    .font(.callout.weight(model.state.phase == .signedIn ? .semibold : .regular))
                    .foregroundStyle(model.state.phase == .signedIn ? Color.green : Color.primary)
                    .accessibilityIdentifier("codex-login-status")
                Spacer()
                if let line = model.state.expiryLine() {
                    Text(line).font(.caption).foregroundStyle(.secondary)
                }
            }
            HStack {
                if model.state.phase == .signedIn {
                    Spacer()
                    Button("Done") { onDone() }.keyboardShortcut(.defaultAction).controlSize(.large)
                } else {
                    Button(CodexLoginCopy.alternative) { model.close(); onAlternative() }
                        .buttonStyle(.link)
                    Spacer()
                    Button("Cancel") { model.close(); onDone() }.keyboardShortcut(.cancelAction)
                    if [.expired, .failed, .cancelled].contains(model.state.phase) {
                        Button("Get a new code") { model.start(switchFromAPIKey: switchFromAPIKey) }
                            .keyboardShortcut(.defaultAction)
                    } else {
                        Button(CodexLoginCopy.open) { if let url = model.state.verificationURL { openURL(url) } }
                            .keyboardShortcut(.defaultAction)
                            .disabled(model.state.verificationURL == nil)
                    }
                }
            }
        }
        .padding(22)
        .frame(width: 440)
        .onAppear { if model.state.phase == .idle { model.start(switchFromAPIKey: switchFromAPIKey) } }
        .onDisappear { model.close() }
    }

    @ViewBuilder private var codeBox: some View {
        HStack {
            if let code = model.state.userCode, model.state.phase == .pending {
                Text(code)
                    .font(.system(size: 32, weight: .semibold, design: .monospaced))
                    .tracking(2).textSelection(.enabled)
                    .accessibilityLabel("Code \(code.map(String.init).joined(separator: " "))")
                    .accessibilityIdentifier("codex-login-code")
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    model.markCopied()
                } label: { Label(model.copied ? "Copied" : CodexLoginCopy.copy, systemImage: model.copied ? "checkmark" : "doc.on.doc") }
            } else if model.state.phase == .signedIn {
                Label("Voice can use your ChatGPT plan now.", systemImage: "checkmark.seal.fill")
                    .font(.title3.weight(.medium)).foregroundStyle(.green)
                Spacer()
            } else {
                Text(model.state.isWaiting || model.state.phase == .idle ? "· · · · - · · · · ·" : "—")
                    .font(.system(size: 32, weight: .semibold, design: .monospaced)).foregroundStyle(.tertiary)
                Spacer()
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.06)))
    }

    @ViewBuilder private var statusIcon: some View {
        switch model.state.phase {
        case .signedIn: EmptyView()
        case .expired, .failed, .cancelled: Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
        default: ProgressView().controlSize(.small)
        }
    }
}

/// The button Settings › Voice and the setup flow show when Codex isn't signed in (plugins with
/// `codex_login`); older plugins keep their text.
struct CodexSignInButton: View {
    @EnvironmentObject var app: AppModel
    var onAlternative: () -> Void = {}

    var body: some View {
        if let status = app.status, status.offersCodexSignIn {
            Button(status.codexOnAPIKey ? CodexLoginCopy.switchButton : CodexLoginCopy.button) { app.showingCodexLogin = true }
                .accessibilityIdentifier("codex-sign-in")
                .sheet(isPresented: $app.showingCodexLogin) {
                    CodexLoginSheet(model: app.codexLoginModel(), switchFromAPIKey: status.codexOnAPIKey,
                                    onAlternative: { app.showingCodexLogin = false; onAlternative() },
                                    onDone: { app.showingCodexLogin = false; Task { await app.refresh() } })
                }
        }
    }
}

extension AppModel {
    @MainActor func codexLoginModel() -> CodexLoginModel {
        let backend = api.map(CodexLoginModel.Backend.server) ?? .demo()
        let model = CodexLoginModel(backend: backend)
        model.onSignedIn = { [weak self] in Task { await self?.refresh() } }
        return model
    }
}

/// `Speakeasy --codex-login-snapshot <file.png> [light|dark] [pending|signed_in]`: the sheet,
/// offscreen with the demo backend (no server, no network), for review as an image.
enum CodexLoginSnapshot {
    @MainActor
    static func render(to path: String, appearance: String, phase: String) {
        let model = CodexLoginModel(backend: .demo(signInAfterPolls: phase == "signed_in" ? 1 : 10_000), pollInterval: 0.2)
        model.start()
        RunLoop.main.run(until: Date().addingTimeInterval(phase == "signed_in" ? 1.2 : 0.6))
        let view = CodexLoginSheet(model: model, onAlternative: {}, onDone: {})
            .background(Color(nsColor: .windowBackgroundColor))
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(x: 0, y: 0, width: 440, height: host.fittingSize.height)
        let window = NSWindow(contentRect: host.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: appearance == "light" ? .aqua : .darkAqua)
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        host.frame.size.height = host.fittingSize.height
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
        try? png.write(to: URL(fileURLWithPath: path))
        print("snapshot \(path)")
        exit(0)
    }
}
