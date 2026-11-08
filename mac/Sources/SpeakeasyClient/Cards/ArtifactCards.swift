import SwiftUI
import SpeakeasyCore

// Artifact cards v1 (catalog P1 carousel, P3 compare, P5 status tiles, P8 draft). The server sends
// typed fields only; layout, links and the photo-or-icon choice live here. Premium bar: no table
// grids, photos only when they load (an icon tile otherwise), one gold primary per card.

/// Highlights (brass on classic, neutral ink on clean) and the one primary action, from the card look.
private var gold: Color { CardStyle.active.emphasis }
private var look: CardStyle { CardStyle.active }

// MARK: - P1 Explore: sections of swipeable things

struct ExploreCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    @State private var byDay = false
    var body: some View {
        let days = ExploreByDay.sections(card)
        let sections = byDay ? (days ?? card["sections"].array) : card["sections"].array
        VStack(alignment: .leading, spacing: 18) {
            if let days, !days.isEmpty, fit != .compact {
                // Things that name their day can be read day by day instead of kind by kind.
                Picker("Show", selection: $byDay) {
                    Text("By kind").tag(false)
                    Text("By day").tag(true)
                }
                .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 260)
                .accessibilityIdentifier("explore-by-day")
            }
            ForEach(Array(sections.prefix(fit == .compact ? 1 : 10).enumerated()), id: \.offset) { _, section in
                ExploreSection(section: section)
            }
        }
    }
}

struct ExploreSection: View {
    let section: JSONValue
    @State private var page: Int? = 0
    var body: some View {
        let entity = section["entity"].string ?? "place"
        let items = section["items"].array
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: ArtifactLinks.symbol(entity)).font(.system(size: 12, weight: .semibold)).foregroundStyle(gold)
                    .frame(width: 26, height: 26).background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(gold.opacity(0.16)))
                Text(section["label"].string ?? "").font(.system(size: 17, weight: .semibold))
                Spacer(minLength: 4)
            }
            ScrollView(.horizontal) {
                LazyHStack(spacing: 12) {
                    ForEach(Array(items.enumerated()), id: \.offset) { i, item in
                        // One thing fills the width; several peek the next one so the swipe is obvious.
                        EntityTile(item: item, entity: item["_entity"].string ?? entity)
                            .containerRelativeFrame(.horizontal) { w, _ in items.count == 1 ? w : min(w * 0.82, 340) }
                            .id(i)
                    }
                }
                .scrollTargetLayout()
            }
            .scrollIndicators(.hidden)
            .scrollTargetBehavior(.viewAligned)
            .scrollPosition(id: $page)
            .scrollClipDisabled()
            if items.count > 1 {
                HStack(spacing: 5) {
                    ForEach(0..<items.count, id: \.self) { i in
                        Capsule().fill(i == (page ?? 0) ? gold : Color.primary.opacity(0.22))
                            .frame(width: i == (page ?? 0) ? 16 : 6, height: 6)
                    }
                }
                .frame(maxWidth: .infinity).accessibilityHidden(true)
            }
        }
    }
}

