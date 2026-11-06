import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient
import UniformTypeIdentifiers

// MARK: - Tokens

enum Tokens {
    static let width: CGFloat = 360
    static let minWidth: CGFloat = 320
    static let maxWidth: CGFloat = 760
    static let maxExtraHeight: CGFloat = 700
    static let radius: CGFloat = 18
    static let blue = Color(red: 0.49, green: 0.67, blue: 1.0)
    /// Brand palette (matches speakeasyvoice.ai): brass on warm near-black.
    /// Light Mode: a deeper brass (AA on the light panel) with ivory text on it (`BrandPalette`).
    static let brass = BrandPalette.brass                                // #d4a24c
    static let brassLight = Color.adaptive(dark: Color(red: 0.941, green: 0.784, blue: 0.447), light: Color(hex: 0xb07a22)) // #f0c872
    static let brassInk = BrandPalette.onBrass                           // #17110a
    static let panelDark = Color(red: 0.086, green: 0.075, blue: 0.059)  // #16130f
    static let amber = BrandPalette.amber
    static let amberInk = Color.adaptive(dark: Color(red: 0.23, green: 0.15, blue: 0.02), light: Color(hex: 0xfff8ec))
    static let red = BrandPalette.red
    static let green = BrandPalette.green
    /// Screen sharing: the purple macOS uses for its screen-recording indicator.
    static let screen = Color(nsColor: .systemPurple)
}

struct VisualEffectBackground: NSViewRepresentable {
    var material: NSVisualEffectView.Material
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.blendingMode = .behindWindow
        view.state = .active
        view.material = material
        return view
    }
    func updateNSView(_ view: NSVisualEffectView, context: Context) { view.material = material }
}

// MARK: - Shimmer status text

struct StatusText: View {
    var text: String
    var tone: StatusTone
    var clickable: Bool
    /// The Work view shows the whole sentence; elsewhere the text wraps up to `maxLines`.
    var wraps = false
    var maxLines = 1
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var scheme

    private var lines: Int? { wraps ? nil : maxLines }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 3) {
            base
            if clickable {
                Image(systemName: "chevron.right").font(.system(size: 8.5, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private var base: some View {
        let label = Text(text).font(.system(size: 11.5, weight: tone == .attention ? .semibold : .regular))
            .lineLimit(lines).truncationMode(.tail)
            .fixedSize(horizontal: false, vertical: wraps || maxLines > 1)
        switch tone {
        case .glimmer where !reduceMotion:
            label.foregroundStyle(.secondary)
                .overlay {
                    TimelineView(.animation(minimumInterval: 1 / 30)) { context in
                        let cycle = 3.4
                        let t = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: cycle) / cycle
                        let x = CGFloat(-0.5 + t * 2.0)
                        // A highlight band 45% of the label wide, sweeping left to right (unit points,
                        // so no layout reader is needed).
                        LinearGradient(colors: [.clear, (scheme == .dark ? Color.white : Color.black).opacity(0.85), .clear],
                                       startPoint: UnitPoint(x: x, y: 0.5), endPoint: UnitPoint(x: x + 0.45, y: 0.5))
                            .mask(label)
                    }
                    .allowsHitTesting(false)
                }
        case .attention:
            label.foregroundStyle(scheme == .dark ? Tokens.amber : Color(red: 0.62, green: 0.36, blue: 0))
        case .warning:
            label.foregroundStyle(scheme == .dark ? Tokens.amber.opacity(0.9) : Color(red: 0.55, green: 0.35, blue: 0.05))
        case .error:
            label.foregroundStyle(Tokens.red)
        case .success:
            label.foregroundStyle(.primary.opacity(0.85))
        default:
            label.foregroundStyle(.secondary)
        }
    }
}

// MARK: - Buttons

/// Which part of the header's 30 pt media capsule a control fills: all of it (a circle, when the
/// call can't share its screen), or its leading (screen) or trailing (mic) half.
enum CapsulePart { case whole, leading, trailing }

/// A capsule control's fill and hairline, cut to its part of the capsule, so the screen and mic
/// halves each keep their own state tint inside one shape.
struct CapsulePartBackground: View {
    var part: CapsulePart
    var fill: Color
    var stroke: Color

    var body: some View {
        switch part {
        case .whole:
            ZStack {
                Capsule().fill(fill)
                Capsule().strokeBorder(stroke, lineWidth: 0.75)
            }
        case .leading, .trailing:
            ZStack {
                CapsuleEnd(leading: part == .leading).fill(fill)
                CapsuleEnd(leading: part == .leading, closed: false).strokeBorder(stroke, lineWidth: 0.75)
            }
        }
    }
}

/// One end of a capsule as tall as its frame: a round end, with straight edges running to the
/// capsule's middle. Unclosed, the hairline leaves that middle edge open so two halves read as one shape.
struct CapsuleEnd: InsettableShape {
    var leading: Bool
    var closed = true
    var inset: CGFloat = 0

    func path(in rect: CGRect) -> Path {
        let top = rect.minY + inset, bottom = rect.maxY - inset
        let radius = max(0, rect.height / 2 - inset)
        let end = leading ? rect.minX + inset : rect.maxX - inset      // the round end's outer edge
        let middle = leading ? rect.maxX : rect.minX                   // where the other half starts
        let center = leading ? end + radius : end - radius
        var path = Path()
        path.move(to: CGPoint(x: middle, y: top))
        path.addLine(to: CGPoint(x: center, y: top))
        path.addArc(tangent1End: CGPoint(x: end, y: top), tangent2End: CGPoint(x: end, y: rect.midY), radius: radius)
        path.addArc(tangent1End: CGPoint(x: end, y: bottom), tangent2End: CGPoint(x: center, y: bottom), radius: radius)
        path.addLine(to: CGPoint(x: middle, y: bottom))
        if closed { path.closeSubpath() }
        return path
    }

    func inset(by amount: CGFloat) -> CapsuleEnd {
        var shape = self
        shape.inset += amount
        return shape
    }
}

struct MicButton: View {
    var muted: Bool
    var enabled: Bool
    var shortcutHint: String = ""
    /// The mic's part of the header capsule: the right half beside the screen button, else all of it.
    var part: CapsulePart = .whole
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        // Icon only: lit (filled, full strength) when live; slashed and dimmed when muted.
        Button(action: action) {
            Image(systemName: muted ? "mic.slash.fill" : "mic.fill").font(.system(size: 12.5, weight: .semibold))
                .frame(width: 30, height: 30)
                .foregroundStyle(muted ? Color.primary.opacity(0.45) : Tokens.green)
                .background(CapsulePartBackground(part: part,
                                                  fill: muted ? Color.primary.opacity(hover ? 0.1 : 0.05)
                                                              : Tokens.green.opacity(hover ? 0.2 : 0.13),
                                                  stroke: muted ? Color.primary.opacity(0.08) : Tokens.green.opacity(0.45)))
                .contentShape(part == .whole ? AnyShape(Circle()) : AnyShape(Rectangle()))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
        .onHover { hover = $0 }
        .help((muted ? "Unmute microphone. The call stays open and billed while muted." : "Mute microphone. The call stays open and billed.")
              + (shortcutHint.isEmpty ? "" : "\n" + shortcutHint))
        .accessibilityLabel(muted ? "Microphone muted" : "Microphone on")
        .accessibilityHint(muted ? "Unmute" : "Mute; the call remains open")
    }
}

/// The screen half of the header capsule. Off: `eye`, dimmed like a muted mic. On: `eye.fill` in the
/// screen-recording purple with the live mic's soft tint. Screen Recording missing: a tiny amber
/// badge (pressing shows the Set up line). One gentle ring when a request needs the screen; none
/// with Reduce Motion.
struct ScreenShareButton: View {
    var state: ScreenButton
    var shortcutHint: String = ""
    /// Grows each time the button should be noticed (`screen.hint`); each change rings once.
    var pulse: Int = 0
    var part: CapsulePart = .leading
    var action: () -> Void
    @State private var hover = false
    /// Rings shown so far: bumped only when `pulse` grows (a new call resetting it never rings).
    @State private var rings = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private enum Ring: CaseIterable { case rest, lit, gone }

    var body: some View {
        let on = state == .on
        Button(action: action) {
            Image(systemName: on ? "eye.fill" : "eye").font(.system(size: 12, weight: .semibold))
                .frame(width: 30, height: 30)
                .foregroundStyle(on ? Tokens.screen : Color.primary.opacity(0.45))
                .overlay(alignment: .topTrailing) {
                    if state == .needsPermission {
                        Circle().fill(Tokens.amber).frame(width: 5, height: 5).offset(x: -5, y: 7)
                    }
                }
                .background(CapsulePartBackground(part: part,
                                                  fill: on ? Tokens.screen.opacity(hover ? 0.2 : 0.13)
                                                           : Color.primary.opacity(hover ? 0.1 : 0.05),
                                                  stroke: on ? Tokens.screen.opacity(0.45) : Color.primary.opacity(0.08)))
                .contentShape(part == .whole ? AnyShape(Circle()) : AnyShape(Rectangle()))
        }
        .buttonStyle(.plain)
        .overlay {
            Circle().strokeBorder(Tokens.screen, lineWidth: 1.25)
                .frame(width: 30, height: 30)
                .phaseAnimator(Ring.allCases, trigger: rings) { ring, phase in
                    ring.scaleEffect(phase == .gone ? 1.5 : 1).opacity(phase == .lit ? 0.6 : 0)
                } animation: { phase in
                    switch phase {
                    case .rest: return nil
                    case .lit: return .easeOut(duration: 0.12)
                    case .gone: return .easeOut(duration: 0.9)
                    }
                }
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
        .onChange(of: pulse) { old, new in
            if new > old && !reduceMotion { rings += 1 }
        }
        .onHover { hover = $0 }
        .help(help + (shortcutHint.isEmpty ? "" : "\n" + shortcutHint))
        .accessibilityLabel(label)
        .accessibilityHint(hint)
    }

    private var help: String {
        switch state {
        case .on: return "Stop sharing your screen."
        case .needsPermission: return "Share your screen. Screen Recording is off for Speakeasy; allow it in Settings."
        default: return "Share your screen: new requests carry a picture of the window you're in."
        }
    }
    private var label: String {
        switch state {
        case .on: return "Screen sharing on"
        case .needsPermission: return "Screen sharing needs Screen Recording"
        default: return "Screen sharing off"
        }
    }
    private var hint: String {
        switch state {
        case .on: return "Stop sharing your screen"
        case .needsPermission: return "Shows how to allow Screen Recording"
        default: return "Share your screen with new requests"
        }
    }
}

/// Screen and mic in one 30 pt capsule, so sharing costs the header one icon width. The screen half
/// shows only when this call can share; the mic alone is the familiar circle.
struct MediaCapsule: View {
    @ObservedObject var model: VoicePanelModel
    var presentation: PillPresentation

    var body: some View {
        let screen = model.screenState
        HStack(spacing: 0) {
            if screen != .unavailable {
                ScreenShareButton(state: screen, shortcutHint: model.screenShortcutHint, pulse: model.screenHintPulse,
                                  action: model.onToggleScreen)
            }
            MicButton(muted: presentation.micMuted, enabled: presentation.micEnabled, shortcutHint: model.muteShortcutHint,
                      part: screen == .unavailable ? .whole : .trailing, action: model.onToggleMic)
        }
        .fixedSize()
    }
}

/// Shown during the first-call tour: says what's happening and offers a way out.
struct TourStrip: View {
    var action: () -> Void
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkles").foregroundStyle(.secondary)
            Text("Quick tour — say \u{201C}skip\u{201D} any time.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            Button("Skip tour", action: action)
                .controlSize(.small)
                .help("End the tour; the call carries on normally")
                .accessibilityLabel("Skip the tour")
        }
        .padding(.horizontal, 14).padding(.vertical, 6)
    }
}

struct PauseButton: View {
    var label: String
    var enabled: Bool
    var shortcutHint: String = ""
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        let resume = label == "Resume"
        // Icon only: pause symbol while live, play symbol while paused.
        Button(action: action) {
            Image(systemName: resume ? "play.fill" : "pause.fill").font(.system(size: 11.5, weight: .semibold))
                .frame(width: 30, height: 30)
                .foregroundStyle(resume ? Tokens.brassInk : Color.primary)
                .background(Circle().fill(resume ? AnyShapeStyle(Tokens.brass.opacity(hover ? 1 : 0.9))
                                                 : AnyShapeStyle(Color.primary.opacity(hover ? 0.16 : 0.1))))
                .overlay(Circle().strokeBorder(resume ? Color.clear : Color.primary.opacity(0.14), lineWidth: 0.5))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
        .onHover { hover = $0 }
        .help((resume ? "Resume the call where you left off." :
               "Pause the call: voice turns off and stops billing; tasks keep running. Resume picks up where you left off.")
              + (shortcutHint.isEmpty ? "" : "\n" + shortcutHint))
        .accessibilityLabel(resume ? "Resume call" : "Pause call")
    }
}

struct EndButton: View {
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Image(systemName: "phone.down.fill").font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 30, height: 30)
                .background(Circle().fill(Tokens.red.opacity(hover ? 1 : 0.9)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("End the voice call. Running tasks keep going.")
        .accessibilityLabel("End call")
    }
}

struct StartButton: View {
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: "phone.fill").font(.system(size: 10.5, weight: .semibold))
                Text("Start").font(.system(size: 12, weight: .semibold))
            }
            .foregroundStyle(Tokens.brassInk)
            .padding(.horizontal, 10)
            .frame(height: 30)
            .background(Capsule().fill(Tokens.brass.opacity(hover ? 1 : 0.9)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Start a new voice call.")
        .accessibilityLabel("Start call")
    }
}

/// Turns listening mode on (idle header only; once on, the strip below has the controls).
struct ListenButton: View {
    var blocked: String?
    var shortcutHint: String
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Image(systemName: "ear").font(.system(size: 12, weight: .semibold))
                .frame(width: 30, height: 30)
                .background(Circle().fill(Color.primary.opacity(hover && blocked == nil ? 0.14 : 0.08)))
                .contentShape(Circle())
                .opacity(blocked == nil ? 1 : 0.45)
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(blocked ?? (["Listening mode: pause the call and hear the room without answering. Turn it off to ask about it.",
                           shortcutHint].filter { !$0.isEmpty }.joined(separator: "\n")))
        .accessibilityLabel("Turn on listening mode")
        .accessibilityHint(blocked ?? "Pauses the call and transcribes the room on this Mac without answering")
    }
}

