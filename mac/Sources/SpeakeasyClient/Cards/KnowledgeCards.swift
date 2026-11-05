import SwiftUI
import Charts
import SpeakeasyCore

// Media, knowledge, comparisons, lists, charts, news and home.

struct MediaCard: View {
    let card: ViewCard
    var body: some View {
        let nowPlaying = card.kind == "now_playing"
        CardBox {
            HStack(alignment: .top, spacing: 12) {
                if let art = card["art_url"].url {
                    CardImage(url: art, size: nowPlaying ? 64 : 92, corner: 8, fill: true).frame(width: 64)
                } else {
                    Image(systemName: nowPlaying ? "music.note" : "film").font(.system(size: 20)).foregroundStyle(.secondary)
                        .frame(width: 44, height: 44).background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.06)))
                }
                VStack(alignment: .leading, spacing: 3) {
                    if let label = card["kind_label"].string {
                        Text(label.uppercased()).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
                    }
                    Text(card["title"].string ?? "").font(.system(size: 13.5, weight: .semibold)).lineLimit(2)
                    Text([card["subtitle"].string ?? card["artist"].string, card["album"].string, card["year"].string,
                          card["runtime"].string].compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                    if let rating = card["rating"].string { Label(rating, systemImage: "star.fill").font(.system(size: 10.5)).foregroundStyle(.orange) }
                    let where_ = card["where"].strings
                    if !where_.isEmpty { Text("On " + where_.joined(separator: ", ")).font(.system(size: 11.5)).foregroundStyle(.secondary) }
                }
                Spacer(minLength: 0)
                openable(card["url"].url)
            }
            if nowPlaying, let duration = card["duration"].double, duration > 0 {
                Bar(fraction: (card["position"].double ?? 0) / duration, tint: .primary.opacity(0.7), height: 4)
                HStack {
                    Text(clock(card["position"].double ?? 0)); Spacer(); Text(clock(duration))
                }
                .font(.system(size: 9.5).monospacedDigit()).foregroundStyle(.tertiary)
            }
            if let overview = card["overview"].string {
                CardDisclosure(expandable: true) { expanded in
                    Text(overview).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(expanded ? nil : 4)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
    func clock(_ s: Double) -> String { String(format: "%d:%02d", Int(s) / 60, Int(s) % 60) }
}

struct FactCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            switch card.kind {
            case "conversion":
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(card["from_value"].string ?? "").font(.system(size: 22, weight: .light).monospacedDigit())
                    Text(card["from_unit"].string ?? "").font(.system(size: 12)).foregroundStyle(.secondary)
                    Image(systemName: "arrow.right").font(.system(size: 12)).foregroundStyle(.tertiary)
                    Text(card["to_value"].string ?? "").font(.system(size: 22, weight: .semibold).monospacedDigit())
                    Text(card["to_unit"].string ?? "").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                if let note = card["note"].string { Text(note).font(.system(size: 11)).foregroundStyle(.secondary) }
            case "math":
                Text(card["expression"].string ?? "").font(.system(size: 13, design: .monospaced)).foregroundStyle(.secondary)
                Text("= " + (card["result"].string ?? "")).font(.system(size: 26, weight: .semibold).monospacedDigit())
                    .textSelection(.enabled)
                ForEach(Array(card["steps"].strings.prefix(4).enumerated()), id: \.offset) { _, s in
                    Text(s).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                }
            default:
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(card["title"].string ?? "").font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary)
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Text(card["value"].string ?? "").font(.system(size: 26, weight: .semibold)).minimumScaleFactor(0.6)
                                .lineLimit(2).textSelection(.enabled)
                            if let unit = card["unit"].string { Text(unit).font(.system(size: 13)).foregroundStyle(.secondary) }
                        }
                        if let s = card["subtitle"].string { Text(s).font(.system(size: 11)).foregroundStyle(.secondary) }
                    }
                    Spacer(minLength: 0)
                    if let img = card["image_url"].url { CardImage(url: img, size: 56, corner: 8, fill: true).frame(width: 56) }
                }
                if let source = card["source"].string { Text(source).font(.system(size: 11.5)).foregroundStyle(.secondary) }
            }
        }
    }
}

