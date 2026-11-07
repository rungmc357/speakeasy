import AppKit
import SwiftUI
import SpeakeasyCore

// Port of SPK-113's invented, offline clips. Kept Mac-local while that phone lane evolves.
// Preserve the scripts and timing when updating the phone and Mac together.
enum WelcomeGrid {
    static let xs: CGFloat = 8
    static let s: CGFloat = 16
    static let m: CGFloat = 24
    static let l: CGFloat = 32
    static let side: CGFloat = 24
}
enum WelcomeCapture {
    static var frozenTime: Double? {
        ProcessInfo.processInfo.arguments.contains("--welcome-shots") ? 9 : nil
    }
}
enum WelcomePalette {
    static func dynamic(_ dark: Color, _ light: Color) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            NSColor(appearance.bestMatch(from: [.aqua, .darkAqua]) == .aqua ? light : dark)
        })
    }
    static let accent = dynamic(Color(red: 0.831, green: 0.635, blue: 0.298), Color(red: 0.55, green: 0.35, blue: 0.09))
    static let ink = dynamic(Color(red: 0.10, green: 0.075, blue: 0.04), .white)
    static let accentDeep = dynamic(Color(red: 0.42, green: 0.29, blue: 0.09), Color(red: 0.91, green: 0.83, blue: 0.65))
    static let bgTop = dynamic(Color(red: 0.086, green: 0.075, blue: 0.059), Color(red: 0.973, green: 0.953, blue: 0.918))
    static let bgBottom = dynamic(Color(red: 0.035, green: 0.03, blue: 0.024), Color(red: 0.945, green: 0.914, blue: 0.863))
    static let fg = dynamic(.white, Color(red: 0.118, green: 0.098, blue: 0.071))
    static func text(_ a: Double) -> Color { dynamic(.white.opacity(a), Color(red: 0.118, green: 0.098, blue: 0.071).opacity(min(1, 0.36 + a * 0.7))) }
    static let raised = dynamic(.white.opacity(0.065), Color(red: 1, green: 0.992, blue: 0.976))
    static let hairline = dynamic(.white.opacity(0.08), .black.opacity(0.10))
    static let cardShadow = dynamic(.clear, .brown.opacity(0.08))
}
struct WelcomeBackground: View {
    var body: some View {
        ZStack {
            LinearGradient(colors: [WelcomePalette.bgTop, WelcomePalette.bgBottom], startPoint: .top, endPoint: .bottom)
            RadialGradient(colors: [WelcomePalette.accent.opacity(0.12), .clear], center: .init(x: 0.5, y: 0.28), startRadius: 8, endRadius: 360)
        }.ignoresSafeArea()
    }
}
extension Text {
    func welcomeButton(filled: Bool) -> some View {
        self.font(.headline).foregroundStyle(filled ? WelcomePalette.ink : WelcomePalette.fg)
            .frame(maxWidth: .infinity, minHeight: 40)
            .background {
                if filled { Capsule().fill(WelcomePalette.accent) }
                else { Capsule().strokeBorder(WelcomePalette.text(0.22)) }
            }.contentShape(Capsule())
    }
}
struct FitToSpace<Content: View>: View {
    let height: CGFloat
    @ViewBuilder var content: Content
    var body: some View {
        GeometryReader { geo in
            let width = min(max(geo.size.width, 300), 440)
            let scale = min(1, geo.size.width / width, geo.size.height / height)
            content.frame(width: width, height: height).dynamicTypeSize(.large)
                .scaleEffect(scale).frame(width: geo.size.width, height: geo.size.height)
        }
    }
}
struct ClipClock<Content: View>: View {
    let loop: Double
    let still: Double
    @ViewBuilder var content: (Double) -> Content
    @State private var start = Date()
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    private var reduceMotion: Bool { systemReduceMotion || ProcessInfo.processInfo.arguments.contains("--welcome-reduce-motion") }

    var body: some View {
        if reduceMotion { content(still) }
        else if let frozen = WelcomeCapture.frozenTime { content(frozen) }
        else {
            TimelineView(.animation(minimumInterval: 1 / 30)) { ctx in
                content(ctx.date.timeIntervalSince(start).truncatingRemainder(dividingBy: loop))
            }
        }
    }
}

