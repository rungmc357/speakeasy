import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// The Mac job page's live parts (servers with `job_activity`), in the iPhone redesign's language
// with Mac manners: a header lit by the job's mood, a picture of its kind of work, what it found,
// its tool calls (the latest pinned, all of them a click away, each opening to its raw call as
// selectable text), and what it touched. Only what the server sent is drawn.

// MARK: - Look

enum Live {
    static let brass = JobLook.brass
    static let brassText = JobStyle.blueInk
    static let green = Tokens.green
    static let red = JobStyle.red
    static let amber = JobStyle.tileAmber
    static let card = Color.white.opacity(0.055)
    static let hairline = Color.white.opacity(0.08)

    /// Reduce Motion, read from the system (hosted scroll content doesn't inherit the environment).
    static var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion || FilmFrames.stillMotion }

    static func tint(_ mood: LiveMood) -> Color {
        switch mood {
        case .working: return brass
        case .needsYou: return amber
        case .done: return green
        case .failed: return red
        case .quiet: return Color.white.opacity(0.5)
        }
    }

    /// The glow's second colour: a warmer or cooler neighbour of the tint.
    static func partner(_ mood: LiveMood) -> Color {
        switch mood {
        case .working: return Color(red: 0.86, green: 0.42, blue: 0.20)
        case .needsYou: return Color(red: 0.95, green: 0.52, blue: 0.22)
        case .done: return Color(red: 0.25, green: 0.62, blue: 0.62)
        case .failed: return Color(red: 0.62, green: 0.20, blue: 0.32)
        case .quiet: return Color(red: 0.35, green: 0.33, blue: 0.30)
        }
    }

    /// "4:08", "1:02:07".
    static func clock(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds))
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
    }
}

// MARK: - Glow

/// A soft light at the top of the job page, tinted by its mood, drifting slowly (still with
/// Reduce Motion).
struct MoodGlow: View {
    var mood: LiveMood
    var body: some View {
        Group {
            if Live.reduceMotion { glow(0) }
            else { TimelineView(.animation(minimumInterval: 1 / 15)) { ctx in glow(ctx.date.timeIntervalSinceReferenceDate) } }
        }
        .mask(LinearGradient(stops: [.init(color: .black, location: 0), .init(color: .black.opacity(0.5), location: 0.45),
                                     .init(color: .clear, location: 1)], startPoint: .top, endPoint: .bottom))
        .animation(.easeInOut(duration: 0.8), value: mood)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func glow(_ t: Double) -> some View {
        let a = CGFloat(sin(t * 0.3) * 0.08), b = CGFloat(cos(t * 0.23) * 0.06)
        return ZStack {
            RadialGradient(colors: [Live.tint(mood).opacity(0.42), .clear], center: UnitPoint(x: 0.22 + a, y: -0.05),
                           startRadius: 0, endRadius: 300)
            RadialGradient(colors: [Live.partner(mood).opacity(0.30), .clear], center: UnitPoint(x: 0.9 - a, y: 0.05 + b),
                           startRadius: 0, endRadius: 240)
        }
    }
}

/// A dot that breathes while something is live.
struct LiveDot: View {
    var color: Color
    var size: CGFloat = 7
    var active = true
    var body: some View {
        ZStack {
            if active && !Live.reduceMotion {
                TimelineView(.animation(minimumInterval: 1 / 20)) { ctx in
                    let p = (ctx.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.6)) / 1.6
                    Circle().fill(color.opacity(0.4)).scaleEffect(1 + p * 1.3).opacity(1 - p)
                }
            }
            Circle().fill(color)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// A small spinner that draws in snapshots too (an arc turning; a still arc with Reduce Motion).
struct LiveSpinner: View {
    var size: CGFloat = 11
    var body: some View {
        Group {
            if Live.reduceMotion { arc(0) }
            else { TimelineView(.animation(minimumInterval: 1 / 30)) { ctx in arc(ctx.date.timeIntervalSinceReferenceDate * 360) } }
        }
        .frame(width: size, height: size)
        .accessibilityLabel("Running")
    }
    private func arc(_ deg: Double) -> some View {
        ZStack {
            Circle().stroke(Live.brass.opacity(0.22), lineWidth: 1.6)
            Circle().trim(from: 0, to: 0.3).stroke(Live.brass, style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
                .rotationEffect(.degrees(deg))
        }
    }
}

// MARK: - Sections

/// A section's header: a brass symbol, a title, a quiet count.
struct LiveHeader: View {
    var title: String
    var symbol: String
    var trailing: String? = nil
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: symbol).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Live.brass)
            Text(title).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(.primary)
            Spacer(minLength: 4)
            if let trailing {
                Text(trailing).font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// Header outside, content on a soft card.
struct LiveCard<Content: View>: View {
    var title: String
    var symbol: String
    var trailing: String? = nil
    var padding: CGFloat = 12
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            LiveHeader(title: title, symbol: symbol, trailing: trailing).padding(.horizontal, 2)
            content()
                .padding(padding)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
        }
    }
}

/// The hero's card: a little brighter than a section, with a fine top highlight.
struct HeroCard<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        content()
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color.white.opacity(0.07)))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(LinearGradient(colors: [.white.opacity(0.20), .white.opacity(0.03)], startPoint: .top, endPoint: .bottom),
                              lineWidth: 0.75))
            .shadow(color: .black.opacity(0.25), radius: 10, y: 4)
    }
}