struct EntityCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack(alignment: .top, spacing: 12) {
                if let img = card["image_url"].url { CardImage(url: img, size: 64, corner: 8, fill: true).frame(width: 64) }
                VStack(alignment: .leading, spacing: 2) {
                    Text(card["title"].string ?? "").font(.system(size: 14, weight: .semibold))
                    if let s = card["subtitle"].string { Text(s).font(.system(size: 11)).foregroundStyle(.secondary) }
                    if let s = card["summary"].string {
                        CardDisclosure(expandable: true) { expanded in
                            Text(s).font(.system(size: 11)).lineLimit(expanded ? nil : 4)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, 2)
                    }
                }
                Spacer(minLength: 0)
                openable(card["url"].url)
            }
            KeyValueRows(rows: card["facts"].array)
        }
    }
}

struct KeyValueRows: View {
    let rows: [JSONValue]
    var body: some View {
        if !rows.isEmpty {
            VStack(spacing: 4) {
                ForEach(Array(rows.prefix(10).enumerated()), id: \.offset) { _, r in
                    HStack(alignment: .firstTextBaseline) {
                        Text(r["label"].string ?? "").foregroundStyle(.secondary)
                        Spacer(minLength: 12)
                        Text(r["value"].string ?? "").fontWeight(.medium).multilineTextAlignment(.trailing)
                    }
                    .font(.system(size: 11.5))
                }
            }
        }
    }
}

struct DefinitionCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(card["word"].string ?? "").font(.system(size: 20, weight: .semibold, design: .serif))
                if let p = card["phonetic"].string { Text(p).font(.system(size: 12)).foregroundStyle(.secondary) }
                if let part = card["part"].string { Text(part).font(.system(size: 11).italic()).foregroundStyle(.secondary) }
            }
            ForEach(Array(card["meanings"].strings.prefix(4).enumerated()), id: \.offset) { i, m in
                HStack(alignment: .top, spacing: 6) {
                    Text("\(i + 1).").font(.system(size: 11.5)).foregroundStyle(.secondary)
                    Text(m).font(.system(size: 12))
                }
            }
            if let ex = card["example"].string { Text("“\(ex)”").font(.system(size: 11.5).italic()).foregroundStyle(.secondary) }
        }
    }
}

struct TranslationCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            if let source = card["source"].string {
                Text((card["source_lang"].string.map { $0.uppercased() + "  " } ?? "") + source)
                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            Text(card["text"].string ?? "").font(.system(size: 18, weight: .medium)).textSelection(.enabled)
            if let p = card["phonetic"].string { Text(p).font(.system(size: 12).italic()).foregroundStyle(.secondary) }
            if let lang = card["target_lang"].string { Text(lang.uppercased()).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary) }
        }
    }
}

struct RecipeCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            if let img = card["image_url"].url { CardImage(url: img, size: 120, corner: 10, fill: true).frame(maxWidth: .infinity) }
            HStack {
                Text(card["title"].string ?? "").font(.system(size: 14, weight: .semibold))
                Spacer()
                openable(card["url"].url)
            }
            HStack(spacing: 12) {
                if let t = card["time"].string { Label(t, systemImage: "clock") }
                if let s = card["serves"].string { Label(s, systemImage: "person.2") }
            }
            .font(.system(size: 11)).foregroundStyle(.secondary)
            let ingredients = card["ingredients"].strings
            if !ingredients.isEmpty {
                Text("INGREDIENTS").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
                ForEach(Array(ingredients.prefix(14).enumerated()), id: \.offset) { _, i in
                    Text("· " + i).font(.system(size: 11.5))
                }
            }
            let steps = card["steps"].strings
            if !steps.isEmpty {
                Text("STEPS").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary).padding(.top, 2)
                ForEach(Array(steps.prefix(10).enumerated()), id: \.offset) { n, s in
                    HStack(alignment: .top, spacing: 6) {
                        Text("\(n + 1)").font(.system(size: 11, weight: .bold)).frame(width: 16, height: 16)
                            .background(Circle().fill(Color.primary.opacity(0.08)))
                        Text(s).font(.system(size: 11.5))
                    }
                }
            }
        }
    }
}