/// Listening mode's strip under the header: what it's doing, and the two ways out.
struct RoomStrip: View {
    var room: RoomPresentation?
    var notice: String?
    /// What it heard can be asked about (on, or kept after listening stopped by itself).
    var canAsk = false
    var onAsk: () -> Void
    var onDiscard: () -> Void
    var onDismissNotice: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var color: Color {
        switch room?.tone ?? .plain {
        case .error: return Tokens.red
        case .warning, .attention: return .orange
        default: return Tokens.brass
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let room {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: room.isOn ? "ear.fill" : "ear.trianglebadge.exclamationmark")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(color)
                        .symbolEffect(.pulse, options: .repeating, isActive: room.isOn && room.tone == .plain && !reduceMotion)
                        .frame(width: 18)
                        .accessibilityHidden(true)   // the title next to it says the same
                    VStack(alignment: .leading, spacing: 2) {
                        Text(room.title).font(.system(size: 12, weight: .semibold))
                        if !room.detail.isEmpty {
                            Text(room.detail).font(.system(size: 11)).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let hint = room.hint {
                            Text(hint).font(.system(size: 11)).foregroundStyle(.tertiary)
                        }
                        ForEach(room.warnings, id: \.self) { warning in
                            Label(warning, systemImage: "exclamationmark.triangle")
                                .font(.system(size: 10.5)).foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityElement(children: .combine)
                }
                if room.isOn || canAsk {
                    HStack(spacing: 6) {
                        Spacer(minLength: 0)
                        PillButton(title: "Discard", destructive: true, action: onDiscard)
                            .help("Stop listening and forget what it heard. No call starts.")
                        PillButton(title: room.isOn ? "Turn off and ask" : "Ask about it", prominent: true, action: onAsk)
                            .help("Start a call that knows what was said.")
                    }
                }
            }
            if let notice {
                HStack(spacing: 6) {
                    Text(notice).font(.system(size: 11)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button { onDismissNotice() } label: {
                        Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).padding(4)
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Dismiss")
                    .accessibilityLabel("Dismiss note")
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(color.opacity(0.10)))
        .accessibilityAddTraits(.updatesFrequently)
    }
}

struct IconButton: View {
    var symbol: String
    var help: String
    var size: CGFloat = 28
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: size < 28 ? 9.5 : 11.5, weight: .semibold))
                .frame(width: size, height: size)
                .background(Circle().fill(Color.primary.opacity(hover ? 0.14 : 0.08)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(help)
        .accessibilityLabel(help)
    }
}

struct PillButton: View {
    var title: String
    var prominent = false
    var destructive = false
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 11.5, weight: .semibold))
                .padding(.horizontal, 11).frame(height: 26)
                .foregroundStyle(prominent ? Tokens.brassInk : (destructive ? Tokens.red : Color.primary))
                .background(Capsule().fill(prominent ? AnyShapeStyle(Tokens.brass) : AnyShapeStyle(Color.primary.opacity(hover ? 0.14 : 0.08))))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
    }
}

