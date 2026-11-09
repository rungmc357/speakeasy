import SwiftUI
import SpeakeasyCore

// Artifact cards v1, the other four layouts (catalog P2 timeline, P4 rows to act on, P6 one number
// with its series, P7 one thing to open). Same bar as ArtifactCards.swift: typed fields in, layout and
// links here, no table grids, photos only once they load, one gold primary per card.

/// Highlights (brass on classic, neutral ink on clean) and the one primary action, from the card look.
private var gold: Color { CardStyle.active.emphasis }
private var look: CardStyle { CardStyle.active }

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
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(look.well))
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
                HStack(alignment: .firstTextBaseline, spacing: 5) {   // the link mark beside the time, not under it
                    Text(EventTimes.clockTime(item["when"].string) ?? "").font(.system(size: 14, weight: .semibold)).foregroundStyle(gold)
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
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(look.good)
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
        // Six rows read at a glance (a 13-row wall on Home did not); the rest are in the answer.
        let cap = expanded ? items.count : (fit == .compact ? 3 : 5)
        // The list's own count ("13 left") when fewer rows were sent: "6 more" under 6 of 13 rows said
        // 12 and disagreed with the reply.
        let total = max(card["total"].double.map(Int.init) ?? items.count, items.count)
        VStack(alignment: .leading, spacing: 8) {
            // The view the rows came from leads ("Cycle 14 · iOS · ends Friday"), else the app and the title.
            let head = card["scope"].string.map { [$0] }
                ?? [card["source"].string, card["title"].string].compactMap { $0 }.removingDuplicates()
            AppHeader(parts: head, source: card["source"].string)
            ForEach(Array(items.prefix(cap).enumerated()), id: \.offset) { _, item in
                ActionRow(item: item, entity: entity)
            }
            if total > min(cap, items.count) {
                let rest = total - min(cap, items.count)
                if items.count > cap {
                    // A control, not a caption: the rest open in place.
                    Button { withAnimation(.snappy) { expanded = true } } label: {
                        HStack(spacing: 6) {
                            Text("Show \(rest) more").font(.system(size: 13.5, weight: .semibold))
                            Image(systemName: "chevron.down").font(.system(size: 11, weight: .semibold))
                        }
                        .foregroundStyle(gold).frame(maxWidth: .infinity, minHeight: 36)
                        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(look.well))
                    }
                    .buttonStyle(.plain).accessibilityIdentifier("rows-show-more")
                } else {
                    // Rows the answer didn't send: say how many, quietly; there is nothing to open.
                    Text("\(rest) more not shown").font(.system(size: 12.5)).foregroundStyle(.secondary)
                }
            }
        }
    }
    @State private var expanded = false
}

