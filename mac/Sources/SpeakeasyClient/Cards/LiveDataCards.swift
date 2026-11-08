import SwiftUI
import SpeakeasyCore

// Markets, weather, sports and time cards.

struct QuoteCard: View {
    let card: ViewCard
    var body: some View {
        let change = card["change_pct"].double ?? 0
        let up = change >= 0
        let currency = card["currency"].string
        CardBox {
            HStack(alignment: .center, spacing: 10) {
                if let logo = card["logo_url"].url {
                    CardImage(url: logo, size: 30, corner: 15)
                } else {
                    Text(String((card["symbol"].string ?? "?").prefix(4)))
                        .font(.system(size: 11, weight: .bold, design: .rounded))
                        .frame(width: 34, height: 34)
                        .background(Circle().fill(Color.primary.opacity(0.08)))
                }
                VStack(alignment: .leading, spacing: 1) {
                    Text(card["name"].string ?? card["symbol"].string ?? "").font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    Text([card["symbol"].string, card["exchange"].string].compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 1) {
                    if let price = card["price"].double {
                        Text(ViewCard.money(price, currency: currency)).font(.system(size: 17, weight: .semibold).monospacedDigit())
                    }
                    HStack(spacing: 3) {
                        Image(systemName: up ? "arrowtriangle.up.fill" : "arrowtriangle.down.fill").font(.system(size: 7))
                        if let abs = card["change"].double { Text(ViewCard.signed(abs, money: true, currency: currency)) }
                        Text("(\(ViewCard.signed(change)))")
                    }
                    .font(.system(size: 11, weight: .medium).monospacedDigit())
                    .foregroundStyle(up ? Color.green : Color.red)
                }
            }
            let points = card["points"].doubles
            if points.count > 2 { Sparkline(points: points, up: up) }
            if let asOf = card["as_of"].string {
                Text(asOf).font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }
}

struct WeatherCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    var body: some View {
        switch fit {
        case .full: full
        case .compact: WeatherGlanceCard(card: card)
        case .line: CardLine(card: card)
        }
    }

    private var full: some View {
        CardBox(tint: .blue) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(card["place"].string ?? "Weather").font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                    Text(card.weatherHeadline).font(.system(size: 38, weight: .light))
                    Text(sentenceCase(card["condition"].string ?? "")).font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 4) {
                    Image(systemName: weatherSymbol(card["icon"].string)).symbolRenderingMode(.multicolor).font(.system(size: 30))
                    if card["temp"].double != nil {
                        Text(card.weatherHighLow)
                            .font(.system(size: 11, weight: .medium).monospacedDigit())
                    }
                    if let rain = card["rain"].double, rain > 0 {
                        Label("\(Int(rain))%", systemImage: "drop.fill").font(.system(size: 10.5)).foregroundStyle(.blue)
                    }
                }
            }
            let hours = card["hours"].array
            if !hours.isEmpty {
                Divider().opacity(0.4)
                HStack(spacing: 0) {
                    ForEach(Array(hours.prefix(6).enumerated()), id: \.offset) { _, h in
                        VStack(spacing: 4) {
                            Text(h["label"].string ?? "").font(.system(size: 11.5)).foregroundStyle(.secondary)
                            Image(systemName: weatherSymbol(h["icon"].string)).symbolRenderingMode(.multicolor).font(.system(size: 13))
                            Text(ViewCard.degrees(h["temp"].double)).font(.system(size: 11, weight: .medium).monospacedDigit())
                        }
                        .frame(maxWidth: .infinity)
                    }
                }
            }
            let days = card["days"].array
            if days.count > 1 {
                Divider().opacity(0.4)
                let lo = days.compactMap { $0["low"].double }.min() ?? 0
                let hi = days.compactMap { $0["high"].double }.max() ?? 1
                VStack(spacing: 5) {
                    ForEach(Array(days.prefix(7).enumerated()), id: \.offset) { _, d in
                        HStack(spacing: 8) {
                            // A day name stays one line ("Tuesday" used to wrap to "Tuesda / y" on Home).
                            Text(d["label"].string ?? "").font(.system(size: 13)).lineLimit(1).minimumScaleFactor(0.7)
                                .frame(width: 58, alignment: .leading)
                            Image(systemName: weatherSymbol(d["icon"].string)).symbolRenderingMode(.multicolor)
                                .font(.system(size: 16)).frame(width: 22)
                            // A real chance of rain (30% and up) under the icon's day, as the designs say it:
                            // "take an umbrella Thursday" needs the number on the card.
                            Group {
                                if let rain = d["rain"].double, rain >= 30 {
                                    Text("\(Int(rain))%").font(.system(size: 11, weight: .semibold).monospacedDigit()).foregroundStyle(.blue)
                                } else { Text("") }
                            }
                            .frame(width: 30, alignment: .leading)
                            // A day with no low shows no low (not a dash) and its bar is only its high,
                            // never a bar stretched down to the week's lowest temperature.
                            Text(d["low"].double == nil ? "" : ViewCard.degrees(d["low"].double))
                                .font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
                                .frame(width: 30, alignment: .trailing)
                            TempBar(low: d["low"].double ?? d["high"].double ?? lo, high: d["high"].double ?? hi, min: lo, max: hi)
                            Text(ViewCard.degrees(d["high"].double)).font(.system(size: 11, weight: .medium).monospacedDigit())
                                .frame(width: 30, alignment: .leading)
                        }
                    }
                }
            }
        }
    }
}