// MARK: - Root

/// The panel's own height: its ideal height (what `fixedSize` gave), or, when the window is capped at
/// the screen and can't give that, exactly the window's height with the scroll areas giving up the
/// difference. A fixed-size card taller than its window was centred in it, clipping the header off
/// the top and the last rows off the bottom.
private struct CappedHeight: ViewModifier {
    func body(content: Content) -> some View { Layout { content } }

    struct Layout: SwiftUI.Layout {
        func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
            guard let child = subviews.first else { return .zero }
            let ideal = child.sizeThatFits(ProposedViewSize(width: proposal.width, height: nil))
            guard let height = proposal.height, height < ideal.height else { return ideal }
            return CGSize(width: ideal.width, height: height)
        }

        func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
            guard let child = subviews.first else { return }
            let ideal = child.sizeThatFits(ProposedViewSize(width: bounds.width, height: nil))
            child.place(at: bounds.origin, anchor: .topLeading,
                        proposal: ProposedViewSize(width: bounds.width, height: bounds.height < ideal.height ? bounds.height : nil))
        }
    }
}

struct VoicePanelView: View {
    @ObservedObject var model: VoicePanelModel
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let p = model.presentation
        if model.showsSlim { orbPanel } else { fullPanel(p) }
    }

    /// Folded into the orb: the orb and the call buttons, nothing else.
    private var orbPanel: some View {
        OrbBar(model: model)
            .onHover { model.hovering = $0 }
            .fixedSize(horizontal: false, vertical: true)
            .background(
                ZStack {
                    VisualEffectBackground(material: scheme == .dark ? .hudWindow : .popover)
                    (scheme == .dark ? Tokens.panelDark : Color.white).opacity(scheme == .dark ? 0.78 : 0.35)
                }
            )
            .clipShape(RoundedRectangle(cornerRadius: Tokens.radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Tokens.radius, style: .continuous)
                .strokeBorder(Color.primary.opacity(scheme == .dark ? 0.16 : 0.12), lineWidth: 0.5))
    }

    private func fullPanel(_ p: PillPresentation) -> some View {
        let board = model.showsBoard
        let job = model.workExpanded ? model.selectedJob : nil
        let pending = model.pendingAttachments
        let holding = model.hold?.isWaiting == true
        return VStack(alignment: .leading, spacing: 0) {
            if !model.state.workOnly { header(p) }
            // A call problem with a one-click fix (mic permission, a dead mic, a failed connect), or
            // on the board alone its "can't load your work" notice (Try again reloads the board).
            if let problem = model.callProblem, !model.state.workOnly || problem.fix == .reloadBoard {
                ProblemBanner(problem: problem, action: { model.onFixProblem(problem.fix) })
                    .padding(.horizontal, 12).padding(.bottom, 10)
                    .padding(.top, model.state.workOnly ? 10 : 0)
                    .transition(.opacity)
            }
            // Pictures and files waiting for the next request: under the header in every layout.
            if !model.state.workOnly && !pending.isEmpty {
                PendingAttachmentsRow(items: pending, onRemove: model.onRemoveAttachment)
                    .padding(.horizontal, 14).padding(.bottom, 8)
                    .transition(.opacity)
            }
            if model.tourActive && model.state.connection.isOpen && !model.state.workOnly {
                TourStrip(action: model.onSkipTour)
            }
            // Beside the call, never in it: shown with no call open, or while the call is paused
            // (listening mode turned on mid-conversation pauses it).
            if !model.state.connection.isInCall && !model.state.workOnly && (model.room != nil || model.roomNotice != nil) {
                RoomStrip(room: model.room, notice: model.roomNotice, canAsk: model.roomCanAsk, onAsk: model.onAskRoom,
                          onDiscard: model.onDiscardRoom, onDismissNotice: model.onDismissRoomNotice)
                    .padding(.horizontal, 12).padding(.bottom, 10)
                    .transition(.opacity)
            }
            // A newer plugin: the notice and its one-click Update (shown with no call; progress and the
            // result stay in view during one). On the board it's a tile under the greeting instead.
            if job == nil && !(board && !model.workExpanded) {
                PluginUpdateCard(notice: model.pluginUpdate, onUpdate: model.onPluginUpdate,
                                 onOpenJob: model.onOpenPluginUpdateJob, onCopy: model.onCopyPluginUpdate)
                    .padding(.horizontal, 12).padding(.top, model.state.workOnly ? 10 : 0).padding(.bottom, 10)
            }
            if let job {
                if !model.state.workOnly { Divider().opacity(0.5).padding(.horizontal, 14) }
                JobDetailView(job: job, model: model)
            } else if model.workExpanded || model.state.workOnly {
                if !model.state.workOnly { Divider().opacity(0.5).padding(.horizontal, 14) }
                WorkDetailView(model: model)
            } else {
                if model.showCaptions, !model.state.exchange.isEmpty {
                    VStack(spacing: 0) {
                        CaptionView(exchange: model.state.exchange.cleaned, transcript: model.state.transcript,
                                    assistantName: model.state.assistantName, expanded: $model.captionExpanded,
                                    extraHeight: model.extraHeight, phase: model.voicePhase)
                        TranscriptGripBar()
                    }
                    .padding(.horizontal, 14).padding(.bottom, 4)
                    .transition(.opacity)
                }
                // A request waiting for the screen, then how that ended.
                if let line = model.holdText {
                    HoldRow(text: line, waiting: holding, shortcutHint: model.screenShortcutHint)
                        .padding(.horizontal, 12).padding(.bottom, model.showsTaskList ? 6 : 10)
                        .transition(.opacity)
                }
                if board {
                    JobBoardView(model: model)
                        .padding(.leading, 12).padding(.trailing, 6).padding(.bottom, 10)
                        .transition(.opacity)
                } else if Self.showsEmptyBoard(model) {
                    EmptyBoardView(now: model.state.now, inCall: model.state.connection.isInCall)
                        .padding(.horizontal, 14).padding(.top, 2).padding(.bottom, 14)
                        .transition(.opacity)
                } else if model.showsTaskList {
                    // Something below needs you (a question, an approval, a draft, a review):
                    // finished tasks shrink to one row so it stays in view.
                    let crowded = model.state.approval != nil || model.pinnedViews != nil
                        || !model.pinnedDrafts.isEmpty || !model.pinnedReviews.isEmpty
                    TaskListView(model: model, compact: true, maxSettled: crowded ? 1 : 2)
                        .padding(.horizontal, 12).padding(.bottom, 10)
                        .transition(.opacity)
                }
                let reviewing = Set(model.pinnedReviews.map(\.taskID))
                // Not when the same task's cards are already pinned below: the bar would repeat them.
                let pinnedTask = model.pinnedViews?.taskID
                if !board, let task = model.state.tasks.last(where: { !reviewing.contains($0.id) && $0.id != pinnedTask &&
                    (!$0.info.products.isEmpty || !$0.info.cards.isEmpty || !$0.info.images.isEmpty || !$0.info.views.isEmpty) }) {
                    Button {
                        model.onSelectTask(task.id)
                    } label: {
                        HStack(spacing: 7) {
                            Image(systemName: "square.stack")
                            let count = (task.info.products.isEmpty ? task.info.cards.count + task.info.images.count
                                                                    : task.info.products.count + task.info.images.count)
                                + task.info.views.count
                            Text("\(count) \(count == 1 ? "card" : "cards") · \(task.name)").lineLimit(1)
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.up")
                        }
                        .font(.system(size: 11, weight: .medium))
                        .padding(.horizontal, 10).frame(height: 30)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.07)))
                    }
                    .buttonStyle(.plain)
                    .help("Show links from this task")
                    .padding(.horizontal, 14).padding(.bottom, 8)
                }
            }
            if let approval = model.state.approval, !(model.workExpanded || model.state.workOnly),
               !(board && model.jobs.contains { j in j.tasks.contains { $0.info.runID == approval.runID } }) {
                ApprovalRow(approval: approval, task: model.state.tasks.last { $0.info.runID == approval.runID },
                            busy: model.busy, onChoice: model.onApproval)
                    .padding(.horizontal, 12).padding(.bottom, 12)
            }
            // Nothing pins under the board: every card opens from its job's tile (see `showsBoard`,
            // which is on whenever a card, draft or review exists). These only draw without a board.
            let loose = Self.looseCards(model)
            if let pinned = loose.views {
                VStack(spacing: 8) {
                    ForEach(pinned.views.prefix(2)) { card in ActionableCard(taskID: pinned.taskID, card: card, model: model) }
                        .environment(\.questionActions, model.questionActions(for: pinned.taskID))
                }
                .overlay(alignment: .topTrailing) {
                    Button { model.dismissPinnedViews(pinned.taskID) } label: {
                        Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).padding(6)
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Hide this card")
                }
                .padding(.horizontal, 12).padding(.bottom, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            ForEach(loose.drafts) { draft in
                EmailDraftCard(draft: draft, model: model)
                    .padding(.horizontal, 12).padding(.bottom, 12)
            }
            let open = ImageReviewLayout.focused(loose.reviews, picked: model.focusedReviewID)
            ForEach(loose.reviews) { review in
                ImageReviewCard(review: review, shownCount: loose.reviews.count + loose.drafts.count,
                                compact: review.runID != open, model: model)
                    .padding(.horizontal, 12).padding(.bottom, review.runID == loose.reviews.last?.runID ? 12 : 6)
            }
            // A card button's result: a line at the bottom that never covers a control.
            if let toast = model.toast {
                ToastView(toast: toast).frame(maxWidth: .infinity).padding(.bottom, 12)
                    .transition(.opacity)
            }
        }
        .frame(width: model.panelWidth, alignment: .top)
        .onHover { model.hovering = $0 }
        .modifier(CappedHeight())
        .contentShape(Rectangle())
        .background(
            ZStack {
                VisualEffectBackground(material: scheme == .dark ? .hudWindow : .popover)
                (scheme == .dark ? Tokens.panelDark : Color.white).opacity(scheme == .dark ? 0.78 : 0.35)
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: Tokens.radius, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Tokens.radius, style: .continuous)
            .strokeBorder(Color.primary.opacity(scheme == .dark ? 0.16 : 0.12), lineWidth: 0.5))
        // A picture or file dragged over the panel during a call: a thin accent ring says it's taken.
        .overlay(RoundedRectangle(cornerRadius: Tokens.radius, style: .continuous)
            .strokeBorder(Tokens.brass, lineWidth: 1.5)
            .opacity(model.dropTargeted ? 1 : 0)
            .allowsHitTesting(false))
        .overlay(alignment: .bottomTrailing) { CornerGrip().padding(5).allowsHitTesting(false) }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: model.toast)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.workExpanded)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.captionExpanded)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.state.approval)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.state.tasks.count)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.pinnedReviews.map(\.id))
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.slim)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: pending.map(\.id))
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.holdText)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: model.dropTargeted)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.room?.isOn)
    }

    /// A call with nothing on the board yet and nothing said: a greeting and what to try.
    static func showsEmptyBoard(_ model: VoicePanelModel) -> Bool {
        model.state.tasks.isEmpty && model.state.exchange.isEmpty && model.holdText == nil && !model.state.workOnly
            && model.state.connection.isInCall && model.callProblem == nil && !model.tourActive
    }

    /// Cards drawn loose in the panel, outside any job view. With a board (on whenever a card,
    /// draft or review exists) there are none: each card opens from its job's tile.
    struct LooseCards {
        var views: (taskID: String, views: [ViewCard])?
        var drafts: [EmailDraft] = []
        var reviews: [ImageReview] = []
        var count: Int { (views?.views.count ?? 0) + drafts.count + reviews.count }
    }
    static func looseCards(_ model: VoicePanelModel) -> LooseCards {
        guard !(model.workExpanded || model.state.workOnly || model.showsSlim || model.showsBoard) else { return LooseCards() }
        return LooseCards(views: model.pinnedViews, drafts: model.pinnedDrafts, reviews: model.pinnedReviews)
    }

    private func header(_ p: PillPresentation) -> some View {
        // "Screen Recording is off · Set up" on the status line: tapping it opens Settings › General › Screen.
        let setUp = model.shareNotice.map { $0.action == .openScreenSettings && $0.text == model.headerStatus } ?? false
        let statusTaps = setUp || p.statusClickable || model.showsSlim
        return HStack(spacing: 8) {
            // The orb: clicking it folds the panel into it (the call keeps running).
            Button(action: { if model.state.connection.isOpen && !model.state.workOnly { model.onToggleSlim() } }) {
                // The same orb as the folded panel: one colour language for the whole call.
                JobOrb(state: model.orbState, size: 38, level: model.orbLevel, mark: model.voicePhase == .hearing ? .listening : p.mark,
                       unreadAnswers: model.unreadAnswers, placement: "header", hearing: model.voicePhase == .hearing)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .disabled(!(model.state.connection.isOpen && !model.state.workOnly))
            .help(model.state.connection.isOpen ? "Fold into the orb (the call keeps running)" : "")
            .padding(.trailing, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.voicePhase == .hearing ? "Hearing you" : p.primary).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                    .minimumScaleFactor(0.85)
                    .help(p.primary)
                    .contentTransition(.opacity)
                Button(action: {
                    if setUp { model.onOpenScreenSettings() }
                    else if p.statusClickable || model.showsSlim { model.onToggleWork() }
                }) {
                    Group {
                        if model.orbAnswerDot && model.orbState != .working, model.orbReason != nil, model.state.quietPausedMinutes == nil || model.state.connection != .paused {
                            // New answers read in the orb dot's calm blue.
                            HStack(alignment: .firstTextBaseline, spacing: 3) {
                                Text(model.headerStatus).font(.system(size: 11.5, weight: .medium)).foregroundStyle(JobOrb.answerBlue).lineLimit(1)
                                if p.statusClickable || model.showsSlim {
                                    Image(systemName: "chevron.right").font(.system(size: 8.5, weight: .semibold)).foregroundStyle(.secondary)
                                }
                            }
                        } else {
                            StatusText(text: model.headerStatus, tone: model.headerTone, clickable: statusTaps,
                                       maxLines: 2)
                        }
                    }
                        .id(model.headerStatus)
                        .transition(.opacity)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!statusTaps)
                .help(setUp ? "Open Settings to allow Screen Recording"
                      : (p.statusClickable || model.showsSlim ? "\(model.headerStatus)\nShow tasks" : model.headerStatus))
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.35), value: model.headerStatus)
                .accessibilityLabel(model.headerStatus)
                .accessibilityHint(setUp ? "Opens Speakeasy Settings to allow Screen Recording" : "")
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .layoutPriority(1)
            // Quiet (silence the current reply) is intentionally not exposed in Speakeasy.
            // Listening mode mid-conversation: pauses the call and listens; turning it off resumes it.
            if model.roomOffered && model.room?.isOn != true
                && (model.state.connection == .live || model.state.connection == .paused) {
                ListenButton(blocked: model.roomBlocked, shortcutHint: model.roomShortcutHint, action: model.onToggleRoom)
                    .fixedSize()
            }
            if let label = p.pauseLabel {
                PauseButton(label: label, enabled: p.pauseEnabled, shortcutHint: model.pauseShortcutHint,
                            action: model.onTogglePause)
                    .fixedSize()
            }
            // Screen and mic share one capsule; both hide while paused (nothing listens or looks).
            if model.state.connection != .paused {
                MediaCapsule(model: model, presentation: p)
            }
            // No separate fold button: clicking the orb folds the panel (one way to shrink, one to hide).
            if !model.state.connection.isOpen {
                // While listening mode holds what it heard, Start asks about it in a new call.
                StartButton(action: model.onStart)
                    .fixedSize()
            } else {
                EndButton(action: model.onEnd)
                    .fixedSize()
            }
            IconButton(symbol: "xmark", help: model.state.connection.isOpen
                       ? "Hide panel (the call continues; use the menu bar or shortcut to show it)"
                       : "Close panel (Esc)", size: 22, action: model.onClosePanel)
                .fixedSize()
        }
        .padding(.leading, 10).padding(.trailing, 12).padding(.vertical, 8)
        .frame(minHeight: 68)
    }
}

