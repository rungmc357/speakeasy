import SwiftUI
import Charts
import SpeakeasyCore

// Media, knowledge, comparisons, lists, charts, news and home.

struct MediaCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
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
                Text(overview).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(fit == .compact ? 2 : 4)
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
                    if let s = card["summary"].string { Text(s).font(.system(size: 11)).lineLimit(4).padding(.top, 2) }
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
    @Environment(\.cardFit) private var fit
    var body: some View {
        CardBox {
            if let img = card["image_url"].url { CardImage(url: img, size: fit == .compact ? 72 : 120, corner: 10, fill: true).frame(maxWidth: .infinity) }
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
            // Compact: the title, time and servings; ingredients and steps are in the full card.
            let ingredients = fit == .compact ? [] : card["ingredients"].strings
            if !ingredients.isEmpty {
                Text("INGREDIENTS").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
                ForEach(Array(ingredients.prefix(14).enumerated()), id: \.offset) { _, i in
                    Text("· " + i).font(.system(size: 11.5))
                }
            }
            let steps = fit == .compact ? [] : card["steps"].strings
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
            CardHeaderRow {
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
                PairGrid(count: min(items.count, 8), spacing: 8) { n in
                    let s = items[n]
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s["label"].string ?? "").font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                            Text(s["value"].string ?? "").font(.system(size: 16, weight: .semibold).monospacedDigit()).lineLimit(1)
                                .minimumScaleFactor(0.7)
                            if let d = s["delta"].string {
                                Text(d).font(.system(size: 11.5, weight: .medium))
                                    .foregroundStyle(s["good"].bool == true ? Color.green : s["good"].bool == false ? Color.red : Color.secondary)
                            }
                        }
                        .padding(8).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                        .background(RoundedRectangle(cornerRadius: 9).fill(Color.primary.opacity(0.05)))
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
                        HStack(spacing: 3) {
                            Text(c).font(.system(size: 11.5, weight: .semibold)).lineLimit(2)
                            if c == winner { Image(systemName: "checkmark.seal.fill").font(.system(size: 10)).foregroundStyle(.green) }
                        }
                    }
                }
                Divider().gridCellUnsizedAxes(.horizontal)
                ForEach(Array(card["rows"].array.prefix(10).enumerated()), id: \.offset) { _, row in
                    GridRow {
                        Text(row["label"].string ?? "").font(.system(size: 11)).foregroundStyle(.secondary)
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
    @Environment(\.cardFit) private var fit
    var body: some View {
        CardBox {
            CardHeader(icon: card.kind == "steps" ? "list.number" : "checklist", title: card["title"].string ?? "List")
            if card.kind == "steps" {
                ForEach(Array(card["items"].strings.prefix(fit.rows(12)).enumerated()), id: \.offset) { n, s in
                    HStack(alignment: .top, spacing: 8) {
                        Text("\(n + 1)").font(.system(size: 11, weight: .bold)).frame(width: 18, height: 18)
                            .background(Circle().fill(Color.accentColor.opacity(0.18)))
                        Text(s).font(.system(size: 12)).textSelection(.enabled)
                    }
                }
                MoreRows(total: min(card["items"].strings.count, 12), shown: fit.rows(12))
            } else {
                ForEach(Array(card["items"].array.prefix(fit.rows(16)).enumerated()), id: \.offset) { _, item in
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
                MoreRows(total: min(card["items"].array.count, 16), shown: fit.rows(16))
            }
        }
    }
}

/// Film frames only: a chart x label to show as if the pointer were over it.
@MainActor public enum ChartPreview { public static var selection: String? }

struct ChartCard: View {
    let card: ViewCard
    struct Point: Identifiable { let id: Int; let series: String; let label: String; let value: Double }
    /// The x label under the pointer (hover or drag on a plot): its values replace the rest line.
    @State private var selected: String?
    /// The plot's value table, one tap away.
    @State private var showsValues = false
    @Environment(\.cardFit) private var fit
    @ScaledMetric(relativeTo: .caption) private var small: CGFloat = 11.5
    /// Charts' own axis label size (caption 2), as it scales with the reader's text size.
    @ScaledMetric(relativeTo: .caption2) private var axisScaled: CGFloat = 11
    @Environment(\.dynamicTypeSize) private var typeSize

    /// Axis labels follow the text size up to a point: past it they'd crowd a 150 pt plot into
    /// overlapping numbers, and the readout line and "Show values" carry every value at full size.
    private var axisSize: CGFloat { min(axisScaled, 16) }

    var body: some View {
        let facts = ChartFacts(card)
        CardBox {
            CardHeader(icon: facts.ranked ? "chart.bar.fill" : "chart.xyaxis.line", title: facts.title, lines: 2)
            // What the numbers are and which order the bars stand in (the bars really do).
            if let subtitle = facts.subtitle {
                Text(subtitle).font(.system(size: small)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("chart-subtitle")
            }
            if facts.ranked {
                ChartRows(facts: facts, shown: fit.rows(ChartFacts.maxRankedRows))
                MoreRows(total: facts.rows.count, shown: fit.rows(ChartFacts.maxRankedRows))
            } else {
                plot(facts)
            }
            // Only what the card says about its data: source, as-of time, delayed. Never invented.
            if let fresh = facts.freshness {
                // Old data says so with its date and a mark, in the card's quiet text color.
                Label(fresh, systemImage: facts.stale ? "exclamationmark.circle" : "clock")
                    .font(.system(size: small - 0.5)).foregroundStyle(Color.secondary)
                    .labelStyle(FreshnessLabel())
                    .fixedSize(horizontal: false, vertical: true)
                    // Read as its words only (newer iOS read the icon's name, "Clock").
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(facts.stale ? "Older data. \(fresh)" : fresh)
                    .accessibilityIdentifier("chart-freshness")
            }
        }
    }

    @ViewBuilder private func plot(_ facts: ChartFacts) -> some View {
        let points: [Point] = facts.rows.enumerated().flatMap { i, row in
            row.values.enumerated().compactMap { s, v in
                v.map { Point(id: s * 1000 + i, series: facts.seriesNames[s], label: row.label, value: $0) }
            }
        }
        let multi = facts.seriesNames.count > 1
        let style = card["style"].string ?? "bar"
        let short = Dictionary(zip(facts.rows.map(\.label), ChartFacts.shortLabels(facts.rows.map(\.label), max: 6)),
                               uniquingKeysWith: { a, _ in a })
        // The numbers at rest (latest, high, low); the one under the finger or pointer while dragging.
        let line = selected.flatMap { label in facts.rows.first { $0.label == label } }.map { "\($0.label): \(facts.text($0))" }
            ?? facts.restLine
        if let line {
            Text(line)
                .font(.system(size: small, weight: selected == nil ? .regular : .semibold).monospacedDigit())
                .foregroundStyle(selected == nil ? Color.secondary : Color.primary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("chart-readout")
        }
        Chart(points) { p in
            if style == "line" {
                LineMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
                    .interpolationMethod(.monotone)
                    .accessibilityLabel(multi ? "\(p.series), \(p.label)" : p.label)
                    .accessibilityValue(facts.text(p.value))
            } else {
                BarMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
                    .position(by: .value("s", p.series))
                    .opacity(selected == nil || selected == p.label ? 1 : 0.45)
                    .accessibilityLabel(multi ? "\(p.series), \(p.label)" : p.label)
                    .accessibilityValue(facts.text(p.value))
            }
            if let selected, selected == p.label, style == "line" {
                PointMark(x: .value("x", p.label), y: .value("y", p.value)).foregroundStyle(by: .value("s", p.series))
            }
            if let selected, p.id % 1000 == 0 || !multi, selected == p.label {
                RuleMark(x: .value("x", selected)).foregroundStyle(Color.primary.opacity(0.25))
            }
        }
        .chartXSelection(value: $selected)
        // Axis labels stay distinct when cut (GOOG and GOOGL never both read "GO…").
        // Only as many x labels as fit side by side: Charts drops the ones that would touch (at the
        // largest text sizes a row of times otherwise runs together into one long number). The
        // readout line and "Show values" still carry every value.
        .chartXAxis {
            AxisMarks { value in
                AxisValueLabel(collisionResolution: .greedy(minimumSpacing: 6)) {
                    if let s = value.as(String.self) { Text(short[s] ?? s).font(.system(size: axisSize)) }
                }
            }
        }
        .chartYAxis {
            AxisMarks(values: .automatic(desiredCount: typeSize.isAccessibilitySize ? 3 : 5)) { _ in
                AxisGridLine()
                AxisValueLabel().font(.system(size: axisSize))
            }
        }
        .modifier(OneSeriesTint(on: JobLook.touch && !multi))
        .chartLegend(multi ? .visible : .hidden)
        .frame(height: fit == .compact ? 88 : 150)
        .onAppear { if let p = ChartPreview.selection, facts.rows.contains(where: { $0.label == p }) { selected = p } }
        // Every value one tap away (and the table is what VoiceOver reads row by row).
        if fit == .full {
            Button {
                withAnimation(.easeOut(duration: 0.2)) { showsValues.toggle() }
            } label: {
                Label(showsValues ? "Hide values" : "Show values", systemImage: showsValues ? "chevron.up" : "list.number")
                    .font(.system(size: small, weight: .semibold))
                    .frame(minHeight: JobLook.touch ? 44 : 24).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .frame(minHeight: JobLook.touch ? 44 : 24)
            .foregroundStyle(JobLook.touch ? CardStyle.active.emphasis : Color.accentColor)
            .accessibilityIdentifier("chart-show-values")
            if showsValues { ChartValueTable(facts: facts) }
        }
    }
}

/// A ranked chart as rows: label, bar and value side by side, every value readable at rest.
/// Labels get their own column (tickers are never cut); at the largest text sizes each row
/// stacks its label and value over the bar.
struct ChartRows: View {
    let facts: ChartFacts
    let shown: Int
    @ScaledMetric(relativeTo: .footnote) private var font: CGFloat = 12.5
    @Environment(\.dynamicTypeSize) private var typeSize
    /// The widest label and value (the columns line up across rows).
    @State private var labelWidth: CGFloat = 0
    @State private var valueWidth: CGFloat = 0

    static func tint(_ series: Int) -> Color {
        if series == 0 { return JobLook.touch ? CardStyle.active.emphasis : Color.accentColor }
        return [Color.teal, Color.purple, Color.pink][(series - 1) % 3]
    }

    var body: some View {
        let rows = Array(facts.rows.prefix(shown))
        let scale = max(facts.rows.flatMap { $0.values.compactMap { $0 }.map(abs) }.max() ?? 1, .ulpOfOne)
        let multi = facts.seriesNames.count > 1
        VStack(alignment: .leading, spacing: 6) {
            if multi {
                HStack(spacing: 10) {
                    ForEach(Array(facts.seriesNames.enumerated()), id: \.offset) { i, name in
                        HStack(spacing: 4) {
                            Circle().fill(Self.tint(i)).frame(width: 7, height: 7)
                            Text(name).font(.system(size: font - 1)).foregroundStyle(.secondary)
                        }
                    }
                }
                .accessibilityHidden(true)
            }
            if typeSize.isAccessibilitySize {
                ForEach(rows) { row in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(row.label).font(.system(size: font, weight: .semibold))
                            Spacer(minLength: 6)
                            Text(facts.text(row)).font(.system(size: font).monospacedDigit())
                        }
                        bars(row, scale: scale)
                    }
                    .rowAccessibility(row, facts: facts)
                }
            } else {
                // One element per row for VoiceOver (a Grid row applies its modifiers to each cell),
                // so the label column is lined up by measuring the widest label instead.
                ForEach(rows) { row in
                    HStack(alignment: .center, spacing: 8) {
                        Text(row.label).font(.system(size: font, weight: .semibold))
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                            .frame(width: labelWidth > 0 ? labelWidth : nil, alignment: .leading)
                            // Its one-line width, measured unseen (a long name wraps past 130 pt).
                            .background(alignment: .leading) {
                                Text(row.label).font(.system(size: font, weight: .semibold)).lineLimit(1).fixedSize()
                                    .hidden().accessibilityHidden(true)
                                    .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { w in
                                        if min(w, 130) > labelWidth { labelWidth = min(w, 130) }
                                    }
                            }
                        bars(row, scale: scale)
                        VStack(alignment: .trailing, spacing: 2) {
                            ForEach(Array(row.values.enumerated()), id: \.offset) { _, v in
                                Text(v.map { facts.text($0) } ?? "–").font(.system(size: font).monospacedDigit())
                                    .lineLimit(1).fixedSize()
                            }
                        }
                        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { w in
                            if w > valueWidth { valueWidth = w }
                        }
                        .frame(width: valueWidth > 0 ? valueWidth : nil, alignment: .trailing)
                    }
                    .rowAccessibility(row, facts: facts)
                }
            }
        }
    }

    private func bars(_ row: ChartFacts.Row, scale: Double) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(Array(row.values.enumerated()), id: \.offset) { s, v in
                GeometryReader { g in
                    Capsule()
                        .fill((v ?? 0) < 0 ? toneColor("bad") : Self.tint(s))
                        .frame(width: max(3, g.size.width * abs(v ?? 0) / scale), height: 8)
                        .frame(maxHeight: .infinity)
                }
                .frame(height: font * 1.25)
            }
        }
        .frame(maxWidth: .infinity)
        .accessibilityHidden(true)
    }
}

private extension View {
    /// One VoiceOver element per row: its label, then its value(s).
    func rowAccessibility(_ row: ChartFacts.Row, facts: ChartFacts) -> some View {
        accessibilityElement(children: .ignore)
            .accessibilityLabel(row.label)
            .accessibilityValue(facts.text(row))
            .accessibilityIdentifier("chart-row")
    }
}

/// A plot's values as a table (the "Show values" tap).
struct ChartValueTable: View {
    let facts: ChartFacts
    @ScaledMetric(relativeTo: .footnote) private var font: CGFloat = 12
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(facts.rows) { row in
                HStack(alignment: .firstTextBaseline) {
                    Text(row.label).font(.system(size: font, weight: .medium))
                    Spacer(minLength: 8)
                    Text(facts.text(row)).font(.system(size: font).monospacedDigit()).multilineTextAlignment(.trailing)
                }
                .rowAccessibility(row, facts: facts)
            }
        }
        // No id on the table itself: it would replace each row's own id.
    }
}

