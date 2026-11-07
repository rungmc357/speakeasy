import SwiftUI
import SpeakeasyCore

// Artifact cards v1, the other four layouts (catalog P2 timeline, P4 rows to act on, P6 one number
// with its series, P7 one thing to open). Same bar as ArtifactCards.swift: typed fields in, layout and
// links here, no table grids, photos only once they load, one gold primary per card.

private let gold = JobLook.brass

// MARK: - P2 Timeline: what happens when

struct TimelineCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    var body: some View {
        let days = card["days"].array
        VStack(alignment: .leading, spacing: 16) {
            if let title = card["title"].string { Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary) }
            ForEach(Array(days.prefix(fit == .compact ? 1 : 10).enumerated()), id: \.offset) { _, day in
                TimelineDay(day: day, cap: fit == .compact ? 3 : 10)
            }
            if fit == .compact, days.count > 1 {
                Text(days.dropFirst().compactMap { $0["label"].string }.joined(separator: " · "))
                    .font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

struct TimelineDay: View {
    let day: JSONValue
    let cap: Int
    var body: some View {
        let items = day["items"].array
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(day["label"].string ?? "").font(.system(size: 20, weight: .bold))
                Spacer(minLength: 4)
                if let sleep = day["sleep"].string {
                    // Where they sleep that night: a quiet chip, never a row of its own.
                    Label(sleep, systemImage: "bed.double.fill").font(.system(size: 12, weight: .semibold)).lineLimit(1)
                        .padding(.horizontal, 9).padding(.vertical, 5)
                        .background(Capsule().fill(gold.opacity(0.16))).foregroundStyle(.primary)
                }
            }
            VStack(spacing: 0) {
                ForEach(Array(items.prefix(cap).enumerated()), id: \.offset) { i, item in
                    TimelineRow(item: item, last: i == min(items.count, cap) - 1)
                }
            }
            .padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color.primary.opacity(0.06)))
        }
    }
}

struct TimelineRow: View {
    let item: JSONValue
    let last: Bool
    @Environment(\.openURL) private var openURL
    @Environment(\.artifactOpener) private var opener
    var body: some View {
        let state = item["state"].string
        let place = ArtifactLinks.hasPlace(placeItem)
        Button(action: open) {
            HStack(alignment: .top, spacing: 12) {
                marker(state).frame(width: 18, height: 18).padding(.top, 2)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item["what"].string ?? "").font(.system(size: 15.5, weight: .semibold))
                        .foregroundStyle(state == "done" ? .secondary : .primary).lineLimit(2)
                    let sub = [item["where"].string, item["note"].string].compactMap { $0 }
                    if !sub.isEmpty { Text(sub.joined(separator: " · ")).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(2) }
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 4) {
                    Text(item["when"].string ?? "").font(.system(size: 14, weight: .semibold)).foregroundStyle(gold)
                        .monospacedDigit().lineLimit(1)
                    if place || item["url"].url != nil {
                        Image(systemName: place ? "mappin" : "arrow.up.right").font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .allowsHitTesting(place || item["url"].url != nil)   // a row with nowhere to go stays full strength
        .overlay(alignment: .bottom) { if !last { Divider().padding(.leading, 42) } }
        .accessibilityElement(children: .combine)
    }

    /// A row's place: its own name ("where", else "what") with the address/coordinates it gave.
    private var placeItem: JSONValue {
        var o: [String: JSONValue] = ["name": .string(item["where"].string ?? item["what"].string ?? "")]
        for k in ["address", "lat", "lon"] where item[k] != .null { o[k] = item[k] }
        return .object(o)
    }

    private func open() {
        if ArtifactLinks.hasPlace(placeItem) { opener.maps(placeItem) } else if let url = item["url"].url { openURL(url) }
    }

    @ViewBuilder private func marker(_ state: String?) -> some View {
        switch state {
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case "now": Circle().fill(gold).frame(width: 12, height: 12).overlay(Circle().stroke(gold.opacity(0.35), lineWidth: 4))
        default: Circle().strokeBorder(Color.primary.opacity(0.35), lineWidth: 1.5).frame(width: 12, height: 12)
        }
    }
}

// MARK: - P4 Rows to act on

struct RowsCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    var body: some View {
        let items = card["items"].array
        let entity = card["entity"].string ?? "item"
        let cap = fit == .compact ? 3 : 12
        VStack(alignment: .leading, spacing: 8) {
            if let title = card["title"].string { Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary) }
            ForEach(Array(items.prefix(cap).enumerated()), id: \.offset) { _, item in
                ActionRow(item: item, entity: entity)
            }
            if items.count > cap {
                Text("\(items.count - cap) more in the answer").font(.system(size: 12.5)).foregroundStyle(.secondary)
            }
        }
    }
}