// MARK: - Captions

/// One turn of the conversation, sized to its content: short turns stay short,
/// the user's turns sit on the right, the assistant's on the left.
struct TranscriptBubble: View {
    var turn: TranscriptTurn
    var assistantName: String
    var body: some View {
        if turn.kind == .text { textReply } else { speech }
    }

    @ViewBuilder private var speech: some View {
        let mine = turn.speaker == .you
        HStack(spacing: 0) {
            if mine { Spacer(minLength: 36) }
            VStack(alignment: mine ? .trailing : .leading, spacing: 2) {
                Text(mine ? (turn.kind == .typed ? "You typed\(turn.taskName.map { " · to \(TaskMessageCopy.shortName($0))" } ?? "")" : "You")
                     : assistantName).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary).lineLimit(1)
                Text(turn.cleanedText).font(.system(size: 11.5))
                    .foregroundStyle(mine ? Color.white : Color.primary.opacity(0.9))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 9).padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .fill(mine ? AnyShapeStyle(Tokens.brass.opacity(0.85)) : AnyShapeStyle(Color.primary.opacity(0.08))))
            }
            .fixedSize(horizontal: false, vertical: true)
            if !mine { Spacer(minLength: 36) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(mine ? "You" : assistantName): \(turn.cleanedText)")
    }

    /// A task's full written answer: the voice speaks the short version, the whole text is here to read.
    private var textReply: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 2) {
                Text(turn.taskName.map { "\(assistantName) · \($0)" } ?? "\(assistantName) · written reply")
                    .font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary).lineLimit(1)
                Text(Self.markdown(turn.cleanedText)).font(.system(size: 11.5))
                    .foregroundStyle(Color.primary.opacity(0.9))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 9).padding(.vertical, 7)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Color.primary.opacity(0.05)))
                    .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .strokeBorder(Tokens.brass.opacity(0.35), lineWidth: 0.75))
            }
            .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 12)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(assistantName) wrote: \(turn.cleanedText)")
    }

    /// Inline markdown (bold, links) with line breaks kept; plain text when it doesn't parse.
    static func markdown(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(text)
    }
}