struct StatsCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack {
                CardHeader(icon: card.kind == "nutrition" ? "fork.knife" : "chart.bar.fill", title: card["title"].string ?? "")
                if let serving = card["serving"].string { Text(serving).font(.system(size: 11.5)).foregroundStyle(.secondary) }
            }
            if let cal = card["calories"].double {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text("\(Int(cal))").font(.system(size: 28, weight: .semibold).monospacedDigit())
                    Text("calories").font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            if card.kind == "nutrition" {
                KeyValueRows(rows: card["rows"].array)
            } else {
                let items = card["items"].array
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)], spacing: 8) {
                    ForEach(Array(items.prefix(8).enumerated()), id: \.offset) { _, s in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s["label"].string ?? "").font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                            Text(s["value"].string ?? "").font(.system(size: 16, weight: .semibold).monospacedDigit()).lineLimit(1)
                                .minimumScaleFactor(0.7)
                            if let d = s["delta"].string {
                                Text(d).font(.system(size: 11.5, weight: .medium))
                                    .foregroundStyle(s["good"].bool == true ? Color.green : s["good"].bool == false ? Color.red : Color.secondary)
                            }
                        }
                        .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 9).fill(Color.primary.opacity(0.05)))
                    }
                }
            }
        }
    }
}

struct ComparisonCard: View {
    let card: ViewCard
    /// Rows opened in place (by index): their values show in full, without the 3-line limit.
    @State private var openRows: Set<Int> = []
    var body: some View {
        let columns = card["columns"].strings
        let winner = card["winner"].string
        CardBox {
            CardHeader(icon: "rectangle.split.3x1", title: card["title"].string ?? "Comparison")
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 6) {
                GridRow {
                    Text("")
                    ForEach(columns, id: \.self) { c in
                        HStack(spacing: 3) {
                            Text(c).font(.system(size: 11.5, weight: .semibold)).lineLimit(2)
                            if c == winner {
                                Image(systemName: "checkmark.seal.fill").font(.system(size: 10)).foregroundStyle(.green)
                                    .help("Winner").accessibilityLabel("Winner")
                            }
                        }
                    }
                }
                Divider().gridCellUnsizedAxes(.horizontal)
                ForEach(Array(card["rows"].array.prefix(10).enumerated()), id: \.offset) { n, row in
                    let open = Binding(get: { openRows.contains(n) },
                                       set: { if $0 { openRows.insert(n) } else { openRows.remove(n) } })
                    let values = Array(row["values"].strings.prefix(columns.count))
                    GridRow {
                        CardDisclosure(expandable: !values.isEmpty, isExpanded: open) { _ in
                            Text(row["label"].string ?? "").font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                        ForEach(Array(values.enumerated()), id: \.offset) { _, v in
                            Text(v).font(.system(size: 11.5)).lineLimit(open.wrappedValue ? nil : 3).textSelection(.enabled)
                        }
                    }
                }
            }
        }
    }
}

struct ListCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            CardHeader(icon: card.kind == "steps" ? "list.number" : "checklist", title: card["title"].string ?? "List")
            if card.kind == "steps" {
                ForEach(Array(card["items"].strings.prefix(12).enumerated()), id: \.offset) { n, s in
                    HStack(alignment: .top, spacing: 8) {
                        Text("\(n + 1)").font(.system(size: 11, weight: .bold)).frame(width: 18, height: 18)
                            .background(Circle().fill(Color.accentColor.opacity(0.18)))
                        Text(s).font(.system(size: 12)).textSelection(.enabled)
                    }
                }
            } else {
                ForEach(Array(card["items"].array.prefix(16).enumerated()), id: \.offset) { _, item in
                    LinkedRow(url: item["url"].url) { HStack(alignment: .top, spacing: 8) {
                        Image(systemName: item["done"].bool == true ? "checkmark.circle.fill" : "circle")
                            .font(.system(size: 13)).foregroundStyle(item["done"].bool == true ? Color.green : Color.secondary)
                        VStack(alignment: .leading, spacing: 0) {
                            Text(item["text"].string ?? "").font(.system(size: 12))
                                .strikethrough(item["done"].bool == true).foregroundStyle(item["done"].bool == true ? .secondary : .primary)
                            if let d = item["detail"].string { Text(d).font(.system(size: 11.5)).foregroundStyle(.secondary) }
                        }
                        Spacer(minLength: 0)
                    } }
                }
            }
        }
    }
}