struct ActionRow: View {
    let item: JSONValue
    let entity: String
    @Environment(\.openURL) private var openURL
    static func tone(_ t: String?) -> Color {
        switch t { case "good": return .green; case "warn": return gold; case "bad": return .red; default: return .secondary }
    }
    var body: some View {
        let tone = item["tone"].string
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: ArtifactLinks.symbol(entity)).font(.system(size: 14, weight: .semibold))
                .foregroundStyle(tone == nil ? gold : Self.tone(tone))
                .frame(width: 34, height: 34)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill((tone == nil ? gold : Self.tone(tone)).opacity(0.14)))
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if let ref = item["ref"].string { Text(ref).font(.system(size: 13, weight: .medium).monospacedDigit()).foregroundStyle(.secondary) }
                    Text(item["name"].string ?? "").font(.system(size: 15, weight: .semibold)).lineLimit(2)
                }
                HStack(spacing: 6) {
                    if let state = item["state"].string {
                        Text(state).font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
                            .padding(.horizontal, 7).padding(.vertical, 2.5)
                            .foregroundStyle(tone == nil ? Color.primary : Self.tone(tone))
                            .background(Capsule().fill((tone == nil ? Color.primary : Self.tone(tone)).opacity(0.12)))
                    }
                    if let note = item["note"].string { Text(note).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(1) }
                }
            }
            Spacer(minLength: 6)
            VStack(alignment: .trailing, spacing: 3) {
                if let amount = item["amount"].string { Text(amount).font(.system(size: 15, weight: .bold)).monospacedDigit() }
                if let when = item["when"].string { Text(when).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1) }
            }
            if let url = item["url"].url {
                PillButton(title: "Open", icon: "arrow.up.right", primary: false, iconOnly: true) { openURL(url) }.fixedSize()
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.primary.opacity(0.06)))
        .accessibilityElement(children: .contain)
    }
}

// MARK: - P6 One number with its series

struct MetricCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    var body: some View {
        let points = card["points"].array
        VStack(alignment: .leading, spacing: 8) {
            Text(card["label"].string ?? "").font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(card["value"].string ?? "").font(.system(size: fit == .compact ? 30 : 40, weight: .bold)).monospacedDigit()
                    .lineLimit(1).minimumScaleFactor(0.6)
                if let unit = card["unit"].string { Text(unit).font(.system(size: 15)).foregroundStyle(.secondary).lineLimit(1) }
                Spacer(minLength: 4)
                if let delta = card["delta"].string {
                    let good = card["good"].bool
                    let color: Color = good == nil ? .secondary : (good! ? .green : .red)
                    Text(delta).font(.system(size: 14, weight: .bold)).monospacedDigit().foregroundStyle(color)
                        .padding(.horizontal, 8).padding(.vertical, 3).background(Capsule().fill(color.opacity(0.13)))
                }
            }
            if points.count >= 2 {
                MetricLine(points: points, threshold: card["threshold"].double, thresholdLabel: card["threshold_label"].string,
                           showMarks: fit != .compact)
                    .frame(height: fit == .compact ? 44 : 120)
                HStack {
                    Text(points.first?["label"].string ?? "")
                    Spacer()
                    Text(points.last?["label"].string ?? "")
                }
                .font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            let foot = [card["source"].string, card["as_of"].string].compactMap { $0 }
            if !foot.isEmpty && fit != .compact { Text(foot.joined(separator: " · ")).font(.system(size: 11.5)).foregroundStyle(.secondary) }
        }
    }
}

/// The series as one line; a dashed threshold with its label; marked points as dots with their word.
/// Unmarked points print no numbers (the server only checks marked ones against the answer).
struct MetricLine: View {
    let points: [JSONValue]
    let threshold: Double?
    let thresholdLabel: String?
    let showMarks: Bool
    var body: some View {
        GeometryReader { geo in
            let values = points.compactMap { $0["value"].double }
            let all = values + (threshold.map { [$0] } ?? [])
            let lo = all.min() ?? 0, hi = all.max() ?? 1, span = max(hi - lo, 0.0001)
            let w = geo.size.width, h = geo.size.height
            let at = { (i: Int, v: Double) in
                CGPoint(x: w * CGFloat(i) / CGFloat(max(values.count - 1, 1)), y: (h - 14) * (1 - CGFloat((v - lo) / span)) + 7)
            }
            ZStack(alignment: .topLeading) {
                if let t = threshold {
                    let y = at(0, t).y
                    Path { p in p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: w, y: y)) }
                        .stroke(Color.secondary.opacity(0.6), style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    if let label = thresholdLabel, showMarks {
                        // On the left, clear of the dashes (below them when the line is at the top).
                        Text(label).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(.secondary).fixedSize()
                            .position(x: 24, y: y > 18 ? y - 10 : y + 10)
                    }
                }
                Path { p in
                    for (i, v) in values.enumerated() { i == 0 ? p.move(to: at(i, v)) : p.addLine(to: at(i, v)) }
                }
                .stroke(gold, style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                if showMarks {
                    ForEach(Array(points.enumerated()), id: \.offset) { i, pt in
                        if let mark = pt["mark"].string, let v = pt["value"].double, i < values.count {
                            let c = at(i, v)
                            Circle().fill(gold).frame(width: 8, height: 8).position(c)
                            Text(mark).font(.system(size: 10.5, weight: .semibold)).lineLimit(1).fixedSize()
                                .position(x: min(max(c.x, 36), w - 36), y: c.y > 20 ? c.y - 12 : c.y + 12)
                        }
                    }
                }
            }
        }
        .accessibilityHidden(true)
    }
}

