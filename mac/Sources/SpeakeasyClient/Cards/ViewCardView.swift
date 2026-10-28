import SwiftUI
import Charts
import SpeakeasyCore

/// The visual cards (`ViewCard`) drawn the same on the Mac panel, iPhone and iPad. Each card is a
/// compact, self-contained view sized for a ~320–420 pt column; the host app supplies the outer
/// list. Remote images (logos, art, photos) load through `CardImage`, https only.
public struct ViewCardView: View {
    let card: ViewCard
    @State private var expandedText: [String: String] = [:]
    public init(_ card: ViewCard) { self.card = card }

    public var body: some View {
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
            case "progress": ProgressCard(card: card)
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
        .frame(maxWidth: .infinity, alignment: .leading)
        .onPreferenceChange(ExpandedCardText.self) { expandedText = $0 }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(([card.summary] + expandedText.keys.sorted().compactMap { expandedText[$0] }).joined(separator: ", "))
        .onChange(of: card) { _, _ in expandedText = [:] }
    }
}

// MARK: - Shared chrome

struct CardBox<Content: View>: View {
    var tint: Color? = nil
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 8) { content }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(tint.map { AnyShapeStyle($0.opacity(0.14)) } ?? AnyShapeStyle(Color.primary.opacity(0.06)))
            )
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.primary.opacity(0.06)))
    }
}

struct CardHeader: View {
    let icon: String
    let title: String
    var trailing: String? = nil
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: icon).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            Text(title.uppercased()).font(.system(size: 10.5, weight: .semibold)).tracking(0.4).foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
            if let trailing { Text(trailing).font(.system(size: 10.5)).foregroundStyle(.tertiary).lineLimit(1) }
        }
    }
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

/// Report text with its markdown links ([name](https://…)) and bare https links tappable.
func linkedText(_ text: String) -> AttributedString {
    var source = text
    if let regex = try? NSRegularExpression(pattern: #"(?<![\[(<])\bhttps://[^\s<>()\[\]]+"#) {
        let ns = source as NSString
        for match in regex.matches(in: source, range: NSRange(location: 0, length: ns.length)).reversed() {
            var raw = ns.substring(with: match.range)
            var tail = ""
            while let last = raw.last, ".,;:!?".contains(last) { tail = String(last) + tail; raw.removeLast() }
            source = (source as NSString).replacingCharacters(in: match.range, with: "[\(raw)](\(raw))" + tail)
        }
    }
    let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    guard var out = try? AttributedString(markdown: source, options: options) else { return AttributedString(text) }
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