/// One thing: its photo (or an icon tile), the name over a gradient, a when pill, two lines of
/// facts and up to three actions.
struct EntityTile: View {
    let item: JSONValue
    let entity: String
    @Environment(\.openURL) private var openURL
    @Environment(\.artifactOpener) private var opener
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .bottomLeading) {
                ArtifactPhoto(url: item["photo_url"].url, symbol: ArtifactLinks.symbol(entity, item: item), number: item["image"].double.map { Int($0) })
                    .frame(height: 138).frame(maxWidth: .infinity).clipped()
                LinearGradient(colors: [.clear, .black.opacity(0.72)], startPoint: .center, endPoint: .bottom)
                Text(item["name"].string ?? "").font(.system(size: 18, weight: .bold)).foregroundStyle(.white)
                    .lineLimit(2).padding(12)
            }
            .overlay(alignment: .topLeading) {
                if let when = item["when"].string {
                    Text(when).font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
                        .padding(.horizontal, 9).padding(.vertical, 5)
                        .background(Capsule().fill(.ultraThinMaterial)).environment(\.colorScheme, .dark).padding(10)
                }
            }
            VStack(alignment: .leading, spacing: 4) {
                let first = [item["sub"].string ?? item["creator"].string, item["city"].string, item["price"].string,
                             item["rating"].double.map { String(format: "★ %.1f", $0) }, item["state"].string].compactMap { $0 }
                if !first.isEmpty { Text(first.joined(separator: " · ")).font(.system(size: 14)).lineLimit(1) }
                let facts = item["facts"].strings
                if !facts.isEmpty { Text(facts.joined(separator: " · ")).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(2) }
                let actions = EntityActions.of(item, entity: entity)
                if !actions.isEmpty {
                    // Every label when they fit; otherwise the first keeps its words and the rest
                    // become round icon buttons (never a clipped "M…").
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 8) {
                            ForEach(Array(actions.enumerated()), id: \.offset) { i, a in
                                PillButton(title: a.title, icon: a.icon, primary: i == 0) { run(a) }.fixedSize()
                            }
                            Spacer(minLength: 0)
                        }
                        HStack(spacing: 8) {
                            ForEach(Array(actions.enumerated()), id: \.offset) { i, a in
                                PillButton(title: a.title, icon: a.icon, primary: i == 0, iconOnly: i > 0) { run(a) }.fixedSize()
                            }
                            Spacer(minLength: 0)
                        }
                    }
                    .padding(.top, 6)
                }
            }
            .padding(12)
        }
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(look.well))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(look.hairline))
        .accessibilityElement(children: .contain)
    }

    private func run(_ a: EntityActions.Action) {
        switch a.kind {
        case .play: opener.play(item)
        case .maps: opener.maps(item)
        case .open(let url): openURL(url)
        }
    }
}

enum EntityActions {
    enum Kind { case play, maps, open(URL) }
    struct Action { var title: String; var icon: String; var kind: Kind }
    static func of(_ item: JSONValue, entity: String) -> [Action] {
        var out: [Action] = []
        if ArtifactLinks.isMusic(item, entity: entity) { out.append(Action(title: "Play", icon: "play.fill", kind: .play)) }
        if entity != "media", entity != "pr", entity != "article", ArtifactLinks.hasPlace(item) {
            out.append(Action(title: "Maps", icon: "mappin", kind: .maps))
        }
        if let url = item["url"].url {
            let title = entity == "product" ? "Buy" : entity == "pr" || entity == "article" ? "Open" : "Website"
            out.append(Action(title: title, icon: entity == "product" ? "bag" : "globe", kind: .open(url)))
        }
        if let tel = ArtifactLinks.call(item), out.count < 3 { out.append(Action(title: "Call", icon: "phone.fill", kind: .open(tel))) }
        return Array(out.prefix(3))
    }
}

/// The photo when it loads; an icon tile while it loads and when it can't (never a dead grey box).
struct ArtifactPhoto: View {
    let url: URL?
    let symbol: String
    var number: Int? = nil
    @Environment(\.questionActions) private var actions
    @State private var loaded: Image?
    var body: some View {
        Group {
        if let loaded {
            loaded.resizable().aspectRatio(contentMode: .fill)
        } else if let number, actions.image != nil {
            IconTile(symbol: symbol).task(id: number) {
                guard let data = await actions.image?(number) else { return }
                #if os(macOS)
                if let image = NSImage(data: data) { loaded = Image(nsImage: image) }
                #else
                if let image = UIImage(data: data) { loaded = Image(uiImage: image) }
                #endif
            }
        } else if let url {
            AsyncImage(url: url) { phase in
                if let image = phase.image { image.resizable().aspectRatio(contentMode: .fill) } else { IconTile(symbol: symbol) }
            }
        } else {
            IconTile(symbol: symbol)
        }
        }
    }
}

