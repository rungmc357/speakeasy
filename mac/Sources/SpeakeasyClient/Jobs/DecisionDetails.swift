import SwiftUI
import SpeakeasyCore

/// A decision's details for a page that answers it its own way (the Mac session page, SPK-540):
/// what it is about, what it touches, and every option with its detail, its risk and the reason for
/// the recommended one. The job page's option rows, without its number keys: the session page has a
/// composer under it, where a typed digit must stay a digit.
public struct DecisionDetails: View {
    var decision: Decision
    var enabled: Bool
    var onPick: (DecisionOption) -> Void
    @Environment(\.jobTextScale) private var s

    public init(decision: Decision, enabled: Bool = true, onPick: @escaping (DecisionOption) -> Void) {
        self.decision = decision; self.enabled = enabled; self.onPick = onPick
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let context = decision.context { quiet(context, size: 11.5) }
            // With a recommended option its row carries the why; without one it still gets said.
            if decision.recommended == nil, let why = decision.why { quiet(why, size: 11.5) }
            if let affects = decision.affects {
                Label(affects, systemImage: "scope").font(.system(size: 11 * s)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(spacing: 6) {
                ForEach(Array(decision.options.enumerated()), id: \.offset) { i, option in
                    let why = i == decision.recommended ? decision.why : nil
                    let row = DecisionOptionRow(number: i + 1, option: option, recommended: i == decision.recommended, why: why,
                                                chosen: false, dimmed: false, enabled: enabled) { onPick(option) }
                    // The row's label is its number and title: VoiceOver gets the rest as its value
                    // (the detail, the why, then the risk and undo pills as they read on screen).
                    row.accessibilityValue(([option.detail, why].compactMap { $0 } + row.pills.map(\.0))
                        .map { $0.hasSuffix(".") ? String($0.dropLast()) : $0 }.joined(separator: ". "))
                }
            }
        }
    }

    private func quiet(_ text: String, size: CGFloat) -> some View {
        Text(text).font(.system(size: size * s)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
    }
}