/// Film frames only: a chart x label to show as if the pointer were over it.
@MainActor public enum ChartPreview { public static var selection: String? }

struct ChartCard: View {
    #if os(macOS)
    static let chartHint = "Hover or drag to read values"
    #else
    static let chartHint = "Drag across to read values"
    #endif
    let card: ViewCard
    /// The x label under the pointer (hover or drag): its values show above the chart.
    @State private var selected: String?
    var body: some View {
        let labels = card["labels"].strings
        let series = card["series"].array
        // A null value leaves a gap at its own label instead of shifting later values left.
        let points = card.chartPoints
        // One rule per selected label, drawn from its first point (series 0 may be null there).
        let ruleID = selected.flatMap { label in points.first { $0.label == label }?.id }
        let style = card["style"].string ?? "bar"
        CardBox {
            HStack {
                CardHeader(icon: "chart.xyaxis.line", title: card["title"].string ?? "Chart")
                if let unit = card["unit"].string { Text(unit).font(.system(size: 11)).foregroundStyle(.secondary) }
            }
            // Hover (Mac) or drag (iPhone/iPad) across the chart to read exact values.
            let unit = card["unit"].string.map { ["%", "°", "x", "×"].contains($0) ? $0 : " \($0)" } ?? ""
            let readout = selected.map { label in
                let vals = points.filter { $0.label == label }
                return "\(label): " + vals.map { (series.count > 1 ? "\($0.series) " : "") + CardText.number($0.value) + unit }.joined(separator: " · ")
            }
            Text(readout ?? Self.chartHint)
                .font(.system(size: 11.5, weight: selected == nil ? .regular : .semibold).monospacedDigit())
                .foregroundStyle(selected == nil ? Color.secondary : Color.primary)
                .lineLimit(1)
            Chart(points) { p in
                if style == "line" {
                    LineMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
                        .interpolationMethod(.monotone)
                } else {
                    BarMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
                        .position(by: .value("s", p.series))
                        .opacity(selected == nil || selected == p.label ? 1 : 0.45)
                }
                if let selected, selected == p.label, style == "line" {
                    PointMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
                }
                if let selected, p.id == ruleID {
                    RuleMark(x: .value("x", selected)).foregroundStyle(Color.primary.opacity(0.25))
                }
            }
            .chartXSelection(value: $selected)
            .modifier(OneSeriesTint(on: JobLook.touch && series.count == 1))
            .chartLegend(series.count > 1 ? .visible : .hidden)
            .frame(height: 150)
            .onAppear { if let p = ChartPreview.selection, labels.contains(p) { selected = p } }
        }
    }
}

/// Touch: a one-line chart is drawn in the brand accent, not the system's default blue.
private struct OneSeriesTint: ViewModifier {
    var on: Bool
    func body(content: Content) -> some View {
        if on { content.chartForegroundStyleScale(range: [JobLook.brass]) } else { content }
    }
}

struct ProgressCard: View {
    let card: ViewCard
    var body: some View {
        let value = card["value"].double ?? 0
        let total = max(card["total"].double ?? 1, 0.0001)
        CardBox {
            Text(card["label"].string ?? "").font(.system(size: 12.5, weight: .medium))
            Bar(fraction: value / total, tint: value >= total ? .green : .accentColor, height: 7)
            HStack {
                let unit = card["unit"].string.map { " \($0)" } ?? ""
                Text("\(JSONValue.number(value).string ?? "")\(unit) of \(JSONValue.number(total).string ?? "")\(unit)")
                Spacer()
                Text("\(Int((value / total * 100).rounded()))%")
            }
            .font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
            if let d = card["detail"].string { Text(d).font(.system(size: 11)).foregroundStyle(.secondary) }
        }
    }
}