struct IconTile: View {
    let symbol: String
    var body: some View {
        ZStack {
            LinearGradient(colors: [gold.opacity(0.30), gold.opacity(0.08)], startPoint: .topLeading, endPoint: .bottomTrailing)
            Image(systemName: symbol).font(.system(size: 34, weight: .semibold)).foregroundStyle(gold.opacity(0.9))
                .offset(y: -12)
        }
        .accessibilityHidden(true)
    }
}

struct PillButton: View {
    var title: String
    var icon: String
    var primary: Bool
    var iconOnly = false
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon).font(.system(size: 12, weight: .semibold))
                if !iconOnly { Text(title).font(.system(size: 14, weight: .semibold)).lineLimit(1) }
            }
            .padding(.horizontal, iconOnly ? 0 : 14).frame(minWidth: 34).frame(height: 34)
            .foregroundStyle(primary ? look.onAction : look.quietInk)
            .background(Capsule().fill(primary ? AnyShapeStyle(look.action) : AnyShapeStyle(look.quietFill)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
    }
}

// MARK: - P3 Compare: the pick first, runners-up beside each other

struct CompareCard: View {
    let card: ViewCard
    @Environment(\.openURL) private var openURL
    @Environment(\.cardFit) private var fit
    var body: some View {
        let options = card["options"].array
        let w = Int(card["winner"].double ?? 0)
        let others = options.enumerated().filter { $0.offset != w }.map(\.element)
        VStack(alignment: .leading, spacing: 12) {
            if let title = card["title"].string { Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary) }
            if options.indices.contains(w) {
                let pick = options[w]
                VStack(alignment: .leading, spacing: 10) {
                    if let url = pick["photo_url"].url {
                        ArtifactPhoto(url: url, symbol: "bag.fill").frame(height: 120).frame(maxWidth: .infinity).clipped()
                            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    }
                    HStack(spacing: 8) {
                        Label("MY PICK", systemImage: "star.fill").font(.system(size: 11, weight: .bold)).tracking(0.6)
                            .foregroundStyle(look.onAction).padding(.horizontal, 8).padding(.vertical, 4)
                            .background(Capsule().fill(gold))
                        Text(card["why"].string ?? "").font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(2)
                    }
                    HStack(alignment: .firstTextBaseline) {
                        Text(pick["name"].string ?? "").font(.system(size: 22, weight: .bold)).lineLimit(2)
                        Spacer(minLength: 8)
                        if let price = pick["price"].string { Text(price).font(.system(size: 22, weight: .bold)).foregroundStyle(gold) }
                    }
                    let sub = [pick["sub"].string].compactMap { $0 } + pick["facts"].strings
                    if !sub.isEmpty { Text(sub.joined(separator: " · ")).font(.system(size: 13)).foregroundStyle(.secondary) }
                    if fit != .compact {
                        ForEach(card["reasons"].strings, id: \.self) { r in
                            Label { Text(r).font(.system(size: 14)) } icon: { Image(systemName: "checkmark").foregroundStyle(gold) }
                        }
                        if let caveat = card["caveat"].string { Text(caveat).font(.system(size: 12.5)).foregroundStyle(.secondary) }
                    }
                    if let url = pick["url"].url {
                        PillButton(title: "Open", icon: "arrow.up.right", primary: false) { openURL(url) }
                    }
                }
                .padding(14)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(look.capsHeaders ? gold.opacity(0.08) : look.well))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(look.capsHeaders ? gold.opacity(0.85) : look.hairline, lineWidth: look.capsHeaders ? 1.2 : 0.7))
            }
            if !others.isEmpty && fit != .compact {
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 10, alignment: .top), GridItem(.flexible(), spacing: 10, alignment: .top)], spacing: 10) {
                    ForEach(Array(others.enumerated()), id: \.offset) { _, o in
                        Button { if let url = o["url"].url { openURL(url) } } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(o["name"].string ?? "").font(.system(size: 15, weight: .semibold)).lineLimit(2)
                                if let price = o["price"].string { Text(price).font(.system(size: 15, weight: .bold)) }
                                if let why = o["why"].string ?? o["sub"].string {
                                    Text(why).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(3)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(look.well))
                        }
                        .buttonStyle(.plain).disabled(o["url"].url == nil)
                    }
                }
            } else if !others.isEmpty {
                Text(others.compactMap { o in [o["name"].string, o["price"].string].compactMap { $0 }.joined(separator: " ") }.joined(separator: " · "))
                    .font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

// MARK: - P5 Status tiles

struct StatusTilesCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    @Environment(\.openURL) private var openURL
    var body: some View {
        let items = card["items"].array
        let order: [String: Int] = ["needs_you": 0, "bad": 1, "warn": 2, "info": 3, "on": 4, "ok": 5, "off": 6]
        let sorted = items.enumerated().sorted { (order[$0.element["state"].string ?? ""] ?? 9, $0.offset) < (order[$1.element["state"].string ?? ""] ?? 9, $1.offset) }.map(\.element)
        VStack(alignment: .leading, spacing: 10) {
            // The app the facts came from, then what they are about ("Vercel · fieldnote-web"), as the designs head a card.
            let head = [card["source"].string, card["scope"].string].compactMap { $0 }.removingDuplicates()
            if !head.isEmpty { Text(head.joined(separator: " · ")).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary) }
            if items.count > 4 {
                // Past four, tiles turn into a wall of repeated state words ("Check" seven times):
                // one row each, what's wrong first, the state word at the end.
                let cap = fit == .compact ? 4 : 6
                VStack(spacing: 0) {
                    ForEach(Array(sorted.prefix(cap).enumerated()), id: \.offset) { n, item in
                        if n > 0 { Divider().overlay(look.hairline) }
                        StatusRow(item: item).onTapGesture { if let url = item["url"].url { openURL(url) } }
                    }
                }
                .padding(.horizontal, 12)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(look.well))
                if sorted.count > cap {
                    // The sort puts trouble first, so what's left is either all fine or said in the answer.
                    let rest = sorted.dropFirst(cap)
                    let fine = rest.allSatisfy { ["ok", "on", "off", "info"].contains($0["state"].string ?? "") }
                    Text(fine ? "\(rest.count) more, all fine" : "\(rest.count) more in the answer")
                        .font(.system(size: 12.5)).foregroundStyle(.secondary)
                }
            } else {
                // Two to a row, equal heights; an odd last tile takes the whole row (three tiles left a hole).
                PairGrid(count: min(sorted.count, 4), spacing: 10) { n in
                    StatusTile(item: sorted[n]).onTapGesture { if let url = sorted[n]["url"].url { openURL(url) } }
                }
            }
        }
    }
}