// MARK: - Page header

/// The mood as an eyebrow (with a ticking clock while it works), the big title, and what it's
/// doing in one line.
struct LiveJobTitle: View {
    let job: Job
    var title: String
    var line: String?
    var wide: Bool
    var extra: String?

    var body: some View {
        let mood = LiveMood.of(job)
        let info = (job.activeTask ?? job.newest).info
        VStack(alignment: .leading, spacing: 6) {
            TimelineView(.periodic(from: .now, by: 1)) { ctx in
                HStack(spacing: 6) {
                    LiveDot(color: Live.tint(mood), size: 6, active: mood.isLive)
                    Text(mood.word.uppercased()).tracking(1.0)
                    if mood == .working, let start = JobProgress.start(info) {
                        Text("·").foregroundStyle(.secondary)
                        Text(Live.clock(ctx.date.timeIntervalSince(start))).monospacedDigit()
                            .contentTransition(.numericText(countsDown: false))
                    } else if let extra {
                        Text("·").foregroundStyle(.secondary)
                        Text(extra.uppercased()).tracking(0.6).lineLimit(1).foregroundStyle(Color.white.opacity(0.55))
                    }
                }
                .font(.system(size: 10.5, weight: .bold))
                .foregroundStyle(Live.tint(mood))
            }
            .accessibilityElement(children: .combine)
            Text(title)
                .font(.system(size: wide ? 24 : 20, weight: .bold))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .accessibilityAddTraits(.isHeader)
            if let line, !line.isEmpty {
                Text(line)
                    .font(.system(size: wide ? 14 : 13, weight: .medium))
                    .foregroundStyle(Color.white.opacity(0.74))
                    .fixedSize(horizontal: false, vertical: true)
                    .lineLimit(3)
            }
        }
    }
}

// MARK: - Heroes

struct LiveHero: View {
    let hero: JobHero
    let info: WorkInfo
    var body: some View {
        HeroCard {
            switch hero {
            case let .trials(total, passed, failed): TrialsPicture(total: total, passed: passed, failed: failed, running: !info.isTerminal)
            case let .sources(items): SourcesPicture(items: items, running: !info.isTerminal)
            case let .diff(files, added, removed, preview): DiffPicture(files: files, added: added, removed: removed, preview: preview)
            case let .series(label, unit, points): SeriesPicture(label: label, unit: unit, points: points)
            case .steps: StepPathPicture(info: info)
            }
        }
        .accessibilityIdentifier("job-hero-\(hero.kind)")
    }
}

