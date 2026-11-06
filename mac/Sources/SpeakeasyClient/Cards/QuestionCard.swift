import SwiftUI
import SpeakeasyCore

/// What a question card can do in this host: send the picked answer back to its task. Hosts that
/// can't answer (or a call that ended) leave `onAnswer` nil and the card says to answer by voice.
public struct QuestionActions {
    public var onAnswer: ((String) -> Void)?
    public var answered: String?
    /// Loads a task picture by card number (the authenticated card-image route); nil = no pictures.
    public var image: ((Int) async -> Data?)?
    /// The pick is held until then so it can be undone; nil once sent.
    public var undoUntil: Date?
    public var onUndo: (() -> Void)?
    /// "Something else…": an answer in the user's own words, to this decision only (servers with
    /// typed messages). Returns whether it went. nil: no field.
    public var onSomethingElse: ((String) -> Bool)?
    public init(onAnswer: ((String) -> Void)? = nil, answered: String? = nil, image: ((Int) async -> Data?)? = nil,
                undoUntil: Date? = nil, onUndo: (() -> Void)? = nil, onSomethingElse: ((String) -> Bool)? = nil) {
        self.onAnswer = onAnswer; self.answered = answered; self.image = image
        self.undoUntil = undoUntil; self.onUndo = onUndo; self.onSomethingElse = onSomethingElse
    }
}

private struct QuestionActionsKey: EnvironmentKey { static let defaultValue = QuestionActions() }
public extension EnvironmentValues {
    var questionActions: QuestionActions {
        get { self[QuestionActionsKey.self] }
        set { self[QuestionActionsKey.self] = newValue }
    }
}