struct StatusTile: View {
    let item: JSONValue
    static func color(_ state: String?) -> Color {
        switch state {
        case "ok", "on": return look.good
        case "warn", "needs_you": return look.needsYou
        case "bad": return look.bad
        default: return .secondary
        }
    }
    static func word(_ state: String?) -> String {
        ["ok": "OK", "on": "On", "off": "Off", "warn": "Needs a look", "bad": "Down", "needs_you": "Needs you", "info": "Info"][state ?? ""] ?? ""
    }
    var body: some View {
        let state = item["state"].string
        let loud = state == "needs_you" || state == "warn" || state == "bad"
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 7) {
                Circle().fill(Self.color(state)).frame(width: 8, height: 8)
                Text(item["name"].string ?? "").font(.system(size: 16, weight: .semibold)).lineLimit(1)
            }
            if let role = item["role"].string { Text(role).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(1) }
            if let value = item["value"].string {
                Text(value).font(.system(size: 26, weight: .bold)).lineLimit(1).minimumScaleFactor(0.6).padding(.top, 2)
                if let unit = item["unit"].string { Text(unit).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(1) }
            } else {
                Text(Self.word(state)).font(.system(size: 20, weight: .bold)).foregroundStyle(loud ? Self.color(state) : .primary).padding(.top, 2)
            }
            let history = item["history"].doubles
            if history.count >= 3 { Spark(points: history).frame(height: 18) }
            if let note = item["note"].string { Text(note).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(2) }
        }
        .padding(12).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(loud ? Self.color(state).opacity(0.08) : look.well))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(loud ? Self.color(state).opacity(0.7) : look.hairline))
        .accessibilityElement(children: .combine)
    }
}