/// Repeated runs: a big count of passes and a grid of dots filling green (or red) as runs land.
struct TrialsPicture: View {
    var total: Int, passed: Int, failed: Int
    var running: Bool
    private var done: Int { passed + failed }
    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Text("\(passed)").font(.system(size: 46, weight: .bold, design: .rounded)).foregroundStyle(.white)
                    .contentTransition(.numericText(value: Double(passed)))
                    .animation(Live.reduceMotion ? nil : .snappy(duration: 0.35), value: passed)
                VStack(alignment: .leading, spacing: 1) {
                    Text("passed").font(.system(size: 15, weight: .semibold)).foregroundStyle(Live.green)
                    Text("of \(total) runs").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 1) {
                    Text("\(failed)").font(.system(size: 24, weight: .bold, design: .rounded))
                        .foregroundStyle(failed > 0 ? Live.red : Color.white.opacity(0.35))
                        .contentTransition(.numericText(value: Double(failed)))
                    Text("failed").font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(passed) of \(total) runs passed, \(failed) failed")
            DotsGrid(total: total, passed: passed, failed: failed, running: running)
            Text(summary).font(.system(size: 11.5, weight: .medium).monospacedDigit()).foregroundStyle(.secondary)
        }
    }
    private var summary: String {
        if done >= total { return failed == 0 ? "All \(total) runs passed" : "\(failed) of \(total) runs failed" }
        return "\(passed) passed · \(failed) failed · \(total - done) to go"
    }
}

/// One dot per run, a fixed size so the grid reads the same in a narrow panel and a wide one.
struct DotsGrid: View {
    var total: Int, passed: Int, failed: Int
    var running: Bool
    var body: some View {
        let shown = min(total, 200)
        let dot: CGFloat = shown <= 50 ? 18 : 11
        let gap: CGFloat = shown <= 50 ? 7 : 4
        let cols = shown <= 50 ? 10 : 20
        let rows = (shown + cols - 1) / cols
        VStack(alignment: .leading, spacing: gap) {
            ForEach(0..<rows, id: \.self) { r in
                HStack(spacing: gap) {
                    ForEach(0..<cols, id: \.self) { c in
                        let i = r * cols + c
                        if i < shown { cell(i).frame(width: dot, height: dot) }
                    }
                }
            }
        }
        .accessibilityHidden(true)
    }
    @ViewBuilder private func cell(_ i: Int) -> some View {
        if i < passed {
            Circle().fill(LinearGradient(colors: [Live.green, Live.green.opacity(0.75)], startPoint: .top, endPoint: .bottom))
                .shadow(color: Live.green.opacity(0.45), radius: 3)
        } else if i < passed + failed {
            Circle().fill(Live.red).shadow(color: Live.red.opacity(0.5), radius: 3)
        } else if i == passed + failed && running {
            Circle().strokeBorder(Live.brass, lineWidth: 1.5).background(Circle().fill(Live.brass.opacity(0.22)))
        } else {
            Circle().fill(Color.white.opacity(0.09))
        }
    }
}

/// Research: the sites it read as a fanned stack of site marks, a count, and the latest three.
struct SourcesPicture: View {
    var items: [JobHero.Source]
    var running: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 12) {
                HStack(spacing: -9) {
                    ForEach(Array(items.suffix(6).enumerated()), id: \.element.id) { i, s in
                        SiteBadge(site: s.site, size: 32).zIndex(Double(i))
                    }
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 0) {
                    Text("\(items.count)").font(.system(size: 34, weight: .bold, design: .rounded)).foregroundStyle(.white)
                        .contentTransition(.numericText(value: Double(items.count)))
                    Text(items.count == 1 ? "source read" : "sources read").font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(items.suffix(3).reversed().enumerated()), id: \.element.id) { i, s in
                    HStack(spacing: 8) {
                        SiteBadge(site: s.site, size: 18)
                        VStack(alignment: .leading, spacing: 0) {
                            Text(s.title).font(.system(size: 12.5, weight: .medium)).lineLimit(1)
                            Text(s.site).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 0)
                        if i == 0 && running { LiveDot(color: Live.brass, size: 5) }
                    }
                    .help("\(s.title) — \(s.site)")
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(items.count) sources read. Latest: \(items.last?.title ?? "")")
    }
}

