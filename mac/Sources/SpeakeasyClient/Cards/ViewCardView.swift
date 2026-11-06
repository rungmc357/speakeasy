import SwiftUI
import Charts
import SpeakeasyCore

/// The visual cards (`ViewCard`) drawn the same on the Mac panel, iPhone and iPad. Each card is a
/// compact, self-contained view sized for a ~320–420 pt column; the host app supplies the outer
/// list. Remote images (logos, art, photos) load through `CardImage`, https only.
public struct ViewCardView: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    public init(_ card: ViewCard) { self.card = card }

    public var body: some View {
        Group {
            // One row (a small call screen): never a question, whose answers are the point.
            if fit == .line, card.kind != "question" { CardLine(card: card) } else { kindView }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .modifier(CardAccessibility(card: card))
    }

    @ViewBuilder private var kindView: some View {
        Group {
            switch card.kind {
            case "quote": QuoteCard(card: card)
            case "weather": WeatherCard(card: card)
            case "game": GameCard(card: card)
            case "games": GamesCard(card: card)
            case "standings": StandingsCard(card: card)
            case "clock": ClockCard(card: card)
            case "timer", "countdown": CountdownCard(card: card)
            case "calendar_day": DayCard(card: card)
            case "agenda": AgendaCard(card: card)
            case "reminder": ReminderCard(card: card)
            case "place": PlaceCard(card: card)
            case "places": PlacesCard(card: card)
            case "route": RouteCard(card: card)
            case "transit": TransitCard(card: card)
            case "flight": FlightCard(card: card)
            case "package": PackageCard(card: card)
            case "contact": ContactCard(card: card)
            case "message_draft": DraftCard(card: card)
            case "message", "inbox": InboxCard(card: card)
            case "media", "now_playing": MediaCard(card: card)
            case "fact", "conversion", "math": FactCard(card: card)
            case "entity": EntityCard(card: card)
            case "definition": DefinitionCard(card: card)
            case "translation": TranslationCard(card: card)
            case "recipe": RecipeCard(card: card)
            case "nutrition", "stats": StatsCard(card: card)
            case "comparison": ComparisonCard(card: card)
            case "list", "steps": ListCard(card: card)
            case "chart": ChartCard(card: card)
            case "news": NewsCard(card: card)
            case "home", "thermostat": HomeCard(card: card)
            case "camera": CameraCard(card: card)
            case "question": QuestionCard(card: card)
            case "lanes": LanesCard(card: card)
            case "before_after": BeforeAfterCard(card: card)
            case "flow": FlowCard(card: card)
            case "devices": DevicesCard(card: card)
            default: EmptyView()
            }
        }
    }
}

/// A card that only shows things reads as one element (its summary). A question card is something
/// to answer: its answers, "Something else…" box and Undo stay separate elements with their own
/// labels and ids inside one "Question" container (merged, VoiceOver heard one button and the box
/// lost its identity).
struct CardAccessibility: ViewModifier {
    let card: ViewCard
    func body(content: Content) -> some View {
        if card.kind == "question" {
            content
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Question")
                .accessibilityIdentifier("question-card")
        } else if card.kind == "chart" {
            // Each row (label and value) stays its own element; the card is named by its title.
            content
                .accessibilityElement(children: .contain)
                .accessibilityLabel(card.summary)
        } else {
            content
                .accessibilityElement(children: .combine)
                .accessibilityLabel(card.summary)
        }
    }
}

// MARK: - Shared chrome

struct CardBox<Content: View>: View {
    var tint: Color? = nil
    @ViewBuilder var content: Content
    @Environment(\.cardCloseRoom) private var closeRoom
    /// The card's header keeps the corner clear itself (`CardHeader`); without one the whole
    /// content keeps clear of the close button.
    @State private var hasHeader = false
    var body: some View {
        VStack(alignment: .leading, spacing: 8) { content }
            .onPreferenceChange(CardHeaderShown.self) { hasHeader = $0 }
            .padding(.trailing, closeRoom > 0 && !hasHeader ? closeRoom : 0)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(tint.map { AnyShapeStyle($0.opacity(0.14)) } ?? AnyShapeStyle(Color.primary.opacity(0.06)))
            )
            // Touch: no hairline (cards sit on grouped sections already).
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.primary.opacity(JobLook.touch ? 0 : 0.06)))
    }
}

struct CardHeader: View {
    let icon: String
    let title: String
    var trailing: String? = nil
    /// Lines the title may take (a chart's title says what is compared, so it wraps rather than cuts).
    var lines: Int = 1
    @Environment(\.cardCloseRoom) private var closeRoom
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: icon).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            Text(title.uppercased()).font(.system(size: 10.5, weight: .semibold)).tracking(0.4).foregroundStyle(.secondary)
                .lineLimit(lines).fixedSize(horizontal: false, vertical: lines > 1)
            Spacer(minLength: 0)
            if let trailing { Text(trailing).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1) }
            // Clear of the card's close button (inside the corner, `cardCloseRoom`).
            if closeRoom > 0 { Color.clear.frame(width: closeRoom, height: 1) }
        }
        .preference(key: CardHeaderShown.self, value: true)
    }
}