/// Cells two to a row with equal heights per row; an odd last cell spans both columns.
struct PairGrid<Cell: View>: View {
    let count: Int
    var spacing: CGFloat = 10
    @ViewBuilder let cell: (Int) -> Cell
    var body: some View {
        Grid(alignment: .topLeading, horizontalSpacing: spacing, verticalSpacing: spacing) {
            ForEach(Array(stride(from: 0, to: count, by: 2)), id: \.self) { n in
                if n + 1 < count {
                    GridRow { cell(n); cell(n + 1) }
                } else {
                    GridRow { cell(n).gridCellColumns(2) }
                }
            }
        }
    }
}

/// One status as a row (cards with more than four): dot, name, the note under it, and the value or
/// state word at the end.
struct StatusRow: View {
    let item: JSONValue
    var body: some View {
        let state = item["state"].string
        let loud = state == "needs_you" || state == "warn" || state == "bad"
        HStack(alignment: .center, spacing: 10) {
            Circle().fill(StatusTile.color(state)).frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(item["name"].string ?? "").font(.system(size: 15, weight: .semibold)).lineLimit(1)
                if let note = item["note"].string ?? item["role"].string {
                    Text(note).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 8)
            Text([item["value"].string, item["unit"].string].compactMap { $0 }.joined(separator: " ").nonEmpty ?? StatusTile.word(state))
                .font(.system(size: 13, weight: .semibold)).monospacedDigit()
                .foregroundStyle(loud ? StatusTile.color(state) : .secondary).lineLimit(1).fixedSize()
        }
        .padding(.vertical, 10)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
    }
}

private extension String { var nonEmpty: String? { isEmpty ? nil : self } }

/// A plain line through the points (no axes, no fill).
struct Spark: View {
    let points: [Double]
    var body: some View {
        GeometryReader { geo in
            let lo = points.min() ?? 0, hi = points.max() ?? 1, span = max(hi - lo, 0.0001)
            Path { p in
                for (i, v) in points.enumerated() {
                    let pt = CGPoint(x: geo.size.width * CGFloat(i) / CGFloat(max(points.count - 1, 1)),
                                     y: geo.size.height * (1 - CGFloat((v - lo) / span)))
                    if i == 0 { p.move(to: pt) } else { p.addLine(to: pt) }
                }
            }
            .stroke(gold, style: StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
        }
        .accessibilityHidden(true)
    }
}

// MARK: - P8 Draft to approve

struct ArtifactDraftCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    static func icon(_ channel: String?) -> String {
        switch channel {
        case "email": return "envelope.fill"
        case "imessage", "sms", "whatsapp": return "message.fill"
        case "slack", "discord", "telegram": return "bubble.left.and.bubble.right.fill"
        case "x", "linkedin": return "text.bubble.fill"
        default: return "paperplane.fill"
        }
    }
    static func channelName(_ channel: String?) -> String {
        ["email": "Email", "imessage": "iMessage", "sms": "Text", "slack": "Slack", "discord": "Discord", "telegram": "Telegram",
         "x": "Post on X", "linkedin": "LinkedIn", "whatsapp": "WhatsApp"][channel ?? ""] ?? "Message"
    }
    var body: some View {
        let channel = card["channel"].string
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: Self.icon(channel)).font(.system(size: 13, weight: .semibold)).foregroundStyle(gold)
                Text("\(Self.channelName(channel)) to \(card["to"].string ?? "")").font(.system(size: 14, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 4)
                Text(card["revision"].double.map { "Draft \(Int($0))" } ?? "Draft").font(.system(size: 12, weight: .semibold)).foregroundStyle(gold)
            }
            if let from = card["send_as"].string { Text("From \(from)").font(.system(size: 12.5)).foregroundStyle(.secondary) }
            if let subject = card["subject"].string { Text(subject).font(.system(size: 15, weight: .semibold)) }
            Text(card["body"].string ?? "").font(.system(size: 14.5)).textSelection(.enabled)
                .lineLimit(fit == .compact ? 3 : nil).fixedSize(horizontal: false, vertical: true)
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(look.well))
            if let changes = card["changes"].string { Text(changes).font(.system(size: 12.5)).foregroundStyle(.secondary) }
        }
    }
}