struct NewsCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            CardHeader(icon: "newspaper.fill", title: card["title"].string ?? "News")
            ForEach(Array(card["items"].array.prefix(5).enumerated()), id: \.offset) { i, n in
                if i > 0 { Divider().opacity(0.4) }
                // A story with a link opens it; one without opens in place to its full headline.
                CardDetailRow(value: n, shows: ["headline", "source", "when"]) { expanded in
                    HStack(alignment: .top, spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(n["headline"].string ?? "").font(.system(size: 12, weight: .medium)).lineLimit(expanded ? nil : 3)
                                .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: expanded)
                            Text([n["source"].string, n["when"].string].compactMap { $0 }.joined(separator: " · "))
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 0)
                        if let img = n["image_url"].url { CardImage(url: img, size: 48, corner: 6, fill: true).frame(width: 64) }
                    }
                }
            }
        }
    }
}

struct HomeCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            if card.kind == "thermostat" {
                CardHeader(icon: "thermometer.medium", title: card["name"].string ?? "Thermostat",
                           trailing: card["mode"].string?.capitalized)
                HStack(alignment: .firstTextBaseline, spacing: 14) {
                    VStack(alignment: .leading) {
                        Text("Now").font(.system(size: 11.5)).foregroundStyle(.secondary)
                        Text(ViewCard.degrees(card["current"].double)).font(.system(size: 30, weight: .light))
                    }
                    Image(systemName: "arrow.right").foregroundStyle(.tertiary)
                    VStack(alignment: .leading) {
                        Text("Set to").font(.system(size: 11.5)).foregroundStyle(.secondary)
                        Text(ViewCard.degrees(card["target"].double)).font(.system(size: 30, weight: .semibold)).foregroundStyle(.orange)
                    }
                    Spacer()
                    if let h = card["humidity"].double { Label("\(Int(h))%", systemImage: "humidity").font(.system(size: 11)).foregroundStyle(.secondary) }
                }
            } else {
                CardHeader(icon: "house.fill", title: card["title"].string ?? "Home")
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)], spacing: 8) {
                    ForEach(Array(card["devices"].array.prefix(12).enumerated()), id: \.offset) { _, d in
                        let on = d["on"].bool ?? false
                        VStack(alignment: .leading, spacing: 4) {
                            Image(systemName: deviceSymbol(d["kind"].string, on: on)).font(.system(size: 16))
                                .foregroundStyle(on ? (d["color"].string.map { lineColor($0) } ?? Color.yellow) : Color.secondary)
                            Text(d["name"].string ?? "").font(.system(size: 11, weight: .medium)).lineLimit(2)
                            Text(d["state"].string ?? (on ? (d["level"].double.map { "\(Int($0))%" } ?? "On") : "Off"))
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                        .padding(9).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 10).fill(on ? Color.yellow.opacity(0.14) : Color.primary.opacity(0.05)))
                    }
                }
            }
        }
    }
}

func deviceSymbol(_ kind: String?, on: Bool) -> String {
    switch kind {
    case "light": return on ? "lightbulb.fill" : "lightbulb"
    case "switch", "plug": return on ? "powerplug.fill" : "powerplug"
    case "fan": return "fan.fill"
    case "lock": return on ? "lock.fill" : "lock.open"
    case "cover", "blind", "shade": return "blinds.horizontal.closed"
    case "climate": return "thermometer.medium"
    case "media": return "hifispeaker.fill"
    default: return "circle.grid.2x2.fill"
    }
}

struct CameraCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            CardHeader(icon: "video.fill", title: card["name"].string ?? "Camera", trailing: card["when"].string)
            CardImage(url: card["image_url"].url, size: 160, corner: 10, fill: true).frame(maxWidth: .infinity)
        }
    }
}
