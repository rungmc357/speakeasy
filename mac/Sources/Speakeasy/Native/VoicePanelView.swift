import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// MARK: - Tokens

enum Tokens {
    static let width: CGFloat = 400
    static let minWidth: CGFloat = 340
    static let maxWidth: CGFloat = 760
    static let maxExtraHeight: CGFloat = 700
    static let radius: CGFloat = 18
    static let blue = Color(red: 0.49, green: 0.67, blue: 1.0)
    /// Brand palette (matches speakeasyvoice.ai): brass on warm near-black.
    static let brass = Color(red: 0.831, green: 0.635, blue: 0.298)      // #d4a24c
    static let brassLight = Color(red: 0.941, green: 0.784, blue: 0.447) // #f0c872
    static let brassGlow = Color(red: 1.0, green: 0.89, blue: 0.64)      // #ffe3a3
    static let brassDeep = Color(red: 0.42, green: 0.29, blue: 0.094)    // #6b4a18
    static let brassInk = Color(red: 0.09, green: 0.067, blue: 0.039)    // #17110a
    static let panelDark = Color(red: 0.086, green: 0.075, blue: 0.059)  // #16130f
    static let amber = Color(red: 0.97, green: 0.745, blue: 0.44)
    static let amberInk = Color(red: 0.23, green: 0.15, blue: 0.02)
    static let red = Color(red: 0.93, green: 0.36, blue: 0.36)
    static let green = Color(red: 0.45, green: 0.82, blue: 0.56)
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

// MARK: - the assistant mark

struct AssistantMark: View {
    var mark: PillPresentation.Mark
    /// 0...1: the assistant's voice while it speaks, the user's mic while listening.
    var level: Double = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let dim = mark == .muted || mark == .idle
        let reacting = !reduceMotion && (mark == .speaking || mark == .listening)
        let lift = reacting ? CGFloat(min(max(level, 0), 1)) : 0
        ZStack {
            // Glow grows with the voice.
            Circle()
                .fill(Tokens.brassLight.opacity(dim ? 0 : 0.28 + 0.4 * lift))
                .frame(width: 34, height: 34)
                .blur(radius: 7 + 7 * lift)
                .scaleEffect(1 + 0.3 * lift)
            Group {
                if mark == .connecting && !reduceMotion {
                    TimelineView(.animation(minimumInterval: 1 / 30)) { context in
                        let t = context.date.timeIntervalSinceReferenceDate
                        orb.scaleEffect(0.92 + 0.06 * CGFloat((sin(t * 3.2) + 1) / 2))
                    }
                } else {
                    orb.scaleEffect(1 + 0.16 * lift)
                }
            }
            .saturation(dim ? 0.2 : 1)
            .opacity(dim ? 0.6 : 1)
            if mark == .muted {
                Image(systemName: "mic.slash.fill")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(Tokens.amberInk)
                    .padding(3)
                    .background(Circle().fill(Tokens.amber))
                    .offset(x: 13, y: 13)
            }
        }
        .frame(width: 36, height: 36)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: lift)
        .accessibilityHidden(true)
    }

    private var orb: some View {
        Circle()
            .fill(RadialGradient(colors: [Tokens.brassGlow, Tokens.brass, Tokens.brassDeep],
                                 center: UnitPoint(x: 0.35, y: 0.35), startRadius: 1, endRadius: 22))
            .overlay(Circle().strokeBorder(.white.opacity(0.18), lineWidth: 0.5))
            .frame(width: 32, height: 32)
    }
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

