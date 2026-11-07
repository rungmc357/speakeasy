import SwiftUI

/// The one look for "something waits on you", on every surface (question cards on a call and on
/// the task page, the session page, the conversation list, the Kanban board, the Mac panel): a
/// brass raised hand and "Needs you" in caption semibold. A filled red capsule is only for a
/// count badge, never this label.
public struct NeedsYouMark: View {
    /// The mark's colour: the brand's needs-you brass (deeper on paper, AA as text both ways).
    public static let color = BrandPalette.amber
    public static let symbol = "hand.raised.fill"
    public static let text = "Needs you"

    var font: Font?
    public init(font: Font? = nil) { self.font = font }

    /// Caption semibold: the touch apps' Dynamic Type caption; the Mac panel's fixed small size.
    public static var defaultFont: Font {
        JobLook.touch ? .caption.weight(.semibold) : .system(size: 11, weight: .semibold)
    }

    public var body: some View {
        HStack(spacing: 4) {
            Image(systemName: Self.symbol).imageScale(.small)
            Text(Self.text)
        }
        .font(font ?? Self.defaultFont)
        .foregroundStyle(Self.color)
        .lineLimit(1)
        .fixedSize()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Self.text)
        .accessibilityIdentifier("needs-you-mark")
    }
}

/// The settled form of the same line once the question is answered: a quiet green check.
public struct AnsweredMark: View {
    public init() {}
    public var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "checkmark.circle.fill").imageScale(.small).foregroundStyle(JobStyle.green)
            Text("Answered").foregroundStyle(.secondary)
        }
        .font(NeedsYouMark.defaultFont)
        .lineLimit(1)
        .fixedSize()
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("answered-mark")
    }
}

private struct NeedsYouShownAboveKey: EnvironmentKey { static let defaultValue = false }
public extension EnvironmentValues {
    /// The page already says "Needs you" above (the task page's eyebrow): question and decision
    /// cards under it leave their own mark out instead of saying it twice.
    var needsYouShownAbove: Bool {
        get { self[NeedsYouShownAboveKey.self] }
        set { self[NeedsYouShownAboveKey.self] = newValue }
    }
}
