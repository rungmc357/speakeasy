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
    /// When the open call began (set by the app when the call opens), for the call bar's timer.
    @Published var callStartedAt: Date?
    var openHome: () -> Void = {}
}

struct MacNowCallControls: View {
    @ObservedObject var voice: VoicePanelModel
    var openHome: (() -> Void)?
    /// The floating call bar (design §4.3): 320 × 56 with orb, state word, caption, timer, mute and end.
    var floating = false
    @ObservedObject private var presentationState = MacNowPresentation.shared

    private var presentation: PillPresentation { present(voice.state) }
    private var paused: Bool { voice.state.connection == .paused }

    var body: some View {
        HStack(spacing: floating ? 8 : 12) {
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: floating ? 26 : 28)).foregroundStyle(JobLook.brass)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: floating ? 1 : 3) {
                Text(paused ? "Call paused" : "On call")
                    .font(floating ? .subheadline.weight(.semibold) : .headline)
                Text(voice.shownStatus.isEmpty ? "Your work stays here." : voice.shownStatus)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture { openHome?() }
            if let since = presentationState.callStartedAt {
                TimelineView(.periodic(from: since, by: 1)) { ctx in
                    Text(MacNowLayout.callClock(seconds: ctx.date.timeIntervalSince(since)))
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                .accessibilityLabel("Call time")
            }
            Button(action: voice.onToggleMic) {
                Image(systemName: presentation.micMuted ? "mic.slash.fill" : "mic.fill")
            }
            .disabled(!presentation.micEnabled)
            .help(presentation.micMuted ? "Unmute microphone" : "Mute microphone")
            .accessibilityLabel(presentation.micMuted ? "Unmute microphone" : "Mute microphone")
            if !floating {
                Button(action: voice.onTogglePause) {
                    Image(systemName: paused ? "play.fill" : "pause.fill")
                }
                .help(paused ? "Resume call" : "Pause call")
                .accessibilityLabel(paused ? "Resume call" : "Pause call")
            }
            Button(action: voice.onEnd) { Image(systemName: "phone.down.fill").foregroundStyle(.red) }
                .help("End call").accessibilityLabel("End call")
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, floating ? 12 : 14).padding(.vertical, floating ? 8 : 14)
        .background(floating ? AnyShapeStyle(.ultraThinMaterial) : AnyShapeStyle(HistoryLook.window))
        .background(JobLook.brass.opacity(floating ? 0.08 : 0))
        .help(openHome == nil ? "" : "Open Speakeasy")
        .accessibilityIdentifier(floating ? "mac-now-call-bar" : "mac-now-call-controls")
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
            MacNowCallControls(voice: voice, openHome: presentation.openHome, floating: true)
                .frame(width: MacNowLayout.callBarSize.width, height: MacNowLayout.callBarSize.height)
                .clipShape(RoundedRectangle(cornerRadius: 14))
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
    private var onCall: Bool { voice.state.connection.isOpen && !voice.state.workOnly }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Speakeasy").font(.headline)
                Spacer()
                Button(action: more) { Image(systemName: "ellipsis") }
                    .help("More options").accessibilityLabel("More options")
            }
            if onCall {
                MacNowCallControls(voice: voice)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
            } else {
                Button(action: voice.onStart) { Label("Start a call", systemImage: "phone.fill") }
                    .buttonStyle(.borderedProminent).tint(JobLook.brass)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if !onCall {
                        Text(now.home?.sentence ?? (now.failed ? "Can't reach Hermes right now." : "All quiet."))
                            .font(.callout).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(MacNowLayout.menuItems(now.visible, onCall: onCall)) { item in
                        MacNowCard(item: item, now: now, focused: now.home?.call?.focus == item.id) {
                            open(item.sessionID)
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 400)
            Button("Open Speakeasy") { open(nil) }
                .buttonStyle(.bordered).frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(16).frame(width: MacNowLayout.popoverWidth)
        .background(HistoryLook.window)
        .onAppear { now.start() }
        .accessibilityIdentifier("mac-now-popover")
    }
}