// MARK: - Opening music and places: ask once

/// Opens music and places in the user's chosen app. The first tap asks which app, with
/// "Remember my choice"; Settings can change or clear it. Set by the host (`ArtifactOpenerHost`).
public struct ArtifactOpener {
    var ask: (Pending) -> Void = { _ in }
    enum Pending: Identifiable { case play(JSONValue), maps(JSONValue)
        var id: String { switch self { case .play(let i): "p" + (i["name"].string ?? ""); case .maps(let i): "m" + (i["name"].string ?? "") } }
    }
    func play(_ item: JSONValue) { ask(.play(item)) }
    func maps(_ item: JSONValue) { ask(.maps(item)) }
}
private struct ArtifactOpenerKey: EnvironmentKey { static let defaultValue = ArtifactOpener() }
extension EnvironmentValues {
    var artifactOpener: ArtifactOpener {
        get { self[ArtifactOpenerKey.self] }
        set { self[ArtifactOpenerKey.self] = newValue }
    }
}

/// Wraps an artifact: opens straight away when a choice is saved, else shows the one-time chooser.
struct ArtifactOpenerHost<Content: View>: View {
    @ViewBuilder var content: Content
    @State private var pending: ArtifactOpener.Pending?
    @State private var remember = true
    @Environment(\.openURL) private var openURL

    var body: some View {
        content
            .environment(\.artifactOpener, ArtifactOpener(ask: { p in
                switch p {
                case .play(let item): if let app = OpenInPrefs.music(), let url = ArtifactLinks.music(item, app: app) { openURL(url) } else { pending = p }
                case .maps(let item): if let app = OpenInPrefs.maps(), let url = ArtifactLinks.maps(item, app: app) { openURL(url) } else { pending = p }
                }
            }))
            .sheet(item: $pending) { p in chooser(p) }
    }

    @ViewBuilder private func chooser(_ p: ArtifactOpener.Pending) -> some View {
        VStack(spacing: 14) {
            switch p {
            case .play(let item):
                Text("Open in Apple Music or Spotify?").font(.headline).multilineTextAlignment(.center)
                ForEach(MusicApp.allCases, id: \.self) { app in
                    choice(app.label) {
                        if remember { OpenInPrefs.setMusic(app) }
                        if let url = ArtifactLinks.music(item, app: app) { openURL(url) }
                    }
                }
            case .maps(let item):
                Text("Open in Apple Maps or Google Maps?").font(.headline).multilineTextAlignment(.center)
                ForEach(MapsApp.allCases, id: \.self) { app in
                    choice(app.label) {
                        if remember { OpenInPrefs.setMaps(app) }
                        if let url = ArtifactLinks.maps(item, app: app) { openURL(url) }
                    }
                }
            }
            Toggle("Remember my choice", isOn: $remember).font(.subheadline)
                .accessibilityIdentifier("artifact-remember-choice")
            Text("You can change this in Settings.").font(.footnote).foregroundStyle(.secondary)
        }
        .padding(22)
        .frame(minWidth: 300)
        #if os(iOS)
        .presentationDetents([.height(300)])
        .presentationDragIndicator(.visible)
        #endif
    }

    private func choice(_ title: String, _ go: @escaping () -> Void) -> some View {
        Button { pending = nil; go() } label: {
            Text(title).font(.system(size: 16, weight: .semibold)).frame(maxWidth: .infinity).frame(height: 44)
                .foregroundStyle(look.onAction).background(Capsule().fill(look.action))
        }
        .buttonStyle(.plain)
    }
}
