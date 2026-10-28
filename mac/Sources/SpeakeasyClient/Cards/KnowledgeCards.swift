import SwiftUI
import Charts
import SpeakeasyCore

// Media, knowledge, comparisons, lists, charts, news and home.

struct MediaCard: View {
    let card: ViewCard
    var body: some View {
        let nowPlaying = card.kind == "now_playing"
        CardBox {
            CardDetailRow(value: card.data, keys: ["title", "subtitle", "artist", "album", "runtime", "rating"]) {
                HStack(alignment: .top, spacing: 12) {
                    if let art = card["art_url"].url {
                        CardImage(url: art, size: nowPlaying ? 64 : 92, corner: 8, fill: true).frame(width: 64)
                    } else {
                        Image(systemName: nowPlaying ? "music.note" : "film").font(.system(size: 20)).foregroundStyle(.secondary)
                            .frame(width: 44, height: 44).background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.06)))
                    }
                    VStack(alignment: .leading, spacing: 3) {
                        if let label = card["kind_label"].string {
                            Text(label.uppercased()).font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.secondary)
                        }
                        Text(card["title"].string ?? "").font(.system(size: 13.5, weight: .semibold)).lineLimit(2)
                        Text([card["subtitle"].string ?? card["artist"].string, card["album"].string, card["year"].string,
                              card["runtime"].string].compactMap { $0 }.joined(separator: " · "))
                            .font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                        if let rating = card["rating"].string { Label(rating, systemImage: "star.fill").font(.system(size: 10.5)).foregroundStyle(.orange) }
                        let where_ = card["where"].strings
                        if !where_.isEmpty { Text("On " + where_.joined(separator: ", ")).font(.system(size: 10.5)).foregroundStyle(.secondary) }
                    }
                    Spacer(minLength: 0)
                    if card["url"].url != nil { Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary) }
                }
            }
            if nowPlaying, let duration = card["duration"].double, duration > 0 {
                Bar(fraction: (card["position"].double ?? 0) / duration, tint: .primary.opacity(0.7), height: 4)
                HStack {
                    Text(clock(card["position"].double ?? 0)); Spacer(); Text(clock(duration))
                }
                .font(.system(size: 9.5).monospacedDigit()).foregroundStyle(.tertiary)
            }
            if let overview = card["overview"].string {
                CardDisclosure(count: 1, spoken: overview) {
                    Text(overview).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(4)
                } detail: { Text(overview).font(.system(size: 11)).textSelection(.enabled) }
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
                if let source = card["source"].string { Text(source).font(.system(size: 9.5)).foregroundStyle(.tertiary) }
            }
        }
    }
}

