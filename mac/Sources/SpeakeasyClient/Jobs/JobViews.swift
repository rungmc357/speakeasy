import SwiftUI
import SpeakeasyCore

// The orb, the job board's tiles and the job view's content (docs/plans/2026-10-14-002-feat-job-view-plan.md),
// shared by the Mac panel and the iPhone/iPad app so both draw the same thing. Platform chrome (the
// Mac's panel, scrolling and keys; the phone's navigation) stays in each app.

// MARK: - Environment

/// How the platform draws the cards this module can't: a design review, an email draft, an approval.
public struct JobRenderers {
    public var review: (ImageReview, _ heading: String?) -> AnyView
    public var draft: (EmailDraft) -> AnyView
    public var approval: (ApprovalInfo) -> AnyView
    public init(review: @escaping (ImageReview, String?) -> AnyView = { r, _ in AnyView(Text(PictureRow.label(r)).foregroundStyle(.secondary)) },
                draft: @escaping (EmailDraft) -> AnyView = { d in AnyView(Text(d.subject).foregroundStyle(.secondary)) },
                approval: @escaping (ApprovalInfo) -> AnyView = { a in AnyView(Text(a.description).foregroundStyle(.secondary)) }) {
        self.review = review; self.draft = draft; self.approval = approval
    }
}

private struct JobRenderersKey: EnvironmentKey { static let defaultValue = JobRenderers() }
private struct JobTextScaleKey: EnvironmentKey { static let defaultValue: CGFloat = 1 }
private struct JobHostDrawsStepsKey: EnvironmentKey { static let defaultValue = false }
private struct JobHostShowsNowKey: EnvironmentKey { static let defaultValue = false }
private struct JobShownBesideKey: EnvironmentKey { static let defaultValue: String? = nil }

public extension EnvironmentValues {
    var jobRenderers: JobRenderers {
        get { self[JobRenderersKey.self] }
        set { self[JobRenderersKey.self] = newValue }
    }
    /// The job whose page is open beside the board (a wide layout: iPhone Duo open, iPad). Its board
    /// card leaves out "See options": the options are already on screen.
    var jobShownBeside: String? {
        get { self[JobShownBesideKey.self] }
        set { self[JobShownBesideKey.self] = newValue }
    }
    /// Text and control size for the job views: 1 on the Mac panel, larger for touch.
    var jobTextScale: CGFloat {
        get { self[JobTextScaleKey.self] }
        set { self[JobTextScaleKey.self] = newValue }
    }
    /// The host page draws the job's steps itself (the phone's step path / live activity), so the
    /// job body leaves its own steps card out. Off by default: the Mac panel is unchanged.
    var jobHostDrawsSteps: Bool {
        get { self[JobHostDrawsStepsKey.self] }
        set { self[JobHostDrawsStepsKey.self] = newValue }
    }
    /// The host page already says what a running job is doing now (status line, pinned tool call) and
    /// has Stop in its header, so the steps card drops its "Now" line and its Stop. Off by default.
    var jobHostShowsNow: Bool {
        get { self[JobHostShowsNowKey.self] }
        set { self[JobHostShowsNowKey.self] = newValue }
    }
}

public extension JobStyle {
    /// "3:42" for a step's time.
    static func clock(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "h:mm"
        return formatter.string(from: date)
    }
}

// MARK: - Style

/// The touch look (iPhone, iPad, Vision Pro): inset-grouped cards on the dark background, the
/// brand's brass as the one accent, colour otherwise only for status. The Mac panel keeps its look.
public enum JobLook {
    #if os(macOS)
    public static let touch = false
    #else
    public static let touch = true
    #endif
    public static let brass = BrandPalette.brass         // #d4a24c (deeper on light)
    public static let brassInk = BrandPalette.onBrass    // text on brass
    /// A grouped section's fill and its corner radius.
    /// Touch on light: a clean near-white sheet instead of a grey wash.
    #if os(macOS)
    public static let card = Color.primary.opacity(0.075)
    #else
    public static let card = Color.adaptive(dark: Color.primary.opacity(0.075), light: Color(hex: 0xfffdf9))
    #endif
    public static let radius: CGFloat = 16
}

extension View {
    /// On touch: a grouped section (16 pt in, continuous corners, no border). On the Mac: unchanged.
    @ViewBuilder public func touchCard(padding: CGFloat = 16) -> some View {
        if JobLook.touch {
            self.padding(padding)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: JobLook.radius, style: .continuous).fill(JobLook.card))
        } else {
            self
        }
    }
}

public enum JobStyle {
    #if os(macOS)
    // Mac redesign: the same one accent as touch, a touch lighter for text on the dark panel.
    public static let blue = JobLook.brass
    public static let blueInk = Color.adaptive(dark: Color(red: 0.90, green: 0.73, blue: 0.42), light: Color(hex: 0x8c5a12)) // #e6ba6b
    /// Text on `blue`.
    public static let onBlue = JobLook.brassInk
    #else
    // Touch: one accent. What the Mac draws blue (primary buttons, links, the current step) is brass.
    public static let blue = JobLook.brass
    public static let blueInk = JobLook.brass
    public static let onBlue = JobLook.brassInk
    #endif
    public static let amber = Color.adaptive(dark: Color(red: 0.94, green: 0.74, blue: 0.36), light: Color(hex: 0x8f5300)) // #f0bd5c
    public static let green = Color.adaptive(dark: Color(red: 0.50, green: 0.85, blue: 0.60), light: Color(hex: 0x2a7340)) // #7fd99a
    public static let red = Color.adaptive(dark: Color(red: 0.94, green: 0.54, blue: 0.48), light: Color(hex: 0xb3261e))   // #f08a7a

    public enum Pill { case amber, blue, green, red, grey }
    public static func pillColors(_ p: Pill) -> (Color, Color) {
        switch p {
        case .amber: return (amber.opacity(0.20), amber)
        case .blue: return (blueInk.opacity(0.18), blueInk)
        case .green: return (green.opacity(0.18), green)
        case .red: return (red.opacity(0.20), red)
        case .grey: return (Color.primary.opacity(0.10), Color.secondary)
        }
    }
    public static func pill(tone: String) -> Pill {
        switch tone { case "good": return .green; case "warn": return .amber; case "bad": return .red; case "info": return .blue; default: return .grey }
    }

    /// The panel's amber (needs-you tiles and briefs), its stop red, and the ink on amber.
    public static let tileAmber = BrandPalette.amber
    public static let amberInk = Color.adaptive(dark: Color(red: 0.23, green: 0.15, blue: 0.02), light: Color(hex: 0xfff8ec))
    public static let stopRed = BrandPalette.red
    #if os(macOS)
    public static let nowBlue = JobLook.brass
    #else
    public static let nowBlue = JobLook.brass
    #endif
}

public struct JobPill: View {
    @Environment(\.jobTextScale) private var s
    var text: String
    var style: JobStyle.Pill
    public init(text: String, style: JobStyle.Pill) { self.text = text; self.style = style }
    public var body: some View {
        let (bg, fg) = JobStyle.pillColors(style)
        if JobLook.touch {
            // A status or a tag in its colour, no capsule: chips on chips read as clutter on a phone.
            Text(text).font(.system(size: 11 * s, weight: .medium)).lineLimit(1)
                .foregroundStyle(style == .blue ? Color.secondary : fg)
                .fixedSize()
        } else {
            Text(text).font(.system(size: 11 * s, weight: .semibold)).lineLimit(1)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(bg)).foregroundStyle(fg)
                .fixedSize()
        }
    }
}

public struct SectionLabel: View {
    @Environment(\.jobTextScale) private var s
    var text: String
    public init(text: String) { self.text = text }
    public var body: some View {
        Text(text.uppercased()).font(.system(size: 10.5 * s, weight: .semibold)).tracking(0.7).foregroundStyle(.secondary)
    }
}

/// A full-width button in a tile (Approve / See options).
public struct TileButton: View {
    @Environment(\.jobTextScale) private var s
    var title: String
    var primary = false
    /// A recommended answer can run to two lines ("Ship it and install it tonight").
    var lines = 1
    var action: () -> Void
    public init(title: String, primary: Bool = false, lines: Int = 1, action: @escaping () -> Void) {
        self.title = title; self.primary = primary; self.lines = lines; self.action = action
    }
    @State private var hover = false
    public var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 11.5 * s, weight: .semibold)).lineLimit(lines)
                .multilineTextAlignment(.center).padding(.horizontal, 10)
                .frame(maxWidth: .infinity).frame(minHeight: (JobLook.touch ? 28 : 26) * s)
                .foregroundStyle(primary ? JobStyle.onBlue : Color.primary)
                .background(RoundedRectangle(cornerRadius: JobLook.touch ? 100 : 8, style: .continuous)
                    .fill(primary ? JobStyle.blue.opacity(hover ? 1 : 0.92) : Color.primary.opacity(hover ? 0.16 : 0.10)))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
    }
}

/// "Not needed": set aside what a job waits on without answering it. A quiet text link, always
/// secondary to Approve, See options and the options themselves.
public struct NotNeededButton: View {
    @Environment(\.jobTextScale) private var s
    var action: () -> Void
    public init(action: @escaping () -> Void) { self.action = action }
    @State private var hover = false
    public var body: some View {
        Button(action: action) {
            Text("Not needed").font(.system(size: 11 * s, weight: .medium))
                .foregroundStyle(hover ? Color.primary.opacity(0.75) : Color.secondary)
                .underline(hover)
                .padding(.horizontal, 4).frame(height: 26 * s)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .fixedSize()
        .help(Self.help)
        .accessibilityHint("Already handled or not needed. Closes it without answering.")
    }
    #if os(macOS)
    static let help = "Already handled or not needed: close it without answering (N)"
    #else
    static let help = "Already handled or not needed: close it without answering"
    #endif
}

/// Wraps children onto new lines (done chips, tag pills).
public struct WrapLayout: Layout {
    public var spacing: CGFloat = 5
    public init(spacing: CGFloat = 5) { self.spacing = spacing }
    public func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 300
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            let w = min(size.width, width)
            if x > 0 && x + w > width { y += row + spacing; x = 0; row = 0 }
            x += w + spacing; row = max(row, size.height); widest = max(widest, x - spacing)
        }
        return CGSize(width: proposal.width ?? widest, height: y + row)
    }
    public func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            let w = min(size.width, bounds.width)
            if x > bounds.minX && x + w > bounds.maxX { y += row + spacing; x = bounds.minX; row = 0 }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(width: w, height: size.height))
            x += w + spacing; row = max(row, size.height)
        }
    }
}

