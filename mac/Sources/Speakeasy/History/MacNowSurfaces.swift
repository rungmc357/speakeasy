import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// Only presentation state lives here. Calls and work keep their existing owners.
@MainActor
final class MacNowPresentation: ObservableObject {
    static let shared = MacNowPresentation()
    @Published var enabled = false
    @Published var homeFrontmost = false
    var openHome: () -> Void = {}
}

struct MacNowCallControls: View {
    @ObservedObject var voice: VoicePanelModel
    var openHome: (() -> Void)?

    private var presentation: PillPresentation { present(voice.state) }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 28)).foregroundStyle(JobLook.brass)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(voice.state.connection == .paused ? "Call paused" : "On call")
                    .font(.headline)
                Text(voice.shownStatus.isEmpty ? "Your work stays here." : voice.shownStatus)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }.frame(maxWidth: .infinity, alignment: .leading)
            Button(action: voice.onToggleMic) {
                Image(systemName: presentation.micMuted ? "mic.slash.fill" : "mic.fill")
            }
            .disabled(!presentation.micEnabled)
            .help(presentation.micMuted ? "Unmute microphone" : "Mute microphone")
            .accessibilityLabel(presentation.micMuted ? "Unmute microphone" : "Mute microphone")
            Button(action: voice.onTogglePause) {
                Image(systemName: voice.state.connection == .paused ? "play.fill" : "pause.fill")
            }
            .help(voice.state.connection == .paused ? "Resume call" : "Pause call")
            .accessibilityLabel(voice.state.connection == .paused ? "Resume call" : "Pause call")
            Button(action: voice.onEnd) { Image(systemName: "phone.down.fill").foregroundStyle(.red) }
                .help("End call").accessibilityLabel("End call")
            if let openHome {
                Button(action: openHome) { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                    .help("Open Home").accessibilityLabel("Open Home")
            }
        }
        .buttonStyle(.borderless)
        .padding(14)
        .background(HistoryLook.window)
        .accessibilityIdentifier("mac-now-call-controls")
    }
}

struct MacNowHomeCall: View {
    @ObservedObject var voice: VoicePanelModel
    var body: some View {
        if voice.state.connection.isOpen && !voice.state.workOnly {
            MacNowCallControls(voice: voice)
                .clipShape(RoundedRectangle(cornerRadius: 12))
        } else {
            Button(action: voice.onStart) { Label("Start a call", systemImage: "phone.fill") }
                .buttonStyle(.borderedProminent).tint(JobLook.brass)
        }
    }
}

/// The legacy panel remains available to old servers. Home-capable servers get only the call bar.
struct MacNowVoiceSurface: View {
    @ObservedObject var voice: VoicePanelModel
    @ObservedObject var presentation = MacNowPresentation.shared
    var body: some View {
        if presentation.enabled && voice.state.connection.isOpen && !voice.state.workOnly {
            MacNowCallControls(voice: voice, openHome: presentation.openHome)
                .frame(width: 420, height: 76)
                .clipShape(RoundedRectangle(cornerRadius: 18))
        } else {
            VoicePanelView(model: voice)
        }
    }
}

struct MacNowPopover: View {
    @ObservedObject var now: MacNowModel
    @ObservedObject var voice: VoicePanelModel
    let open: (String?) -> Void
    let more: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Speakeasy").font(.headline)
                Spacer()
                Button(action: more) { Image(systemName: "ellipsis") }
                    .help("More options").accessibilityLabel("More options")
            }
            if voice.state.connection.isOpen && !voice.state.workOnly {
                MacNowCallControls(voice: voice)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
            } else {
                Button(action: voice.onStart) { Label("Start a call", systemImage: "phone.fill") }
                    .buttonStyle(.borderedProminent).tint(JobLook.brass)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text(now.home?.sentence ?? (now.failed ? "Can't reach Hermes right now." : "All quiet."))
                        .font(.callout).foregroundStyle(.secondary)
                    ForEach(MacNowLayout.menuItems(now.visible)) { item in
                        MacNowCard(item: item, now: now, focused: now.home?.call?.focus == item.id) {
                            open(item.sessionID)
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 400)
            Button("Open Home") { open(nil) }
                .buttonStyle(.bordered).frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(16).frame(width: 400)
        .background(HistoryLook.window)
        .onAppear { now.start() }
        .accessibilityIdentifier("mac-now-popover")
    }
}