/// The weather's short form (the call screen): now, the day's high and low, the chance of rain and
/// the next few hours on one row. The 7-day forecast stays in the full card.
struct WeatherGlanceCard: View {
    let card: ViewCard
    var body: some View {
        CardBox(tint: .blue) {
            WeatherNow(card: card)
            let hours = card["hours"].array
            if !hours.isEmpty {
                Divider().opacity(0.4)
                // As many of the next four hours as fit the width on one row.
                ViewThatFits(in: .horizontal) {
                    ForEach([4, 3, 2], id: \.self) { n in hoursRow(Array(hours.prefix(n))) }
                }
            }
        }
    }

    private func hoursRow(_ hours: [JSONValue]) -> some View {
        HStack(spacing: 8) {
            ForEach(Array(hours.enumerated()), id: \.offset) { _, h in
                HStack(spacing: 4) {
                    Text(h["label"].string ?? "").font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                    Image(systemName: weatherSymbol(h["icon"].string)).symbolRenderingMode(.multicolor).font(.system(size: 11))
                    Text(ViewCard.degrees(h["temp"].double)).font(.system(size: 11, weight: .medium).monospacedDigit())
                }
                .fixedSize()
                .frame(maxWidth: .infinity)
            }
        }
    }
}

/// The weather now in one row: symbol, temperature, condition and place, high/low and rain chance.
struct WeatherNow: View {
    let card: ViewCard
    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: weatherSymbol(card["icon"].string)).symbolRenderingMode(.multicolor).font(.system(size: 26))
            Text(card.weatherHeadline).font(.system(size: 30, weight: .light)).lineLimit(1).fixedSize()
            VStack(alignment: .leading, spacing: 1) {
                Text(sentenceCase(card["condition"].string ?? "")).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                Text(card["place"].string ?? "Weather").font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            VStack(alignment: .trailing, spacing: 3) {
                if card["temp"].double != nil {
                    Text(card.weatherHighLow)
                        .font(.system(size: 11, weight: .medium).monospacedDigit()).lineLimit(1)
                }
                if let rain = card["rain"].double, rain > 0 {
                    Label("\(Int(rain))%", systemImage: "drop.fill").font(.system(size: 10.5)).foregroundStyle(.blue)
                }
            }
            .fixedSize()
        }
    }
}

struct TempBar: View {
    let low: Double, high: Double, min: Double, max: Double
    var body: some View {
        GeometryReader { geo in
            let span = Swift.max(max - min, 1)
            let x0 = (low - min) / span * geo.size.width
            let x1 = (high - min) / span * geo.size.width
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.08))
                Capsule().fill(LinearGradient(colors: [.teal, .orange], startPoint: .leading, endPoint: .trailing))
                    .frame(width: Swift.max(x1 - x0, 6)).offset(x: x0)
            }
        }
        .frame(height: 5)
    }
}