struct CaptionView: View {
    var exchange: Exchange
    /// The whole call so far; the expanded view renders it as chat bubbles.
    var transcript: [TranscriptTurn] = []
    var assistantName: String = VoiceState.defaultAssistantName
    @Binding var expanded: Bool
    var extraHeight: CGFloat = 0
    /// What the voice is doing: the collapsed caption shows the line being said now.
    var phase: VoicePhase = .off

    /// The call's turns; a state without a transcript (older server, previews) shows the exchange.
    private var shownTurns: [TranscriptTurn] {
        let turns = transcript.filter { !$0.cleanedText.isEmpty }
        if !turns.isEmpty { return turns }
        var out: [TranscriptTurn] = []
        if !exchange.you.isEmpty { out.append(TranscriptTurn(id: 0, speaker: .you, text: exchange.you)) }
        if !exchange.assistant.isEmpty { out.append(TranscriptTurn(id: 1, speaker: .assistant, text: exchange.assistant)) }
        return out
    }

    var body: some View {
        Group {
            if expanded {
                // Opens on the latest words and follows them only while the user is
                // at the bottom; scrolling up holds the reading position.
                PanelScrollView(maxHeight: 150 + extraHeight, minHeight: extraHeight > 0 ? 150 + extraHeight : 0,
                                followsBottom: true) {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(shownTurns) { turn in
                            TranscriptBubble(turn: turn, assistantName: assistantName)
                        }
                    }
                    .padding(.trailing, 14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    // Bubbles grow as words arrive: no per-word resize/scroll animation.
                    .transaction { $0.animation = nil }
                }
            } else {
                // The live caption: just the line being said now, cut on a word, up to two lines.
                if let now = LiveCaption.current(Self.desktopWording(exchange), phase: phase) {
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text(now.you ? "You" : assistantName).font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(now.you ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(.secondary))
                            .lineLimit(1).fixedSize()
                        Text(now.text).font(.system(size: 13)).foregroundStyle(.primary.opacity(0.9))
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .transaction { $0.animation = nil }
                        if phase == .thinking {
                            ThinkingDots().padding(.trailing, 12)
                        }
                    }
                    .padding(.trailing, 14)
                }
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.055)))
        .overlay(alignment: .topTrailing) {
            Button(action: { expanded.toggle() }) {
                Image(systemName: expanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                    .font(.system(size: 8.5, weight: .semibold)).foregroundStyle(.tertiary).padding(6)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(expanded ? "Collapse transcript" : "Read the full exchange")
            .accessibilityLabel(expanded ? "Collapse transcript" : "Expand transcript")
        }
        .contentShape(Rectangle())
        .onTapGesture { if !expanded { expanded = true } }
        .help(expanded ? "Scroll to read back · collapse with the corner button" : "Read the full exchange")
    }

    /// The voice sometimes says "tap" (it's the same model the phone talks to); on a Mac it's a click.
    static func desktopWording(_ e: Exchange) -> Exchange {
        var out = e
        out.assistant = e.assistant
            .replacingOccurrences(of: #"\bTap\b"#, with: "Click", options: .regularExpression)
            .replacingOccurrences(of: #"\btap\b"#, with: "click", options: .regularExpression)
        return out
    }

    @ViewBuilder
    private func line(_ speaker: String, _ text: String, full: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Text(speaker).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
                .lineLimit(1).fixedSize()
                .frame(minWidth: 30, alignment: .leading)
            if full {
                Text(text).font(.system(size: 11.5)).foregroundStyle(.primary.opacity(0.88))
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            } else {
                // Collapsed captions follow the end of the utterance.
                Text(text).font(.system(size: 11.5)).foregroundStyle(.primary.opacity(0.82))
                    .lineLimit(1).truncationMode(.head)
            }
        }
        .padding(.trailing, full ? 0 : 14)
    }
}

/// Drag handle under the transcript: a small bar, with the grab target and
/// resize cursor supplied by `TranscriptGrip` (the drag itself is handled by the panel).
struct TranscriptGripBar: View {
    @State private var hover = false
    var body: some View {
        ZStack {
            Capsule().fill(Color.primary.opacity(hover ? 0.32 : 0.16)).frame(width: 34, height: 3.5)
            TranscriptGrip()
        }
        .frame(maxWidth: .infinity).frame(height: 12)
        .onHover { hover = $0 }
    }
}

/// Diagonal lines in the bottom-right corner so the corner reads as grabbable.
struct CornerGrip: View {
    var body: some View {
        Canvas { context, size in
            for inset in [3.0, 7.0] {
                var path = Path()
                path.move(to: CGPoint(x: size.width - 1, y: inset))
                path.addLine(to: CGPoint(x: inset, y: size.height - 1))
                context.stroke(path, with: .color(.primary.opacity(0.28)), style: StrokeStyle(lineWidth: 1.1, lineCap: .round))
            }
        }
        .frame(width: 10, height: 10)
        .accessibilityHidden(true)
    }
}

// MARK: - Approval

struct ApprovalRow: View {
    var approval: ApprovalInfo
    var task: TaskItem? = nil
    var busy: Bool
    var onChoice: (String) -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: "hand.raised.fill").font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(scheme == .dark ? Tokens.amber : Color(red: 0.62, green: 0.36, blue: 0))
                VStack(alignment: .leading, spacing: 5) {
                    Text("Needs your OK").font(.system(size: 12, weight: .semibold))
                    ApprovalContext(approval: approval, task: task)
                }
            }
            HStack(spacing: 8) {
                Spacer()
                PillButton(title: "Deny", action: { onChoice("deny") })
                PillButton(title: "Approve", prominent: true, action: { onChoice("once") })
            }
            .disabled(busy)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous)
            .fill(Tokens.amber.opacity(scheme == .dark ? 0.12 : 0.16)))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous)
            .strokeBorder(Tokens.amber.opacity(0.45), lineWidth: 0.75))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Needs your approval: \(approval.description)")
    }
}

// MARK: - Task list

/// Every task of the call, newest last; tasks run in parallel. Tap a row to
/// open that task in the Work view.
struct TaskListView: View {
    @ObservedObject var model: VoicePanelModel
    /// Main panel: the last few rows, one line each.
    var compact: Bool
    /// Main panel: how many finished tasks to show. Running or waiting tasks always show (up to
    /// `compactRows`); older finished ones fold into "+N earlier", which opens the full list.
    var maxSettled: Int = 2
    static let compactRows = 4