enum Clip {
    /// 0 → 1 over `duration` from `start`, eased.
    static func ramp(_ t: Double, _ start: Double, _ duration: Double = 0.45) -> Double {
        let x = min(1, max(0, (t - start) / duration))
        return x * x * (3 - 2 * x)
    }
    /// Fades in at `start`, out at `end`.
    static func window(_ t: Double, _ start: Double, _ end: Double, fade: Double = 0.4) -> Double {
        ramp(t, start, fade) * (1 - ramp(t, end - fade, fade))
    }
    /// The words said so far, with the rest kept (invisible) so the caption never reflows.
    static func spoken(_ text: String, from start: Double, rate: Double = 3.2, at t: Double) -> AttributedString {
        let words = text.split(separator: " ", omittingEmptySubsequences: false)
        let n = t <= start ? 0 : min(words.count, Int((t - start) * rate) + 1)
        let said = words.prefix(n).joined(separator: " ")
        var rest = AttributedString(String(text.dropFirst(said.count)))
        rest.foregroundColor = .clear
        return AttributedString(said) + rest
    }
}

// MARK: - Clip 1: a call (orb + one caption)

struct CallClip: View {
    static let you = "Find us a quiet table for two on Friday at 8."
    static let voice = "On it. I'll look near the office."

    var body: some View {
        ClipClock(loop: 10, still: 7.5) { t in
            let listening = t < 3.6
            VStack(spacing: WelcomeGrid.l) {
                Spacer(minLength: 0)
                WelcomeOrb(mark: t < 0.4 ? .connecting : listening ? .listening : (t < 4.2 ? .thinking : .speaking),
                         showsSymbol: false)
                    .frame(width: 176, height: 176)
                // One caption at a time: your words, then the reply in their place.
                ZStack(alignment: .topLeading) {
                    Caption(who: "You", text: Clip.spoken(Self.you, from: 0.6, at: t))
                        .opacity(Clip.window(t, 0.4, 4.2))
                    Caption(who: "Speakeasy", text: Clip.spoken(Self.voice, from: 4.3, at: t))
                        .opacity(Clip.window(t, 4.2, 9.6))
                }
                .frame(maxWidth: .infinity, minHeight: 88, alignment: .topLeading)
                Spacer(minLength: 0)
            }
        }
    }
}

