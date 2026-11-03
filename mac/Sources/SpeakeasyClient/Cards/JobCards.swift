import SwiftUI
import SpeakeasyCore

// Visuals a task draws to explain a decision or a result: lanes (what ran when, where it broke),
// before → after, a flow (where something goes) and a devices grid. Only the data sent is drawn:
// no bar, point or label is invented here.

/// good | warn | bad | info | neutral → a colour that reads on the dark and the light panel.
public func toneColor(_ tone: String?) -> Color {
    switch tone {
    case "good": return Color(red: 0.31, green: 0.68, blue: 0.45)
    case "warn": return Color(red: 0.94, green: 0.65, blue: 0.19)
    case "bad": return Color(red: 0.90, green: 0.38, blue: 0.33)
    case "info": return Color(red: 0.37, green: 0.61, blue: 0.94)
    default: return Color.gray
    }
}

struct LanesCard: View {
    let card: ViewCard
    var body: some View {
        let lanes = Array(card["lanes"].array.prefix(6))
        let ticks = Array(card["ticks"].strings.prefix(6))
        CardBox {
            if let title = card["title"].string { CardHeader(icon: "chart.bar.doc.horizontal", title: title) }
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(lanes.enumerated()), id: \.offset) { _, lane in
                    HStack(spacing: 8) {
                        Text(lane["label"].string ?? "").font(.system(size: 11)).foregroundStyle(.secondary)
                            .lineLimit(1).frame(width: 70, alignment: .leading)
                        LaneTrack(segments: Array(lane["segments"].array.prefix(8)))
                    }
                }
                if !ticks.isEmpty || card["start_label"].string != nil || card["end_label"].string != nil {
                    HStack(spacing: 8) {
                        Color.clear.frame(width: 70, height: 1)
                        HStack(spacing: 0) {
                            let labels = ticks.isEmpty ? [card["start_label"].string ?? "", card["end_label"].string ?? ""] : ticks
                            ForEach(Array(labels.enumerated()), id: \.offset) { i, t in
                                if i > 0 { Spacer(minLength: 2) }
                                Text(t).font(.system(size: 11.5).monospacedDigit()).foregroundStyle(.secondary).lineLimit(1)
                            }
                        }
                    }
                }
            }
            if let note = card["note"].string {
                Text(note).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

private struct LaneTrack: View {
    let segments: [JSONValue]
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 4).fill(Color.primary.opacity(0.07))
                ForEach(Array(segments.enumerated()), id: \.offset) { _, seg in
                    if let from = seg["from"].double, let to = seg["to"].double {
                        let a = min(max(from, 0), 1), b = min(max(to, 0), 1)
                        if b > a {
                            RoundedRectangle(cornerRadius: 3).fill(toneColor(seg["tone"].string ?? "info"))
                                .frame(width: max(3, geo.size.width * (b - a)), height: geo.size.height)
                                .offset(x: geo.size.width * a)
                                .help(seg["label"].string ?? "")
                        }
                    }
                }
            }
        }
        .frame(height: 16)
    }
}

struct BeforeAfterCard: View {
    let card: ViewCard
    @Environment(\.questionActions) private var actions
    var body: some View {
        CardBox {
            if let title = card["title"].string { CardHeader(icon: "arrow.left.arrow.right", title: title) }
            HStack(alignment: .top, spacing: 8) {
                side(card["before"], fallback: "Before")
                Image(systemName: "arrow.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(.tertiary)
                    .padding(.top, 10)
                side(card["after"], fallback: "After")
            }
        }
    }

    @ViewBuilder private func side(_ v: JSONValue, fallback: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            if v["image"].url != nil || v["image"].double != nil {
                ZStack(alignment: .bottomLeading) {
                    if let url = v["image"].url {
                        CardImage(url: url, size: 96, corner: 8, fill: true)
                    } else if let n = v["image"].double, let load = actions.image {
                        NumberedImage(number: Int(n), load: load).frame(height: 96)
                    }
                    Text(v["label"].string ?? fallback).font(.system(size: 11.5, weight: .bold))
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(Capsule().fill(Color.black.opacity(0.6))).foregroundStyle(.white).padding(4)
                }
                .frame(maxWidth: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                Text((v["label"].string ?? fallback).uppercased()).font(.system(size: 10.5, weight: .semibold)).tracking(0.5)
                    .foregroundStyle(.secondary)
            }
            if let text = v["text"].string {
                Text(text).font(.system(size: 11.5)).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.05)))
    }
}