/// The freshness line's icon sits on its first line, small.
private struct FreshnessLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            configuration.icon.imageScale(.small)
            configuration.title
        }
    }
}

/// Touch: a one-line chart is drawn in the brand accent, not the system's default blue.
private struct OneSeriesTint: ViewModifier {
    var on: Bool
    func body(content: Content) -> some View {
        if on { content.chartForegroundStyleScale(range: [CardStyle.active.emphasis]) } else { content }
    }
}

struct NewsCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    var body: some View {
        CardBox {
            CardHeader(icon: "newspaper.fill", title: card["title"].string ?? "News")
            ForEach(Array(card["items"].array.prefix(fit.rows(5)).enumerated()), id: \.offset) { i, n in
                if i > 0 { Divider().opacity(0.4) }
                let row = HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(n["headline"].string ?? "").font(.system(size: 12, weight: .medium)).lineLimit(3)
                            .multilineTextAlignment(.leading)
                        Text([n["source"].string, n["when"].string].compactMap { $0 }.joined(separator: " · "))
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                    if let img = n["image_url"].url { CardImage(url: img, size: 48, corner: 6, fill: true).frame(width: 64) }
                }
                if let url = n["url"].url { Link(destination: url) { row }.buttonStyle(.plain) } else { row }
            }
            MoreRows(total: min(card["items"].array.count, 5), shown: fit.rows(5))
        }
    }
}

struct HomeCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
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
                    // Compact: the first four tiles (two rows), then "N more".
                    ForEach(Array(card["devices"].array.prefix(fit == .compact ? 4 : 12).enumerated()), id: \.offset) { _, d in
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
            if card.kind != "thermostat" { MoreRows(total: min(card["devices"].array.count, 12), shown: fit == .compact ? 4 : 12) }
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
    @Environment(\.cardFit) private var fit
    var body: some View {
        CardBox {
            CardHeader(icon: "video.fill", title: card["name"].string ?? "Camera", trailing: card["when"].string)
            CardImage(url: card["image_url"].url, size: fit == .compact ? 96 : 160, corner: 10, fill: true).frame(maxWidth: .infinity)
        }
    }
}