struct TeamRow: View {
    let side: JSONValue
    let showScore: Bool
    var body: some View {
        HStack(spacing: 8) {
            if let logo = side["logo_url"].url { CardImage(url: logo, size: 26, corner: 4) }
            VStack(alignment: .leading, spacing: 0) {
                Text(side["name"].string ?? side["abbr"].string ?? "").font(.system(size: 12.5, weight: side["winner"].bool == true ? .semibold : .regular))
                    .lineLimit(1)
                if let record = side["record"].string {
                    Text(record).font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
            if showScore, let score = side["score"].string {
                Text(score).font(.system(size: 20, weight: side["winner"].bool == true ? .bold : .regular).monospacedDigit())
                    .foregroundStyle(side["winner"].bool == false ? .secondary : .primary)
            }
        }
    }
}

struct GameCard: View {
    let card: ViewCard
    var body: some View {
        let status = card["status"].string ?? "scheduled"
        CardBox {
            HStack {
                CardHeader(icon: "sportscourt.fill", title: card["league"].string ?? "Game")
                if status == "live" {
                    Text("LIVE").font(.system(size: 11.5, weight: .bold)).foregroundStyle(.white)
                        .padding(.horizontal, 5).padding(.vertical, 1.5).background(Capsule().fill(.red))
                } else if status == "projected" {
                    Text("PROJECTED").font(.system(size: 11.5, weight: .bold)).foregroundStyle(.secondary)
                        .padding(.horizontal, 5).padding(.vertical, 1.5).background(Capsule().fill(.primary.opacity(0.08)))
                }
            }
            ForEach(Array(card["teams"].array.enumerated()), id: \.offset) { _, side in
                TeamRow(side: side, showScore: status != "scheduled")
            }
            Text([status == "scheduled" ? card["when"].string : card["detail"].string, card["venue"].string,
                  card["broadcast"].string].compactMap { $0 }.joined(separator: " · "))
                .font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(2)
        }
    }
}

struct GamesCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            CardHeader(icon: "sportscourt.fill", title: card["title"].string ?? "Games")
            ForEach(Array(card["games"].array.prefix(8).enumerated()), id: \.offset) { index, g in
                if index > 0 { Divider().opacity(0.4) }
                let teams = g["teams"].array
                let scored = g["status"].string != "scheduled"
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(teams.enumerated()), id: \.offset) { _, side in
                            HStack(spacing: 6) {
                                if let logo = side["logo_url"].url { CardImage(url: logo, size: 16, corner: 3) }
                                Text(side["abbr"].string ?? side["name"].string ?? "").font(.system(size: 11.5,
                                    weight: side["winner"].bool == true ? .semibold : .regular))
                                Spacer(minLength: 0)
                                if scored, let s = side["score"].string { Text(s).font(.system(size: 11.5).monospacedDigit()) }
                            }
                        }
                    }
                    Text(g["status"].string == "final" ? "Final" : g["status"].string == "live" ? "Live" : (g["when"].string ?? ""))
                        .font(.system(size: 11)).foregroundStyle(g["status"].string == "live" ? Color.red : Color.secondary)
                        .frame(width: 110, alignment: .trailing).lineLimit(2).multilineTextAlignment(.trailing)
                }
            }
        }
    }
}

struct StandingsCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            CardHeader(icon: "list.number", title: card["title"].string ?? "Standings")
            ForEach(Array(card["rows"].array.prefix(12).enumerated()), id: \.offset) { i, row in
                HStack(spacing: 8) {
                    Text(row["rank"].string ?? "\(i + 1)").font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
                        .frame(width: 18, alignment: .trailing)
                    if let logo = row["logo_url"].url { CardImage(url: logo, size: 16, corner: 3) }
                    Text(row["name"].string ?? "").font(.system(size: 11.5)).lineLimit(1)
                    Spacer(minLength: 0)
                    Text(row["record"].string ?? "").font(.system(size: 11).monospacedDigit())
                    if let note = row["note"].string { Text(note).font(.system(size: 11)).foregroundStyle(.secondary) }
                }
            }
        }
    }
}

struct ClockCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack(spacing: 10) {
                ForEach(Array(card["zones"].array.prefix(3).enumerated()), id: \.offset) { _, z in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(z["place"].string ?? "").font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
                        Text(z["time"].string ?? "").font(.system(size: 24, weight: .light).monospacedDigit())
                            .minimumScaleFactor(0.7).lineLimit(1)
                        Text([z["day"].string, z["offset"].string, z["zone"].string].compactMap { $0 }.joined(separator: " · "))
                            .font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(RoundedRectangle(cornerRadius: 10).fill(z["here"].bool == true ? Color.clear : Color.primary.opacity(0.05)))
                }
            }
        }
    }
}

struct CountdownCard: View {
    let card: ViewCard
    var body: some View {
        CardBox(tint: .orange) {
            CardHeader(icon: card.kind == "timer" ? "timer" : "calendar.badge.clock", title: card["label"].string ?? (card.kind == "timer" ? "Timer" : "Countdown"))
            if card.kind == "timer", let ends = card["ends_at"].string.flatMap(parseISO) {
                Text(timerInterval: Date()...max(ends, Date()), countsDown: true)
                    .font(.system(size: 34, weight: .light).monospacedDigit())
            } else if let days = card["days"].double {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(Int(days))").font(.system(size: 34, weight: .light).monospacedDigit())
                    Text(Int(days) == 1 ? "day" : "days").font(.system(size: 13)).foregroundStyle(.secondary)
                }
            }
            if let detail = card["detail"].string ?? card["date"].string {
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }
}

func parseISO(_ text: String) -> Date? {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = f.date(from: text) { return d }
    f.formatOptions = [.withInternetDateTime]
    return f.date(from: text)
}