    /// Newest-last, like `tasks`: every unfinished task (newest `compactRows`), plus the newest
    /// `maxSettled` finished ones, within `compactRows` rows in all.
    static func compactShown(_ tasks: [TaskItem], maxSettled: Int) -> [TaskItem] {
        let open = Set(tasks.filter { !$0.isSettled }.suffix(compactRows).map(\.id))
        let room = max(0, min(maxSettled, compactRows - open.count))
        let done = Set(tasks.filter(\.isSettled).suffix(room).map(\.id))
        return tasks.filter { open.contains($0.id) || done.contains($0.id) }
    }

    var body: some View {
        let tasks = model.state.tasks
        let shown = compact ? Self.compactShown(tasks, maxSettled: maxSettled) : tasks
        VStack(alignment: .leading, spacing: compact ? 2 : 4) {
            if compact {
                HStack(spacing: 6) {
                    Text(summary(tasks)).font(.system(size: 10.5, weight: .semibold)).tracking(0.6)
                        .foregroundStyle(.tertiary)
                    Spacer()
                    if tasks.count > shown.count {
                        Button(action: { model.onSelectTask(nil) }) {
                            Text("+\(tasks.count - shown.count) earlier").font(.system(size: 11)).foregroundStyle(.secondary)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .help("Show every task")
                    }
                    ClearDoneButton(model: model)
                }
                .padding(.horizontal, 4)
            }
            ForEach(shown.reversed()) { task in
                TaskRow(task: task, now: model.state.now, assistantName: model.state.assistantName, compact: compact,
                        canStop: task.canStop && model.state.connection.isOpen,
                        onOpen: { model.onOpenTask(task.id) },
                        onStop: { if let run = task.info.runID { model.onStopTask(run) } },
                        onDismiss: { if let run = task.info.runID { model.onDismissTasks([run]) } },
                        loadLive: model.loadLiveImage)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(summary(tasks))
    }

    private func summary(_ tasks: [TaskItem]) -> String {
        let running = tasks.filter(\.isActive).count
        return running == 0 ? "TASKS" : "TASKS · \(running) RUNNING"
    }
}

/// "Clear done": shown once two or more tasks have finished.
struct ClearDoneButton: View {
    @ObservedObject var model: VoicePanelModel
    var body: some View {
        let done = model.state.tasks.filter(\.isSettled).compactMap(\.info.runID)
        if done.count >= 2 {
            Button(action: { model.onDismissTasks(done) }) {
                Text("Clear done").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(Color.primary.opacity(0.07)))
                    .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .help("Clear every finished task from this list")
            .accessibilityLabel("Clear finished tasks")
        }
    }
}

struct TaskRow: View {
    var task: TaskItem
    var now: Date
    var assistantName: String = VoiceState.defaultAssistantName
    var compact: Bool
    var canStop: Bool
    var onOpen: () -> Void
    var onStop: () -> Void
    var onDismiss: () -> Void = {}
    /// Loads the live "looking at" thumbnail for a running task.
    var loadLive: ((String) async -> Data?)? = nil
    @State private var hover = false

    var body: some View {
        let (status, tone) = taskStatusLine(task.info, now: now, assistantName: assistantName)
        // A task that finished fine is one quiet line in the main panel; failures keep their reason.
        let oneLine = compact && task.isSettled && task.info.status == "completed"
        HStack(alignment: .center, spacing: 8) {
            icon
            if oneLine {
                Text(task.name).font(.system(size: 11.5, weight: .medium))
                    .lineLimit(1).truncationMode(.tail)
                    .foregroundStyle(.primary.opacity(0.75))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .help(status)
            } else {
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text(task.name).font(.system(size: 11.5, weight: .medium))
                        .lineLimit(compact ? 1 : 2).truncationMode(.tail)
                        .foregroundStyle(.primary.opacity(0.9))
                    // It carried your screen (eye) or a picture or file (paperclip).
                    if let glyph = sharedGlyph {
                        Image(systemName: glyph).font(.system(size: 9, weight: .semibold)).foregroundStyle(.tertiary)
                            .accessibilityLabel(glyph == "eye" ? "Carried your screen" : "Carried an attachment")
                    }
                }
                StatusText(text: status, tone: tone, clickable: false, maxLines: 3)
                    .help(status)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            }
            if task.isActive, let live = task.info.liveImage, let run = task.info.runID, let loadLive {
                LiveImageView(runID: run, live: live, height: compact ? 26 : 34, load: loadLive)
            }
            if canStop && (hover || !compact) {
                Button(action: onStop) {
                    Image(systemName: "stop.fill").font(.system(size: 8.5, weight: .bold))
                        .foregroundStyle(Tokens.red)
                        .frame(width: 22, height: 22)
                        .background(Circle().fill(Color.primary.opacity(0.08)))
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .help("Stop this task. The call and other tasks continue.")
                .accessibilityLabel("Stop task")
            }
            if task.isSettled && task.info.runID != nil {
                Button(action: onDismiss) {
                    Image(systemName: "xmark").font(.system(size: 8.5, weight: .bold))
                        .foregroundStyle(hover ? .secondary : .tertiary)
                        .frame(width: 22, height: 22)
                        .background(Circle().fill(Color.primary.opacity(hover ? 0.09 : 0.0)))
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .help("Clear this finished task from the list")
                .accessibilityLabel("Clear task")
            }
            Image(systemName: "chevron.right").font(.system(size: 8.5, weight: .semibold)).foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 8).padding(.vertical, oneLine ? 2 : compact ? 4 : 6)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
            .fill(Color.primary.opacity(hover ? 0.09 : oneLine ? 0.03 : 0.05)))
        .contentShape(Rectangle())
        .onTapGesture(perform: onOpen)
        .onHover { hover = $0 }
        .help("Show this task")
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder private var icon: some View {
        let (symbol, color): (String, Color) = {
            switch task.info.status {
            case "completed": return ("checkmark.circle.fill", Tokens.green)
            case "failed": return ("xmark.circle.fill", Tokens.red)
            case "cancelled", "interrupted": return ("stop.circle", .secondary)
            case "waiting_for_approval": return ("hand.raised.circle.fill", Tokens.amber)
            case "ambiguous": return ("questionmark.circle", Tokens.amber)
            default: return ("circle.dotted", Tokens.brassLight)
            }
        }()
        Image(systemName: symbol).font(.system(size: 13, weight: .semibold)).foregroundStyle(color)
            .frame(width: 16)
    }

    private var sharedGlyph: String? {
        let shared = task.info.shared
        if shared.isEmpty { return nil }
        return shared.contains { $0.kind == .screen } ? "eye" : "paperclip"
    }
}

// MARK: - Look at this: pending pictures and files, the hold line, what a task carried

/// A 36 pt picture thumbnail or a file chip (its icon and name): the pending row under the header,
/// and the strip of what a task carried in its detail.
struct AttachmentTile: View {
    enum Content {
        case picture(CGImage?)
        case file(String)
    }
    var content: Content
    static let side: CGFloat = 36

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 8, style: .continuous)
        switch content {
        case .picture(let image):
            ZStack {
                Color.primary.opacity(0.07)
                if let image {
                    Image(decorative: image, scale: 1).resizable().interpolation(.high).scaledToFill()
                } else {
                    Image(systemName: "photo").font(.system(size: 12)).foregroundStyle(.tertiary)
                }
            }
            .frame(width: Self.side, height: Self.side)
            .clipShape(shape)
            .overlay(shape.strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5))
        case .file(let name):
            // Hugs its name (long ones shortened in the middle, keeping the extension); a crowded
            // row still truncates it further.
            HStack(spacing: 6) {
                Image(nsImage: Self.icon(for: name)).resizable().interpolation(.high).frame(width: 18, height: 18)
                Text(Self.shortName(name)).font(.system(size: 11, weight: .medium)).foregroundStyle(.primary.opacity(0.85))
                    .lineLimit(1).truncationMode(.middle)
            }
            .padding(.horizontal, 9)
            .frame(height: Self.side)
            .background(shape.fill(Color.primary.opacity(0.07)))
            .overlay(shape.strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5))
        }
    }

    /// At most `limit` characters, the middle replaced with "…" and the extension kept.
    static func shortName(_ name: String, limit: Int = 22) -> String {
        guard name.count > limit else { return name }
        let ext = (name as NSString).pathExtension
        let tail = ext.isEmpty ? 6 : min(ext.count + 5, limit / 2)
        return String(name.prefix(limit - tail - 1)) + "…" + String(name.suffix(tail))
    }

    /// The system's icon for the file's type (by its extension; never reads the file).
    static func icon(for name: String) -> NSImage {
        let ext = (name as NSString).pathExtension
        return NSWorkspace.shared.icon(for: UTType(filenameExtension: ext) ?? .data)
    }
}

