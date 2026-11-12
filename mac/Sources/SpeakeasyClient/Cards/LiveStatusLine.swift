import SwiftUI
import SpeakeasyCore

/// A running job's one-line status ("Reading Yelp"), as the Home thread, the work strip and the
/// running card show it (SPK-231 R4, B73-05).
///
/// Always ONE line, cut with an ellipsis, never wrapped onto the row below, and always the SAME
/// size whatever the step says: the line's size comes from a hidden one-line "Ag" in its font and
/// the width it is offered, never from the status text. The text is drawn in an overlay, which
/// takes no part in its parent's layout, and a new step crossfades in there. So a status change
/// re-draws the line but cannot re-lay out the row or the thread it sits in (perf73 pass 3: the old
/// push transition inserted and removed lines inside the stack's layout; 40 ms/s of hitches with
/// 10 lines changing on a long thread).
///
/// Consequence: the line has no ideal width of its own. Don't put it under `fixedSize()`; give it
/// the width it may use. Digits are monospaced, so "3 of 12" → "4 of 12" doesn't jitter sideways.
/// With Reduce Motion the new step simply replaces the old one.
public struct LiveStatusLine: View {
    public let text: String
    public var font: Font
    public var color: Color
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// How long the old step takes to fade into the new one.
    public static let crossfade: Double = 0.22

    public init(_ text: String, font: Font = .footnote, color: Color = .secondary) {
        self.text = text
        self.font = font
        self.color = color
    }

    private var lineFont: Font { font.monospacedDigit() }

    public var body: some View {
        // The size: one line of this font, as wide as offered. Independent of the text.
        Text(verbatim: "Ag").font(lineFont).lineLimit(1).hidden()
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .leading) {
                ZStack(alignment: .leading) {
                    Text(AgentMarkdown.attributed(text))
                        .font(lineFont).foregroundStyle(color)
                        .lineLimit(1).truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .id(text)
                        .transition(reduceMotion ? .identity : .opacity)
                }
                .animation(reduceMotion ? nil : .easeInOut(duration: Self.crossfade), value: text)
            }
            .clipped()
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(Text(AgentMarkdown.attributed(text)))
    }
}

/// The kind of thing a running job is doing, for the step capsule's glyph (home-fixes-73). The
/// plugin names it (`step_kind`, from the tool the job called); with an older plugin the app reads it
/// from the step's own words, which the plugin made from that same tool call. Never guessed beyond that.
public enum LiveStepKind: String, CaseIterable, Sendable {
    case mail, calendar, web, terminal, doc, edit, search, think, message, helper, tool

    public var symbol: String {
        switch self {
        case .mail: return "envelope"
        case .calendar: return "calendar"
        case .web: return "globe"
        case .terminal: return "terminal"
        case .doc: return "doc.text"
        case .edit: return "pencil"
        case .search: return "magnifyingglass"
        case .think: return "sparkle"
        case .message: return "text.bubble"
        case .helper: return "person.2"
        case .tool: return "wrench.and.screwdriver"
        }
    }

    /// The plugin's kind when it sent one, else the step's words, else "think" (the job is on its
    /// model's turn: no tool call yet).
    public static func resolve(_ raw: String?, text: String) -> LiveStepKind {
        if let raw, let kind = LiveStepKind(rawValue: raw) { return kind }
        return infer(text)
    }

    public static func infer(_ text: String) -> LiveStepKind {
        let t = text.lowercased()
        func has(_ words: [String]) -> Bool { words.contains { t.contains($0) } }
        if has(["mail", "inbox", "message from"]) { return .mail }
        if has(["calendar", "schedule", "agenda"]) { return .calendar }
        if has(["web", "site", "page", "browsing", "opened ", ".com", ".org", "online"]) { return .web }
        if has(["searching", "searched", "looking for", "looked through", "finding"]) { return .search }
        if has(["writing", "wrote", "editing", "edited", "drafting", "updating"]) { return .edit }
        if has(["reading", "read ", "notes", "file"]) { return .doc }
        if has(["running", "ran ", "command", "tests", "building", "installing"]) { return .terminal }
        if has(["helper"]) { return .helper }
        return .think
    }
}

