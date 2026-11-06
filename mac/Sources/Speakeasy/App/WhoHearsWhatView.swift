import AppKit
import SpeakeasyCore
import SwiftUI

/// "Who hears what" on the Mac: the onboarding step's content, the Settings sheet, and the
/// Settings row. Copy comes from `WhoHearsWhat` (SpeakeasyCore), shared with iPhone/iPad.
extension WhoHearsWhat {
    /// The copy for this Mac's current voice and connection.
    @MainActor
    static func current(_ app: AppModel) -> WhoHearsWhat {
        WhoHearsWhat(voice: Voice(provider: app.status?.resolvedProvider),
                     viaCloudflare: isCloudflare(server: app.config.serverURL, endpoints: nil),
                     notifications: false,
                     listening: UserDefaults.standard.bool(forKey: Prefs.listeningBeta),
                     device: "Mac")
    }
}

/// The screen's text, without buttons (the onboarding step and the sheet add their own).
struct WhoHearsWhatBody: View {
    var copy: WhoHearsWhat
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(WhoHearsWhat.intro)
            (Text(copy.callLead).fontWeight(.semibold) + Text(" ") + Text(copy.callBody))
            ForEach(copy.details, id: \.self) { Text($0) }
            if !copy.extras.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(copy.extras, id: \.self) { line in
                        Text(line).font(.callout).foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 2)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .textSelection(.enabled)
        .accessibilityIdentifier("who-hears-what")
    }
}

/// Settings › Voice (top) and Settings › About: the row that opens the screen.
struct WhoHearsWhatRow: View {
    @EnvironmentObject var app: AppModel
    /// The voice picked in an unsaved Settings draft, so the line follows the picker at once.
    var provider: VoiceProvider? = nil
    /// About tab: a centered link instead of a form row.
    var link = false
    @State private var open = false

    private var copy: WhoHearsWhat {
        var c = WhoHearsWhat.current(app)
        if let provider { c.voice = .init(provider: provider) }
        return c
    }

    var body: some View {
        Group {
            if link {
                Button(WhoHearsWhat.title) { open = true }.buttonStyle(.link).font(.callout)
            } else {
                Button { open = true } label: {
                    HStack(alignment: .center, spacing: 10) {
                        Image(systemName: "hand.raised.fill").foregroundStyle(Color.accentColor).frame(width: 20)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(WhoHearsWhat.title).foregroundStyle(.primary)
                            Text(copy.settingsSubtitle)
                                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .accessibilityIdentifier(link ? "about-who-hears-what" : "settings-who-hears-what")
        .sheet(isPresented: $open) { WhoHearsWhatSheet(copy: copy) }
    }
}

struct WhoHearsWhatSheet: View {
    var copy: WhoHearsWhat
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(WhoHearsWhat.title).font(.title2.weight(.semibold))
            ScrollView { WhoHearsWhatBody(copy: copy).frame(maxWidth: .infinity, alignment: .leading) }
            HStack {
                Text(WhoHearsWhat.footnote).font(.callout).foregroundStyle(.secondary)
                Spacer()
                Button(WhoHearsWhat.button) { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 480, height: 500)
    }
}

/// `Speakeasy --privacy-shots <dir>`: the onboarding step and the Settings sheet with invented
/// settings, light and dark, as PNGs. Development only; no server.
@MainActor
enum WhoHearsWhatSnapshot {
    static func run(dir: String) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let chatgpt = WhoHearsWhat(voice: .openai)
        let grokAll = WhoHearsWhat(voice: .xai, viaCloudflare: true, listening: true)
        let local = WhoHearsWhat(voice: .local)
        var shots: [(String, AnyView, NSSize)] = []
        for look in ["light", "dark"] {
            shots.append(("mac-privacy-onboarding-\(look)", AnyView(PrivacyStepPreview(copy: chatgpt)), NSSize(width: 520, height: 460)))
            shots.append(("mac-privacy-sheet-grok-extras-\(look)", AnyView(WhoHearsWhatSheet(copy: grokAll)), NSSize(width: 480, height: 500)))
            shots.append(("mac-privacy-sheet-private-\(look)", AnyView(WhoHearsWhatSheet(copy: local)), NSSize(width: 480, height: 500)))
            shots.append(("mac-privacy-settings-row-\(look)", AnyView(Form { Section { RowPreview(copy: chatgpt) }; Section { RowPreview(copy: grokAll) } }
                .formStyle(.grouped)), NSSize(width: 560, height: 200)))
        }
        var windows: [(NSWindow, String)] = []
        for (name, view, size) in shots {
            let host = NSHostingController(rootView: view.frame(width: size.width, height: size.height)
                .background(Color(nsColor: .windowBackgroundColor)))
            let window = NSWindow(contentViewController: host)
            window.appearance = NSAppearance(named: name.hasSuffix("dark") ? .darkAqua : .aqua)
            window.setContentSize(size)
            window.makeKeyAndOrderFront(nil)
            windows.append((window, name))
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            for (window, name) in windows {
                guard let view = window.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { continue }
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
                print("snapshot \(dir)/\(name).png")
            }
            exit(0)
        }
    }

    private struct RowPreview: View {
        var copy: WhoHearsWhat
        var body: some View {
            HStack(spacing: 10) {
                Image(systemName: "hand.raised.fill").foregroundStyle(Color.accentColor).frame(width: 20)
                VStack(alignment: .leading, spacing: 2) {
                    Text(WhoHearsWhat.title)
                    Text(copy.settingsSubtitle).font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
            }
        }
    }

    /// The onboarding step as it renders (same layout as `PrivacyStep`, without the flow).
    private struct PrivacyStepPreview: View {
        var copy: WhoHearsWhat
        var body: some View {
            PrivacyStepLayout(copy: copy, back: {}, next: {}).padding(24)
        }
    }
}