struct EntityCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            CardDetailRow(value: card.data, keys: ["title", "subtitle", "summary"]) {
                HStack(alignment: .top, spacing: 12) {
                    if let img = card["image_url"].url { CardImage(url: img, size: 64, corner: 8, fill: true).frame(width: 64) }
                    VStack(alignment: .leading, spacing: 2) {
                        Text(card["title"].string ?? "").font(.system(size: 14, weight: .semibold))
                        if let s = card["subtitle"].string { Text(s).font(.system(size: 11)).foregroundStyle(.secondary) }
                        if let s = card["summary"].string { Text(s).font(.system(size: 11)).lineLimit(4).padding(.top, 2) }
                    }
                    Spacer(minLength: 0)
                    if card["url"].url != nil { Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary) }
                }
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
                ForEach(Array(rows.filter { !CardDetail.fields($0, keys: ["label", "value"]).isEmpty }.enumerated()), id: \.offset) { _, r in
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
            if let lang = card["target_lang"].string { Text(lang.uppercased()).font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.tertiary) }
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
                Text("INGREDIENTS").font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.secondary)
                ForEach(Array(ingredients.prefix(14).enumerated()), id: \.offset) { _, i in
                    Text("· " + i).font(.system(size: 11.5))
                }
            }
            let steps = card["steps"].strings
            if !steps.isEmpty {
                Text("STEPS").font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.secondary).padding(.top, 2)
                ForEach(Array(steps.prefix(10).enumerated()), id: \.offset) { n, s in
                    HStack(alignment: .top, spacing: 6) {
                        Text("\(n + 1)").font(.system(size: 10, weight: .bold)).frame(width: 16, height: 16)
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
            let allRows = CardDetail.rows(card[card.kind == "nutrition" ? "rows" : "items"].array, keys: ["label", "value", "delta"])
            CardDisclosure(count: allRows.count, spoken: allRows.flatMap { CardDetail.fields($0, keys: ["label", "value", "delta"]) }.map(\.text).joined(separator: ", ")) {
                HStack {
                    CardHeader(icon: card.kind == "nutrition" ? "fork.knife" : "chart.bar.fill", title: card["title"].string ?? "")
                    if let serving = card["serving"].string { Text(serving).font(.system(size: 10.5)).foregroundStyle(.tertiary) }
                }
            } detail: {
                KeyValueRows(rows: allRows)
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
                let items = CardDetail.rows(card["items"].array, keys: ["label", "value", "delta"])
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)], spacing: 8) {
                    ForEach(Array(items.prefix(8).enumerated()), id: \.offset) { _, s in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s["label"].string ?? "").font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(1)
                            Text(s["value"].string ?? "").font(.system(size: 16, weight: .semibold).monospacedDigit()).lineLimit(1)
                                .minimumScaleFactor(0.7)
                            if let d = s["delta"].string {
                                Text(d).font(.system(size: 10.5, weight: .medium))
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
    var body: some View {
        let columns = card["columns"].strings
        let winner = card["winner"].string
        CardBox {
            CardHeader(icon: "rectangle.split.3x1", title: card["title"].string ?? "Comparison")
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 6) {
                GridRow {
                    Text("")
                    ForEach(columns, id: \.self) { c in
                        CardDisclosure(count: c == winner ? 1 : 0, spoken: "Winner: " + c) {
                            HStack(spacing: 3) {
                                Text(c).font(.system(size: 11.5, weight: .semibold)).lineLimit(2)
                                if c == winner { Image(systemName: "checkmark.seal.fill").font(.system(size: 10)).foregroundStyle(.green) }
                            }
                        } detail: { Text("Winner: " + c).font(.system(size: 11.5)) }
                    }
                }
                Divider().gridCellUnsizedAxes(.horizontal)
                ForEach(Array(card["rows"].array.prefix(10).enumerated()), id: \.offset) { _, row in
                    GridRow {
                        CardDisclosure(count: row["values"].strings.count, spoken: zip(columns, row["values"].strings).map { "\($0): \($1)" }.joined(separator: ", ")) {
                            Text(row["label"].string ?? "").font(.system(size: 11)).foregroundStyle(.secondary)
                        } detail: {
                            ForEach(Array(row["values"].strings.enumerated()), id: \.offset) { index, value in
                                Text((index < columns.count ? columns[index] + ": " : "") + value).font(.system(size: 11))
                            }
                        }
                        ForEach(Array(row["values"].strings.prefix(columns.count).enumerated()), id: \.offset) { _, v in
                            Text(v).font(.system(size: 11.5)).lineLimit(3)
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
                    CardDisclosure(count: 1, spoken: s) {
                        HStack(alignment: .top, spacing: 8) {
                            Text("\(n + 1)").font(.system(size: 10, weight: .bold)).frame(width: 18, height: 18)
                                .background(Circle().fill(Color.accentColor.opacity(0.18)))
                            Text(s).font(.system(size: 12)).textSelection(.enabled)
                        }
                    } detail: { Text(s).font(.system(size: 12)).textSelection(.enabled) }
                }
            } else {
                ForEach(Array(CardDetail.rows(card["items"].array, keys: ["text", "detail"]).prefix(16).enumerated()), id: \.offset) { _, item in
                    CardDetailRow(value: item, keys: ["text", "detail"], showsLinkArrow: true) { HStack(alignment: .top, spacing: 8) {
                        Image(systemName: item["done"].bool == true ? "checkmark.circle.fill" : "circle")
                            .font(.system(size: 13)).foregroundStyle(item["done"].bool == true ? Color.green : Color.secondary)
                        VStack(alignment: .leading, spacing: 0) {
                            Text(item["text"].string ?? "").font(.system(size: 12))
                                .strikethrough(item["done"].bool == true).foregroundStyle(item["done"].bool == true ? .secondary : .primary)
                            if let d = item["detail"].string { Text(d).font(.system(size: 10.5)).foregroundStyle(.secondary) }
                        }
                        Spacer(minLength: 0)
                    } }
                }
            }
        }
    }
}

struct ChartCard: View {
    let card: ViewCard
    @State private var selectedLabel: String?
    var body: some View {
        let series = card["series"].array
        let points = card.chartPoints
        let style = card["style"].string ?? "bar"
        CardBox {
            HStack {
                CardHeader(icon: "chart.xyaxis.line", title: card["title"].string ?? "Chart")
                if let unit = card["unit"].string { Text(unit).font(.system(size: 10.5)).foregroundStyle(.tertiary) }
            }
            Chart(points) { p in
                if style == "line" {
                    LineMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
                        .interpolationMethod(.monotone)
                } else {
                    BarMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
                        .position(by: .value("s", p.series))
                }
            }
            .chartLegend(series.count > 1 ? .visible : .hidden)
            .chartOverlay { proxy in
                GeometryReader { geometry in
                    Rectangle().fill(.clear).contentShape(Rectangle())
                        .onContinuousHover { phase in
                            if case .active(let location) = phase, let frame = proxy.plotFrame {
                                selectedLabel = proxy.value(atX: location.x - geometry[frame].minX, as: String.self)
                            }
                        }
                        .gesture(SpatialTapGesture().onEnded { tap in
                            guard let frame = proxy.plotFrame else { return }
                            selectedLabel = proxy.value(atX: tap.location.x - geometry[frame].minX, as: String.self)
                        })
                }
            }
            .frame(height: 150)
            if let selectedLabel {
                let readout = points.filter { $0.label == selectedLabel }.map {
                    "\($0.series): \(JSONValue.number($0.value).string ?? "")\(card["unit"].string.map { " " + $0 } ?? "")"
                }.joined(separator: ", ")
                Text(selectedLabel + " — " + readout).font(.system(size: 11)).textSelection(.enabled)
                    .preference(key: ExpandedCardText.self, value: ["chart": selectedLabel + ", " + readout])
            }
        }
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
            ForEach(Array(CardDetail.rows(card["items"].array, keys: ["headline", "source", "when"]).prefix(5).enumerated()), id: \.offset) { i, n in
                if i > 0 { Divider().opacity(0.4) }
                let row = HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(n["headline"].string ?? "").font(.system(size: 12, weight: .medium)).lineLimit(3)
                            .multilineTextAlignment(.leading)
                        Text([n["source"].string, n["when"].string].compactMap { $0 }.joined(separator: " · "))
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                    if let img = n["image_url"].url { CardImage(url: img, size: 48, corner: 6, fill: true).frame(width: 64) }
                }
                CardDetailRow(value: n, keys: ["headline", "source", "when"]) { row }
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
                        Text("Now").font(.system(size: 10.5)).foregroundStyle(.secondary)
                        Text(ViewCard.degrees(card["current"].double)).font(.system(size: 30, weight: .light))
                    }
                    Image(systemName: "arrow.right").foregroundStyle(.tertiary)
                    VStack(alignment: .leading) {
                        Text("Set to").font(.system(size: 10.5)).foregroundStyle(.secondary)
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
                                .font(.system(size: 10)).foregroundStyle(.secondary)
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