/// A decision a task needs: the question, numbered answers (one marked Recommended), answered with
/// one tap, the number key, or out loud. After answering it settles to a quiet "Answered" line.
struct QuestionCard: View {
    let card: ViewCard
    @Environment(\.questionActions) private var actions
    @State private var picked: String?
    @State private var comparing = false
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.needsYouShownAbove) private var shownAbove

    var body: some View {
        if JobLook.touch { touchBody } else { macBody }
    }

    /// Touch (iPhone call screen, iPad, Vision): the job page's decision language on glass — the
    /// question, the same option rows with a brass star on the recommended one, and "Something
    /// else…" where the host can send it. No Compare here: tapping the card opens the job, which has it.
    @ViewBuilder private var touchBody: some View {
        let decision = Decision(card: card)
        let options = decision?.options ?? []
        let answer = actions.undoUntil != nil ? actions.answered : (picked ?? actions.answered)
        let s = 1.2 * Self.typeFactor(typeSize)
        VStack(alignment: .leading, spacing: 10) {
            if answer == nil {
                if !shownAbove { NeedsYouMark() }
            } else {
                AnsweredMark()
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(card["question"].string ?? "").font(.system(size: 14 * s, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if let context = decision?.context {
                    Text(context).font(.system(size: 11.5 * s)).foregroundStyle(.secondary).lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            let pictures = card["images"].doubles.isEmpty ? options.map { $0.image ?? 0 } : card["images"].doubles.map { Int($0) }
            if pictures.contains(where: { $0 > 0 }), let load = actions.image {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(Array(options.prefix(4).enumerated()), id: \.offset) { i, option in
                        PictureOption(number: i + 1, text: option.text, picture: i < pictures.count ? pictures[i] : 0,
                                      load: load, recommended: i == decision?.recommended, chosen: answer == option.text,
                                      dimmed: answer != nil && answer != option.text,
                                      enabled: answer == nil && actions.onAnswer != nil) {
                            picked = option.text
                            actions.onAnswer?(option.text)
                        }
                    }
                }
            } else {
                VStack(spacing: 6) {
                    ForEach(Array(options.prefix(4).enumerated()), id: \.offset) { i, option in
                        DecisionOptionRow(number: i + 1, option: DecisionOption(text: option.text), recommended: i == decision?.recommended,
                                          why: nil, chosen: answer == option.text, dimmed: answer != nil && answer != option.text,
                                          enabled: answer == nil && actions.onAnswer != nil, compact: true) {
                            picked = option.text
                            actions.onAnswer?(option.text)
                        }
                    }
                }
            }
            if let answer, let until = actions.undoUntil, let undo = actions.onUndo {
                PickUndoBar(picked: answer, until: until) { picked = nil; undo() }
            } else if answer == nil, actions.onAnswer != nil, let other = actions.onSomethingElse {
                SomethingElseInput { text in
                    guard other(text) else { return false }
                    picked = TaskMessageRequest.clean(text)
                    return true
                }
            } else if answer == nil {
                Text(actions.onAnswer != nil ? Self.hint(min(options.count, 4)) : "Say your answer.")
                    .font(.system(size: 11 * s)).foregroundStyle(.secondary)
            } else if !options.contains(where: { $0.text == answer }) {
                Text("\u{201C}\(answer ?? "")\u{201D} · sent to the task.").font(.system(size: 11 * s)).foregroundStyle(.secondary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Sent to the task.").font(.system(size: 11 * s)).foregroundStyle(.secondary)
            }
        }
        .environment(\.jobTextScale, s)
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .modifier(TouchGlass(RoundedRectangle(cornerRadius: 22, style: .continuous), interactive: false))
        .animation(.spring(response: 0.3, dampingFraction: 0.85), value: answer)
    }

    /// Follows the reader's text size the way the job views do (Large is 1×).
    static func typeFactor(_ size: DynamicTypeSize) -> CGFloat {
        switch size {
        case .xSmall: return 0.85
        case .small: return 0.9
        case .medium: return 0.95
        case .large: return 1
        case .xLarge: return 1.1
        case .xxLarge: return 1.2
        case .xxxLarge: return 1.3
        default: return 1.4
        }
    }

    @ViewBuilder private var macBody: some View {
        // Options are strings (older plugins) or objects with a detail line, tags and a picture.
        let decision = Decision(card: card)
        let options = decision?.options.map(\.text) ?? []
        let recommended = decision?.recommended
        let answer = actions.undoUntil != nil ? actions.answered : (picked ?? actions.answered)
        CardBox(tint: answer == nil ? NeedsYouMark.color : nil) {
            if answer == nil {
                if !shownAbove { NeedsYouMark() }
            } else {
                AnsweredMark()
            }
            Text(card["question"].string ?? "").font(.system(size: 14, weight: .semibold))
                .fixedSize(horizontal: false, vertical: true)
            if let context = decision?.context {
                Text(context).font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let decision, DecisionCompare.worthComparing(decision) {
                HStack { Spacer(minLength: 0); CompareToggle(comparing: $comparing) }
            }
            let pictures = card["images"].doubles.isEmpty ? (decision?.options.map { $0.image ?? 0 } ?? [])
                : card["images"].doubles.map { Int($0) }
            if pictures.contains(where: { $0 > 0 }), let load = actions.image {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(Array(options.prefix(4).enumerated()), id: \.offset) { i, option in
                        PictureOption(number: i + 1, text: option, picture: i < pictures.count ? pictures[i] : 0,
                                      load: load, recommended: i == recommended, chosen: answer == option,
                                      dimmed: answer != nil && answer != option,
                                      enabled: answer == nil && actions.onAnswer != nil) {
                            picked = option
                            actions.onAnswer?(option)
                        }
                    }
                }
            } else if comparing, let decision {
                DecisionCompare(decision: decision, chosen: answer,
                                onPick: answer == nil && actions.onAnswer != nil ? { n in
                                    let o = decision.options[n - 1].text; picked = o; actions.onAnswer?(o) } : nil)
            } else {
            VStack(spacing: 6) {
                ForEach(Array(options.prefix(4).enumerated()), id: \.offset) { i, option in
                    OptionRow(number: i + 1, text: option, recommended: i == recommended,
                              chosen: answer == option, dimmed: answer != nil && answer != option,
                              enabled: answer == nil && actions.onAnswer != nil) {
                        picked = option
                        actions.onAnswer?(option)
                    }
                }
            }
            }
            if let answer, let until = actions.undoUntil, let undo = actions.onUndo {
                PickUndoBar(picked: answer, until: until) { picked = nil; undo() }
            } else {
                Text(answer != nil ? "Sent to the task." :
                        actions.onAnswer != nil ? Self.hint(min(options.count, 4)) : "Say your answer.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
        .animation(.spring(response: 0.3, dampingFraction: 0.85), value: answer)
    }

    static func hint(_ n: Int) -> String {
        #if os(macOS)
        return "Click, press 1–\(n), or just say it."
        #else
        return "Tap one, or just say it."
        #endif
    }
}

private struct OptionRow: View {
    let number: Int
    let text: String
    let recommended: Bool
    let chosen: Bool
    let dimmed: Bool
    let enabled: Bool
    let action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                NumberBadge(number: number, chosen: chosen)
                Text(text).font(.system(size: 12.5, weight: chosen ? .semibold : .regular)).lineLimit(2)
                Spacer(minLength: 4)
                if recommended && !chosen && !dimmed {
                    Text("Recommended").font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Capsule().fill(Color.accentColor.opacity(0.14)))
                        .foregroundStyle(Color.accentColor)
                }
                if chosen { Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(Color.accentColor) }
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .contentShape(Rectangle())
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(chosen ? Color.accentColor.opacity(0.12) : Color.primary.opacity(hover && enabled ? 0.08 : 0.04)))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(recommended && !dimmed && !chosen ? Color.accentColor.opacity(0.35) : Color.clear, lineWidth: 1))
            .opacity(dimmed ? 0.45 : 1)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        #if os(macOS)
        .onHover { hover = $0 }
        .keyboardShortcut(KeyEquivalent(Character("\(number)")), modifiers: [])
        #endif
    }
}

/// An option that is something to look at: its picture, number and name; click to pick, or press
/// its number. Opens larger on hover via the tooltip-sized preview is left to the task's image cards.
private struct PictureOption: View {
    let number: Int
    let text: String
    let picture: Int
    let load: (Int) async -> Data?
    let recommended: Bool
    let chosen: Bool
    let dimmed: Bool
    let enabled: Bool
    let action: () -> Void
    @State private var image: Image?
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                ZStack(alignment: .topLeading) {
                    RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06))
                    if let image {
                        image.resizable().scaledToFit()   // the whole design, never cropped
                            .frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
                    } else if picture > 0 {
                        ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    Text("\(number)")
                        .font(.system(size: 11.5, weight: .bold, design: .rounded))
                        .frame(width: 18, height: 18)
                        .background(RoundedRectangle(cornerRadius: 5, style: .continuous)
                            .fill(chosen ? Color.accentColor : Color.black.opacity(0.55)))
                        .foregroundStyle(.white)
                        .padding(5)
                }
                .aspectRatio(4.0 / 3.0, contentMode: .fit)
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(
                    chosen ? Color.accentColor : recommended && !dimmed ? Color.accentColor.opacity(0.5)
                        : Color.primary.opacity(hover && enabled ? 0.25 : 0.08),
                    lineWidth: chosen ? 2 : 1))
                HStack(spacing: 4) {
                    Text(text).font(.system(size: 11.5, weight: chosen ? .semibold : .regular)).lineLimit(2)
                    Spacer(minLength: 0)
                    if chosen { Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(Color.accentColor) }
                }
                if recommended && !chosen && !dimmed {
                    Text("Recommended").font(.system(size: 11, weight: .semibold)).foregroundStyle(Color.accentColor)
                }
            }
            .contentShape(Rectangle())
            .opacity(dimmed ? 0.45 : 1)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .frame(maxWidth: .infinity)
        .task(id: picture) {
            guard picture > 0, image == nil, let data = await load(picture) else { return }
            #if os(macOS)
            if let ns = NSImage(data: data) { image = Image(nsImage: ns) }
            #else
            if let ui = UIImage(data: data) { image = Image(uiImage: ui) }
            #endif
        }
        #if os(macOS)
        .onHover { hover = $0 }
        .keyboardShortcut(KeyEquivalent(Character("\(number)")), modifiers: [])
        #endif
    }
}
