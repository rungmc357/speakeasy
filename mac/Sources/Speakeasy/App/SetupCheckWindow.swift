import AppKit
import SpeakeasyClient
import SpeakeasyCore
import SwiftUI

/// The Setup check (SetupCheck.swift): opens instead of a call when Start a call is pressed before
/// setup is finished. One row per requirement, green when ready, red with one fix button when not,
/// grey for optional ones; Start a call turns on once nothing red is left.
struct SetupCheckView: View {
    @ObservedObject var model: SetupCheckModel
    /// Opens the Sign in to ChatGPT sheet (the window owner shows it).
    var onCodexSignIn: () -> Void
    var onOpenSettings: (String) -> Void
    var onStartCall: () -> Void
    var onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(SetupCheckCopy.title).font(.title2.weight(.semibold))
            Text(model.ready ? SetupCheckCopy.allSet : SetupCheckCopy.intro)
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                if let error = model.error {
                    SetupCheckRowView(row: .init(id: "reach", ok: false, title: SetupCheckCopy.unreachable, detail: error),
                                      working: false, copied: false, onFix: { _ in })
                } else if let check = model.check {
                    ForEach(Array(check.rows.enumerated()), id: \.element.id) { index, row in
                        if index > 0 { Divider().padding(.leading, 40) }
                        SetupCheckRowView(row: row, working: model.working == row.id, copied: model.copiedRow == row.id,
                                          onFix: fix)
                    }
                } else {
                    HStack { ProgressView().controlSize(.small); Text(SetupCheckCopy.checking).foregroundStyle(.secondary) }
                        .frame(maxWidth: .infinity, alignment: .leading).padding(14)
                }
            }
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.05)))
            HStack {
                if !model.ready, model.check != nil {
                    Text(SetupCheckCopy.fixFirst).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Close") { onClose() }.keyboardShortcut(.cancelAction)
                Button(SetupCheckCopy.startCall) { onStartCall() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.ready)
                    .accessibilityIdentifier("setup-check-start")
            }
        }
        .padding(22)
        .frame(width: 480)
    }

    private func fix(_ row: SetupCheck.Row) {
        guard let fix = row.fix else { return }
        switch fix.kind {
        case .codexSignIn: onCodexSignIn()
        case .action: Task { await model.runAction(row) }
        case .copy:
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(fix.payload, forType: .string)
            model.markCopied(row)
        case .openSettings: onOpenSettings(fix.payload)
        case .unknown: break
        }
    }
}

struct SetupCheckRowView: View {
    let row: SetupCheck.Row
    let working: Bool
    let copied: Bool
    var onFix: (SetupCheck.Row) -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            icon.font(.system(size: 18)).frame(width: 22)
            VStack(alignment: .leading, spacing: 3) {
                Text(row.title).font(.body.weight(row.state == .needsFix ? .semibold : .regular))
                if !row.ok, !row.detail.isEmpty {
                    Text(row.detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            if let fix = row.fix, fix.actionable {
                if working {
                    ProgressView().controlSize(.small)
                } else {
                    Button { onFix(row) } label: {
                        if copied { Label("Copied", systemImage: "checkmark") } else { Text(fix.label) }
                    }
                    .accessibilityIdentifier("setup-fix-\(row.id)")
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
    }

    @ViewBuilder private var icon: some View {
        switch row.state {
        case .ready: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green).accessibilityLabel("Ready")
        case .needsFix: Image(systemName: "xmark.circle.fill").foregroundStyle(.red).accessibilityLabel("Needs fixing")
        case .optional: Image(systemName: "circle.dashed").foregroundStyle(.secondary).accessibilityLabel("Optional")
        }
    }
}

enum SetupCheckCopy {
    static let title = "Setup check"
    static let intro = "A few things need fixing before your first call. Each one has a button that does it, or copies what to send your Hermes."
    static let allSet = "Everything a call needs is ready."
    static let checking = "Checking your Hermes…"
    static let unreachable = "Can't reach your Hermes"
    static let fixFirst = "Fix the red items first"
    static let startCall = "Start a call"
}

/// The Setup check window on the Mac, with its own Sign in to ChatGPT sheet.
@MainActor
final class SetupCheckWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let app: AppModel
    private var model: SetupCheckModel?
    var onStartCall: () -> Void = {}

    init(app: AppModel) { self.app = app }

    func show() {
        if window == nil {
            let model = SetupCheckModel(backend: app.api.map(SetupCheckModel.Backend.server) ?? .demo())
            self.model = model
            let host = NSHostingController(rootView: SetupCheckHost(model: model, onStartCall: { [weak self] in
                self?.window?.close(); self?.onStartCall()
            }, onClose: { [weak self] in self?.window?.close() }).environmentObject(app))
            let w = NSWindow(contentViewController: host)
            w.title = "Setup check"
            w.styleMask = [.titled, .closable]
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            window = w
        }
        model?.start()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        model?.stop()
        model = nil
        window = nil
        Task { await app.refresh() }
    }
}

/// The view plus the sign-in sheet, which needs the app model.
struct SetupCheckHost: View {
    @EnvironmentObject var app: AppModel
    @ObservedObject var model: SetupCheckModel
    var onStartCall: () -> Void
    var onClose: () -> Void
    @State private var signingIn = false

    var body: some View {
        SetupCheckView(model: model, onCodexSignIn: { signingIn = true },
                       onOpenSettings: { page in app.showSettings(SettingsTab(rawValue: page) ?? .voice) },
                       onStartCall: onStartCall, onClose: onClose)
            .sheet(isPresented: $signingIn) {
                CodexLoginSheet(model: app.codexLoginModel(), switchFromAPIKey: app.status?.codexOnAPIKey ?? false,
                                onAlternative: { signingIn = false; app.showSettings(.voice) },
                                onDone: { signingIn = false; Task { await app.refresh(); await model.refresh() } })
            }
    }
}

/// `Speakeasy --setup-check-snapshot <file.png> [light|dark] [fix|ready]`: the Setup check with
/// invented rows, offscreen (no server, no network), for review as an image.
enum SetupCheckSnapshot {
    @MainActor
    static func render(to path: String, appearance: String, state: String) {
        let check = state == "ready" ? SetupCheckModel.demoReadyCheck : SetupCheckModel.demoCheck
        let model = SetupCheckModel(backend: .demo(check), initial: check)
        let view = SetupCheckView(model: model, onCodexSignIn: {}, onOpenSettings: { _ in }, onStartCall: {}, onClose: {})
            .background(Color(nsColor: .windowBackgroundColor))
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(x: 0, y: 0, width: 480, height: host.fittingSize.height)
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