// MARK: - The orb

// MARK: - The orb

/// The one orb of a call: the folded panel, the panel header, the board and the job view all draw
/// this, so a colour means the same everywhere. Grey idle, blue the user talking, violet working,
/// amber something needs you (with a count), red a failure; a calm blue dot marks unread answers.
public struct JobOrb: View {
    @Environment(\.jobTextScale) private var s
    var state: OrbState
    var size: CGFloat = 40
    /// 0...1: the user's mic while they talk, the assistant's voice while it speaks.
    var level: Double = 0
    /// What the call is doing (speaking moves the orb a little, muted dims it). nil = no call detail.
    var mark: PillPresentation.Mark? = nil
    /// Answers finished and not opened yet: a small blue dot when nothing more pressing shows.
    var unreadAnswers: Int = 0
    /// Where this orb sits ("folded", "header"); the smoke test reads what each one drew.
    var placement = ""
    /// The mic is hearing the user right now (the ring follows the mic level).
    var hearing = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    public init(state: OrbState, size: CGFloat = 40, level: Double = 0, mark: PillPresentation.Mark? = nil,
                unreadAnswers: Int = 0, placement: String = "", hearing: Bool = false) {
        self.state = state; self.size = size; self.level = level; self.mark = mark
        self.unreadAnswers = unreadAnswers; self.placement = placement; self.hearing = hearing
    }

    /// The voice ring around the orb: a quiet blue ring while the mic is open, swelling with your
    /// voice while it hears you and with the reply while it speaks, and a turning arc while thinking.
    /// Its colour never fights the orb's (amber still means needs you).
    @ViewBuilder private var voiceRing: some View {
        let blue = Color(red: 0.36, green: 0.62, blue: 1.0)
        let lw = max(1.5, size * 0.055)
        if mark == .thinking {
            if reduceMotion {
                Circle().stroke(blue.opacity(0.6), style: StrokeStyle(lineWidth: lw, dash: [3, 3]))
            } else {
                TimelineView(.animation(minimumInterval: 1 / 30)) { ctx in
                    let t = ctx.date.timeIntervalSinceReferenceDate
                    Circle().trim(from: 0, to: 0.3)
                        .stroke(blue, style: StrokeStyle(lineWidth: lw, lineCap: .round))
                        .rotationEffect(.degrees(t * 300))
                }
            }
        } else if mark == .speaking {
            Circle().stroke(Color.white.opacity(0.35 + 0.5 * Double(voice)), lineWidth: lw)
                .scaleEffect(1 + (reduceMotion ? 0 : 0.18 * voice))
                .animation(.easeOut(duration: 0.1), value: voice)
        } else if mark == .listening {
            Circle().stroke(blue.opacity(hearing ? 0.55 + 0.45 * Double(voice) : 0.4), lineWidth: lw)
                .scaleEffect(1 + (reduceMotion || !hearing ? 0 : 0.2 * voice))
                .animation(.easeOut(duration: 0.1), value: voice)
        }
    }

    public static func colors(_ state: OrbState) -> [Color] {
        switch state {
        case .idle: return [Color(red: 0.86, green: 0.87, blue: 0.91), Color(red: 0.52, green: 0.55, blue: 0.64), Color(red: 0.28, green: 0.30, blue: 0.37)]
        case .talking: return [Color(red: 0.86, green: 0.94, blue: 1.0), Color(red: 0.36, green: 0.62, blue: 1.0), Color(red: 0.12, green: 0.31, blue: 0.70)]
        case .working: return [Color(red: 0.80, green: 0.74, blue: 1.0), Color(red: 0.45, green: 0.45, blue: 0.98), Color(red: 0.22, green: 0.20, blue: 0.62)]
        case .needsYou: return [Color(red: 1.0, green: 0.90, blue: 0.69), Color(red: 0.94, green: 0.65, blue: 0.19), Color(red: 0.60, green: 0.36, blue: 0.05)]
        case .failed: return [Color(red: 1.0, green: 0.74, blue: 0.68), Color(red: 0.90, green: 0.34, blue: 0.29), Color(red: 0.54, green: 0.12, blue: 0.09)]
        }
    }
    public static let answerBlue = Color(red: 0.37, green: 0.61, blue: 0.94)
    /// `answerBlue` for text: deeper on a light background (AA).
    public static let answerBlueText = Color.adaptive(dark: answerBlue, light: Color(hex: 0x2f62b8))

    private var dim: Bool { mark == .muted || mark == .idle }
    private var answerDot: Bool { state.showsAnswerDot(unread: unreadAnswers) }
    private var voice: CGFloat { CGFloat(min(max(level, 0), 1)) }

    public var body: some View {
        Group {
            if reduceMotion { orb(scale: 1, glow: 0.45) }
            else {
                switch state {
                case .talking:
                    // Breathing: a slow swell, lifted by the voice.
                    TimelineView(.animation(minimumInterval: 1 / 30)) { ctx in
                        let t = ctx.date.timeIntervalSinceReferenceDate
                        let breath = (sin(t * 2.4) + 1) / 2
                        orb(scale: 1 + 0.04 * breath + 0.2 * voice, glow: 0.45 + 0.35 * breath + 0.3 * Double(voice))
                    }
                case _ where mark == .connecting:
                    TimelineView(.animation(minimumInterval: 1 / 30)) { ctx in
                        let t = ctx.date.timeIntervalSinceReferenceDate
                        orb(scale: 0.92 + 0.06 * CGFloat((sin(t * 3.2) + 1) / 2), glow: 0.3)
                    }
                case .working:
                    // Soft pulse of the glow only; the orb keeps its size (it still lifts a little with the voice).
                    TimelineView(.animation(minimumInterval: 1 / 20)) { ctx in
                        let t = ctx.date.timeIntervalSinceReferenceDate
                        orb(scale: 1 + speakingLift, glow: 0.25 + 0.4 * (sin(t * 1.6) + 1) / 2)
                    }
                default:
                    // The assistant speaking moves the orb a little, in whatever colour it is.
                    orb(scale: 1 + speakingLift, glow: (state == .idle ? 0.1 : 0.55) + 0.4 * Double(speakingLift * 8))
                        .animation(.easeOut(duration: 0.12), value: speakingLift)
                }
            }
        }
        .saturation(dim ? 0.25 : 1)
        .opacity(dim ? 0.7 : 1)
        .overlay { voiceRing.frame(width: size + 6, height: size + 6).allowsHitTesting(false) }
        .frame(width: size + 10, height: size + 10)
        .overlay(alignment: .topTrailing) {
            if case .needsYou(let n) = state {
                // The count shows only when something needs you.
                Text("\(n)").font(.system(size: size < 34 ? 9.5 : max(10, size * 0.14), weight: .bold)).monospacedDigit()
                    .foregroundStyle(Color(red: 0.6, green: 0.36, blue: 0.05))
                    .padding(.horizontal, 4).frame(minWidth: max(15, size * 0.2), minHeight: max(15, size * 0.2))
                    .background(Capsule().fill(Color.white))
                    .offset(x: 1, y: -1)
            } else if answerDot {
                Circle().fill(Self.answerBlue).frame(width: 9, height: 9)
                    .overlay(Circle().strokeBorder(Color.white.opacity(0.85), lineWidth: 1.2))
                    .offset(x: -2, y: 2)
                    .accessibilityHidden(true)
            }
        }
        .overlay(alignment: .bottomTrailing) {
            if mark == .muted {
                Image(systemName: "mic.slash.fill").font(.system(size: 8 * s, weight: .bold))
                    .foregroundStyle(JobStyle.amberInk).padding(3)
                    .background(Circle().fill(JobStyle.tileAmber))
                    .offset(x: -1, y: -1)
            }
        }
        .accessibilityElement()
        .accessibilityLabel(accessibilityText)
        .onAppear { Self.drawn[placement] = Drawn(state: state, answerDot: answerDot) }
        .onChange(of: Drawn(state: state, answerDot: answerDot)) { _, now in Self.drawn[placement] = now }
    }

    public struct Drawn: Equatable {
        public var state: OrbState; public var answerDot: Bool
        public init(state: OrbState, answerDot: Bool) { self.state = state; self.answerDot = answerDot }
    }
    /// What each placed orb last drew (panel smoke checks the header and the folded orb agree).
    @MainActor public static var drawn: [String: Drawn] = [:]

    private var speakingLift: CGFloat { !reduceMotion && mark == .speaking ? 0.2 * voice : 0 }

    private var accessibilityText: String {
        guard answerDot else { return state.label }
        return state.label + (unreadAnswers == 1 ? ", 1 new answer" : ", \(unreadAnswers) new answers")
    }

    private func orb(scale: CGFloat, glow: Double) -> some View {
        let colors = Self.colors(state)
        return ZStack {
            Circle().fill(colors[1].opacity(min(glow, 0.95))).frame(width: size, height: size).blur(radius: size * 0.22)
            Circle()
                .fill(RadialGradient(colors: colors, center: UnitPoint(x: 0.35, y: 0.3), startRadius: 1, endRadius: size * 0.62))
                .overlay(Circle().strokeBorder(.white.opacity(0.2), lineWidth: 0.5))
                .frame(width: size, height: size)
        }
        .scaleEffect(scale)
    }
}

// MARK: - Board tiles

/// A finished answer the user hasn't opened: what it was about, the answer in one line, and a
/// small picture of it when the result has a card worth a glance.
public struct AnswerTile: View {
    @Environment(\.jobTextScale) private var s
    var job: Job
    var focused: Bool
    var action: () -> Void
    /// Dismiss without opening (the x, or Dismiss in the long-press menu): it moves to Done today.
    var onDismiss: (() -> Void)?
    /// What the voice is saying right now: the tile doesn't print the same line again under it.
    var spokenNow: String?
    public init(job: Job, focused: Bool, spokenNow: String? = nil, onDismiss: (() -> Void)? = nil, action: @escaping () -> Void) {
        self.job = job; self.focused = focused; self.spokenNow = spokenNow; self.onDismiss = onDismiss; self.action = action
    }
    @State private var hover = false