struct MicButton: View {
    var muted: Bool
    var enabled: Bool
    var shortcutHint: String = ""
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        // Icon only: lit (filled, full strength) when live; slashed and dimmed when muted.
        Button(action: action) {
            Image(systemName: muted ? "mic.slash.fill" : "mic.fill").font(.system(size: 12.5, weight: .semibold))
                .frame(width: 30, height: 30)
                .foregroundStyle(muted ? Color.primary.opacity(0.45) : Tokens.green)
                .background(Circle().fill(muted ? Color.primary.opacity(hover ? 0.1 : 0.05)
                                                : Tokens.green.opacity(hover ? 0.2 : 0.13)))
                .overlay(Circle().strokeBorder(muted ? Color.primary.opacity(0.08) : Tokens.green.opacity(0.45),
                                               lineWidth: 0.75))
                .contentShape(Circle())
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

struct VoicePanelView: View {
    @ObservedObject var model: VoicePanelModel
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let p = model.presentation
        VStack(alignment: .leading, spacing: 0) {
            if !model.state.workOnly { header(p) }
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
            if model.showsSlim {
                // Slim: header only. A task summary stands in for the list; clicking it
                // (or the status line, or the expand button) brings the full panel back.
                if let summary = slimTaskSummary(model.state.tasks, approvalPending: model.state.approval != nil) {
                    Button { model.onToggleSlim() } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "list.bullet")
                            Text(summary).lineLimit(1)
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.down")
                        }
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10).frame(height: 24)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Show the full panel")
                    .accessibilityLabel("\(summary). Show the full panel")
                    .padding(.horizontal, 8).padding(.bottom, 6)
                }
            } else if model.workExpanded || model.state.workOnly {
                if !model.state.workOnly { Divider().opacity(0.5).padding(.horizontal, 14) }
                WorkDetailView(model: model)
            } else {
                if model.showCaptions, !model.state.exchange.isEmpty {
                    VStack(spacing: 0) {
                        CaptionView(exchange: model.state.exchange.cleaned, transcript: model.state.transcript,
                                    assistantName: model.state.assistantName, expanded: $model.captionExpanded,
                                    extraHeight: model.extraHeight)
                        TranscriptGripBar()
                    }
                    .padding(.horizontal, 14).padding(.bottom, 4)
                    .transition(.opacity)
                }
                if model.showsTaskList {
                    TaskListView(model: model, compact: true)
                        .padding(.horizontal, 12).padding(.bottom, 10)
                        .transition(.opacity)
                }
                let reviewing = Set(model.pinnedReviews.map(\.taskID))
                if let task = model.state.tasks.last(where: { !reviewing.contains($0.id) &&
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
            if let approval = model.state.approval, !(model.workExpanded || model.state.workOnly) {
                ApprovalRow(approval: approval, busy: model.busy, onChoice: model.onApproval)
                    .padding(.horizontal, 12).padding(.bottom, 12)
            }
            if !(model.workExpanded || model.state.workOnly), let pinned = model.pinnedViews {
                VStack(spacing: 8) {
                    ForEach(pinned.views.prefix(2)) { card in ViewCardView(card) }
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
            if !(model.workExpanded || model.state.workOnly) {
                ForEach(model.pinnedDrafts) { draft in
                    EmailDraftCard(draft: draft, model: model)
                        .padding(.horizontal, 12).padding(.bottom, 12)
                }
                // Finished images pop up for review without opening the task; they stay until dismissed.
                let reviews = model.pinnedReviews
                let open = ImageReviewLayout.focused(reviews, picked: model.focusedReviewID)
                ForEach(reviews) { review in
                    ImageReviewCard(review: review, shownCount: reviews.count + model.pinnedDrafts.count,
                                    compact: review.runID != open, model: model)
                        .padding(.horizontal, 12).padding(.bottom, review.runID == reviews.last?.runID ? 12 : 6)
                }
            }
        }
        .frame(width: model.panelWidth, alignment: .top)
        .onHover { model.hovering = $0 }
        .fixedSize(horizontal: false, vertical: true)
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
        .overlay(alignment: .bottomTrailing) { CornerGrip().padding(5).allowsHitTesting(false) }
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.workExpanded)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.captionExpanded)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.state.approval)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.state.tasks.count)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.pinnedReviews.map(\.id))
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.slim)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.room?.isOn)
    }

    private func header(_ p: PillPresentation) -> some View {
        HStack(spacing: 8) {
            AssistantMark(mark: p.mark, level: model.orbLevel)
                .padding(.trailing, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(p.primary).font(.system(size: 13.5, weight: .semibold)).lineLimit(1)
                    .minimumScaleFactor(0.85)
                    .help(p.primary)
                    .contentTransition(.opacity)
                Button(action: { if p.statusClickable || model.showsSlim { model.onToggleWork() } }) {
                    StatusText(text: model.shownStatus, tone: model.shownTone, clickable: p.statusClickable || model.showsSlim,
                               maxLines: 2)
                        .id(model.shownStatus)
                        .transition(.opacity)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!(p.statusClickable || model.showsSlim))
                .help(p.statusClickable || model.showsSlim ? "\(model.shownStatus)\nShow tasks" : model.shownStatus)
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.35), value: model.shownStatus)
                .accessibilityLabel(model.shownStatus)
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
            if model.state.connection != .paused {
                MicButton(muted: p.micMuted, enabled: p.micEnabled, shortcutHint: model.muteShortcutHint, action: model.onToggleMic)
                    .fixedSize()
            }
            if !model.state.workOnly && model.state.connection.isOpen {
                IconButton(symbol: model.slim ? "arrow.up.left.and.arrow.down.right" : "arrow.down.right.and.arrow.up.left",
                           help: model.slim ? "Full panel" : "Slim — just the controls",
                           action: model.onToggleSlim)
                    .fixedSize()
            }
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
        .padding(.leading, 12).padding(.trailing, 12).padding(.vertical, 8)
        .frame(minHeight: 64)
    }
}

// MARK: - Captions