/// A site's mark: its first letter on a colour picked from its name (no icons fetched).
struct SiteBadge: View {
    var site: String
    var size: CGFloat
    var body: some View {
        let name = site.replacingOccurrences(of: "www.", with: "")
        let hue = Double(abs(name.unicodeScalars.reduce(5381) { ($0 &* 33) &+ Int($1.value) }) % 360) / 360
        Text(String(name.prefix(1)).uppercased())
            .font(.system(size: size * 0.45, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(Circle().fill(LinearGradient(colors: [Color(hue: hue, saturation: 0.45, brightness: 0.72),
                                                              Color(hue: hue, saturation: 0.55, brightness: 0.45)],
                                                     startPoint: .topLeading, endPoint: .bottomTrailing)))
            .overlay(Circle().strokeBorder(Tokens.panelDark, lineWidth: size > 24 ? 2 : 1))
            .accessibilityHidden(true)
    }
}

/// A code change: files, lines added and removed, a proportion bar, and a peek at the change.
struct DiffPicture: View {
    var files: Int, added: Int, removed: Int
    var preview: [String]
    @State private var peek = false
    @State private var hover = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 16) {
                stat("\(files)", files == 1 ? "file" : "files", .white)
                stat("+\(added)", "added", Live.green)
                stat("−\(removed)", "removed", Live.red)
                Spacer(minLength: 0)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(files) files changed, \(added) lines added, \(removed) removed")
            GeometryReader { g in
                let total = max(added + removed, 1)
                HStack(spacing: 3) {
                    Capsule().fill(Live.green).frame(width: max(4, (g.size.width - 3) * CGFloat(added) / CGFloat(total)))
                    Capsule().fill(Live.red)
                }
            }
            .frame(height: 5)
            if !preview.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array((peek ? preview : Array(preview.prefix(3))).enumerated()), id: \.offset) { _, line in
                        let tone: Color = line.hasPrefix("+") ? Live.green : line.hasPrefix("-") || line.hasPrefix("−") ? Live.red : .secondary
                        Text(line).font(.system(size: 11, design: .monospaced)).foregroundStyle(tone).lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 7).padding(.vertical, 1.5)
                            .background(tone == .secondary ? Color.clear : tone.opacity(0.10))
                    }
                }
                .textSelection(.enabled)
                .padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.black.opacity(0.35)))
                if preview.count > 3 {
                    Button { withAnimation(Live.reduceMotion ? nil : .snappy) { peek.toggle() } } label: {
                        Label(peek ? "Less" : "Peek at the change", systemImage: peek ? "chevron.up" : "eye")
                            .font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Live.brassText)
                            .underline(hover)
                    }
                    .buttonStyle(.plain).onHover { hover = $0 }
                    .help(peek ? "Show fewer lines" : "Show all \(preview.count) lines")
                }
            }
        }
    }
    private func stat(_ value: String, _ label: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value).font(.system(size: 28, weight: .bold, design: .rounded)).foregroundStyle(color)
            Text(label).font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }
}

/// Numbers it pulled: the lowest value large, the usual level beside it, a bar per point.
struct SeriesPicture: View {
    var label: String
    var unit: String?
    var points: [JobHero.Point]
    var body: some View {
        let low = points.min { $0.y < $1.y }
        let top = max(points.map(\.y).max() ?? 1, 0.0001)
        let ys = points.map(\.y).sorted()
        let typical = ys.isEmpty ? 0 : ys[ys.count / 2]
        VStack(alignment: .leading, spacing: 10) {
            if !label.isEmpty { Text(label).font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary) }
            if let low {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(CardText.number(low.y)).font(.system(size: 34, weight: .bold, design: .rounded))
                    if let unit { Text(unit).font(.system(size: 14, weight: .semibold)).foregroundStyle(Color.white.opacity(0.7)) }
                }
                Text("Lowest, at \(low.x) · usually \(CardText.number(typical))").font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            HStack(alignment: .bottom, spacing: 4) {
                ForEach(Array(points.enumerated()), id: \.offset) { _, p in
                    VStack(spacing: 3) {
                        RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .fill(p == low ? Live.red : Live.brass)
                            .frame(height: max(3, 90 * CGFloat(max(p.y, 0) / top)))
                        if points.count <= 12 { Text(p.x).font(.system(size: 9.5)).foregroundStyle(.secondary).lineLimit(1) }
                    }
                    .help("\(p.x): \(CardText.number(p.y))\(unit.map { " \($0)" } ?? "")")
                }
            }
            .frame(height: 110, alignment: .bottom)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label). " + points.map { "\($0.x): \(CardText.number($0.y))" }.joined(separator: ", "))
    }
}