    public var body: some View {
        let info = (job.newTask ?? job.newest).info
        let look = job.answerTask == nil && job.lookTask != nil
        let glance = look ? nil : JobAnswer.glanceCard(info)
        let headline = look ? JobAnswer.lookLine(info) : JobAnswer.headline(info)
        Button(action: action) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Circle().fill(JobLook.touch ? JobLook.brass : JobOrb.answerBlue).frame(width: 6, height: 6)
                        Text(job.name).font(.system(size: 13 * s, weight: .semibold)).lineLimit(1)
                    }
                    if let headline, !TextEcho.repeats(headline, in: spokenNow) {
                        Text(headline).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text(look ? "Look ›" : "Read ›").font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(JobStyle.blueInk)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if let glance {
                    AnswerGlance(glance)
                        .fixedSize()
                        .padding(.horizontal, 8).padding(.vertical, 6)
                        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))
                }
            }
            .padding(.leading, JobLook.touch ? 16 : 11).padding(.trailing, onDismiss == nil ? (JobLook.touch ? 16 : 11) : 30 * s)
            .padding(.vertical, JobLook.touch ? 12 : 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: JobLook.touch ? JobLook.radius : 12, style: .continuous)
                .fill(JobLook.touch ? JobLook.card : JobStyle.blueInk.opacity(hover ? 0.12 : 0.08)))
            .overlay(RoundedRectangle(cornerRadius: JobLook.touch ? JobLook.radius : 12, style: .continuous)
                .strokeBorder(focused ? JobStyle.blueInk : JobLook.touch ? .clear : JobStyle.blueInk.opacity(0.28), lineWidth: focused ? 1.5 : 0.75))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("\(look ? "Look at" : "Read") \(job.name)")
        .accessibilityLabel("\(look ? "New" : "New answer"): \(job.name). \(headline ?? "")")
        .accessibilityAction(named: "Dismiss") { onDismiss?() }
        .overlay(alignment: .topTrailing) {
            if let onDismiss { DismissAnswerButton(name: job.name, action: onDismiss).padding(4 * s) }
        }
        .contextMenu {
            if let onDismiss {
                Button(look ? "Look" : "Read", action: action)
                Button("Dismiss", action: onDismiss)
            }
        }
    }
}

/// The x on a new answer: clears it without opening it.
struct DismissAnswerButton: View {
    @Environment(\.jobTextScale) private var s
    var name: String
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Image(systemName: "xmark")
                .font(.system(size: 9.5 * s, weight: .bold))
                .foregroundStyle(hover ? Color.primary.opacity(0.8) : Color.secondary)
                .frame(width: 22 * s, height: 22 * s)
                .background(Circle().fill(Color.primary.opacity(hover ? 0.12 : 0.06)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Dismiss without opening")
        .accessibilityLabel("Dismiss \(name)")
    }
}

public struct DoneChip: View {
    @Environment(\.jobTextScale) private var s
    var job: Job
    var focused: Bool
    var action: () -> Void
    public init(job: Job, focused: Bool, action: @escaping () -> Void) { self.job = job; self.focused = focused; self.action = action }
    @State private var hover = false
    public var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: job.setAside ? "arrow.uturn.down" : job.newest.info.status == "completed" ? "checkmark" : "stop.fill")
                    .font(.system(size: 8 * s, weight: .bold)).foregroundStyle(JobStyle.green.opacity(0.9))
                Text(job.name).font(.system(size: 11 * s)).lineLimit(1)
                if job.setAside {
                    Text("· Set aside").font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(1).fixedSize()
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Capsule().fill(Color.primary.opacity(hover ? 0.13 : 0.07)))
            .overlay(Capsule().strokeBorder(focused ? JobStyle.blueInk : .clear, lineWidth: 1.2))
            .foregroundStyle(.primary.opacity(0.8))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(job.setAside ? "\(job.name): set aside" : "Open \(job.name)")
        .accessibilityLabel(job.setAside ? "\(job.name), set aside" : job.name)
    }
}

public struct JobTile: View {
    @Environment(\.jobTextScale) private var s
    @Environment(\.jobShownBeside) private var shownBeside
    var job: Job
    @ObservedObject var model: VoicePanelModel
    var focused: Bool
    public init(job: Job, model: VoicePanelModel, focused: Bool) { self.job = job; self.model = model; self.focused = focused }
    @State private var hover = false

    public var body: some View {
        let needs = job.stage.needsYou
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(job.name).font(.system(size: 13 * s, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 4)
                pill
            }
            content
        }
        .padding(.horizontal, JobLook.touch ? 16 : 11).padding(.vertical, JobLook.touch ? 12 : 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: JobLook.touch ? JobLook.radius : 12, style: .continuous)
            .fill(JobLook.touch ? JobLook.card : needs ? JobStyle.tileAmber.opacity(hover ? 0.16 : 0.11) : Color.primary.opacity(hover ? 0.09 : 0.06)))
        .overlay(RoundedRectangle(cornerRadius: JobLook.touch ? JobLook.radius : 12, style: .continuous)
            .strokeBorder(focused ? JobStyle.blueInk : JobLook.touch ? .clear : (needs ? JobStyle.tileAmber.opacity(0.42) : Color.primary.opacity(0.07)),
                          lineWidth: focused ? 1.5 : 0.75))
        .contentShape(Rectangle())
        .onTapGesture { model.onOpenJob(job.id) }
        .onHover { hover = $0 }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(job.name)
    }

    @ViewBuilder private var pill: some View {
        switch job.stage {
        // A status, never a button look-alike: the actions are the buttons under it.
        // Touch: the board's "Needs you" section header already says it.
        case .needsYou: EmptyView()   // the board's "Needs you" label already says it
        case .failed: JobPill(text: "Failed", style: .red)
        case .running:
            if job.activeTask?.info.status == "waiting_on_task" {
                JobPill(text: "Waiting", style: .grey)   // held so two tasks don't edit one project at once
            } else if let e = JobProgress.elapsed(job.activeTask?.info ?? job.newest.info, now: model.state.now) {
                JobPill(text: JobLook.touch ? e : "Running · \(e)", style: .blue)
            } else { JobPill(text: "Running", style: .blue) }
        case .done: JobPill(text: job.setAside ? "Set aside" : "Done", style: job.setAside ? .grey : .green)
        }
    }

    @ViewBuilder private var content: some View {
        switch job.stage {
        case .needsYou(.decide):
            if let d = job.decision {
                let buttons = d.boardButtons(optionsShowing: shownBeside == job.id)
                if buttons.pick != nil {
                    // The recommendation is the button: the line above says what's being decided.
                    Text(d.question).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let rec = d.recommendedOption {
                    // The question is one click away; the tile leads with what to do.
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if JobLook.touch {
                            Image(systemName: "star.fill").font(.system(size: 9 * s, weight: .semibold)).foregroundStyle(JobLook.brass)
                                .accessibilityLabel("Recommended")
                        } else {
                            Text("RECOMMENDED").font(.system(size: 10 * s, weight: .semibold)).tracking(0.6).foregroundStyle(.secondary)
                                .fixedSize()
                        }
                        Text(rec.text).font(.system(size: 12 * s, weight: .medium)).lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .help(d.question)
                } else {
                    Text(d.question).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 6) {
                    if let pick = buttons.pick {
                        TileButton(title: pick, primary: true, lines: 2) { model.approve(job) }
                            .help("Answer: \(pick)")
                            .layoutPriority(1)
                            .accessibilityIdentifier("tile-pick")
                    }
                    if let more = buttons.more {
                        // Beside the pick it's the quieter way in, sized to its words.
                        TileButton(title: more) { model.onOpenJob(job.id) }
                            .fixedSize(horizontal: buttons.pick != nil, vertical: false)
                            .accessibilityIdentifier("tile-more-options")
                    }
                    if model.setAsideTarget(job) != nil { NotNeededButton { model.setAside(job) } }
                }
                .padding(.top, 3)
            }
        case .needsYou(.approve):
            let approval = model.state.approval.flatMap { a in job.tasks.contains { $0.info.runID == a.runID } ? a : nil }
            let drafts = job.tasks.flatMap(\.info.pendingDrafts).count
            if let approval {
                ApprovalContext(approval: approval, task: job.tasks.last { $0.info.runID == approval.runID }, compact: true)
            } else {
                Text(drafts > 0 ? (drafts == 1 ? "An email is ready to send. Check it first." : "\(drafts) emails are ready to send.")
                     : job.newest.info.shortStatus ?? "Waiting for your OK to continue")
                    .font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            }
            if approval == nil && drafts > 0 {
                HStack(spacing: 6) {
                    TileButton(title: drafts == 1 ? "Review & send" : "Review drafts", primary: true) { model.onOpenJob(job.id) }
                    if model.setAsideTarget(job) != nil { NotNeededButton { model.setAside(job) } }
                }
                .padding(.top, 4)
            } else if approval != nil {
                HStack(spacing: 6) {
                    TileButton(title: "Approve", primary: true) { model.onApproval("once") }
                    TileButton(title: "Deny") { model.onApproval("deny") }
                    if model.setAsideTarget(job) != nil { NotNeededButton { model.setAside(job) } }
                }
                .disabled(model.busy)
                .padding(.top, 4)
            } else if model.setAsideTarget(job) != nil {
                HStack { Spacer(minLength: 0); NotNeededButton { model.setAside(job) } }
            }
        case .needsYou(.review):
            let drafts = job.tasks.flatMap(\.info.pendingDrafts).count
            Text(drafts > 0 ? (drafts == 1 ? "An email draft to check" : "\(drafts) email drafts to check") : "Pictures to look at")
                .font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
            HStack(spacing: 6) {
                TileButton(title: drafts > 0 ? "Review & send" : "Look", primary: true) { model.onOpenJob(job.id) }
            }
            .padding(.top, 3)
        case .failed:
            let info = job.newest.info
            Text(JobProgress.failureReason(info) ?? "Stopped with an error")
                .font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                if model.canRetry { TileButton(title: "Retry", primary: true) { model.onRetry(job.newest.id) } }
                TileButton(title: "Details") { model.onOpenJob(job.id) }
            }
            .padding(.top, 3)
        case .running:
            let info = job.activeTask?.info ?? job.newest.info
            Text(taskStatusLine(info, now: model.state.now, assistantName: model.state.assistantName).0)
                .font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(1)
            if let step = JobProgress.currentStep(info) {
                Text([step, JobProgress.eta(info)].compactMap { $0 }.joined(separator: " · "))
                    .font(.system(size: 11 * s, weight: .medium)).foregroundStyle(.secondary).lineLimit(1)
                    .padding(.top, 3)
            } else if let eta = JobProgress.eta(info) {
                Text(eta).font(.system(size: 11 * s)).foregroundStyle(.secondary)
            }
        case .done:
            detailsLine(job.setAside ? "Already handled or not needed" : job.newest.info.result?.label ?? "Done")
        }
    }

    private func detailsLine(_ text: String) -> some View {
        HStack(spacing: 6) {
            Text(text).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
            Spacer(minLength: 4)
            Text("Details ›").font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(JobStyle.blueInk)
        }
    }
}