/// A task picture by its card number, through the host's authenticated loader.
struct NumberedImage: View {
    let number: Int
    let load: (Int) async -> Data?
    @State private var image: Image?
    var body: some View {
        ZStack {
            Rectangle().fill(Color.primary.opacity(0.06))
            if let image { image.resizable().scaledToFill() }
        }
        .frame(maxWidth: .infinity)
        .clipped()
        .task(id: number) {
            guard image == nil, let data = await load(number) else { return }
            #if os(macOS)
            if let ns = NSImage(data: data) { image = Image(nsImage: ns) }
            #else
            if let ui = UIImage(data: data) { image = Image(uiImage: ui) }
            #endif
        }
    }
}

struct FlowCard: View {
    let card: ViewCard
    var body: some View {
        let nodes = Array(card["nodes"].array.prefix(5))
        CardBox {
            if let title = card["title"].string { CardHeader(icon: "arrow.triangle.branch", title: title) }
            // Up to four steps sit in a row; five stack (each would be too narrow to read).
            if nodes.count <= 4 {
                HStack(spacing: 4) {
                    ForEach(Array(nodes.enumerated()), id: \.offset) { i, node in
                        if i > 0 { Image(systemName: "arrow.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(.tertiary) }
                        box(node).frame(minWidth: 58, maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(spacing: 3) {
                    ForEach(Array(nodes.enumerated()), id: \.offset) { i, node in
                        if i > 0 { Image(systemName: "arrow.down").font(.system(size: 9, weight: .semibold)).foregroundStyle(.tertiary) }
                        box(node).frame(maxWidth: .infinity)
                    }
                }
            }
        }
    }

    private func box(_ node: JSONValue) -> some View {
        let tone = node["tone"].string
        return VStack(spacing: 2) {
            Text(node["label"].string ?? "").font(.system(size: 11.5, weight: .semibold)).multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if let d = node["detail"].string {
                Text(d).font(.system(size: 11.5)).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 6).padding(.vertical, 7)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(tone == nil || tone == "neutral" ? Color.primary.opacity(0.07) : toneColor(tone).opacity(0.18)))
    }
}

struct DevicesCard: View {
    let card: ViewCard
    var body: some View {
        let items = Array(card["items"].array.prefix(8))
        CardBox {
            if let title = card["title"].string { CardHeader(icon: "laptopcomputer.and.iphone", title: title) }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 88), spacing: 6)], spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in tile(item) }
            }
        }
    }

    private func tile(_ item: JSONValue) -> some View {
        let tone = item["tone"].string
        let history = Array(item["history"].doubles.prefix(6))
        return VStack(spacing: 3) {
            Text(item["name"].string ?? "").font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
            if let value = item["value"].string {
                Text(value).font(.system(size: 12, weight: .semibold, design: .monospaced)).lineLimit(1).minimumScaleFactor(0.7)
                    .foregroundStyle(tone == nil || tone == "neutral" ? Color.secondary : toneColor(tone))
            }
            if !history.isEmpty {
                let hi = max(history.max() ?? 1, 0.0001)
                HStack(alignment: .bottom, spacing: 2) {
                    ForEach(Array(history.enumerated()), id: \.offset) { _, h in
                        RoundedRectangle(cornerRadius: 1)
                            .fill(toneColor(tone == nil || tone == "neutral" ? "info" : tone).opacity(0.85))
                            .frame(width: 5, height: max(2, 12 * CGFloat(max(h, 0) / hi)))
                    }
                }
                .frame(height: 12)
            }
            if let detail = item["detail"].string {
                Text(detail).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.center)
            }
        }
        .padding(.horizontal, 6).padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))
    }
}

// MARK: - Answer glance

/// A result card drawn small for a board tile: the weather's temperature, a chart's bars, a
/// comparison's winner, the first stat. Only values from the card; nothing when there's nothing to draw.
public struct AnswerGlance: View {
    let card: ViewCard
    public init(_ card: ViewCard) { self.card = card }