struct Caption: View {
    let who: String
    let text: AttributedString
    var body: some View {
        VStack(alignment: .leading, spacing: WelcomeGrid.xs) {
            Text(who).font(.caption.weight(.semibold)).textCase(.uppercase).kerning(0.6)
                .foregroundStyle(who == "You" ? WelcomePalette.text(0.5) : WelcomePalette.accent)
            Text(text).font(.title3.weight(.medium))
                .foregroundStyle(WelcomePalette.text(0.92))
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Clip 2: a task card filling in

/// One invented task: what it's doing now, what it finished so far, and its outcome.
struct ClipTask {
    var title: String
    var lines: [(at: Double, text: String)]
    var sofar: [(at: Double, text: String)]
    var doneAt: Double
    var result: String

    func live(_ t: Double) -> String {
        t >= doneAt ? result : (lines.last { t >= $0.at }?.text ?? lines.first?.text ?? "")
    }

    /// When the live line last changed: the new words fade in from there (the old ones are already
    /// gone, so two lines never overlap mid-change).
    func changedAt(_ t: Double) -> Double {
        if t >= doneAt { return doneAt }
        return lines.last { t >= $0.at && $0.at > 0 }?.at ?? -1
    }

    static let table = ClipTask(
        title: "Table for two, Friday",
        lines: [(0, "Looking for quiet places near the office"),
                (2.8, "Checking who's free at 8"),
                (5.4, "Holding the quietest one")],
        sofar: [(2.6, "Found 6 places within a 10-minute walk"),
                (5.2, "3 are free at 8")],
        doneAt: 7.8,
        result: "Corner booth held, Friday, 8 PM")
}

struct TaskClip: View {
    var body: some View {
        ClipClock(loop: 12, still: 9) { t in
            VStack(spacing: 0) {
                // The card's top edge stays put; it only grows downward as rows arrive.
                TaskCardClip(task: .table, t: t, showsSoFar: true)
                    .opacity(1 - Clip.ramp(t, 11.4, 0.5))
                    .padding(.top, WelcomeGrid.s)
                Spacer(minLength: WelcomeGrid.m)
                // What the voice says follows the card: it only reports what the card already shows.
                let said: [(at: Double, text: String)] = [
                    (0.6, "Looking for quiet places near the office."),
                    (5.2, "Three places are free at 8. I'm holding the quietest."),
                    (7.8, "Done. A corner booth, Friday at 8."),
                ]
                let i = said.lastIndex { t >= $0.at } ?? 0
                let next = i + 1 < said.count ? said[i + 1].at : 11.8
                VoiceLine(text: said[i].text, t: t)
                    .opacity(Clip.ramp(t, said[i].at, 0.4) * (1 - Clip.ramp(t, next - 0.3, 0.3)))
                    .padding(.bottom, WelcomeGrid.s)
            }
        }
    }
}

struct TaskCardClip: View {
    let task: ClipTask
    let t: Double
    var showsSoFar = true

    var body: some View {
        let done = t >= task.doneAt
        VStack(alignment: .leading, spacing: WelcomeGrid.s) {
            HStack(alignment: .firstTextBaseline) {
                Text(task.title).font(.headline).foregroundStyle(WelcomePalette.fg).lineLimit(1)
                Spacer(minLength: WelcomeGrid.xs)
                StatusChip(done: done)
            }
            HStack(alignment: .firstTextBaseline, spacing: WelcomeGrid.xs) {
                Image(systemName: done ? "checkmark.circle.fill" : "circle.fill")
                    .font(.system(size: done ? 14 : 8))
                    .foregroundStyle(WelcomePalette.accent)
                    .opacity(done ? 1 : 0.55 + 0.45 * sin(t * 4))
                    .frame(width: 16)
                Text(task.live(t))
                    .font(.subheadline.weight(done ? .semibold : .regular))
                    .foregroundStyle(WelcomePalette.text(done ? 0.92 : 0.72))
                    .fixedSize(horizontal: false, vertical: true)
                    .opacity(Clip.ramp(t, task.changedAt(t), 0.4))
                    .offset(y: 4 * (1 - Clip.ramp(t, task.changedAt(t), 0.4)))
            }
            if showsSoFar {
                let shown = task.sofar.filter { t >= $0.at - 0.3 }
                if !shown.isEmpty {
                    VStack(alignment: .leading, spacing: WelcomeGrid.xs) {
                        Text("So far").font(.caption.weight(.semibold)).foregroundStyle(WelcomePalette.text(0.45))
                        ForEach(Array(shown.enumerated()), id: \.offset) { _, row in
                            HStack(alignment: .firstTextBaseline, spacing: WelcomeGrid.xs) {
                                Image(systemName: "checkmark").font(.caption2.weight(.bold))
                                    .foregroundStyle(WelcomePalette.text(0.45)).frame(width: 16)
                                Text(row.text).font(.footnote).foregroundStyle(WelcomePalette.text(0.62))
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .opacity(Clip.ramp(t, row.at - 0.3))
                            .offset(y: 8 * (1 - Clip.ramp(t, row.at - 0.3)))
                        }
                    }
                }
            }
        }
        .padding(WelcomeGrid.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(WelcomePalette.raised, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 24, style: .continuous).strokeBorder(WelcomePalette.hairline))
        .shadow(color: WelcomePalette.cardShadow, radius: 16, y: 8)
        .animation(.spring(response: 0.35, dampingFraction: 0.9), value: task.sofar.filter { t >= $0.at - 0.3 }.count)
    }
}

struct StatusChip: View {
    let done: Bool
    var body: some View {
        Text(done ? "Done" : "Working")
            .font(.caption.weight(.semibold))
            .foregroundStyle(done ? WelcomePalette.ink : WelcomePalette.accent)
            .padding(.horizontal, WelcomeGrid.xs).padding(.vertical, 4)
            .background(WelcomePalette.accent.opacity(done ? 1 : 0.14), in: Capsule())
            .animation(.easeInOut(duration: 0.3), value: done)
    }
}

/// The voice's line under a card: a small speaking orb and the words.
struct VoiceLine: View {
    let text: String
    let t: Double
    var body: some View {
        HStack(alignment: .center, spacing: WelcomeGrid.s) {
            WelcomeOrb(mark: .speaking, showsSymbol: false).frame(width: 40, height: 40)
            Text(text).font(.body.weight(.medium)).foregroundStyle(WelcomePalette.text(0.85))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }
}

// MARK: - Clip 3: your phone, your computer

struct YoursClip: View {
    var body: some View {
        ClipClock(loop: 6, still: 4.5) { t in
            VStack {
                Spacer(minLength: 0)
                HStack(alignment: .bottom, spacing: 0) {
                    DeviceTile(symbol: "iphone", label: "Your phone", lock: false)
                    PrivateLine(t: t).frame(height: 80).padding(.bottom, 32)
                    DeviceTile(symbol: "desktopcomputer", label: "Your computer", lock: true)
                }
                Spacer(minLength: 0)
            }
        }
    }
}

struct DeviceTile: View {
    let symbol: String
    let label: String
    let lock: Bool
    var body: some View {
        VStack(spacing: WelcomeGrid.xs) {
            Image(systemName: symbol)
                .font(.system(size: 32, weight: .light))
                .foregroundStyle(WelcomePalette.fg)
                .frame(width: 80, height: 80)
                .background(WelcomePalette.raised, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 24, style: .continuous).strokeBorder(WelcomePalette.hairline))
                .overlay(alignment: .topTrailing) {
                    if lock {
                        Image(systemName: "lock.fill").font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(WelcomePalette.ink)
                            .frame(width: 24, height: 24)
                            .background(WelcomePalette.accent, in: Circle())
                            .offset(x: 8, y: -8)
                    }
                }
            Text(label).font(.caption.weight(.medium)).foregroundStyle(WelcomePalette.text(0.55))
                .lineLimit(1).fixedSize()
                .frame(height: 24)
        }
    }
}

/// A private line between the two: dots travel both ways, and only between them.
struct PrivateLine: View {
    let t: Double
    var body: some View {
        Canvas { ctx, size in
            let y = size.height / 2
            var line = Path()
            line.move(to: CGPoint(x: 8, y: y)); line.addLine(to: CGPoint(x: size.width - 8, y: y))
            ctx.stroke(line, with: .color(WelcomePalette.text(0.2)), style: StrokeStyle(lineWidth: 1.5, dash: [2, 6]))
            for k in 0..<2 {
                let p = ((t / 2.2) + Double(k) * 0.5).truncatingRemainder(dividingBy: 1)
                let x = 8 + (size.width - 16) * (k == 0 ? p : 1 - p)
                let a = sin(p * .pi)
                ctx.fill(Path(ellipseIn: CGRect(x: x - 4, y: y - 4, width: 8, height: 8)),
                         with: .color(WelcomePalette.accent.opacity(a)))
            }
        }
        .frame(minWidth: 64)
    }
}

// MARK: - Clip 4: ready to connect

struct ConnectClip: View {
    var body: some View {
        VStack {
            Spacer(minLength: 0)
            BreathingOrb().frame(width: 160, height: 160)
            Spacer(minLength: 0)
        }
    }
}

/// The orb at rest, with a ring that slowly breathes out from it.
struct BreathingOrb: View {
    var body: some View {
        WelcomeOrb(mark: .thinking, showsSymbol: false)
            .overlay {
                ClipClock(loop: 4, still: 0) { t in
                    Circle().strokeBorder(WelcomePalette.accent.opacity(0.35 * (1 - t / 4)), lineWidth: 1)
                        .scaleEffect(0.7 + 0.55 * (t / 4))
                }
            }
    }
}

struct HowItWorksDemo: View {
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    private var reduceMotion: Bool { systemReduceMotion || ProcessInfo.processInfo.arguments.contains("--welcome-reduce-motion") }
    var onTryIt: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var start = Date()
    static let length = 21.0
    static let end = 20.0

    var body: some View {
        GeometryReader { geo in
            let compact = geo.size.height < 720
            Group {
                if reduceMotion { scene(Self.end, compact: compact) }
                else if WelcomeCapture.frozenTime != nil { scene(Self.end, compact: compact) }
                else {
                    TimelineView(.animation(minimumInterval: 1 / 30)) { ctx in
                        scene(min(Self.length, ctx.date.timeIntervalSince(start)), compact: compact)
                    }
                }
            }
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .background(WelcomeBackground())
        .frame(width: 520, height: 600)
        .buttonStyle(.plain)
        .foregroundStyle(WelcomePalette.fg)
        // The demo is a picture of a call: it grows with the text size up to the point where the
        // caption and the two task cards still share one screen.
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
    }

    struct Line { let who: String; let text: String; let at: Double; let until: Double }
    private static let script: [Line] = [
        Line(who: "You", text: "Move my 3 PM with Sam to tomorrow, and find us a table for Friday at 8.", at: 0.8, until: 4.4),
        Line(who: "Speakeasy", text: "Sure. I'll move the meeting and look for a table.", at: 4.6, until: 7.2),
        Line(who: "You", text: "Somewhere quiet, near the office.", at: 7.6, until: 9.6),
        Line(who: "Speakeasy", text: "Got it. Quiet and close by.", at: 9.9, until: 11.6),
        Line(who: "Speakeasy", text: "Your meeting with Sam is now tomorrow at 3. He said yes.", at: 12.6, until: 15.4),
        Line(who: "Speakeasy", text: "And I'm holding a corner booth for Friday at 8.", at: 16.0, until: 19.2),
    ]
    private static let meeting = ClipTask(
        title: "Move the 3 PM with Sam",
        lines: [(0, "Finding a free hour tomorrow"), (8.5, "Sending Sam the new time")],
        sofar: [], doneAt: 12.4, result: "Moved to tomorrow, 3 PM. Sam said yes.")
    private static let table: ClipTask = {
        var t = ClipTask.table
        t.lines = [(0, "Looking for a table on Friday at 8"), (10.4, "Looking for quiet places near the office"),
                   (14, "Holding the quietest one")]
        t.sofar = []
        t.doneAt = 15.8
        return t
    }()

    @ViewBuilder private func scene(_ t: Double, compact: Bool) -> some View {
        let index = Self.script.lastIndex { t >= $0.at - 0.2 }
        let line = index.map { Self.script[$0] }
        // One caption at a time: the last one has fully faded before the next one starts, so two
        // sentences never sit on top of each other.
        let lineOpacity: Double = index.map { i in
            let l = Self.script[i]
            let next = i + 1 < Self.script.count ? Self.script[i + 1].at - 0.2 : .infinity
            let out = min(l.until + 0.5, next - 0.3)
            return Clip.ramp(t, l.at - 0.2, 0.25) * (1 - Clip.ramp(t, out, 0.3))
        } ?? 0
        let ended = t >= Self.end
        let mark: PillPresentation.Mark = line?.who == "You" && t < (line?.until ?? 0) ? .listening
            : line != nil && t < (line?.until ?? 0) ? .speaking : (t < 0.6 ? .connecting : .thinking)
        VStack(spacing: 0) {
            HStack {
                Text(ended ? "Call ended · 0:20" : "Sample call · \(Self.clock(t))")
                    .font(.subheadline.weight(.semibold)).monospacedDigit()
                    .foregroundStyle(WelcomePalette.text(0.55))
                    .lineLimit(1).minimumScaleFactor(0.7)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.body.weight(.semibold))
                        .foregroundStyle(WelcomePalette.text(0.7))
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("Close")
                .accessibilityIdentifier("demo-close")
            }
            .padding(.horizontal, WelcomeGrid.side)

            // The orb, then what is being said (real text, so it grows with the text size), then the
            // two tasks the call started.
            Group {
                if ended {
                    BreathingOrb()
                } else {
                    WelcomeOrb(mark: mark, showsSymbol: false)
                }
            }
            .frame(width: compact ? 112 : 160, height: compact ? 112 : 160)
            .padding(.top, compact ? WelcomeGrid.s : WelcomeGrid.l)
            .accessibilityHidden(true)

            ZStack(alignment: .topLeading) {
                if ended {
                    Text("Both done, in one call.")
                        .font(.system(.title, design: .serif).weight(.semibold))
                        .foregroundStyle(WelcomePalette.fg)
                        .transition(.opacity)
                } else if let line {
                    Caption(who: line.who, text: Clip.spoken(line.text, from: line.at, rate: 3.6, at: t))
                        .opacity(lineOpacity)
                }
            }
            .frame(maxWidth: .infinity, minHeight: compact ? 80 : 96, alignment: .topLeading)
            .animation(.easeInOut(duration: 0.5), value: ended)
            .padding(.horizontal, WelcomeGrid.side)
            .padding(.top, compact ? WelcomeGrid.s : WelcomeGrid.m)

            FitToSpace(height: 200) {
                VStack(spacing: WelcomeGrid.xs) {
                    TaskCardClip(task: Self.meeting, t: t, showsSoFar: false)
                        .opacity(Clip.ramp(t, 5.2)).offset(y: 16 * (1 - Clip.ramp(t, 5.2)))
                    TaskCardClip(task: Self.table, t: t, showsSoFar: false)
                        .opacity(Clip.ramp(t, 5.7)).offset(y: 16 * (1 - Clip.ramp(t, 5.7)))
                    Spacer(minLength: 0)
                }
            }
            .padding(.horizontal, WelcomeGrid.side)
            .padding(.top, WelcomeGrid.m)
            .accessibilityHidden(true)

            ZStack {
                // The ending's buttons always hold their room (so nothing moves when they appear),
                // but only exist once the call has ended.
                endButtons.hidden().accessibilityHidden(true)
                if ended {
                    endButtons.transition(.opacity)
                } else {
                    Text("A made-up call. Your mic stays off.")
                        .font(.footnote).foregroundStyle(WelcomePalette.text(0.5))
                        .multilineTextAlignment(.center)
                        .transition(.opacity)
                }
            }
            .animation(.easeInOut(duration: 0.4), value: ended)
            .padding(.horizontal, WelcomeGrid.side)
            .padding(.top, WelcomeGrid.s)
            .padding(.bottom, compact ? WelcomeGrid.s : WelcomeGrid.m)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(Self.summary)
        
        
        
    }

    private var endButtons: some View {
        VStack(spacing: WelcomeGrid.xs) {
            Button { start = Date() } label: { Text("Watch again").welcomeButton(filled: true) }
                .accessibilityIdentifier("demo-replay")
            Button("Connect your agent") { onTryIt() }
                .font(.subheadline.weight(.medium)).foregroundStyle(WelcomePalette.text(0.7))
                .frame(maxWidth: .infinity, minHeight: 44)
                .accessibilityIdentifier("try-sample")
        }
    }

    static let summary = "A sample call. You ask to move a 3 PM meeting with Sam and find a quiet table for Friday at 8. "
        + "Two tasks start while you talk. The meeting moves to tomorrow and a corner booth is held."

    static func clock(_ t: Double) -> String {
        let s = Int(t)
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

struct WelcomeOrb: View {
    let mark: PillPresentation.Mark
    var showsSymbol = true
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    private var reduceMotion: Bool { systemReduceMotion || ProcessInfo.processInfo.arguments.contains("--welcome-reduce-motion") }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: reduceMotion || WelcomeCapture.frozenTime != nil || mark == .idle)) { context in
            let t = reduceMotion ? 0 : (WelcomeCapture.frozenTime ?? context.date.timeIntervalSinceReferenceDate)
            Canvas { ctx, size in
                let c = CGPoint(x: size.width / 2, y: size.height / 2)
                let base = min(size.width, size.height) / 2 * 0.62
                let energy: Double = mark == .speaking ? 1 : (mark == .listening ? 0.45 : 0.12)
                for ring in 0..<3 {
                    let phase = t * (1.2 + Double(ring) * 0.35) + Double(ring)
                    let r = base * (1 + 0.16 * Double(ring + 1) * (0.5 + 0.5 * sin(phase)) * energy)
                    let rect = CGRect(x: c.x - r, y: c.y - r, width: r * 2, height: r * 2)
                    ctx.fill(Path(ellipseIn: rect), with: .color(color.opacity(0.10 + 0.05 * Double(2 - ring))))
                }
                var path = Path()
                let points = 96
                for i in 0...points {
                    let a = Double(i) / Double(points) * 2 * .pi
                    let wobble = energy * (0.05 * sin(a * 3 + t * 2.1) + 0.035 * sin(a * 5 - t * 3.3))
                    let r = base * (1 + wobble)
                    let pt = CGPoint(x: c.x + r * cos(a), y: c.y + r * sin(a))
                    if i == 0 { path.move(to: pt) } else { path.addLine(to: pt) }
                }
                path.closeSubpath()
                ctx.fill(path, with: .radialGradient(
                    Gradient(colors: [color.opacity(0.95), color.opacity(0.55), WelcomePalette.accentDeep.opacity(0.6)]),
                    center: CGPoint(x: c.x - base * 0.3, y: c.y - base * 0.35), startRadius: 2, endRadius: base * 1.6))
            }
        }
        .overlay {
            if showsSymbol {
            Image(systemName: symbol)
                .font(.system(size: 26, weight: .semibold))
                .foregroundStyle(WelcomePalette.text(0.92))
                .contentTransition(.symbolEffect(.replace))
            }
        }
        .accessibilityHidden(true)
    }

    private var symbol: String {
        switch mark {
        case .speaking: return "waveform"
        case .listening: return "waveform.and.mic"
        case .thinking: return "ellipsis"
        case .muted: return "mic.slash.fill"
        case .connecting: return "ellipsis"
        case .idle: return "waveform"
        }
    }
    private var color: Color {
        switch mark {
        case .speaking: return WelcomePalette.accent
        case .listening: return WelcomePalette.accent
        case .muted: return WelcomePalette.accent
        case .thinking: return WelcomePalette.accent
        case .connecting, .idle: return WelcomePalette.accentDeep
        }
    }
}