/// Pictures and files waiting to go with the next request, in drop order. No caption: each tile's
/// tooltip says where it goes, and its ✕ (on hover) takes it back.
struct PendingAttachmentsRow: View {
    var items: [PendingAttachmentItem]
    var onRemove: (String) -> Void

    var body: some View {
        HStack(spacing: 8) {
            ForEach(items) { item in
                PendingAttachmentTile(item: item, onRemove: { onRemove(item.id) })
                    .transition(.opacity)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(items.count == 1 ? "1 attachment for your next request"
                                             : "\(items.count) attachments for your next request")
    }
}

struct PendingAttachmentTile: View {
    var item: PendingAttachmentItem
    var onRemove: () -> Void
    @State private var hover = false

    var body: some View {
        // On its way to the plugin, or already claimed by a request: shown a little faded.
        let settling = item.phase == .uploading || item.phase == .sending
        AttachmentTile(content: item.kind == .picture ? .picture(item.thumbnail) : .file(item.name))
            .opacity(settling ? 0.55 : 1)
            .overlay(alignment: .topTrailing) {
                RemoveTileButton(name: item.kind == .picture ? "picture" : item.name, action: onRemove)
                    .offset(x: 5, y: -5)
                    .opacity(hover ? 1 : 0)
            }
            .contentShape(Rectangle())
            .onHover { hover = $0 }
            .help(item.kind == .file ? "\(item.name)\nGoes with your next request" : "Goes with your next request")
            // The ✕ only appears on hover, so VoiceOver gets Remove as an action on the tile itself.
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(item.kind == .picture ? "Picture" : item.name)
            .accessibilityHint("Goes with your next request")
            .accessibilityAction(named: "Remove") { onRemove() }
    }
}

/// The ✕ on a pending tile.
struct RemoveTileButton: View {
    var name: String
    var action: () -> Void
    @State private var hover = false
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        Button(action: action) {
            Image(systemName: "xmark").font(.system(size: 7, weight: .bold))
                .foregroundStyle(hover ? .primary : .secondary)
                .frame(width: 16, height: 16)
                .background(Circle().fill(scheme == .dark ? Color(white: 0.24) : Color.white))
                .overlay(Circle().strokeBorder(Color.primary.opacity(0.15), lineWidth: 0.5))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Don't send this")
        .accessibilityLabel("Remove \(name)")
        .accessibilityHint("It won't go with your next request")
    }
}

/// "Waiting for your screen" while a request waits for sharing to come on, then how it ended
/// ("Not sent: screen sharing was off"). The task-row style, one line.
struct HoldRow: View {
    var text: String
    var waiting: Bool
    var shortcutHint: String = ""

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Image(systemName: waiting ? "eye" : "eye.slash").font(.system(size: 12, weight: .semibold))
                .foregroundStyle(waiting ? AnyShapeStyle(Tokens.screen) : AnyShapeStyle(.secondary))
                .frame(width: 16)
            StatusText(text: text, tone: waiting ? .glimmer : .plain, clickable: false)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 8).padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.05)))
        .help(waiting ? "Turn on screen sharing to send it" + (shortcutHint.isEmpty ? "" : "\n" + shortcutHint) : text)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text)
    }
}

/// What a task carried to Hermes, in its detail: screen captures and pictures as thumbnails (click
/// for full size), files as chips.
struct SharedStrip: View {
    let runID: String
    let items: [SharedItem]
    var load: (String, Int) async -> Data?

    var body: some View {
        HStack(spacing: 8) {
            ForEach(items) { item in
                if item.isImage {
                    SharedThumbnail(runID: runID, item: item, load: load)
                } else {
                    AttachmentTile(content: .file(item.label))
                        .help(item.label)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel("Sent \(item.label)")
                }
            }
            Spacer(minLength: 0)
        }
    }
}

struct SharedThumbnail: View {
    let runID: String
    let item: SharedItem
    var load: (String, Int) async -> Data?
    @State private var image: NSImage?
    /// Made once with `image`, so the detail redrawing during a call never re-rasterizes it.
    @State private var cgImage: CGImage?

    var body: some View {
        Button {
            if let image { ImagePreviewWindow.shared.show(image, title: item.label) }
        } label: {
            AttachmentTile(content: .picture(cgImage))
        }
        .buttonStyle(.plain)
        .disabled(image == nil)
        .help(image == nil ? item.label : "\(item.label). Open full size")
        .accessibilityLabel("Sent \(item.label)")
        .task(id: "\(runID)/\(item.number)") {
            if let data = await load(runID, item.number), let loaded = NSImage(data: data) {
                cgImage = loaded.cgImage(forProposedRect: nil, context: nil, hints: nil)
                image = loaded
            }
        }
    }
}

// MARK: - Product photos
struct ProductPhoto: View {
    let runID: String
    let number: Int
    @ObservedObject var model: VoicePanelModel
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image { Image(nsImage: image).resizable().scaledToFit() }
            else { Image(systemName: "shippingbox").foregroundStyle(.secondary) }
        }
        .frame(width: 90, height: 90)
        .background(Color.primary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .task(id: "\(runID)/\(number)") {
            image = nil
            if let data = await model.loadProductImage(runID, number) {
                image = NSImage(data: data)
            }
        }
    }
}

// MARK: - Work view

struct WorkDetailView: View {
    @ObservedObject var model: VoicePanelModel
    @Environment(\.colorScheme) private var scheme

    /// Several tasks and none opened: show the list instead of one task.
    private var listing: Bool { model.showsTaskList && model.selectedTask == nil }
    /// The task being shown: the opened one, else the call's current work.
    private var info: WorkInfo? { model.selectedTask?.info ?? model.state.workInfo }