/// Plan steps as segments: done green, current blue, the rest grey.
public struct StepBar: View {
    var done: Int
    var total: Int
    public init(done: Int, total: Int) { self.done = done; self.total = total }
    public var body: some View {
        if JobLook.touch {
            // One thin bar in the accent, like the system's linear progress.
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.12))
                    Capsule().fill(JobLook.brass)
                        .frame(width: g.size.width * CGFloat(min(done + 1, total)) / CGFloat(max(total, 1)))
                }
            }
            .frame(height: 4)
            .accessibilityElement()
            .accessibilityLabel("Step \(min(done + 1, total)) of \(total)")
        } else {
            segments
        }
    }

    private var segments: some View {
        HStack(spacing: 3) {
            ForEach(0..<max(total, 1), id: \.self) { i in
                Capsule().fill(i < done ? JobStyle.green.opacity(0.85) : i == done ? JobStyle.nowBlue : Color.primary.opacity(0.12))
                    .frame(height: 4)
            }
        }
        .accessibilityLabel("Step \(min(done + 1, total)) of \(total)")
    }
}

// MARK: - Job view

/// A job view's content under its header: the decision brief, approval, cards, live steps, report,
/// pictures and the job's other tasks; or, for an answer, its cards, report, pictures and sources.
/// The Mac panel and the phone wrap this in their own header and scrolling.
public struct JobDetailBody: View {
    var job: Job
    @ObservedObject var model: VoicePanelModel
    /// Which part of the decision brief to draw here (nil: none, another column draws it).
    var decisionPart: DecisionBrief.Part?
    var allowUndo: Bool
    /// A host that measures this content off screen (the Mac panel's scroller) passes a fresh value
    /// on every rebuild, so the body is rebuilt with it rather than updating a beat later.
    var revision: UUID?
    @Environment(\.jobRenderers) private var renderers
    @Environment(\.jobTextScale) private var s
    @Environment(\.jobHostDrawsSteps) private var hostDrawsSteps

    public init(job: Job, model: VoicePanelModel, decisionPart: DecisionBrief.Part? = .all, allowUndo: Bool = false,
                revision: UUID? = nil) {
        self.job = job; self.model = model; self.decisionPart = decisionPart; self.allowUndo = allowUndo
        self.revision = revision
    }

    public var body: some View {
        if let answer = job.answerTask { answerSections(answer) } else { sections }
    }