/// The plan's steps as a path: done ones checked with how long each took, the current one lit.
struct StepPathPicture: View {
    let info: WorkInfo
    var body: some View {
        let plan = info.plan
        let done = JobBoard.progress(info)?.done ?? 0
        let times = Self.durations(info)
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(plan.enumerated()), id: \.offset) { i, step in
                let state = i < done ? 1 : i == done && !info.isTerminal ? 2 : 0
                HStack(alignment: .top, spacing: 11) {
                    node(state).frame(width: 18)
                    HStack(alignment: .firstTextBaseline) {
                        Text(step).font(.system(size: 13, weight: state == 2 ? .semibold : .regular))
                            .foregroundStyle(state == 0 ? Color.white.opacity(0.45) : .white)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 6)
                        if state == 1, let t = times.indices.contains(i) ? times[i] : nil, !t.isEmpty {
                            Text(t).font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
                        } else if state == 2 {
                            Text("Now").font(.system(size: 11, weight: .semibold)).foregroundStyle(Live.brassText)
                        }
                    }
                    .padding(.bottom, i < plan.count - 1 ? 13 : 0)
                }
                // The path down to the next step, as tall as this row (a background takes the row's height).
                .background(alignment: .topLeading) {
                    if i < plan.count - 1 {
                        Rectangle().fill(i < done ? Live.green.opacity(0.6) : Color.white.opacity(0.12))
                            .frame(width: 2).padding(.top, 20).padding(.leading, 8)
                    }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Step \(i + 1) of \(plan.count): \(step). " + (state == 1 ? "Done" : state == 2 ? "In progress" : "Next"))
            }
        }
    }
    @ViewBuilder private func node(_ state: Int) -> some View {
        ZStack {
            switch state {
            case 1:
                Circle().fill(Live.green)
                Image(systemName: "checkmark").font(.system(size: 8.5, weight: .heavy)).foregroundStyle(Tokens.brassInk)
            case 2:
                Circle().fill(Live.brass.opacity(0.25))
                Circle().fill(Live.brass).padding(4).shadow(color: Live.brass, radius: 4)
            default:
                Circle().strokeBorder(Color.white.opacity(0.22), lineWidth: 1.5)
            }
        }
        .frame(width: 18, height: 18)
    }
    /// How long each finished step took, from its milestone's time (the first from the request).
    static func durations(_ info: WorkInfo) -> [String] {
        var prev = info.events.first?.at ?? info.startedAt
        return info.milestones.map { m in
            defer { prev = m.at ?? prev }
            guard let at = m.at, let p = prev, at > p else { return "" }
            return JobProgress.duration(at.timeIntervalSince(p))
        }
    }
}

/// No hero and no plan: the work itself, as a strip of what it did, coloured by kind.
struct ActivityStripPicture: View {
    var items: [JobActivityItem]
    var touched: Int
    var findings: Int
    var running: Bool

    static func color(_ kind: JobActivityItem.Kind) -> Color {
        switch kind {
        case .read, .browse: return Color(red: 0.55, green: 0.70, blue: 0.95)
        case .search: return Color(red: 0.62, green: 0.58, blue: 0.95)
        case .edit: return Live.green
        case .run: return Live.brass
        case .think, .message: return Color.white.opacity(0.55)
        case .tool: return Color.white.opacity(0.4)
        }
    }
    static func word(_ kind: JobActivityItem.Kind) -> String {
        switch kind {
        case .read, .browse: return "Reading"
        case .search: return "Searching"
        case .edit: return "Editing"
        case .run: return "Running"
        case .think, .message: return "Thinking"
        case .tool: return "Tools"
        }
    }

