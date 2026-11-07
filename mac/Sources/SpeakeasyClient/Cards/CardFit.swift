import SwiftUI
import SpeakeasyCore

/// How much of a card to draw. `.compact` is the call screen's short form for a card that would
/// otherwise crowd the orb, the caption and the controls: the weather's now + next few hours
/// instead of the 7-day forecast, a list's first rows with "N more", a draft's first lines, a
/// smaller picture or chart. The full card is one tap away (the host shows it in a sheet).
public enum CardFit: Sendable, Equatable {
    case full
    case compact
    /// One row: what the card is and its key value ("Packing list · 10 items", the weather's now).
    /// For a call screen with no room even for the compact form (a small phone).
    case line

    /// Rows a list-like card draws: all of them (up to the card's own cap), or the first few.
    public func rows(_ cap: Int) -> Int { self == .full ? cap : min(cap, Self.compactRows) }

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
    /// What the hidden rows are when it isn't obvious from the rows above ("days" under one day's events).
    var noun: (one: String, many: String)? = nil
    var body: some View {
        if total > shown {
            let n = total - shown
            Text(noun.map { "\(n) more \(n == 1 ? $0.one : $0.many)" } ?? "\(n) more").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                .accessibilityHidden(true)
        }
    }
}

/// A card in one row (`CardFit.line`): its icon, what it is, one detail, and its value drawn small
/// where it has one (the weather's temperature, a chart's bars, a price). Only values from the card.
struct CardLine: View {
    let card: ViewCard

    var body: some View {
        if card.kind == "weather" {
            CardBox(tint: .blue) { WeatherNow(card: card) }
        } else {
            CardBox {
                HStack(spacing: 10) {
                    Image(systemName: Self.icon(card.kind)).font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.secondary).frame(width: 22)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(Self.title(card)).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                        if let detail = Self.detail(card) {
                            Text(detail).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer(minLength: 4)
                    AnswerGlance(card)
                }
            }
        }
    }

    static func icon(_ kind: String) -> String {
        switch kind {
        case "list": return "checklist"
        case "steps": return "list.number"
        case "calendar_day", "agenda": return "calendar"
        case "places", "place": return "mappin.and.ellipse"
        case "route", "transit": return "map"
        case "message_draft": return "envelope"
        case "message", "inbox": return "tray.full.fill"
        case "news": return "newspaper.fill"
        case "recipe": return "fork.knife"
        case "chart": return "chart.bar.fill"
        case "camera": return "video.fill"
        case "media", "now_playing": return "music.note"
        case "home", "thermostat", "devices": return "house.fill"
        case "package": return "shippingbox.fill"
        case "flight": return "airplane"
        case "explore": return "square.stack.fill"
        case "compare": return "star.fill"
        case "status": return "circle.grid.2x2.fill"
        case "draft": return "paperplane.fill"
        default: return "square.text.square"
        }
    }

    static func title(_ card: ViewCard) -> String {
        switch card.kind {
        case "message_draft": return card["subject"].string ?? "Email draft"
        case "draft": return card["subject"].string ?? ArtifactDraftCard.channelName(card["channel"].string) + " draft"
        case "explore": return card["title"].string ?? card["sections"].array.first?["label"].string ?? "Ideas"
        case "status": return card["scope"].string ?? "Status"
        case "calendar_day": return card["title"].string ?? card["date"].string ?? "Today"
        default: return card.summary
        }
    }

    /// The one detail under the title: how much there is, or who a draft goes to.
    static func detail(_ card: ViewCard) -> String? {
        func count(_ n: Int, _ one: String, _ many: String) -> String? { n > 0 ? "\(n) \(n == 1 ? one : many)" : nil }
        switch card.kind {
        case "list", "steps": return count(card["items"].array.count, "item", "items")
        case "news": return count(card["items"].array.count, "story", "stories")
        case "inbox": return count(card["items"].array.count, "message", "messages")
        case "places": return count(card["items"].array.count, "place", "places")
        case "calendar_day": return count(card["events"].array.count, "event", "events") ?? "Nothing scheduled"
        case "agenda": return count(card["days"].array.count, "day", "days")
        case "chart": return ChartFacts(card).subtitle
        case "draft": return card["to"].string.map { "To \($0)" }
        case "explore":
            return card["sections"].array.compactMap { $0["label"].string }.joined(separator: " · ")
        case "message_draft":
            return card["to"].string.map { "To \($0)" } ?? card["body"].string?.split(separator: "\n").first.map(String.init)
        default: return nil
        }
    }
}
