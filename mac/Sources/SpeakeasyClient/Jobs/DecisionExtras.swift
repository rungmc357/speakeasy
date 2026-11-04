import SwiftUI
import SpeakeasyCore

// Decision helpers shared by the question card and the job view's decision brief:
// a side-by-side Compare of the options and the 5-second Undo after a pick.

/// The options as columns: each option's detail, risk, reversibility and tags in rows, so they
/// can be read across instead of one after another. The recommended column is tinted.
public struct DecisionCompare: View {
    @Environment(\.jobTextScale) private var s
    var decision: Decision
    var chosen: String?
    var onPick: ((Int) -> Void)?
    public init(decision: Decision, chosen: String? = nil, onPick: ((Int) -> Void)? = nil) {
        self.decision = decision; self.chosen = chosen; self.onPick = onPick
    }

    public var body: some View {
        let options = Array(decision.options.prefix(4))
        HStack(alignment: .top, spacing: 6) {
            ForEach(Array(options.enumerated()), id: \.offset) { i, option in
                let rec = i == decision.recommended
                VStack(alignment: .leading, spacing: 6) {
                    NumberBadge(number: i + 1, chosen: chosen == option.text)
                    Text(option.text).font(.system(size: 12 * s, weight: .semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    if rec { JobPill(text: "Recommended", style: .blue) }
                    row("Risk", Self.risk(option))
                    row("Undo", option.reversible.map { $0 ? "Reversible" : "Can't undo" })
                    ForEach(Array(option.tags.prefix(3).enumerated()), id: \.offset) { _, tag in
                        Text(tag.text).font(.system(size: 11 * s)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let detail = option.detail {
                        Text(detail).font(.system(size: 11 * s)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                    if let onPick, chosen == nil {
                        TileButton(title: "Pick \(i + 1)", primary: rec) { onPick(i + 1) }
                    }
                }
                .padding(7)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(rec ? JobStyle.blue.opacity(0.13) : Color.primary.opacity(0.05)))
                .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .strokeBorder(chosen == option.text ? JobStyle.blueInk : Color.primary.opacity(0.08), lineWidth: 1))
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Options side by side")
    }

    @ViewBuilder private func row(_ label: String, _ value: String?) -> some View {
        if let value {
            VStack(alignment: .leading, spacing: 1) {
                Text(label.uppercased()).font(.system(size: 10.5 * s, weight: .semibold)).tracking(0.4).foregroundStyle(.secondary)
                Text(value).font(.system(size: 11 * s, weight: .medium)).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    static func risk(_ o: DecisionOption) -> String? {
        switch o.risk { case "low": return "Low"; case "medium": return "Some"; case "high": return "High"; default: return nil }
    }

    /// Worth comparing: two or more options and at least one says more than its name.
    public static func worthComparing(_ d: Decision) -> Bool {
        d.options.count >= 2 && d.options.contains { $0.detail != nil || !$0.tags.isEmpty || $0.risk != nil || $0.reversible != nil }
    }
}

/// The option number, so a pick can be said or typed ("2").
public struct NumberBadge: View {
    @Environment(\.jobTextScale) private var s
    var number: Int
    var chosen: Bool
    public init(number: Int, chosen: Bool = false) { self.number = number; self.chosen = chosen }
    public var body: some View {
        Text("\(number)").font(.system(size: 11 * s, weight: .bold, design: .rounded))
            .frame(width: 19, height: 19)
            .background(RoundedRectangle(cornerRadius: 5).fill(chosen ? JobStyle.blue : Color.primary.opacity(0.1)))
            .foregroundStyle(chosen ? Color.white : Color.primary.opacity(0.75))
    }
}

/// "Sending “Option” in 4 s · Undo": shown while a pick waits out its undo window.
public struct PickUndoBar: View {
    @Environment(\.jobTextScale) private var s
    var picked: String
    var until: Date
    var onUndo: () -> Void
    public init(picked: String, until: Date, onUndo: @escaping () -> Void) {
        self.picked = picked; self.until = until; self.onUndo = onUndo
    }
    public var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { ctx in
            let left = max(0, Int(until.timeIntervalSince(ctx.date).rounded(.up)))
            HStack(spacing: 8) {
                Image(systemName: "paperplane").font(.system(size: 11 * s, weight: .semibold)).foregroundStyle(JobStyle.blueInk)
                Text(left > 0 ? "Sending “\(picked)” in \(left) s" : "Sending “\(picked)”…")
                    .font(.system(size: 11.5 * s, weight: .medium)).lineLimit(2)
                Spacer(minLength: 4)
                if left > 0 {
                    Button(action: onUndo) {
                        Text("Undo").font(.system(size: 11.5 * s, weight: .semibold))
                            .padding(.horizontal, 10).padding(.vertical, 4)
                            .background(Capsule().fill(JobStyle.blue.opacity(0.18)))
                            .foregroundStyle(JobStyle.blueInk)
                    }
                    .buttonStyle(.plain)
                    #if os(macOS)
                    .keyboardShortcut("z", modifiers: .command)
                    #endif
                    .accessibilityLabel("Undo pick")
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(JobStyle.blue.opacity(0.09)))
        }
    }
}

/// A Compare / List switch for decision options.
public struct CompareToggle: View {
    @Environment(\.jobTextScale) private var s
    @Binding var comparing: Bool
    public init(comparing: Binding<Bool>) { self._comparing = comparing }
    public var body: some View {
        Button { comparing.toggle() } label: {
            Label(comparing ? "List" : "Compare", systemImage: comparing ? "list.bullet" : "rectangle.split.3x1")
                .font(.system(size: 11 * s, weight: .semibold))
                .foregroundStyle(JobStyle.blueInk)
        }
        .buttonStyle(.plain)
        .help(comparing ? "Show options as a list" : "Show options side by side")
    }
}