    var body: some View {
        let done = items.filter { $0.status != .running }.count
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Text("\(done)").font(.system(size: 46, weight: .bold, design: .rounded)).foregroundStyle(.white)
                    .contentTransition(.numericText(value: Double(done)))
                VStack(alignment: .leading, spacing: 1) {
                    Text(done == 1 ? "step done" : "steps done").font(.system(size: 15, weight: .semibold)).foregroundStyle(Live.brassText)
                    Text([touched > 0 ? "\(touched) things touched" : nil, findings > 0 ? "\(findings) found" : nil]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            HStack(alignment: .bottom, spacing: 3) {
                ForEach(items) { item in
                    let live = item.status == .running && running
                    Capsule().fill(item.status == .failed ? Live.red : Self.color(item.kind))
                        .frame(maxWidth: 14)
                        .frame(height: live ? 36 : item.kind == .run || item.kind == .edit ? 27 : 18)
                        .opacity(live ? 1 : 0.85)
                        .help(item.text)
                }
            }
            .frame(height: 38, alignment: .bottom)
            WrapLayout(spacing: 10) {
                ForEach(Self.legend(items), id: \.0) { word, count, color in
                    HStack(spacing: 4) {
                        Circle().fill(color).frame(width: 6, height: 6)
                        Text("\(word) \(count)").font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary).fixedSize()
                    }
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(done) steps done, \(touched) things touched, \(findings) found")
    }

    static func legend(_ items: [JobActivityItem]) -> [(String, Int, Color)] {
        var order: [String] = []
        var counts: [String: (Int, Color)] = [:]
        for i in items {
            let w = word(i.kind)
            if counts[w] == nil { order.append(w); counts[w] = (0, color(i.kind)) }
            counts[w]!.0 += 1
        }
        return order.prefix(4).map { ($0, counts[$0]!.0, counts[$0]!.1) }
    }
}

// MARK: - Found so far

struct FindingsList: View {
    let findings: [JobFinding]
    var running: Bool
    var body: some View {
        LiveCard(title: running ? "Found so far" : "What it found", symbol: "lightbulb.fill",
                 trailing: findings.count > 1 ? "\(findings.count)" : nil) {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(findings.reversed().enumerated()), id: \.element.id) { i, f in
                    HStack(alignment: .firstTextBaseline, spacing: 9) {
                        Circle().fill(i == 0 ? Live.brass : Color.white.opacity(0.3)).frame(width: 6, height: 6)
                            .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + 4 }
                        Text(f.text).font(.system(size: i == 0 ? 13 : 12, weight: i == 0 ? .medium : .regular))
                            .foregroundStyle(Color.white.opacity(i == 0 ? 1 : 0.7))
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                }
            }
        }
        .accessibilityIdentifier("job-findings")
    }
}

// MARK: - Tool calls

/// The latest call pinned; "All N tool calls ›" opens the rest, newest first. A row with more to
/// show (the raw arguments, the result) opens to it as selectable monospaced text.
struct ToolCallsCard: View {
    let job: Job
    let items: [JobActivityItem]
    var running: Bool
    @ObservedObject var model: VoicePanelModel

    var body: some View {
        let pinned = ToolCallLog.pinned(items)
        let rest = Array(items.reversed().filter { $0.id != pinned?.id })
        let open = model.openSections.contains(ToolCallLog.listKey(job: job.id))
        LiveCard(title: running && !job.stage.needsYou ? "Doing now" : "Last tool call", symbol: "bolt.horizontal.fill", padding: 4) {
            VStack(alignment: .leading, spacing: 0) {
                if let pinned { row(pinned, live: running && !job.stage.needsYou && pinned.status == .running) }
                if !rest.isEmpty {
                    Divider().overlay(Live.hairline).padding(.horizontal, 8)
                    DisclosureRow(title: ToolCallLog.allLabel(items.count), open: open) {
                        withAnimation(Live.reduceMotion ? nil : .snappy(duration: 0.25)) { model.toggleSection(ToolCallLog.listKey(job: job.id)) }
                    }
                    if open {
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(rest) { item in
                                Divider().overlay(Live.hairline.opacity(0.6)).padding(.leading, 40)
                                row(item, live: false)
                            }
                        }
                        .transition(.opacity)
                    }
                }
            }
        }
        .accessibilityIdentifier("job-tool-calls")
    }

    private func row(_ item: JobActivityItem, live: Bool) -> some View {
        ToolCallRow(item: item, live: live, now: model.state.now,
                    open: model.openSections.contains(ToolCallLog.rowKey(job: job.id, item: item.id))) {
            withAnimation(Live.reduceMotion ? nil : .snappy(duration: 0.22)) {
                model.toggleSection(ToolCallLog.rowKey(job: job.id, item: item.id))
            }
        }
    }
}

struct DisclosureRow: View {
    var title: String
    var open: Bool
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text(open ? "Hide earlier calls" : title).font(.system(size: 12, weight: .medium))
                    .foregroundStyle(hover ? Live.brassText : .secondary)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.system(size: 9.5, weight: .bold)).foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(open ? 90 : 0))
            }
            .padding(.horizontal, 10).frame(height: 30)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.white.opacity(hover ? 0.05 : 0)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(open ? "Fold the earlier calls" : "Show every tool call this job made")
        .accessibilityIdentifier("tool-calls-all")
    }
}

struct ToolCallRow: View {
    let item: JobActivityItem
    var live: Bool
    var now: Date
    var open: Bool
    var toggle: () -> Void
    @State private var hover = false

