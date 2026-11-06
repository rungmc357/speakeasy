import SwiftUI
import SpeakeasyCore

/// How much of a card to draw. `.compact` is the call screen's short form for a card that would
/// otherwise crowd the orb, the caption and the controls: the weather's now + next few hours
/// instead of the 7-day forecast, a list's first rows with "N more", a draft's first lines, a
/// smaller picture or chart. The full card is one tap away (the host shows it in a sheet).
public enum CardFit: Sendable, Equatable {
    case full
    case compact

    /// Rows a list-like card draws: all of them (up to the card's own cap), or the first few.
    public func rows(_ cap: Int) -> Int { self == .compact ? min(cap, Self.compactRows) : cap }

    /// Rows a compact list-like card keeps.
    public static let compactRows = 3
}

private struct CardFitKey: EnvironmentKey { static let defaultValue = CardFit.full }
public extension EnvironmentValues {
    var cardFit: CardFit {
        get { self[CardFitKey.self] }
        set { self[CardFitKey.self] = newValue }
    }
}

/// "3 more" under a list cut short by the compact form.
struct MoreRows: View {
    let total: Int
    let shown: Int
    var body: some View {
        if total > shown {
            Text("\(total - shown) more").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                .accessibilityHidden(true)
        }
    }
}