/// A running job as Home draws it (home-fixes-73; Geo's direction N27/R29/N32): a small capsule
/// riding the reply, with a glyph for the KIND of step (it changes with a symbol transition), the
/// step's words with a soft shimmer sweeping across while live (a new step crossfades in), and three
/// breathing dots. Nothing else: no title, no counter, no ring.
///
/// Fixed height whatever the step says, and the words sit in an overlay (`LiveStatusLine`'s rule),
/// so a step change re-draws the capsule but never re-lays out the exchange or the thread around it.
/// Every animation is a symbol effect or one repeating offset, which the render loop drives without
/// re-running this body. Reduce Motion: no shimmer, no dots moving, a plain swap of the words.
public struct LiveStepCapsule: View {
    public let text: String
    public let kind: LiveStepKind
    public var font: Font
    public var ink: Color
    public var tint: Color
    public var fill: Color
    public var stroke: Color
    public var live: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .subheadline) private var height: CGFloat = 30
    @ScaledMetric(relativeTo: .subheadline) private var glyph: CGFloat = 13

    public init(_ text: String, kind: LiveStepKind, font: Font = .subheadline, ink: Color = .primary,
                tint: Color = .accentColor, fill: Color = Color.primary.opacity(0.06),
                stroke: Color = Color.primary.opacity(0.08), live: Bool = true) {
        self.text = text; self.kind = kind; self.font = font; self.ink = ink; self.tint = tint
        self.fill = fill; self.stroke = stroke; self.live = live
    }

    public var body: some View {
        HStack(spacing: 7) {
            Image(systemName: kind.symbol)
                .font(.system(size: glyph, weight: .semibold))
                .foregroundStyle(tint)
                .contentTransition(reduceMotion ? .identity : .symbolEffect(.replace))
                .frame(width: glyph + 6)
                .accessibilityHidden(true)
            LiveShimmerLine(text: text, font: font, ink: ink, shimmer: live && !reduceMotion)
                .layoutPriority(1)
            if live {
                Image(systemName: "ellipsis")
                    .font(.system(size: glyph - 1, weight: .bold))
                    .foregroundStyle(ink.opacity(0.55))
                    .symbolEffect(.variableColor.iterative, options: .repeating, isActive: !reduceMotion)
                    .accessibilityHidden(true)
            }
        }
        .padding(.leading, 10).padding(.trailing, 12)
        .frame(height: height)
        .background(Capsule(style: .continuous).fill(fill))
        .overlay(Capsule(style: .continuous).strokeBorder(stroke, lineWidth: 0.5))
        .fixedSize(horizontal: false, vertical: true)
        .animation(reduceMotion ? nil : .easeInOut(duration: LiveStatusLine.crossfade), value: kind)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(AgentMarkdown.attributed(text)))
        .accessibilityAddTraits(.updatesFrequently)
    }
}

/// The capsule's words: one line, sized by a hidden "Ag" so it never changes height, as wide as its
/// words up to the room offered; a highlight band sweeps across while live.
public struct LiveShimmerLine: View {
    public let text: String
    public let font: Font
    public let ink: Color
    public let shimmer: Bool
    @State private var sweep = false
    /// Call energy / SPK-430: no sweep behind a call or while the phone is hot (the words stay, still).
    @ObservedObject private var energy = EnergyGovernor.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    public init(text: String, font: Font, ink: Color, shimmer: Bool) {
        self.text = text; self.font = font; self.ink = ink; self.shimmer = shimmer
    }

    public var body: some View {
        // Width follows the words (so the capsule hugs them), height never does.
        let shimmer = self.shimmer && energy.policy.shimmer
        Text(verbatim: text.isEmpty ? "Ag" : text).font(font).lineLimit(1).hidden()
            .overlay(alignment: .leading) {
                ZStack(alignment: .leading) {
                    words(ink.opacity(shimmer ? 0.62 : 0.8))
                        .overlay {
                            if shimmer {
                                words(ink).mask {
                                    GeometryReader { g in
                                        LinearGradient(colors: [.clear, .white, .clear], startPoint: .leading, endPoint: .trailing)
                                            .frame(width: max(40, g.size.width * 0.45))
                                            .offset(x: sweep ? g.size.width : -max(40, g.size.width * 0.45))
                                            .animation(.linear(duration: 1.7).repeatForever(autoreverses: false), value: sweep)
                                    }
                                }
                                .onAppear { sweep = true }
                                .onDisappear { sweep = false }
                            }
                        }
                        .id(text)
                        .transition(reduceMotion ? .identity : .opacity)
                }
                .animation(reduceMotion ? nil : .easeInOut(duration: LiveStatusLine.crossfade), value: text)
            }
            .clipped()
    }

    private func words(_ color: Color) -> some View {
        Text(AgentMarkdown.attributed(text)).font(font).foregroundStyle(color)
            .lineLimit(1).truncationMode(.tail)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

#if DEBUG
#Preview("LiveStatusLine") {
    VStack(alignment: .leading, spacing: 8) {
        LiveStatusLine("Reading 3 of 12 invented pages")
        LiveStatusLine("A much longer invented step that will not fit on one line and is cut with an ellipsis", font: .subheadline)
        LiveStatusLine("", font: .headline)
        LiveStepCapsule("Searching mail for the invented flight", kind: .mail)
        LiveStepCapsule("Thinking it over", kind: .think)
    }
    .padding()
    .frame(width: 320)
}
#endif