    var body: some View {
        let s = model.state
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(listing ? "\(s.assistantName)'s tasks" : (model.selectedTask?.info.title ?? info?.title ?? "\(s.assistantName)'s work"))
                    .font(.system(size: 12.5, weight: .semibold)).lineLimit(1).truncationMode(.tail)
                Spacer()
                if listing { ClearDoneButton(model: model) }
                if let task = model.selectedTask {
                    if task.canStop && s.connection.isOpen, let run = task.info.runID {
                        PillButton(title: "Stop task", destructive: true, action: { model.onStopTask(run) })
                            .help("Stop this task. The call and other tasks continue.")
                    }
                } else if !listing && model.presentation.showWorkStop {
                    PillButton(title: "Stop work", destructive: true, action: model.onStopWork)
                        .help("Stop this task. The voice call continues.")
                }
                PillButton(title: s.workOnly ? "Close" : "Back", action: model.onCloseWork)
            }
            PanelScrollView(maxHeight: 280 + model.extraHeight,
                            minHeight: model.extraHeight > 0 ? 280 + model.extraHeight : 0) {
                if listing {
                    TaskListView(model: model, compact: false)
                        .padding(.trailing, 6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    detail
                }
            }
            if let approval = s.approval {
                ApprovalRow(approval: approval, task: model.state.tasks.last { $0.info.runID == approval.runID },
                            busy: model.busy, onChoice: model.onApproval)
            }
        }
        .padding(.horizontal, 14).padding(.top, 11).padding(.bottom, 13)
    }

    @ViewBuilder private var detail: some View {
        let s = model.state
                VStack(alignment: .leading, spacing: 12) {
                    // "Now" first: with a picture above it, the 280 pt view cut it off at the bottom.
                    section("Now") {
                        VStack(alignment: .leading, spacing: 3) {
                            Self.nowStatus(nowLine, tone: nowTone)
                            if let detail = nowDetail {
                                Text(detail).font(.system(size: 11.5)).foregroundStyle(.secondary)
                                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            }
                            if let fresh = freshness {
                                Text(fresh).font(.system(size: 11.5)).foregroundStyle(.secondary)
                            }
                        }
                    }
                    if let info, !info.isTerminal, let live = info.liveImage, let runID = info.runID {
                        section(live.label) {
                            VStack(alignment: .leading, spacing: 4) {
                                LiveImageView(runID: runID, live: live, height: 170, load: model.loadLiveImage)
                                Text(live.name).font(.system(size: 11.5)).foregroundStyle(.secondary)
                                    .lineLimit(1).truncationMode(.middle)
                            }
                        }
                    }
                    if let request = info?.askedFor {
                        section("Your request") {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(request).font(.system(size: 12)).textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                                // The screen, pictures and files that went with it.
                                if let shared = info?.shared, !shared.isEmpty, let runID = info?.runID {
                                    SharedStrip(runID: runID, items: shared, load: model.loadSharedImage)
                                }
                            }
                        }
                    } else if info != nil {
                        section("Your request") {
                            Text("This job predates work history.").font(.system(size: 11.5)).foregroundStyle(.secondary)
                        }
                    }
                    if let milestones = info?.milestones, !milestones.isEmpty {
                        section("Progress") {
                            VStack(alignment: .leading, spacing: 5) {
                                ForEach(Array(milestones.enumerated()), id: \.offset) { _, item in
                                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                                        Text(item.at.map(Self.time) ?? "").font(.system(size: 11.5).monospacedDigit())
                                            .foregroundStyle(.tertiary).frame(width: 44, alignment: .leading)
                                        Text(item.text).font(.system(size: 11.5)).textSelection(.enabled)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                }
                            }
                        }
                    }
                    if let images = info?.images, !images.isEmpty, let runID = info?.runID {
                        section(images.count == 1 ? "Image" : "Images") {
                            VStack(spacing: 8) {
                                ForEach(images) { card in
                                    ImageResultCard(runID: runID, card: card, all: images, model: model)
                                }
                            }
                        }
                    }
                    if let products = info?.products, !products.isEmpty {
                        section("Products") {
                            VStack(spacing: 8) {
                                ForEach(products) { product in
                                    Link(destination: product.url) {
                                        HStack(alignment: .top, spacing: 10) {
                                            if product.imageURL != nil, let runID = info?.runID {
                                                ProductPhoto(runID: runID, number: product.number, model: model)
                                            }
                                            VStack(alignment: .leading, spacing: 4) {
                                                Text("\(product.number). \(product.name)")
                                                    .font(.system(size: 12, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                                                HStack(spacing: 6) {
                                                    if let price = product.price { Text(price).fontWeight(.semibold) }
                                                    if let store = product.store { Text(store) }
                                                    if let rating = product.rating { Text("★ \(rating)") }
                                                }.font(.system(size: 11)).foregroundStyle(.secondary)
                                                ForEach(product.specs, id: \.self) { spec in
                                                    Text("· \(spec)").font(.system(size: 11.5)).foregroundStyle(.secondary)
                                                        .fixedSize(horizontal: false, vertical: true)
                                                }
                                            }
                                            Spacer(minLength: 0)
                                            Image(systemName: "arrow.up.right").font(.system(size: 10))
                                        }
                                        .padding(9).frame(maxWidth: .infinity, alignment: .leading)
                                        .background(RoundedRectangle(cornerRadius: 9).fill(Color.primary.opacity(0.06)))
                                    }
                                    .buttonStyle(.plain)
                                    .accessibilityLabel("Product \(product.number): \(product.name), open \(product.store ?? "site")")
                                }
                            }
                        }
                    } else if let cards = info?.cards, !cards.isEmpty {
                        section("Links") {
                            VStack(alignment: .leading, spacing: 6) {
                                ForEach(cards) { card in
                                    Link(destination: card.url) {
                                        HStack(spacing: 6) {
                                            Text("\(card.number). \(card.title)").lineLimit(2)
                                            Spacer(minLength: 0)
                                            Image(systemName: "arrow.up.right").font(.system(size: 10))
                                        }
                                        .font(.system(size: 11.5)).padding(8)
                                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.06)))
                                    }.buttonStyle(.plain)
                                }
                            }
                        }
                    }
                    if let views = info?.views, !views.isEmpty {
                        VStack(spacing: 8) {
                            ForEach(views) { card in ViewCardView(card) }
                                .environment(\.questionActions, model.questionActions(for: model.selectedTask?.id ?? ""))
                        }
                    }
                    if let full = info?.result?.full ?? info?.result?.spoken {
                        section("Result") {
                            Text(linkified(full)).font(.system(size: 12)).textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(10)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))
                        }
                    }
                    if let drafts = info?.emailDrafts, !drafts.isEmpty {
                        section(drafts.count == 1 ? "Email draft" : "Email drafts") {
                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(drafts) { draft in EmailDraftCard(draft: draft, model: model) }
                            }
                        }
                    }
                    if info == nil && s.workOnly && s.approval == nil {
                        Text("No recent work.").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                }
                .padding(.trailing, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The full live status, wrapped — never truncated in the Work view.
    static func nowStatus(_ text: String, tone: StatusTone) -> some View {
        StatusText(text: text, tone: tone, clickable: false, wraps: true)
            .textSelection(.enabled)
            .accessibilityLabel(text)
    }

    private var nowLine: String {
        if let task = model.selectedTask { return taskStatusLine(task.info, now: model.state.now, assistantName: model.state.assistantName).0 }
        return workStatusLine(model.state.work, now: model.state.now, assistantName: model.state.assistantName,
                              failure: model.state.workInfo?.failure)?.0 ?? (model.state.workInfo == nil ? "No active work" : "Idle")
    }
    private var nowTone: StatusTone {
        if let task = model.selectedTask { return taskStatusLine(task.info, now: model.state.now, assistantName: model.state.assistantName).1 }
        return workStatusLine(model.state.work, now: model.state.now, assistantName: model.state.assistantName,
                              failure: model.state.workInfo?.failure)?.1 ?? .plain
    }
    private var nowDetail: String? {
        // A finished task's live detail is stale; a failed one says why it failed instead.
        if let task = model.selectedTask { return task.info.isTerminal ? failureDetail(task.info) : task.info.detail }
        switch model.state.work {
        case .active(_, let detail, _, _): return detail
        case .done: return failureDetail(model.state.workInfo)
        case .stale: return model.state.workInfo?.detail ?? "No verified update has arrived recently."
        case .notReceived: return "The request never reached \(model.state.assistantName). Say it again; if it keeps happening, restart Hermes."
        default: return nil
        }
    }
    private var freshness: String? {
        guard let at = info?.updatedAt ?? info?.updated else { return nil }
        let age = model.state.now.timeIntervalSince(at)
        let source = info?.statusSource.map { $0 == "authored" ? " · reported by \(model.state.assistantName)" : ($0 == "tool" ? " · from a tool step" : "") } ?? ""
        return "Updated \(formatElapsed(age)) ago\(source)"
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased()).font(.system(size: 10.5, weight: .semibold)).tracking(0.6).foregroundStyle(.secondary)
            content()
        }
    }

    static func time(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "h:mm"
        return formatter.string(from: date)
    }

    private func linkified(_ text: String) -> AttributedString {
        // Agent text is Markdown: render it (bold, links, bullets) instead of showing the marks.
        var attributed = AgentMarkdown.attributed(text)
        for run in attributed.runs where run.link != nil { attributed[run.range].foregroundColor = Tokens.blue }
        return attributed
    }
}

// MARK: - Problems and toasts

/// A call problem and its fix, like Screen Recording's "Set up": one cause, one button.
struct ProblemBanner: View {
    var problem: CallProblem
    var action: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: problem.fix == .openMicSettings ? "mic.slash.fill" : "exclamationmark.triangle.fill")
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(Color(red: 0.92, green: 0.38, blue: 0.32))
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 3) {
                Text(problem.title).font(.system(size: 12.5, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                if let detail = problem.detail {
                    Text(detail).font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            Spacer(minLength: 4)
            PillButton(title: problem.fixLabel, prominent: true, action: action).fixedSize()
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Color.red.opacity(0.09)))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(Color.red.opacity(0.3), lineWidth: 0.75))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(problem.title). \(problem.detail ?? "")")
    }
}

/// A short confirmation at the bottom of the panel ("Office lamp is off.").
struct ToastView: View {
    var toast: PanelToast
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        HStack(spacing: 7) {
            Image(systemName: toast.ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(toast.ok ? Color.green : Color.orange)
            Text(toast.text).font(.system(size: 12, weight: .medium)).lineLimit(2)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(Capsule().fill(scheme == .dark ? Color(white: 0.22) : Color.white).shadow(color: .black.opacity(0.25), radius: 8, y: 2))
        .overlay(Capsule().strokeBorder(Color.primary.opacity(0.1), lineWidth: 0.5))
        .accessibilityAddTraits(.updatesFrequently)
    }
}

/// Three dots that pulse while the reply is being worked out.
struct ThinkingDots: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 15, paused: reduceMotion)) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            HStack(spacing: 3) {
                ForEach(0..<3, id: \.self) { i in
                    Circle().fill(Color.secondary)
                        .frame(width: 4.5, height: 4.5)
                        .opacity(reduceMotion ? 0.7 : 0.3 + 0.7 * (sin(t * 5 - Double(i) * 0.8) + 1) / 2)
                }
            }
        }
        .accessibilityLabel("Thinking")
    }
}