/// One status word, one colour, on every card: "Missed" is red on every row, "Backlog" is never the
/// orange of "Waiting on Geo". Words it doesn't know keep the tone the agent gave.
enum StateWords {
    static func tone(_ state: String?, given: String?) -> String? {
        guard let s = state?.lowercased().trimmingCharacters(in: .whitespaces), !s.isEmpty else { return given }
        func has(_ re: String) -> Bool { s.range(of: re, options: .regularExpression) != nil }
        if has(#"^(done|merged|shipped|ready( to ship)?|paid|approved|passed|live|fixed|delivered|completed?|confirmed|sent)$"#) { return "good" }
        if has(#"^(blocked|failed|failing|overdue|conflicts?|missed|down|crash(ed|ing)?|declined|waiting on .+|needs you|your call|needs (a )?decision|decide)$"#) { return "bad" }
        // Fresh activity is news, not trouble: blue, so it stands apart from both amber and grey.
        if has(#"^(new activity|new comments?|new reply|needs review|review requested|unread)$"#) { return "info" }
        if has(#"^(in progress|rebasing|running|stuck|stale|due .+|today|tomorrow|building|deploying|started)$"#) { return "warn" }
        if has(#"^(backlog|todo|to do|canceled|cancelled|open|draft|in review|review|skipped|idle|queued|triage|quiet|this week|later)$"#) { return "neutral" }
        return given
    }
    /// The tracker glyph for a state: a check when it's done, a half circle only while it moves.
    static func symbol(_ state: String?, tone: String?) -> String {
        let words = (state ?? "").lowercased()
        // Waiting on a person ("Needs Theo", "Waiting on Geo") is not the same as moving: a raised
        // hand, so it never wears In progress's half circle beside it.
        if tone != "good", words.hasPrefix("needs ") || words.hasPrefix("waiting on") || words.contains("blocked") {
            return "hand.raised.fill"
        }
        switch tone {
        case "good": return "checkmark.circle.fill"
        case "bad": return "exclamationmark.circle.fill"
        case "warn": return "circle.lefthalf.filled"
        default: return (state ?? "").lowercased().contains("review") ? "circle.righthalf.filled" : "circle.dashed"
        }
    }
}

struct ActionRow: View {
    let item: JSONValue
    let entity: String
    @Environment(\.openURL) private var openURL
    static func tone(_ t: String?) -> Color {
        switch t { case "good": return look.good; case "warn": return look.warn; case "bad": return look.bad; case "accent", "info": return look.info; default: return .secondary }
    }
    var body: some View {
        // A known status word sets its colour (Backlog grey, Waiting on Geo red); otherwise the agent's tone.
        let tone = StateWords.tone(item["state"].string, given: item["tone"].string)
        // Top-aligned: the glyph tile and the corner time sit on the name's first line, not floating
        // mid-row beside a three-line name and note.
        HStack(alignment: .top, spacing: 12) {
            // A row with a status wears the tracker's glyph for it (done a check, moving a half
            // circle, waiting dashed), so "In progress" and "Needs Theo" never share one mark.
            let glyph = item["state"].string != nil ? StateWords.symbol(item["state"].string, tone: tone) : ArtifactLinks.symbol(entity)
            Image(systemName: glyph).font(.system(size: 14, weight: .semibold))
                .foregroundStyle(tone == nil ? gold : Self.tone(tone))
                .frame(width: CleanGrid.rowGlyph, height: CleanGrid.rowGlyph)
                .background(RoundedRectangle(cornerRadius: CleanGrid.tileRadius, style: .continuous).fill((tone == nil ? gold : Self.tone(tone)).opacity(0.14)))
            VStack(alignment: .leading, spacing: 3) {
                // A short ref ("#418", "SPK-12") leads the name; a long one ("FIELDNOTE-IOS-3KD") squeezed
                // the name into a letter-wide column, so it moves to the line under it.
                let ref = item["ref"].string
                // "#418" and a tracker key up to "SPK-207" lead the name, as the designs do; "#66526" and
                // "FIELDNOTE-IOS-3KD" took a third of the row and cut the title, so they join the note line.
                let n = ref?.count ?? 0
                let inline = n <= 5 || (n <= 7 && ref?.range(of: #"^[A-Z]{1,4}-\d+$"#, options: .regularExpression) != nil)
                // A time fits the corner ("2:48 AM"); a phrase ("Last seen 2:48 AM ET") joins the note line.
                let when = EventTimes.stamp(item["when"].string)
                let corner = (when?.count ?? 0) <= 9
                // One text: the ref leads and a second line wraps back to the row's edge, never under
                // the name alone in a hanging column ("FN-148  Onboarding: three / design directions").
                let name = Text(item["name"].string ?? "").font(.system(size: 15, weight: .semibold))
                Group {
                    if let ref, inline {
                        Text("\(Text(ref).font(.system(size: 13, weight: .medium).monospacedDigit()).foregroundColor(.secondary))\u{2002}\(name)")
                    } else { name }
                }
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                // Line two is the chip with who and how old (short, always fits); the note, when there is
                // one, is its own quiet line under it, so no row strands a chip alone or cuts the note.
                let meta = [inline ? nil : ref, item["who"].string, corner ? nil : when].compactMap { $0 }.joined(separator: " · ")
                let note = item["note"].string
                let chip = item["state"].string.map { state in
                    Text(state).font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
                        .padding(.horizontal, 7).padding(.vertical, 2.5)
                        .foregroundStyle(tone == nil ? Color.primary : Self.tone(tone))
                        .background(Capsule().fill((tone == nil ? Color.primary : Self.tone(tone)).opacity(0.12)))
                        .fixedSize()
                }
                // Chip and note share a line when the note fits whole; else the note takes its own line
                // under the chip (beside it, it was cut to nothing: "Unread · Dana · Confirm pr…").
                if chip != nil || !meta.isEmpty {
                    HStack(spacing: 6) {
                        chip
                        if !meta.isEmpty { Text(meta).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(1) }
                    }
                }
                if let note {
                    Text(note).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 6)
            let when = EventTimes.stamp(item["when"].string)
            VStack(alignment: .trailing, spacing: 3) {
                if let amount = item["amount"].string { Text(amount).font(.system(size: 15, weight: .bold)).monospacedDigit() }
                if let when, when.count <= 9 { Text(when).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1) }
            }
            .fixedSize()
            // On the name's first line (the 32 pt tile's centre is about where a 15 pt line sits).
            .padding(.top, 1)
            if let url = item["url"].url {
                PillButton(title: "Open", icon: "arrow.up.right", primary: false, iconOnly: true) { openURL(url) }.fixedSize()
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(look.well))
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
            // The app heads the card ("Sentry · fieldnote-ios"), as the designs do; what the number counts
            // sits under it, whole (in the header it was cut: "Shared notes crashes p…").
            let label = card["label"].string ?? ""
            let where_ = [card["source"].string, card["scope"].string].compactMap { $0 }.filter { !$0.isEmpty }.removingDuplicates()
            AppHeader(parts: where_.isEmpty ? [label] : where_, source: card["source"].string)
            VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(card["value"].string ?? "").font(.system(size: fit == .compact ? 30 : 40, weight: .bold)).monospacedDigit()
                    .lineLimit(1).minimumScaleFactor(0.6)
                if let unit = MetricCard.unit(card["unit"].string, label: label) {
                    Text(unit).font(.system(size: 15)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
                if let delta = card["delta"].string {
                    let good = card["good"].bool
                    let color: Color = good == nil ? .secondary : (good! ? look.good : look.bad)
                    Text(delta).font(.system(size: 14, weight: .bold)).monospacedDigit().foregroundStyle(color)
                        .padding(.horizontal, 8).padding(.vertical, 3).background(Capsule().fill(color.opacity(0.13)))
                }
            }
            // "crashes per hour · 41 people affected": what it counts, and the one fact beside it.
            let sub = [where_.isEmpty || label.isEmpty ? nil : label, card["note"].string]
                .compactMap { $0 }.joined(separator: " · ")
            if !sub.isEmpty {
                Text(sub).font(.system(size: 13.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
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
            let foot = [EventTimes.stamp(card["as_of"].string).map { "As of \($0)" }].compactMap { $0 }
            if !foot.isEmpty && fit != .compact { Text(foot.joined(separator: " · ")).font(.system(size: 11.5)).foregroundStyle(.secondary) }
        }
    }
}

extension MetricCard {
    /// The unit beside the number, unless the label already says it: "Crashes per hour" with
    /// "crashes/hr" read the same thing twice.
    static func unit(_ unit: String?, label: String) -> String? {
        guard let unit, !unit.isEmpty else { return nil }
        let low = label.lowercased()
        let per = ["hr": "hour", "hour": "hour", "h": "hour", "min": "minute", "day": "day", "d": "day", "wk": "week", "week": "week", "s": "second", "sec": "second"]
        if let slash = unit.lowercased().split(separator: "/").last, unit.contains("/"),
           let word = per[String(slash).trimmingCharacters(in: .whitespaces)], low.contains("per \(word)") || low.contains("/\(slash)") {
            return nil
        }
        if low.contains(unit.lowercased()) { return nil }
        return unit
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
            // Room above the line for a mark's word ("Peak"): a peak at the very top put its label
            // under the dot, on the falling line.
            let top: CGFloat = showMarks && points.contains { $0["mark"].string != nil } ? 20 : 7
            let at = { (i: Int, v: Double) in
                CGPoint(x: w * CGFloat(i) / CGFloat(max(values.count - 1, 1)), y: (h - 7 - top) * (1 - CGFloat((v - lo) / span)) + top)
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
                                .position(x: min(max(c.x, 36), w - 36), y: c.y - 13)
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
        let doc = DocPreview.isDocument(entity)
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: doc ? CleanGrid.glyphGap : 14) {
                if !doc, card["photo_url"].url != nil || music {
                    ArtifactPhoto(url: card["photo_url"].url, symbol: ArtifactLinks.symbol(entity, item: item))
                        .frame(width: square ? (fit == .compact ? 72 : 112) : (fit == .compact ? 56 : 84),
                               height: fit == .compact ? 72 : 112)
                        .clipShape(RoundedRectangle(cornerRadius: square ? 12 : 8, style: .continuous))
                } else {
                    // No cover (a document never has one, SPK-231 D3): a small mark beside the title, the
                    // app's own glyph when it's named, not a big grey tile standing in for a picture.
                    Image(systemName: (doc ? DocPreview.appSymbol(card["app"].string) : nil) ?? ArtifactLinks.symbol(entity, item: item))
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.secondary).frame(width: CleanGrid.glyphTile, height: CleanGrid.glyphTile)
                        .background(GlyphTileBackground())
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(card["title"].string ?? "").font(.system(size: 19, weight: .bold)).lineLimit(3)
                    if let creator = card["creator"].string { Text(creator).font(.system(size: 15)).lineLimit(1) }
                    let sub = [doc ? card["app"].string : nil, card["sub"].string, card["year"].string].compactMap { $0 }
                    if !sub.isEmpty { Text(sub.joined(separator: " · ")).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(1) }
                    if doc, let edited = DocPreview.editedLine(editor: card["editor"].string, edited: card["edited"].string) {
                        Text(edited).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    if music {
                        PillButton(title: "Play", icon: "play.fill", primary: true) { opener.play(item) }.fixedSize().padding(.top, 6)
                    } else if !doc, let url = card["url"].url {
                        PillButton(title: "Open", icon: "arrow.up.right", primary: true) { openURL(url) }.fixedSize().padding(.top, 6)
                    }
                }
                Spacer(minLength: 0)
            }
            if let summary = card["summary"].string, fit != .compact {
                // A document reads by its first line (D3); the rest is in the doc.
                Text(doc ? (DocPreview.firstLine(summary) ?? summary) : summary).font(.system(size: 14.5))
                    .lineLimit(doc ? 2 : nil).fixedSize(horizontal: false, vertical: true)
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
                    // Six lines at most, each whole (two lines before a cut): nine one-line items cut
                    // mid-word ran the card off the screen. The rest are counted; the answer has them.
                    let shown = list.count > 7 ? Array(list.prefix(6)) : list
                    ForEach(Array(shown.enumerated()), id: \.offset) { i, line in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text("\(i + 1)").font(.system(size: 13).monospacedDigit()).foregroundStyle(.secondary).frame(width: 18, alignment: .trailing)
                            Text(line).font(.system(size: 14.5)).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                        .padding(.vertical, 6)
                        .overlay(alignment: .bottom) { if i < shown.count - 1 { Divider().padding(.leading, 28) } }
                    }
                    // The count the header gives ("10 changes") when the list sent fewer: "3 more" under 6
                    // of a 10-change build disagreed with the line above it.
                    let said = (card["sub"].string ?? "").range(of: #"\b(\d{1,3}) (?:changes|issues|fixes|items)\b"#, options: .regularExpression)
                        .flatMap { Int((card["sub"].string ?? "")[$0].split(separator: " ")[0]) } ?? 0
                    let total = max(list.count, said)
                    if shown.count < total {
                        Text("\(total - shown.count) more").font(.system(size: 12.5)).foregroundStyle(.secondary).padding(.leading, 28).padding(.top, 6)
                    }
                }
            }
            // A document's Open is the card's action, at the card's edge under what it says (in the
            // title's indented column it made a second left edge beside the body).
            if doc, !music, let url = card["url"].url {
                PillButton(title: "Open", icon: "arrow.up.right", primary: true) { openURL(url) }.fixedSize()
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