/// One turn of the conversation, sized to its content: short turns stay short,
/// the user's turns sit on the right, the assistant's on the left.
struct TranscriptBubble: View {
    var turn: TranscriptTurn
    var assistantName: String
    var body: some View {
        let mine = turn.speaker == .you
        HStack(spacing: 0) {
            if mine { Spacer(minLength: 36) }
            VStack(alignment: mine ? .trailing : .leading, spacing: 2) {
                Text(mine ? "You" : assistantName).font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.tertiary)
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
}

struct CaptionView: View {
    var exchange: Exchange
    /// The whole call so far; the expanded view renders it as chat bubbles.
    var transcript: [TranscriptTurn] = []
    var assistantName: String = VoiceState.defaultAssistantName
    @Binding var expanded: Bool
    var extraHeight: CGFloat = 0

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
                VStack(alignment: .leading, spacing: 3) {
                    if !exchange.you.isEmpty { line("You", exchange.you, full: false) }
                    if !exchange.assistant.isEmpty { line(assistantName, exchange.assistant, full: false) }
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

    @ViewBuilder
    private func line(_ speaker: String, _ text: String, full: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Text(speaker).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(.tertiary)
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
    var busy: Bool
    var onChoice: (String) -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: "hand.raised.fill").font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(scheme == .dark ? Tokens.amber : Color(red: 0.62, green: 0.36, blue: 0))
                Text(approval.description).font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(.primary).fixedSize(horizontal: false, vertical: true)
                    .lineLimit(4).textSelection(.enabled)
            }
            HStack(spacing: 8) {
                Spacer()
                PillButton(title: "Deny", action: { onChoice("deny") })
                PillButton(title: "Approve once", prominent: true, action: { onChoice("once") })
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
    static let compactRows = 4

    var body: some View {
        let tasks = model.state.tasks
        let shown = compact ? Array(tasks.suffix(Self.compactRows)) : tasks
        VStack(alignment: .leading, spacing: compact ? 2 : 4) {
            if compact {
                HStack(spacing: 6) {
                    Text(summary(tasks)).font(.system(size: 9.5, weight: .semibold)).tracking(0.6)
                        .foregroundStyle(.tertiary)
                    Spacer()
                    if tasks.count > shown.count {
                        Text("+\(tasks.count - shown.count) earlier").font(.system(size: 10)).foregroundStyle(.tertiary)
                    }
                    ClearDoneButton(model: model)
                }
                .padding(.horizontal, 4)
            }
            ForEach(shown.reversed()) { task in
                TaskRow(task: task, now: model.state.now, assistantName: model.state.assistantName, compact: compact,
                        canStop: task.canStop && model.state.connection.isOpen,
                        onOpen: { model.onSelectTask(task.id) },
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
                Text("Clear done").font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
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
        HStack(alignment: .center, spacing: 8) {
            icon
            VStack(alignment: .leading, spacing: 1) {
                Text(task.name).font(.system(size: 11.5, weight: .medium))
                    .lineLimit(compact ? 1 : 2).truncationMode(.tail)
                    .foregroundStyle(.primary.opacity(0.9))
                StatusText(text: status, tone: tone, clickable: false, maxLines: 3)
                    .help(status)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
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
        .padding(.horizontal, 8).padding(.vertical, compact ? 4 : 6)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
            .fill(Color.primary.opacity(hover ? 0.09 : 0.05)))
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
                ApprovalRow(approval: approval, busy: model.busy, onChoice: model.onApproval)
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
                                Text(fresh).font(.system(size: 10.5)).foregroundStyle(.tertiary)
                            }
                        }
                    }
                    if let info, !info.isTerminal, let live = info.liveImage, let runID = info.runID {
                        section(live.label) {
                            VStack(alignment: .leading, spacing: 4) {
                                LiveImageView(runID: runID, live: live, height: 170, load: model.loadLiveImage)
                                Text(live.name).font(.system(size: 10.5)).foregroundStyle(.secondary)
                                    .lineLimit(1).truncationMode(.middle)
                            }
                        }
                    }
                    if let request = info?.askedFor {
                        section("Your request") {
                            Text(request).font(.system(size: 12)).textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
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
                                        Text(item.at.map(Self.time) ?? "").font(.system(size: 10.5).monospacedDigit())
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
                                                    Text("· \(spec)").font(.system(size: 10.5)).foregroundStyle(.secondary)
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
            Text(title.uppercased()).font(.system(size: 9.5, weight: .semibold)).tracking(0.6).foregroundStyle(.tertiary)
            content()
        }
    }

    static func time(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "h:mm"
        return formatter.string(from: date)
    }

    private func linkified(_ text: String) -> AttributedString {
        var attributed = AttributedString(text)
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return attributed }
        let ns = text as NSString
        for match in detector.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            guard let url = match.url, let range = Range(match.range, in: text),
                  let lower = AttributedString.Index(range.lowerBound, within: attributed),
                  let upper = AttributedString.Index(range.upperBound, within: attributed) else { continue }
            attributed[lower..<upper].link = url
            attributed[lower..<upper].foregroundColor = Tokens.blue
        }
        return attributed
    }
}