// MARK: - P7 One thing to open

struct PreviewCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    @Environment(\.openURL) private var openURL
    @Environment(\.artifactOpener) private var opener
    var body: some View {
        let entity = card["entity"].string ?? "file"
        let item = ArtifactLinks.previewItem(card.data)
        let music = ArtifactLinks.isMusic(item, entity: entity)
        let square = entity == "media"
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 14) {
                ArtifactPhoto(url: card["photo_url"].url, symbol: ArtifactLinks.symbol(entity, item: item))
                    .frame(width: square ? (fit == .compact ? 72 : 112) : (fit == .compact ? 56 : 84),
                           height: fit == .compact ? 72 : 112)
                    .clipShape(RoundedRectangle(cornerRadius: square ? 12 : 8, style: .continuous))
                VStack(alignment: .leading, spacing: 4) {
                    Text(card["title"].string ?? "").font(.system(size: 19, weight: .bold)).lineLimit(3)
                    if let creator = card["creator"].string { Text(creator).font(.system(size: 15)).lineLimit(1) }
                    let sub = [card["sub"].string, card["year"].string].compactMap { $0 }
                    if !sub.isEmpty { Text(sub.joined(separator: " · ")).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(1) }
                    if music {
                        PillButton(title: "Play", icon: "play.fill", primary: true) { opener.play(item) }.fixedSize().padding(.top, 6)
                    } else if let url = card["url"].url {
                        PillButton(title: "Open", icon: "arrow.up.right", primary: true) { openURL(url) }.fixedSize().padding(.top, 6)
                    }
                }
                Spacer(minLength: 0)
            }
            if let summary = card["summary"].string, fit != .compact {
                Text(summary).font(.system(size: 14.5)).fixedSize(horizontal: false, vertical: true)
            }
            let flags = card["flags"].strings
            ForEach(Array(flags.prefix(fit == .compact ? 1 : 4).enumerated()), id: \.offset) { _, flag in
                Label { Text(flag).font(.system(size: 13.5)) } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(gold)
                }
                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(gold.opacity(0.10)))
            }
            let list = card["list"].strings
            if !list.isEmpty && fit != .compact {
                VStack(alignment: .leading, spacing: 0) {
                    if let label = card["list_label"].string {
                        Text(label).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(.secondary).padding(.bottom, 4)
                    }
                    ForEach(Array(list.enumerated()), id: \.offset) { i, line in
                        HStack(spacing: 10) {
                            Text("\(i + 1)").font(.system(size: 13).monospacedDigit()).foregroundStyle(.secondary).frame(width: 18, alignment: .trailing)
                            Text(line).font(.system(size: 14.5)).lineLimit(1)
                            Spacer(minLength: 0)
                        }
                        .padding(.vertical, 6)
                        .overlay(alignment: .bottom) { if i < list.count - 1 { Divider().padding(.leading, 28) } }
                    }
                }
            }
        }
    }
}

// MARK: - Explore by day

/// When an explore card's things name their day, the same things regrouped day by day (each keeps
/// its own kind for its icon and buttons). nil when fewer than two days are named.
enum ExploreByDay {
    static func sections(_ card: ViewCard) -> [JSONValue]? {
        var order: [String] = []
        var groups: [String: [JSONValue]] = [:]
        for sec in card["sections"].array {
            let entity = sec["entity"]
            for item in sec["items"].array {
                guard let day = item["day"].string, case .object(var o) = item else { continue }
                o["_entity"] = entity
                if groups[day] == nil { order.append(day) }
                groups[day, default: []].append(.object(o))
            }
        }
        guard order.count >= 2 else { return nil }
        return order.map { .object(["label": .string($0), "entity": .string("day"), "items": .array(groups[$0] ?? [])]) }
    }
}