    var body: some View {
        let more = ToolCallLog.expandable(item)
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 9) {
                ZStack {
                    Circle().fill(live ? Live.brass.opacity(0.22) : Color.white.opacity(0.07))
                    Image(systemName: item.status == .failed ? "exclamationmark" : item.symbol)
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(item.status == .failed ? Live.red : live ? Live.brass : Color.white.opacity(0.7))
                }
                .frame(width: 24, height: 24)
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.text).font(.system(size: 12.5, weight: live ? .semibold : .regular))
                        .foregroundStyle(.primary).lineLimit(open ? nil : 2).fixedSize(horizontal: false, vertical: true)
                    if let tool = item.tool {
                        Text(tool).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary).lineLimit(1)
                    } else if let detail = item.detail, !open {
                        Text(detail).font(item.kind == .run ? .system(size: 10.5, design: .monospaced) : .system(size: 11))
                            .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    }
                }
                Spacer(minLength: 4)
                if let d = ToolCallLog.duration(item, now: now) {
                    Text(ToolCallLog.durationText(d)).font(.system(size: 10.5).monospacedDigit()).foregroundStyle(.secondary).fixedSize()
                }
                status.frame(width: 13)
                if more {
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 7)
            .contentShape(Rectangle())
            .onTapGesture { if more { toggle() } }
            if open && more { expanded.padding(.leading, 41).padding(.trailing, 8).padding(.bottom, 9) }
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.white.opacity(hover && more ? 0.045 : 0)))
        .onHover { hover = $0 }
        .help(more ? (open ? "Fold" : "Show the call's details") : item.text)
        .accessibilityElement(children: open ? .contain : .combine)
        .accessibilityLabel(item.text + (live ? ", running" : item.status == .failed ? ", failed" : ", done"))
        .accessibilityAddTraits(more ? .isButton : [])
    }

    @ViewBuilder private var status: some View {
        switch item.status {
        case .running: if live { LiveSpinner(size: 11) } else { LiveSpinner(size: 11).opacity(0.6) }
        case .done: Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(Live.green.opacity(0.85))
        case .failed: Image(systemName: "xmark").font(.system(size: 10, weight: .bold)).foregroundStyle(Live.red)
        }
    }

    private var expanded: some View {
        VStack(alignment: .leading, spacing: 7) {
            if let args = item.args { block("Arguments", args) }
            else if let detail = item.detail { block(item.kind == .run ? "Command" : "Detail", detail) }
            if let result = item.result { block(item.status == .failed ? "Error" : "Result", result) }
        }
        .transition(.opacity)
    }

    private func block(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased()).font(.system(size: 9.5, weight: .semibold)).tracking(0.6).foregroundStyle(.tertiary)
            Text(text).font(.system(size: 11, design: .monospaced)).foregroundStyle(Color.white.opacity(0.82))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8).padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(Color.black.opacity(0.32)))
                .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
        }
    }
}

// MARK: - What it touched

/// Files, sites and commands as wrapping chips. A site opens; anything can be copied (the files
/// live on the server, so there's nothing to reveal here).
struct TouchedChipsView: View {
    let items: [JobTouched]
    @ObservedObject var model: VoicePanelModel
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            LiveHeader(title: "What it touched", symbol: "hand.point.up.left.fill", trailing: "\(items.count)").padding(.horizontal, 2)
            WrapLayout(spacing: 6) {
                ForEach(items) { t in TouchedChip(item: t, model: model) }
            }
        }
        .accessibilityIdentifier("job-touched")
    }
}