    /// An answer: its cards first and large, then the sorted report or the text, then pictures that
    /// came along (folded to a one-line row), then sources. Nothing to decide here.
    @ViewBuilder private func answerSections(_ task: TaskItem) -> some View {
        let cards = JobCards.items(job, model: model)
        let pictures = JobCards.pictures(job, model: model)
        let unread = task.info.followUps.filter { $0.state == .notRead }
        if !unread.isEmpty { NotReadNotice(items: unread, finished: task.info.status == "completed") }
        ForEach(JobCards.answerBlocks(job, model: model), id: \.self) { block in
            switch block {
            case .cards: JobCardStack(job: job, items: cards, model: model)
            case .report: answerReport(task, hasCards: !cards.isEmpty).layoutProbe("answer.report")
            case .pictures:
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(pictures) { PictureRow(job: job, review: $0, model: model) }
                }
                .layoutProbe("answer.pictures")
            case .sources: answerSources(task)
            case .tasks: answerTasks
            }
        }
    }

    @ViewBuilder private func answerReport(_ task: TaskItem, hasCards: Bool) -> some View {
        if let shape = task.info.result?.shape {
            ReportBoard(shape: shape, full: task.info.result?.full ?? task.info.result?.spoken, showHeadline: false,
                        expanded: model.expandedReports.contains(task.id),
                        toggle: { if model.expandedReports.contains(task.id) { model.expandedReports.remove(task.id) } else { model.expandedReports.insert(task.id) } })
        } else if let text = Self.bodyText(task.info) {
            // The headline above already says the answer: key points, then the long text folded.
            let points = task.info.result?.summaryPoints ?? []
            VStack(alignment: .leading, spacing: 6) {
                if !points.isEmpty { KeyPoints(points: points) }
                if CollapsedText.isShort(text) && points.isEmpty {
                    Text(Self.readable(text)).font(.system(size: 12 * s)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                } else {
                    CollapsedText(text: Self.readable(text), open: model.openSections.contains("\(task.id)|details")) {
                        model.toggleSection("\(task.id)|details")
                    }
                }
            }
        } else if let points = task.info.result?.summaryPoints, !points.isEmpty {
            KeyPoints(points: points)
        }
    }

    @ViewBuilder private func answerSources(_ task: TaskItem) -> some View {
        let links = task.info.cards
        VStack(alignment: .leading, spacing: 4) {
            SectionLabel(text: links.count == 1 ? "Source" : "Sources")
            ForEach(links) { link in
                Link(destination: link.url) {
                    HStack(spacing: 6) {
                        Image(systemName: "link").font(.system(size: 9.5 * s, weight: .semibold)).foregroundStyle(.tertiary)
                        Text(link.title).font(.system(size: 11.5 * s)).lineLimit(1)
                        Spacer(minLength: 4)
                        Text(link.url.host ?? "").font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    .foregroundStyle(JobStyle.blueInk)
                }
                .help(link.url.absoluteString)
            }
        }
    }

    private var answerTasks: some View {
        VStack(alignment: .leading, spacing: 4) {
            SectionLabel(text: "In this job")
            ForEach(job.tasks.reversed()) { t in
                HStack(spacing: 6) {
                    Circle().fill(dot(t)).frame(width: 6, height: 6)
                    Text(t.name).font(.system(size: 11.5 * s)).lineLimit(1)
                }
            }
        }
    }

    /// The answer's text beyond its headline: nil when it would only repeat the headline
    /// (the spoken line, said again under it).
    public static func bodyText(_ info: WorkInfo) -> String? {
        guard let text = info.result?.full ?? info.result?.spoken else { return nil }
        return TextEcho.remainder(text, after: JobAnswer.headline(info) ?? info.result?.spoken)
    }

    /// Markdown links `[name](url)` as just their names; bold markers dropped.
    public static func readable(_ text: String) -> String {
        text.replacingOccurrences(of: #"\[([^\]\n]{1,100})\]\((https?://[^\s)]+)\)"#, with: "$1", options: .regularExpression)
            .replacingOccurrences(of: "**", with: "")
    }

    /// The job view's status line while the job waits on the user.
    /// After the user picks (still inside the Undo window, or before the agent reports back), the
    /// task's own "Needs your pick" status is stale: say the pick is on its way instead.
    @MainActor public static func pickedLine(_ job: Job, model: VoicePanelModel) -> String? {
        guard let t = job.tasks.last(where: { $0.info.decision != nil && model.answers[$0.id] != nil }), t.isActive else { return nil }
        return model.pendingPicks[t.id] != nil ? "Sending your pick" : "Got your pick · carrying on"
    }

    public static func stageLine(_ job: Job) -> String {
        switch job.stage {
        case .needsYou(.decide): return "Waiting for your decision"
        case .needsYou(.approve): return "Waiting for your OK"
        case .needsYou(.review): return "Waiting for you to look"
        case .done where job.setAside: return "Set aside"
        default: return ""
        }
    }

    @ViewBuilder private var sections: some View {
        let decisionTask = job.decisionTask
        let answered = job.tasks.last { $0.info.decision != nil && model.answers[$0.id] != nil }
        if let task = decisionTask, let d = task.info.decision {
            if decisionPart != nil { DecisionBrief(job: job, task: task, decision: d, model: model, part: decisionPart ?? .all) }
        } else if let task = answered, let picked = model.answers[task.id] {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(JobStyle.green).font(.system(size: 12 * s))
                VStack(alignment: .leading, spacing: 1) {
                    Text("You picked").font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(.secondary)
                    Text(picked).font(.system(size: 12 * s, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                    if let until = model.pendingPicks[task.id] {
                        PickUndoBar(picked: picked, until: until) { model.undoPick(task.id) }.padding(.top, 4)
                    }
                    if let q = task.info.decision?.question {
                        Text(q).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
            }
            .touchCard()
        }
        if let approval = model.state.approval, job.tasks.contains(where: { $0.info.runID == approval.runID }),
           job.stage == .needsYou(.approve) {
            renderers.approval(approval)
            HStack { Spacer(minLength: 0); NotNeededButton { model.setAside(job) } }
        } else if job.stage == .needsYou(.approve), model.setAsideTarget(job) != nil {
            HStack { Spacer(minLength: 0); NotNeededButton { model.setAside(job) } }
        }
        if job.setAside {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: "arrow.uturn.down.circle.fill").foregroundStyle(.secondary).font(.system(size: 12 * s))
                VStack(alignment: .leading, spacing: 1) {
                    Text("Set aside").font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(.secondary)
                    Text("Already handled or not needed. Nothing is waiting on you here.")
                        .font(.system(size: 12 * s, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                    if let q = job.tasks.last(where: { $0.info.decision != nil })?.info.decision?.question {
                        Text(q).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
            }
            .touchCard()
        }
        if let failed = job.tasks.last, failed.info.status == "failed" {
            VStack(alignment: .leading, spacing: 5) {
                SectionLabel(text: "What went wrong")
                Text(JobProgress.failureReason(failed.info) ?? "The task stopped with an error.").font(.system(size: 12.5 * s, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
                if let more = failed.info.failure?.text, more != JobProgress.failureReason(failed.info) {
                    Text(more).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if model.canRetry {
                    HStack { TileButton(title: "Retry", primary: true) { model.onRetry(failed.id) }; Spacer(minLength: 0) }
                        .padding(.top, 2)
                }
            }
            .touchCard()
        }
        // Every card of the job (a review, drafts, result cards): the most relevant open, the rest
        // one-line rows. Nothing else in the panel draws them.
        let cards = JobCards.items(job, model: model)
        if !cards.isEmpty { JobCardStack(job: job, items: cards, model: model) }
        if !hostDrawsSteps { LiveSteps(job: job, model: model, allowUndo: allowUndo) }
        // A finished task that never read what the user added: say so on the card, not only aloud.
        if let task = job.tasks.last, task.info.isTerminal {
            let unread = task.info.followUps.filter { $0.state == .notRead }
            if !unread.isEmpty { NotReadNotice(items: unread, finished: task.info.status == "completed") }
        }
        let reportTask = job.tasks.last { $0.info.result != nil && $0.id != decisionTask?.id }
        if let task = reportTask {
            if let shape = task.info.result?.shape {
                ReportBoard(shape: shape, full: task.info.result?.full ?? task.info.result?.spoken,
                            expanded: model.expandedReports.contains(task.id),
                            toggle: { if model.expandedReports.contains(task.id) { model.expandedReports.remove(task.id) } else { model.expandedReports.insert(task.id) } })
            } else if let text = task.info.result?.full ?? task.info.result?.spoken {
                let points = task.info.result?.summaryPoints ?? []
                VStack(alignment: .leading, spacing: 5) {
                    SectionLabel(text: "Result")
                    if !points.isEmpty { KeyPoints(points: points) }
                    if CollapsedText.isShort(text) && points.isEmpty {
                        Text(linkedText(text)).font(.system(size: 12 * s)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true).tint(JobStyle.blueInk)
                    } else {
                        CollapsedText(text: text, open: model.openSections.contains("\(task.id)|details")) {
                            model.toggleSection("\(task.id)|details")
                        }
                    }
                }
                .touchCard()
            }
        }
        // Pictures that came along with a report: folded under it.
        ForEach(JobCards.pictures(job, model: model)) { PictureRow(job: job, review: $0, model: model) }
        if job.tasks.count > 1 {
            VStack(alignment: .leading, spacing: 4) {
                SectionLabel(text: "In this job")
                ForEach(job.tasks.reversed()) { task in
                    HStack(spacing: 6) {
                        Circle().fill(dot(task)).frame(width: 6, height: 6)
                        Text(task.name).font(.system(size: 11.5 * s)).lineLimit(1)
                        Spacer(minLength: 4)
                        Text(task.isActive ? "running" : (task.info.status == "completed" ? "done" : task.info.status.replacingOccurrences(of: "_", with: " ")))
                            .font(.system(size: 11.5 * s)).foregroundStyle(.secondary)
                    }
                }
            }
            .touchCard()
        }
    }

    private func dot(_ t: TaskItem) -> Color {
        if t.isActive { return JobStyle.nowBlue }
        switch t.info.status { case "completed": return JobStyle.green; case "failed": return JobStyle.red; default: return .secondary }
    }
}

/// The decision a job waits on: the question in plain words, what it's about, a picture of the
/// problem (the task's own cards), why the recommendation, and the options.
public struct DecisionBrief: View {
    /// Which half to draw: everything (phone, Mac), or the context and the options apart (a wide
    /// two-column layout puts the context left and the options right).
    public enum Part: Sendable { case all, context, options }
    var job: Job
    var task: TaskItem
    var decision: Decision
    @ObservedObject var model: VoicePanelModel
    var part: Part = .all
    @Environment(\.jobTextScale) private var s
    @Environment(\.jobReplyBarAnswers) private var replyBarAnswers
    @Environment(\.needsYouShownAbove) private var shownAbove
    private var comparing: Binding<Bool> {
        let key = "\(task.id)|compare"
        return Binding(get: { model.openSections.contains(key) }, set: { on in
            if on { model.openSections.insert(key) } else { model.openSections.remove(key) } })
    }

    public init(job: Job, task: TaskItem, decision: Decision, model: VoicePanelModel, part: Part = .all) {
        self.job = job; self.task = task; self.decision = decision; self.model = model; self.part = part
    }

    public var body: some View {
        let answer = model.answers[task.id]
        let visuals = task.info.views.filter { $0.kind != "question" }
        VStack(alignment: .leading, spacing: 8) {
            if part != .options {
                if answer == nil {
                    if !shownAbove { NeedsYouMark() }
                } else {
                    AnsweredMark()
                }
                Text(decision.question).font(.system(size: 14 * s, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                if let context = decision.context {
                    Text(context).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if !visuals.isEmpty {
                    VStack(spacing: 8) { ForEach(visuals) { ViewCardView($0) } }
                        .environment(\.questionActions, model.questionActions(for: task.id))
                }
                if part == .context, let why = decision.why {
                    VStack(alignment: .leading, spacing: 3) {
                        SectionLabel(text: "Why it matters")
                        Text(why).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
                if let affects = decision.affects {
                    Label(affects, systemImage: "scope").font(.system(size: 11 * s)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if part != .context {
                HStack {
                    SectionLabel(text: "Options")
                    Spacer(minLength: 4)
                    // The phone's full-width layout has its own Compare (a sheet) under the card.
                    if DecisionCompare.worthComparing(decision) && !(JobLook.touch && part == .all) { CompareToggle(comparing: comparing) }
                }
                .padding(.top, 2)
                if comparing.wrappedValue {
                    DecisionCompare(decision: decision, chosen: answer, onPick: answer == nil ? { model.pick($0, in: job) } : nil)
                    if part == .all, let why = decision.why {
                        Text("Why the recommendation: \(why)").font(.system(size: 11 * s)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    ForEach(Array(decision.options.enumerated()), id: \.offset) { i, option in
                        DecisionOptionRow(number: i + 1, option: option, recommended: i == decision.recommended,
                                          why: i == decision.recommended && part == .all ? decision.why : nil,
                                          chosen: answer == option.text, dimmed: answer != nil && answer != option.text,
                                          enabled: answer == nil) { model.pick(i + 1, in: job) }
                    }
                }
                if model.taskMessagesEnabled && part != .context && (answer == nil || !decision.options.contains { $0.text == answer }) {
                    if JobLook.touch && replyBarAnswers {
                        // The page's reply bar is the one field ("Something else? Type it here");
                        // what was sent that way still shows here, under the options it answers.
                        SentMessages(model: model, jobID: job.id, choiceTaskID: task.id, limit: 1)
                    } else {
                        SomethingElseField(model: model, job: job, task: task)
                    }
                }
                if let answer, let until = model.pendingPicks[task.id] {
                    PickUndoBar(picked: answer, until: until) { model.undoPick(task.id) }
                }
                if answer == nil {
                    // One-tap Approve only when the plugin said so; never inferred here.
                    if decision.approvable, let rec = decision.recommendedOption {
                        TileButton(title: "Approve: \(rec.text)", primary: true) { model.approve(job) }
                    }
                    HStack(alignment: .center, spacing: 6) {
                        Text(hint).font(.system(size: 11 * s)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 4)
                        if model.setAsideTarget(job) != nil { NotNeededButton { model.setAside(job) } }
                    }
                }
            }
        }
        .modifier(BriefFrame(open: answer == nil))
    }

    private var hint: String {
        #if os(macOS)
        return "Click, press 1–\(decision.options.count), or just say it."
        #else
        return "Tap one, or just say it."
        #endif
    }
}

/// The decision card: an amber-edged box on the Mac; a plain grouped section on touch.
private struct BriefFrame: ViewModifier {
    var open: Bool
    func body(content: Content) -> some View {
        if JobLook.touch {
            content.touchCard()
        } else {
            content
                .padding(11)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(JobStyle.tileAmber.opacity(open ? 0.08 : 0.0)))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(open ? JobStyle.tileAmber.opacity(0.4) : Color.primary.opacity(0.08), lineWidth: 0.75))
        }
    }
}

struct DecisionOptionRow: View {
    @Environment(\.jobTextScale) private var s
    var number: Int
    var option: DecisionOption
    var recommended: Bool
    var why: String?
    var chosen: Bool
    var dimmed: Bool
    var enabled: Bool
    /// Touch, pinned over a call: the title only, the recommended star at its end.
    var compact = false
    var action: () -> Void
    @State private var hover = false
    /// Under the title, past the number badge.
    private var indent: CGFloat { JobLook.touch ? 32 : 26 }

    private var pills: [(String, JobStyle.Pill)] {
        var out = option.tags.map { ($0.text, JobStyle.pill(tone: $0.tone)) }
        let said = option.tags.map { $0.text.lowercased() }.joined(separator: " ")
        if let risk = option.risk, !said.contains("risk") {
            out.append(risk == "low" ? ("Low risk", .green) : risk == "medium" ? ("Some risk", .amber) : ("High risk", .red))
        }
        if let r = option.reversible, !said.contains("reversible"), !said.contains("undo") {
            out.append(r ? ("Reversible", .green) : ("Can't undo", .amber))
        }
        return out
    }

    public var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    NumberBadge(number: number, chosen: chosen)
                    Text(option.text).font(.system(size: 12.5 * s, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    if compact && recommended && !dimmed && !chosen {
                        Image(systemName: "star.fill").font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(JobLook.brass)
                    }
                    if chosen { Image(systemName: "checkmark").font(.system(size: 11 * s, weight: .bold)).foregroundStyle(JobStyle.blueInk) }
                }
                // Its own line: beside the title it squeezed the option onto three hyphenated lines on a phone.
                if recommended && !dimmed && !compact {
                    if JobLook.touch {
                        Label("Recommended", systemImage: "star.fill").font(.system(size: 11 * s, weight: .semibold))
                            .foregroundStyle(JobLook.brass).padding(.leading, 32)
                    } else {
                        JobPill(text: "Recommended", style: .blue).padding(.leading, indent)
                    }
                }
                if let detail = option.detail {
                    Text(detail).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.leading, indent)
                }
                if let why {
                    Text("Why: \(why)").font(.system(size: 11 * s)).foregroundStyle(.secondary).italic()
                        .fixedSize(horizontal: false, vertical: true).padding(.leading, indent)
                }
                if !pills.isEmpty {
                    WrapLayout(spacing: JobLook.touch ? 12 : 4) { ForEach(Array(pills.enumerated()), id: \.offset) { JobPill(text: $0.element.0, style: $0.element.1) } }
                        .padding(.leading, indent)
                }
            }
            .padding(.horizontal, JobLook.touch ? 12 : 10).padding(.vertical, JobLook.touch ? 11 : 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: JobLook.touch ? 12 : 10, style: .continuous)
                .fill(JobLook.touch ? Color.primary.opacity(recommended && !dimmed ? 0.1 : 0.06)
                      : recommended && !dimmed ? JobStyle.blue.opacity(hover && enabled ? 0.22 : 0.14) : Color.primary.opacity(hover && enabled ? 0.09 : 0.05)))
            .overlay(RoundedRectangle(cornerRadius: JobLook.touch ? 12 : 10, style: .continuous)
                .strokeBorder(JobLook.touch ? (chosen ? JobLook.brass : recommended && !dimmed ? JobLook.brass.opacity(0.45) : .clear)
                              : chosen || (recommended && !dimmed) ? JobStyle.blueInk.opacity(0.6) : Color.primary.opacity(0.08),
                              lineWidth: JobLook.touch && chosen ? 1.5 : 1))
            .opacity(dimmed ? 0.45 : 1)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .onHover { hover = $0 }
        .accessibilityLabel("Option \(number): \(option.text)\(recommended ? ", recommended" : "")")
    }
}

/// What the user added that the task never read before it ended, with the way to send it on.
public struct NotReadNotice: View {
    var items: [FollowUp]
    var finished: Bool
    @Environment(\.jobTextScale) private var s
    public init(items: [FollowUp], finished: Bool) { self.items = items; self.finished = finished }
    public var body: some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "exclamationmark.circle.fill").foregroundStyle(JobStyle.tileAmber).font(.system(size: 12 * s))
            VStack(alignment: .leading, spacing: 2) {
                Text(finished ? "Finished before reading what you added" : "Stopped before reading what you added")
                    .font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(.secondary)
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    Text("\u{201C}\(item.text)\u{201D}").font(.system(size: 12 * s, weight: .medium))
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("It wasn't part of this answer. Ask again to run it as new work.")
                    .font(.system(size: 11.5 * s)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// What the job is doing now, step by step, with a way to add something or stop it.
public struct LiveSteps: View {
    var job: Job
    @ObservedObject var model: VoicePanelModel
    /// Finished steps get an Undo button (it asks the job, through the same answer route a spoken
    /// "undo that" takes). Only for jobs whose pick or report says it can be undone.
    var allowUndo = false
    @Environment(\.jobTextScale) private var s
    @Environment(\.jobHostShowsNow) private var hostShowsNow

    public init(job: Job, model: VoicePanelModel, allowUndo: Bool = false) {
        self.job = job; self.model = model; self.allowUndo = allowUndo
    }

    public var body: some View {
        if let task = job.activeTask ?? recentPlanned {
            let info = task.info
            let all = rows(info)
            let key = "\(job.id)|steps"
            let open = model.openSections.contains(key)
            // Collapsed: just where it is now (the current step, else the last one).
            let focus = all.lastIndex { $0.state == .current } ?? (all.isEmpty ? nil : all.count - 1)
            // Touch: the status line above may already say the current step in other words
            // ("Running it 50 times to be sure" / "Run it 50 times"). Then the collapsed card
            // doesn't say it again: just the way into all the steps.
            let dupCurrent = JobLook.touch && focus.map { all[$0].state == .current && TextEcho.nearDuplicate(all[$0].text, statusLine(task)) } == true
            let echoed = dupCurrent && !open
            // The host page says the current step itself and has Stop up top: no "Now" line, no Stop
            // here. Collapsed, only what it doesn't say: earlier steps when few, follow-up receipts.
            let hostNow = hostShowsNow && task.isActive
            let shown: [Row] = hostNow && !open ? (all.count > 2 ? all.filter { $0.receipt != nil } : all.filter { $0.state != .current })
                : echoed ? (all.count > 2 ? [] : all.filter { $0.state != .current })
                : open || all.count <= 2 ? all : focus.map { [all[$0]] } ?? []
            let more = all.count > 2 || dupCurrent || (hostNow && open && all.count > 1)
            let tell = task.isActive && model.state.connection.isOpen && !(JobLook.touch && model.taskMessagesEnabled)
                && (!model.taskMessagesEnabled || (!hostNow && task.canStop && info.runID != nil && !JobLook.touch))
            if !(hostNow && shown.isEmpty && !more && !tell) {
            VStack(alignment: .leading, spacing: JobLook.touch ? 10 : 6) {
                // Touch: the page's status block above already says the step, time and ETA.
                if !JobLook.touch && !hostNow {
                    HStack(alignment: .firstTextBaseline) {
                        SectionLabel(text: task.isActive ? "Now" : "Steps")
                        Spacer(minLength: 4)
                        if let line = JobProgress.line(info, now: model.state.now) {
                            Text(line).font(.system(size: 11 * s, weight: .medium)).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                }
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.offset) { i, row in
                        StepRow(text: row.text, sub: row.sub, state: row.state, last: i == shown.count - 1,
                                undo: undoable && row.state == .done && row.receipt == nil ? { model.undoStep(row.text, in: job) } : nil,
                                receipt: row.receipt)
                    }
                }
                if more {
                    if JobLook.touch {
                        // A disclosure row, like a grouped list's: the count, a chevron that turns.
                        if !shown.isEmpty { Divider().opacity(0.6) }
                        Button { withAnimation(.easeOut(duration: 0.2)) { model.toggleSection(key) } } label: {
                            HStack {
                                Text(open ? "Hide steps" : "All \(all.count) steps").font(.system(size: 12 * s))
                                    .foregroundStyle(Color.primary)
                                Spacer(minLength: 4)
                                Image(systemName: "chevron.right").font(.system(size: 10 * s, weight: .semibold))
                                    .foregroundStyle(.tertiary).rotationEffect(.degrees(open ? 90 : 0))
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(open ? "Hide steps" : "Show all \(all.count) steps")
                    } else {
                        Button { model.toggleSection(key) } label: {
                            Text(open ? "Show less" : "Show all \(all.count) steps").font(.system(size: 11.5 * s, weight: .semibold))
                                .foregroundStyle(JobStyle.blueInk).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                // Touch: Stop lives in the page's toolbar menu (with a confirm), not as a slab here.
                if tell {
                    HStack(spacing: 6) {
                        // With typed messages the reply bar under the job does this.
                        if !model.taskMessagesEnabled {
                            TileButton(title: "Tell it something") { model.onTellJob(job.id) }
                        }
                        if task.canStop, let run = info.runID, !JobLook.touch, !hostNow {
                            Button { model.onStopTask(run) } label: {
                                Text("Stop").font(.system(size: 11.5 * s, weight: .semibold)).frame(maxWidth: .infinity).frame(height: 26)
                                    .foregroundStyle(Color(red: 1, green: 0.7, blue: 0.66))
                                    .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(JobStyle.stopRed.opacity(0.28)))
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .help("Stop this task. The call and other jobs continue.")
                        }
                    }
                    if model.tellingJobID == job.id {
                        Text("Go ahead: what you say now goes to \(job.name).").font(.system(size: 11 * s)).foregroundStyle(JobStyle.blueInk)
                    }
                }
            }
            .touchCard()
            }
        }
    }

    /// Undo shows when the pick was marked reversible or the report says it can be undone.
    private var undoable: Bool {
        guard allowUndo else { return false }
        if job.tasks.contains(where: { $0.info.result?.shape?.undo == true }) { return true }
        guard let t = job.tasks.last(where: { $0.info.decision != nil }), let d = t.info.decision,
              let picked = model.answers[t.id] else { return false }
        return d.options.first { $0.text == picked }?.reversible == true
    }

    /// The line the page's status block leads with (same order as the phone's JobStatusBlock).
    private func statusLine(_ task: TaskItem) -> String? {
        JobDetailBody.pickedLine(job, model: model) ?? task.info.shortStatus
            ?? taskStatusLine(task.info, now: model.state.now, assistantName: model.state.assistantName).0
    }

    /// A finished task that had a plan still shows its steps, ticked.
    private var recentPlanned: TaskItem? { job.tasks.last.flatMap { $0.info.plan.isEmpty ? nil : $0 } }

    struct Row { var text: String; var sub: String?; var state: StepRow.State; var receipt: FollowUp.State? = nil }

    private func rows(_ info: WorkInfo) -> [Row] {
        let milestones = info.milestones
        if var p = JobBoard.progress(info) {
            // The server's own step number wins over counting milestones.
            if let i = info.stepIndex, info.stepCount == info.plan.count, !info.isTerminal { p.done = min(max(i - 1, 0), p.total - 1) }
            return info.plan.enumerated().map { i, step in
                let sub = i < milestones.count && milestones[i].text != step ? milestones[i].text : nil
                if i < p.done { return Row(text: step, sub: sub, state: .done) }
                if i == p.done && !info.isTerminal {
                    // Touch: the page's status line already says the short status.
                    return Row(text: step, sub: JobDetailBody.pickedLine(job, model: model) ?? (JobLook.touch ? nil : info.shortStatus), state: .current)
                }
                return Row(text: step, sub: nil, state: info.isTerminal && info.status == "completed" ? .done : .next)
            }
        }
        var out = milestones.suffix(5).map { m -> Row in
            // Something the user added: say what is known of it, never more (queued is not read).
            if let f = info.followUp(forStep: m.text) {
                return Row(text: m.text, sub: f.label, state: f.state == .landed ? .done : .next, receipt: f.state)
            }
            return Row(text: m.text, sub: JobProgress.relative(m.at, now: model.state.now), state: .done)
        }
        // Steps logged together read the same time: say it once, on the last of them.
        for i in out.indices.dropLast() where out[i].receipt == nil && out[i].sub != nil && out[i].sub == out[i + 1].sub {
            out[i].sub = nil
        }
        if !info.isTerminal, let now = JobDetailBody.pickedLine(job, model: model) ?? info.shortStatus {
            out.append(Row(text: now, sub: info.detail, state: .current))
        }
        return out
    }
}

public struct StepRow: View {
    public enum State: Sendable { case done, current, next }
    var text: String
    var sub: String?
    var state: State
    var last: Bool
    var undo: (() -> Void)? = nil
    /// A follow-up's receipt: its second line is tinted by what is known of it.
    var receipt: FollowUp.State? = nil
    @Environment(\.jobTextScale) private var s
    public init(text: String, sub: String? = nil, state: State, last: Bool, undo: (() -> Void)? = nil,
                receipt: FollowUp.State? = nil) {
        self.text = text; self.sub = sub; self.state = state; self.last = last; self.undo = undo; self.receipt = receipt
    }
    private var subColor: Color {
        switch receipt {
        case .landed: return JobStyle.green
        case .notRead: return JobStyle.tileAmber
        default: return Color.secondary
        }
    }
    private var receiptSymbol: String? {
        switch receipt {
        case .queued: return "clock"
        case .landed: return "checkmark"
        case .notRead: return "exclamationmark.circle"
        case nil: return nil
        }
    }
    public var body: some View {
        HStack(alignment: .top, spacing: 9) {
            VStack(spacing: 0) {
                Circle()
                    .fill(state == .done ? JobStyle.green : state == .current ? JobStyle.nowBlue : Color.primary.opacity(0.2))
                    .frame(width: 9, height: 9)
                    .overlay(Circle().strokeBorder(Color.white.opacity(state == .current ? 0.6 : 0), lineWidth: 1.5))
                    .padding(.top, 3)
                if !last { Rectangle().fill(Color.primary.opacity(0.14)).frame(width: 1.5).frame(maxHeight: .infinity) }
            }
            .frame(width: 10)
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(text).font(.system(size: 12 * s, weight: state == .next ? .regular : .semibold))
                        .foregroundStyle(state == .next ? Color.secondary : Color.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    if let undo {
                        Button("Undo", action: undo).buttonStyle(.plain)
                            .font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(JobStyle.blueInk)
                            .accessibilityLabel("Undo \(text)")
                    }
                }
                if let sub {
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        if let receiptSymbol { Image(systemName: receiptSymbol).font(.system(size: 9 * s, weight: .semibold)) }
                        Text(sub).font(.system(size: 11 * s, weight: receipt == .notRead ? .semibold : .regular)).lineLimit(2)
                    }
                    .foregroundStyle(subColor)
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(.bottom, last ? 0 : 8)
        }
        // Rows take their natural height, so the connector line fills it instead of growing forever.
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// A sorted report: checks as tiles, what shipped, what's still open, then the sections.
/// The full text is one click away.
public struct ReportBoard: View {
    var shape: ResultShape
    var full: String?
    /// False when the view above already leads with the headline (an answer).
    var showHeadline = true
    var expanded: Bool
    var toggle: () -> Void
    @Environment(\.jobTextScale) private var s
    public init(shape: ResultShape, full: String?, showHeadline: Bool = true, expanded: Bool, toggle: @escaping () -> Void) {
        self.shape = shape; self.full = full; self.showHeadline = showHeadline; self.expanded = expanded; self.toggle = toggle
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if showHeadline && !shape.headline.isEmpty {
                Text(shape.headline).font(.system(size: 13 * s, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
            }
            if !shape.checks.isEmpty {
                SectionLabel(text: "Checks")
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 92), spacing: 6)], spacing: 6) {
                    ForEach(Array(shape.checks.prefix(6).enumerated()), id: \.offset) { _, c in
                        VStack(alignment: .leading, spacing: 1) {
                            Text(c.value).font(.system(size: 15 * s, weight: .bold)).lineLimit(1).minimumScaleFactor(0.6)
                                .foregroundStyle(c.good ? JobStyle.green : JobStyle.amber)
                            Text(c.label).font(.system(size: 11 * s)).lineLimit(2)
                                .foregroundStyle(c.good ? JobStyle.green.opacity(0.75) : JobStyle.amber.opacity(0.8))
                        }
                        .padding(.horizontal, 8).padding(.vertical, 6)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .topLeading)
                        .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                            .fill((c.good ? JobStyle.green : JobStyle.amber).opacity(0.13)))
                    }
                }
            }
            list("Shipped", shape.shipped, JobStyle.green)
            list("Still open", shape.open, JobStyle.amber)
            ForEach(Array(shape.sections.prefix(5).enumerated()), id: \.offset) { _, s in
                list(s.title, s.points, Color.secondary)
            }
            if shape.undo {
                Label("This can be undone: say \u{201C}undo it\u{201D}.", systemImage: "arrow.uturn.backward")
                    .font(.system(size: 11 * s)).foregroundStyle(.secondary)
            }
            if let full {
                Button(action: toggle) {
                    Text(expanded ? "Hide full report" : "Full report ›").font(.system(size: 11 * s, weight: .semibold))
                        .foregroundStyle(JobStyle.blueInk).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if expanded {
                    Text(linkedText(full)).font(.system(size: 11.5 * s)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        .tint(JobStyle.blueInk)
                        .padding(9).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))
                }
            }
        }
    }

    @ViewBuilder private func list(_ title: String, _ items: [String], _ color: Color) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                SectionLabel(text: title)
                ForEach(Array(items.prefix(8).enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Circle().fill(color).frame(width: 6, height: 6).alignmentGuide(.firstTextBaseline) { $0[.bottom] - 1 }
                        Text(linkedText(item)).font(.system(size: 12 * s)).fixedSize(horizontal: false, vertical: true)
                            .tint(JobStyle.blueInk)
                    }
                }
            }
        }
    }
}

// MARK: - Cards in a job view

/// "‹ All jobs": the way back to the board, top-left of every job and answer view.
public struct AllJobsButton: View {
    var action: () -> Void
    @Environment(\.jobTextScale) private var s
    public init(action: @escaping () -> Void) { self.action = action }
    @State private var hover = false
    public var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: "chevron.left").font(.system(size: 10 * s, weight: .bold))
                Text("All jobs").font(.system(size: 12 * s, weight: .semibold))
            }
            .foregroundStyle(JobStyle.blueInk)
            .padding(.leading, 7).padding(.trailing, 10).frame(height: 26 * s)
            .background(Capsule().fill(JobStyle.blueInk.opacity(hover ? 0.2 : 0.12)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Back to all your jobs (Esc)")
        .accessibilityLabel("All jobs")
    }
}

/// One card of a job: a review, an email draft, or a result card.
public struct JobCardItem: Identifiable {
    public enum Content { case review(ImageReview), draft(EmailDraft), view(taskID: String, card: ViewCard) }
    public var key: String
    public var icon: String
    public var title: String
    public var summary: String
    public var content: Content
    public var id: String { key }
    public init(key: String, icon: String, title: String, summary: String, content: Content) {
        self.key = key; self.icon = icon; self.title = title; self.summary = summary; self.content = content
    }
}

public enum JobCards {
    /// A job's cards, most relevant first: what waits on you (a review, then drafts), then the
    /// newest result's cards, then older ones. The decision's own pictures stay in its brief.
    @MainActor public static func items(_ job: Job, model: VoicePanelModel) -> [JobCardItem] {
        var out: [JobCardItem] = []
        let ids = Set(job.tasks.map(\.id))
        for r in model.pinnedReviews.reversed() where ids.contains(r.taskID) && !isIncidental(r, in: job) {
            out.append(JobCardItem(key: "review:\(r.runID)", icon: "photo.on.rectangle.angled", title: "Ready to review",
                                   summary: r.countLabel, content: .review(r)))
        }
        var seen = Set<String>()
        for d in job.tasks.reversed().flatMap(\.info.emailDrafts) where seen.insert(d.draftID).inserted {
            let to = d.to.first.map { "To \($0)" } ?? ""
            out.append(JobCardItem(key: "draft:\(d.draftID)", icon: "envelope", title: d.subject.isEmpty ? "Email draft" : d.subject,
                                   summary: to, content: .draft(d)))
        }
        for t in job.tasks.reversed() where t.id != job.decisionTaskID {
            for v in t.info.views where v.kind != "question" {
                let (icon, title, summary) = describe(v)
                out.append(JobCardItem(key: "view:\(t.id):\(v.number)", icon: icon, title: title, summary: summary,
                                       content: .view(taskID: t.id, card: v)))
            }
        }
        return out
    }

    /// Pictures that came with a finished report (`WorkInfo.reviewIsIncidental`): they fold to a
    /// one-line row under the report instead of leading the job as something to review.
    @MainActor public static func pictures(_ job: Job, model: VoicePanelModel) -> [ImageReview] {
        model.pinnedReviews.reversed().filter { isIncidental($0, in: job) }
    }

    public static func isIncidental(_ r: ImageReview, in job: Job) -> Bool {
        job.tasks.first { $0.id == r.taskID }?.info.reviewIsIncidental ?? false
    }

    /// What an answer view draws, top to bottom: its cards, the report, pictures that came along,
    /// sources, the job's other tasks. The report always comes before the pictures.
    public enum AnswerBlock: Hashable { case cards, report, pictures, sources, tasks }

    @MainActor public static func answerBlocks(_ job: Job, model: VoicePanelModel) -> [AnswerBlock] {
        guard let task = job.answerTask else { return [] }
        var out: [AnswerBlock] = []
        if !items(job, model: model).isEmpty { out.append(.cards) }
        if task.info.result?.shape != nil || (task.info.result?.full ?? task.info.result?.spoken) != nil { out.append(.report) }
        if !pictures(job, model: model).isEmpty { out.append(.pictures) }
        if !task.info.cards.isEmpty { out.append(.sources) }
        if job.tasks.count > 1 { out.append(.tasks) }
        return out
    }

    /// Icon, title and a few words for a card's one-line row.
    public static func describe(_ c: ViewCard) -> (String, String, String) {
        let title = c["title"].string ?? c["place"].string ?? c["name"].string
        func label(_ fallback: String) -> String { title ?? fallback }
        switch c.kind {
        case "weather":
            let parts = [c["temp"].double.map { ViewCard.degrees($0) }, c["condition"].string.map(sentenceCase)]
            return ("cloud.sun", "Weather" + (c["place"].string.map { " · \($0)" } ?? ""), parts.compactMap { $0 }.joined(separator: " · "))
        case "quote":
            let price = c["price"].double.map { ViewCard.money($0, currency: c["currency"].string) }
            return ("chart.line.uptrend.xyaxis", c["name"].string ?? c["symbol"].string ?? "Price", price ?? "")
        case "chart": return ("chart.bar", label("Chart"), c["labels"].strings.count > 1 ? "\(c["labels"].strings.first!)–\(c["labels"].strings.last!)" : "")
        case "stats", "nutrition":
            let items = c["items"].array.prefix(2).compactMap { i in i["value"].string.map { "\($0) \((i["label"].string ?? "").lowercased())" } }
            return ("square.grid.2x2", label("Numbers"), items.joined(separator: " · "))
        case "comparison": return ("rectangle.split.2x1", label("Comparison"), c["winner"].string.map { "\($0) comes out ahead" } ?? "")
        case "list", "steps": return ("list.bullet", label("List"), "\(c["items"].array.count) items")
        case "place", "places", "route", "transit": return ("mappin.and.ellipse", label("Places"), c["items"].array.isEmpty ? "" : "\(c["items"].array.count) places")
        case "news": return ("newspaper", label("News"), c["items"].array.isEmpty ? "" : "\(c["items"].array.count) stories")
        case "agenda", "calendar_day":
            let n = c["days"].array.reduce(0) { $0 + $1["events"].array.count } + c["events"].array.count
            return ("calendar", label("Calendar"), n == 0 ? "" : "\(n) events")
        case "flight": return ("airplane", label("Flight"), c["status"].string ?? "")
        case "devices": return ("laptopcomputer.and.iphone", label("Devices"), "\(c["items"].array.count) devices")
        case "lanes": return ("chart.bar.xaxis", label("Timeline"), c["note"].string ?? "")
        case "flow": return ("arrow.triangle.branch", label("Flow"), "")
        case "before_after": return ("arrow.left.arrow.right", label("Before and after"), c["after"]["text"].string ?? "")
        case "fact", "conversion", "math": return ("number", label("Answer"), c["value"].string ?? c["to_value"].string ?? c["result"].string ?? "")
        default: return ("square.text.square", label(c.summary), "")
        }
    }
}

/// A job's cards: one card shows whole, the others fold to a one-line row (icon, title, a few
/// words, chevron). Clicking a row opens it; clicking an open card's row folds it. A single card
/// shows whole with no row.
public struct JobCardStack: View {
    var job: Job
    var items: [JobCardItem]
    @ObservedObject var model: VoicePanelModel
    @Environment(\.jobRenderers) private var renderers

    public init(job: Job, items: [JobCardItem], model: VoicePanelModel) { self.job = job; self.items = items; self.model = model }

    /// How tall a picture in a job view gets; click it for full size.
    public static let imageCap: CGFloat = 170

    public var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if items.count == 1, let only = items.first {
                content(only)
            } else {
                ForEach(Array(items.enumerated()), id: \.element.id) { i, item in
                    let open = model.isCardOpen(job: job.id, key: item.key, index: i)
                    VStack(alignment: .leading, spacing: 6) {
                        JobCardRow(item: item, open: open) { model.toggleCard(job: job.id, key: item.key, index: i) }
                        if open { content(item).padding(.bottom, 4) }
                    }
                }
            }
        }
    }

    @ViewBuilder private func content(_ item: JobCardItem) -> some View {
        switch item.content {
        case .review(let review):
            renderers.review(review, nil)
        case .draft(let draft):
            renderers.draft(draft)
        case .view(let taskID, let card):
            ActionableCard(taskID: taskID, card: card, model: model)
        }
    }
}

public struct JobCardRow: View {
    var item: JobCardItem
    var open: Bool
    var action: () -> Void
    @Environment(\.jobTextScale) private var s
    public init(item: JobCardItem, open: Bool, action: @escaping () -> Void) { self.item = item; self.open = open; self.action = action }
    @State private var hover = false
    public var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: item.icon).font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(JobStyle.blueInk)
                    .frame(width: 16)
                Text(item.title).font(.system(size: 12 * s, weight: .semibold)).lineLimit(1).layoutPriority(1)
                if !item.summary.isEmpty {
                    Text(item.summary).font(.system(size: 11 * s)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                }
                Spacer(minLength: 4)
                Image(systemName: open ? "chevron.down" : "chevron.right").font(.system(size: 9 * s, weight: .bold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 10).frame(height: 30 * s)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(hover ? 0.10 : 0.055)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(open ? "Fold this card" : "Open this card")
        .accessibilityLabel("\(item.title)\(item.summary.isEmpty ? "" : ", \(item.summary)"), \(open ? "open" : "folded")")
    }
}

/// Pictures that came along with an answer: one folded row ("1 picture ›") under the report; click
/// to show the capped picture (click that for full size).
public struct PictureRow: View {
    var job: Job
    var review: ImageReview
    @ObservedObject var model: VoicePanelModel
    @Environment(\.jobRenderers) private var renderers
    public init(job: Job, review: ImageReview, model: VoicePanelModel) { self.job = job; self.review = review; self.model = model }

    public static func label(_ r: ImageReview) -> String { r.images.count == 1 ? "1 picture" : "\(r.images.count) pictures" }

    public var body: some View {
        let open = model.isPicturesOpen(job: job.id, run: review.runID)
        VStack(alignment: .leading, spacing: 6) {
            JobCardRow(item: JobCardItem(key: "pictures:\(review.runID)", icon: "photo", title: Self.label(review),
                                         summary: review.images.first?.name ?? "", content: .review(review)),
                       open: open) { model.togglePictures(job: job.id, run: review.runID) }
            if open {
                renderers.review(review, "Came with the answer")
            }
        }
    }
}
/// Where a few job-view blocks were last drawn (window coordinates, y down), so the panel smoke can
/// check the real on-screen order. Written on layout; cheap.
@MainActor public enum LayoutProbe {
    public static var frames: [String: CGRect] = [:]
}

extension View {
    public func layoutProbe(_ key: String) -> some View {
        background(GeometryReader { g in
            let frame = g.frame(in: .global)
            Color.clear
                .onAppear { LayoutProbe.frames[key] = frame }
                .onChange(of: frame) { _, new in LayoutProbe.frames[key] = new }
                .onDisappear { LayoutProbe.frames[key] = nil }
        })
    }
}

// MARK: - Approvals with their context

/// What an approval will do, to what, and why, so the OK is an informed one.
public struct ApprovalContext: View {
    @Environment(\.jobTextScale) private var s
    var approval: ApprovalInfo
    var task: TaskItem?
    var compact = false
    public init(approval: ApprovalInfo, task: TaskItem?, compact: Bool = false) {
        self.approval = approval; self.task = task; self.compact = compact
    }
    public var body: some View {
        let ctx = approval.context(task: task)
        VStack(alignment: .leading, spacing: compact ? 4 : 6) {
            row("Will", ctx.action)
            if let target = ctx.target { row("On", target) }
            if let why = ctx.why { row("Why", why) }
        }
    }
    private func row(_ label: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Text(label.uppercased()).font(.system(size: 10.5 * s, weight: .semibold)).tracking(0.5).foregroundStyle(.secondary)
                .frame(width: 34, alignment: .leading)
            Text(text).font(.system(size: (compact ? 11.5 : 12) * s, weight: label == "Will" ? .medium : .regular))
                .foregroundStyle(label == "Will" ? Color.primary : Color.secondary)
                .lineLimit(compact ? 2 : 6).fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }
}

// MARK: - Lighter job pages (contract §5)

/// Up to four key points as a tight bullet list.
public struct KeyPoints: View {
    @Environment(\.jobTextScale) private var s
    var points: [String]
    public init(points: [String]) { self.points = points }
    public var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(points.prefix(4).enumerated()), id: \.offset) { _, p in
                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    Circle().fill(JobStyle.blueInk.opacity(0.8)).frame(width: 5, height: 5).offset(y: -2)
                    Text(p).font(.system(size: 12.5 * s)).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// Long text folded to its first three lines, with Details to read the rest.
public struct CollapsedText: View {
    @Environment(\.jobTextScale) private var s
    var text: String
    var label: String
    var open: Bool
    var toggle: () -> Void
    public init(text: String, label: String = "Details", open: Bool, toggle: @escaping () -> Void) {
        self.text = text; self.label = label; self.open = open; self.toggle = toggle
    }
    /// Short enough to show whole.
    public static func isShort(_ text: String) -> Bool { text.count <= 180 && !text.contains("\n\n") }
    public var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(linkedText(text)).font(.system(size: 12 * s)).foregroundStyle(open ? Color.primary : Color.secondary)
                .textSelection(.enabled).tint(JobStyle.blueInk)
                .lineLimit(open ? nil : 3).fixedSize(horizontal: false, vertical: true)
            Button(action: toggle) {
                Text(open ? "Less" : "\(label) ›").font(.system(size: 11.5 * s, weight: .semibold))
                    .foregroundStyle(JobStyle.blueInk).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
    }
}