    public var body: some View {
        Group {
            switch card.kind {
            case "weather":
                HStack(spacing: 5) {
                    Image(systemName: weatherSymbol(card["icon"].string)).symbolRenderingMode(.multicolor).font(.system(size: 16))
                    VStack(alignment: .leading, spacing: 0) {
                        Text(ViewCard.degrees(card["temp"].double)).font(.system(size: 17, weight: .medium).monospacedDigit())
                        if let c = card["condition"].string { Text(sentenceCase(c)).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1) }
                    }
                }
            case "chart":
                MiniBars(values: Array((card["series"].array.first?["values"].doubles ?? []).suffix(14)),
                         line: card["style"].string == "line")
                    .frame(width: 76, height: 28)
            case "quote":
                VStack(alignment: .trailing, spacing: 0) {
                    Text(card["price"].double.map { ViewCard.money($0, currency: card["currency"].string) } ?? "")
                        .font(.system(size: 14, weight: .semibold).monospacedDigit()).lineLimit(1)
                    if let c = card["change_pct"].double {
                        Text(ViewCard.signed(c)).font(.system(size: 11, weight: .medium).monospacedDigit())
                            .foregroundStyle(c >= 0 ? toneColor("good") : toneColor("bad"))
                    }
                }
            case "comparison":
                HStack(spacing: 4) {
                    Image(systemName: "checkmark.seal.fill").font(.system(size: 11)).foregroundStyle(toneColor("good"))
                    Text(card["winner"].string ?? "").font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
                        .truncationMode(.tail).frame(maxWidth: 96, alignment: .leading)
                }
            case "stats":
                let item = card["items"].array.first ?? .null
                value(item["value"].string ?? "", item["label"].string, tone: item["good"].bool.map { $0 ? "good" : "warn" })
            case "fact":
                value([card["value"].string, card["unit"].string].compactMap { $0 }.joined(separator: " "), card["title"].string)
            case "conversion":
                value([card["to_value"].string, card["to_unit"].string].compactMap { $0 }.joined(separator: " "), nil)
            case "math":
                value(card["result"].string ?? "", nil)
            case "progress":
                let v = card["value"].double ?? 0, total = max(card["total"].double ?? 1, 1)
                VStack(alignment: .trailing, spacing: 3) {
                    Text("\(JSONValue.number(v).string ?? "") of \(JSONValue.number(total).string ?? "")").font(.system(size: 12, weight: .semibold).monospacedDigit())
                    Capsule().fill(Color.primary.opacity(0.12)).frame(width: 64, height: 4)
                        .overlay(alignment: .leading) { Capsule().fill(toneColor("info")).frame(width: 64 * min(max(v / total, 0), 1), height: 4) }
                }
            default: EmptyView()
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(card.summary)
    }

    private func value(_ v: String, _ label: String?, tone: String? = nil) -> some View {
        VStack(alignment: .trailing, spacing: 0) {
            Text(v).font(.system(size: 16, weight: .semibold).monospacedDigit()).lineLimit(1).minimumScaleFactor(0.6)
                .frame(maxWidth: 96, alignment: .trailing)
                .foregroundStyle(tone.map { toneColor($0) } ?? Color.primary)
            if let label { Text(label).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1).frame(maxWidth: 96, alignment: .trailing) }
        }
    }
}

/// Tiny bars (or a sparkline) from a chart's first series. Heights are the data, scaled to the tallest.
struct MiniBars: View {
    var values: [Double]
    var line = false
    var body: some View {
        let top = max(values.max() ?? 1, 0.0001)
        GeometryReader { geo in
            if line {
                Path { p in
                    for (i, v) in values.enumerated() {
                        let pt = CGPoint(x: geo.size.width * CGFloat(i) / CGFloat(max(values.count - 1, 1)),
                                         y: geo.size.height * (1 - CGFloat(max(v, 0) / top)))
                        if i == 0 { p.move(to: pt) } else { p.addLine(to: pt) }
                    }
                }
                .stroke(toneColor("info"), style: StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
            } else {
                HStack(alignment: .bottom, spacing: 2) {
                    ForEach(Array(values.enumerated()), id: \.offset) { i, v in
                        RoundedRectangle(cornerRadius: 1.5)
                            .fill(toneColor("info").opacity(i == values.count - 1 ? 1 : 0.6))
                            .frame(height: max(2, geo.size.height * CGFloat(max(v, 0) / top)))
                    }
                }
                .frame(width: geo.size.width, height: geo.size.height, alignment: .bottom)
            }
        }
        .accessibilityHidden(true)
    }
}