struct TouchedChip: View {
    let item: JobTouched
    @ObservedObject var model: VoicePanelModel
    @State private var hover = false
    var body: some View {
        Button(action: primary) {
            HStack(spacing: 5) {
                Image(systemName: item.symbol).font(.system(size: 9.5, weight: .semibold)).foregroundStyle(Live.brass)
                Text(item.label)
                    .font(item.kind == .command ? .system(size: 11, weight: .medium, design: .monospaced) : .system(size: 11.5, weight: .medium))
                    .lineLimit(1).truncationMode(.middle)
                if let d = item.detail {
                    Text(d).font(.system(size: 10.5).monospacedDigit()).foregroundStyle(d.hasPrefix("+") ? Live.green : .secondary)
                }
                if hover {
                    Image(systemName: item.url != nil ? "arrow.up.right" : "doc.on.doc").font(.system(size: 8.5, weight: .bold))
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 9).frame(height: 24)
            .background(Capsule().fill(Color.white.opacity(hover ? 0.11 : 0.065)))
            .overlay(Capsule().strokeBorder(Color.white.opacity(hover ? 0.16 : 0.08), lineWidth: 0.5))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help(item.url != nil ? "Open \(item.url!.absoluteString) — right-click to copy" : "Copy “\(item.label)”")
        .contextMenu {
            if let url = item.url { Button("Open") { NSWorkspace.shared.open(url) } }
            Button(item.url != nil ? "Copy Link" : "Copy") { copy() }
        }
        .accessibilityLabel("\(item.kind == .file ? "File" : item.kind == .url ? "Site" : "Command") \(item.label)\(item.detail.map { ", \($0)" } ?? "")")
    }
    private func primary() {
        if let url = item.url { NSWorkspace.shared.open(url) } else { copy() }
    }
    private func copy() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(item.url?.absoluteString ?? item.label, forType: .string)
        model.showToast("Copied")
    }
}

// MARK: - The live block

/// Everything the server sent about the job: hero, found so far, tool calls, touched. Two columns
/// in a wide panel. Draws nothing for a job without it.
struct LiveJobBlock: View {
    let job: Job
    @ObservedObject var model: VoicePanelModel
    var wide: Bool

    static func task(_ job: Job) -> TaskItem { job.activeTask ?? job.newest }

    /// This job has something for the live block.
    static func has(_ job: Job) -> Bool {
        let info = task(job).info
        return info.hasLiveActivity || JobHero.select(info) != nil
    }

    /// The hero the page draws on top (an answer keeps its own picture; no step path over it).
    static func hero(_ job: Job) -> JobHero? {
        guard let h = JobHero.select(task(job).info) else { return nil }
        return job.answerTask != nil && h == .steps ? nil : h
    }

    /// The page draws the job's steps itself (hero or plan card), so the body leaves its own out.
    static func drawsSteps(_ job: Job) -> Bool {
        let info = task(job).info
        return hero(job) == .steps || (hero(job) != nil && info.plan.count >= 2 && !info.isTerminal)
    }

    var body: some View {
        let info = Self.task(job).info
        let running = !info.isTerminal
        if wide {
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 14) { picture(info, running); findings(info, running); plan(info, running) }
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                VStack(alignment: .leading, spacing: 14) { calls(info, running); touched(info) }
                    .frame(maxWidth: .infinity, alignment: .topLeading)
            }
        } else {
            VStack(alignment: .leading, spacing: 14) {
                picture(info, running); findings(info, running); calls(info, running); touched(info); plan(info, running)
            }
        }
    }

    @ViewBuilder private func picture(_ info: WorkInfo, _ running: Bool) -> some View {
        if let hero = Self.hero(job) {
            LiveHero(hero: hero, info: info)
        } else if JobHero.select(info) == nil, info.activity.count >= 3 {
            HeroCard { ActivityStripPicture(items: info.activity, touched: info.touched.count, findings: info.findings.count, running: running) }
                .accessibilityIdentifier("job-hero-activity")
        }
    }
    @ViewBuilder private func findings(_ info: WorkInfo, _ running: Bool) -> some View {
        if !info.findings.isEmpty { FindingsList(findings: info.findings, running: running) }
    }
    @ViewBuilder private func calls(_ info: WorkInfo, _ running: Bool) -> some View {
        if !info.activity.isEmpty { ToolCallsCard(job: job, items: info.activity, running: running, model: model) }
    }
    @ViewBuilder private func touched(_ info: WorkInfo) -> some View {
        if !info.touched.isEmpty { TouchedChipsView(items: info.touched, model: model) }
    }
    /// The plan as its own path under a trials/sources/diff hero (the step hero already is it).
    @ViewBuilder private func plan(_ info: WorkInfo, _ running: Bool) -> some View {
        if let hero = Self.hero(job), hero != .steps, info.plan.count >= 2, running {
            LiveCard(title: "The plan", symbol: "point.topleft.down.to.point.bottomright.curvepath.fill",
                     trailing: JobProgress.step(info).map { "Step \($0.index) of \($0.count)" }) {
                StepPathPicture(info: info)
            }
        }
    }
}