/// Room a card keeps at its top-right corner for a close button drawn inside it (the pinned call
/// card's ×: a 30 pt circle in a 44 pt tap area). 0: no close button.
public struct CardCloseRoomKey: EnvironmentKey { public static let defaultValue: CGFloat = 0 }
public extension EnvironmentValues {
    var cardCloseRoom: CGFloat {
        get { self[CardCloseRoomKey.self] }
        set { self[CardCloseRoomKey.self] = newValue }
    }
}

/// A `CardHeader` is drawn in this card (it keeps the close button's corner clear).
struct CardHeaderShown: PreferenceKey {
    static let defaultValue = false
    static func reduce(value: inout Bool, nextValue: () -> Bool) { value = value || nextValue() }
}

/// An https image (logo, album art, photo). Nothing loads for other schemes.
struct CardImage: View {
    let url: URL?
    var size: CGFloat = 28
    var corner: CGFloat = 6
    var fill: Bool = false
    var body: some View {
        Group {
            if let url {
                AsyncImage(url: url) { phase in
                    if let image = phase.image {
                        image.resizable().aspectRatio(contentMode: fill ? .fill : .fit)
                    } else {
                        RoundedRectangle(cornerRadius: corner).fill(Color.primary.opacity(0.06))
                    }
                }
            } else {
                RoundedRectangle(cornerRadius: corner).fill(Color.primary.opacity(0.06))
            }
        }
        .frame(width: fill ? nil : size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: corner, style: .continuous))
    }
}

struct Sparkline: View {
    let points: [Double]
    let up: Bool
    var height: CGFloat = 56
    var body: some View {
        let lo = points.min() ?? 0, hi = points.max() ?? 1
        Chart(Array(points.enumerated()), id: \.offset) { item in
            LineMark(x: .value("t", item.offset), y: .value("v", item.element))
                .interpolationMethod(.monotone)
                .foregroundStyle(up ? Color.green : Color.red)
            AreaMark(x: .value("t", item.offset), yStart: .value("lo", lo), yEnd: .value("v", item.element))
                .interpolationMethod(.monotone)
                .foregroundStyle(LinearGradient(colors: [(up ? Color.green : Color.red).opacity(0.25), .clear],
                                                startPoint: .top, endPoint: .bottom))
        }
        .chartXAxis(.hidden).chartYAxis(.hidden)
        .chartYScale(domain: lo...(hi == lo ? lo + 1 : hi))
        .frame(height: height)
    }
}

func weatherSymbol(_ icon: String?) -> String {
    switch icon {
    case "sun": return "sun.max.fill"
    case "cloud_sun": return "cloud.sun.fill"
    case "cloud": return "cloud.fill"
    case "fog": return "cloud.fog.fill"
    case "drizzle": return "cloud.drizzle.fill"
    case "rain": return "cloud.rain.fill"
    case "heavy_rain": return "cloud.heavyrain.fill"
    case "sleet": return "cloud.sleet.fill"
    case "snow": return "cloud.snow.fill"
    case "storm": return "cloud.bolt.rain.fill"
    default: return "cloud.fill"
    }
}

/// A row that opens its link when tapped: the whole row is the target, with a small ↗ so it reads as clickable.
struct LinkedRow<Content: View>: View {
    let url: URL?
    @ViewBuilder var content: Content
    var body: some View {
        if let url = webURL(url) {
            Link(destination: url) {
                HStack(alignment: .top, spacing: 6) {
                    content
                    Image(systemName: "arrow.up.right").font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.secondary)
                        .padding(.top, 2)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(url.absoluteString)
        } else {
            content
        }
    }
}

/// Only https links open from a card.
func webURL(_ url: URL?) -> URL? {
    guard let url, url.scheme?.lowercased() == "https", url.host?.contains(".") == true else { return nil }
    return url
}

/// Report text rendered with `AgentMarkdown` (bold, headings, bullets, never raw marks), its markdown
/// links ([name](https://…)) and bare https links tappable.
func linkedText(_ text: String) -> AttributedString {
    var out = AgentMarkdown.attributed(text)
    for run in out.runs where run.link != nil {
        if webURL(run.link) == nil { out[run.range].link = nil } else { out[run.range].underlineStyle = .single }
    }
    return out
}

func openable(_ url: URL?) -> some View {
    Group {
        if let url {
            Link(destination: url) { Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold)) }
                .foregroundStyle(.secondary)
        }
    }
}

/// A thin rounded bar (0...1). Used instead of ProgressView so it looks the same on Mac and iPhone.
struct Bar: View {
    let fraction: Double
    var tint: Color = .accentColor
    var height: CGFloat = 5
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.10))
                Capsule().fill(tint).frame(width: max(height, geo.size.width * min(max(fraction, 0), 1)))
            }
        }
        .frame(height: height)
    }
}
