import SwiftUI
import MapKit
import SpeakeasyCore

// Day, places, travel, people and messages.

struct EventRow: View {
    let event: JSONValue
    var body: some View {
        LinkedRow(url: event["url"].url) { row }
    }

    private var row: some View {
        HStack(alignment: .top, spacing: 8) {
            RoundedRectangle(cornerRadius: 2).fill(eventColor(event["color"].string)).frame(width: 3)
            VStack(alignment: .leading, spacing: 1) {
                Text(event["title"].string ?? "").font(.system(size: 12, weight: .medium)).lineLimit(2)
                Text(event["all_day"].bool == true ? "All day"
                     : [EventTimes.span(start: event["start"].string, end: event["end"].string),
                        event["place"].string].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · "))
                    .font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

func eventColor(_ name: String?) -> Color {
    switch name?.lowercased() {
    case "red": return .red
    case "orange": return .orange
    case "yellow": return .yellow
    case "green": return .green
    case "purple": return .purple
    case "pink": return .pink
    case "teal": return .teal
    default: return .accentColor
    }
}

struct DayCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    @Environment(\.cardStyle) private var style
    @Environment(\.cardRunAction) private var runAction
    var body: some View {
        let events = Array(card["events"].array.prefix(fit.rows(12)))
        let pairs = DayOverlaps.pairs(events)
        CardBox {
            CardHeader(icon: "calendar", title: card["title"].string ?? EventTimes.dayTitle(card["date"].string) ?? "Today",
                       trailing: style.capsHeaders || events.isEmpty ? nil : "\(card["events"].array.count) event\(card["events"].array.count == 1 ? "" : "s")")
            if events.isEmpty {
                Text("Nothing scheduled.").font(.system(size: style.capsHeaders ? 12 : 15)).foregroundStyle(style.secondary)
            }
            // Overlapping events sit together in one well, under a line saying by how much.
            ForEach(groups(events, pairs), id: \.first) { group in
                if group.count > 1 {
                    VStack(alignment: .leading, spacing: 8) {
                        Label(overlapText(events, group), systemImage: "exclamationmark.triangle.fill")
                            .font(style.capsHeaders ? .system(size: 11, weight: .semibold) : .caption.weight(.semibold))
                            .foregroundStyle(style.warn)
                        ForEach(group, id: \.self) { i in row(events[i], next: i == nextIndex(events)) }
                        if let move = card.actions.first(where: { $0.id == "move" }), let runAction {
                            CardPillButton(move.label, compact: style.capsHeaders) { runAction(move.id) }
                                .accessibilityIdentifier("card-action-move")
                        }
                    }
                    .cardWell(style, padding: style.capsHeaders ? 8 : 12)
                    .accessibilityElement(children: .contain)
                } else {
                    row(events[group[0]], next: group[0] == nextIndex(events))
                }
            }
            MoreRows(total: min(card["events"].array.count, 12), shown: events.count)
            let free = card["free"].strings
            if !free.isEmpty {
                HStack(spacing: 5) {
                    if style.capsHeaders { Image(systemName: "checkmark.circle").font(.system(size: 10)).foregroundStyle(style.good) }
                    Text("Free " + free.joined(separator: ", ")).font(style.capsHeaders ? .system(size: 11.5) : .footnote)
                        .foregroundStyle(style.capsHeaders ? style.secondary : style.faint).lineLimit(2)
                }
            }
        }
    }

    /// Events in order, overlapping ones gathered (transitively) into one group.
    private func groups(_ events: [JSONValue], _ pairs: [(Int, Int)]) -> [[Int]] {
        var parent = Array(events.indices)
        func root(_ i: Int) -> Int { var i = i; while parent[i] != i { i = parent[i] }; return i }
        for (a, b) in pairs { parent[root(b)] = root(a) }
        var seen: [Int: Int] = [:], out: [[Int]] = []
        for i in events.indices {
            let r = root(i)
            if let g = seen[r] { out[g].append(i) } else { seen[r] = out.count; out.append([i]) }
        }
        return out
    }

    private func overlapText(_ events: [JSONValue], _ group: [Int]) -> String {
        let spans = group.compactMap { i -> (Date, Date)? in
            guard let s = events[i]["start"].string.flatMap(EventTimes.date), let e = events[i]["end"].string.flatMap(EventTimes.date) else { return nil }
            return (s, e)
        }
        guard group.count == 2, spans.count == 2 else { return "These overlap" }
        let minutes = Int((min(spans[0].1, spans[1].1).timeIntervalSince(max(spans[0].0, spans[1].0)) / 60).rounded())
        guard minutes > 0 else { return "These overlap" }
        let span = minutes >= 60 && minutes % 60 == 0 ? "\(minutes / 60) hour\(minutes == 60 ? "" : "s")" : "\(minutes) minutes"
        return "These overlap by \(span)"
    }

    /// The call starting within 15 minutes (or under way) gets Join; a standup tomorrow at 9:30 is not
    /// something to join now, and its gold button pulled the eye from the clash.
    private func nextIndex(_ events: [JSONValue]) -> Int? {
        let now = Date()
        return events.firstIndex { e in
            guard e["join_url"].url != nil, let start = e["start"].string.flatMap(EventTimes.date) else { return false }
            let end = e["end"].string.flatMap(EventTimes.date) ?? start.addingTimeInterval(1800)
            return start.timeIntervalSince(now) <= 15 * 60 && end > now
        }
    }

    @ViewBuilder private func row(_ e: JSONValue, next: Bool) -> some View {
        if style.capsHeaders {
            HStack(spacing: 6) {
                EventRow(event: e)
                if next, let join = e["join_url"].url { Link("Join", destination: join).font(.system(size: 11.5, weight: .semibold)) }
            }
        } else {
            HStack(alignment: .center, spacing: 12) {
                Text(e["all_day"].bool == true ? "All day" : EventTimes.one(e["start"].string).map(Self.bare) ?? "")
                    .font(.footnote.weight(.semibold)).monospacedDigit().foregroundStyle(style.secondary)
                    .frame(width: 48, alignment: .leading)
                VStack(alignment: .leading, spacing: 1) {
                    Text(e["title"].string ?? "").font(.subheadline.weight(.medium)).foregroundStyle(style.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    let note = [e["all_day"].bool == true ? nil : EventTimes.span(start: e["start"].string, end: e["end"].string),
                                e["place"].string ?? (e["join_url"].url != nil ? "Video call" : nil)].compactMap { $0 }
                    if !note.isEmpty { Text(note.joined(separator: " · ")).font(.footnote).foregroundStyle(style.secondary).lineLimit(2) }
                }
                Spacer(minLength: 4)
                if next, let join = e["join_url"].url {
                    Link(destination: join) {
                        Text("Join").font(.subheadline.weight(.semibold)).foregroundStyle(style.onAction)
                            .padding(.horizontal, 16).frame(height: 34).background(Capsule().fill(style.action))
                    }
                    .accessibilityLabel("Join \(e["title"].string ?? "the call")")
                } else if let url = e["url"].url {
                    Link(destination: url) { Image(systemName: "arrow.up.right").font(.caption.weight(.semibold)).foregroundStyle(style.faint) }
                        .accessibilityLabel("Open \(e["title"].string ?? "event")")
                }
            }
            .accessibilityElement(children: .combine)
        }
    }

    /// "1:00 PM" → "1:00" in the time column (the span underneath says AM/PM).
    static func bare(_ time: String) -> String {
        for suffix in [" AM", " PM", "\u{202F}AM", "\u{202F}PM"] where time.hasSuffix(suffix) { return String(time.dropLast(suffix.count)) }
        return time
    }
}

struct AgendaCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    var body: some View {
        CardBox {
            CardHeader(icon: "calendar", title: card["title"].string ?? "Upcoming")
            // Compact: the first day only.
            let days = card["days"].array
            ForEach(Array(days.prefix(fit == .compact ? 1 : 7).enumerated()), id: \.offset) { i, day in
                if i > 0 { Divider().opacity(0.4) }
                Text(day["date"].string ?? "").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                ForEach(Array(day["events"].array.prefix(fit.rows(6)).enumerated()), id: \.offset) { _, e in EventRow(event: e) }
            }
            if fit == .compact { MoreRows(total: min(days.count, 7), shown: 1, noun: ("day", "days")) }
        }
    }
}

struct ReminderCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: card["done"].bool == true ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 18)).foregroundStyle(card["done"].bool == true ? Color.green : Color.orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text(card["title"].string ?? "Reminder").font(.system(size: 13, weight: .medium))
                    Text([card["due"].string, card["list"].string].compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    if let notes = card["notes"].string { Text(notes).font(.system(size: 11)).foregroundStyle(.secondary) }
                }
            }
        }
    }
}

struct MiniMap: View {
    let lat: Double, lon: Double
    var title: String = ""
    var height: CGFloat = 110
    var body: some View {
        let center = CLLocationCoordinate2D(latitude: lat, longitude: lon)
        Map(initialPosition: .region(MKCoordinateRegion(center: center, latitudinalMeters: 900, longitudinalMeters: 900)),
            interactionModes: []) {
            Marker(title, coordinate: center)
        }
        .frame(height: height)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .allowsHitTesting(false)
    }
}

func mapsURL(_ name: String?, lat: Double?, lon: Double?) -> URL? {
    var c = URLComponents(string: "https://maps.apple.com/")!
    var q: [URLQueryItem] = []
    if let name { q.append(URLQueryItem(name: "q", value: name)) }
    if let lat, let lon { q.append(URLQueryItem(name: "ll", value: "\(lat),\(lon)")) }
    c.queryItems = q
    return q.isEmpty ? nil : c.url
}

struct PlaceCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            if let photo = card["photo_url"].url {
                CardImage(url: photo, size: 120, corner: 10, fill: true).frame(maxWidth: .infinity)
            } else if let lat = card["lat"].double, let lon = card["lon"].double {
                MiniMap(lat: lat, lon: lon, title: card["name"].string ?? "")
            }
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(card["name"].string ?? "").font(.system(size: 13.5, weight: .semibold))
                    Text([card["category"].string, card["price"].string, card["distance"].string].compactMap { $0 }
                        .joined(separator: " · ")).font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                openable(card["url"].url ?? mapsURL(card["name"].string, lat: card["lat"].double, lon: card["lon"].double))
            }
            HStack(spacing: 10) {
                if let rating = card["rating"].double {
                    Label(String(format: "%.1f", rating) + (card["reviews"].double.map { " (\(Int($0)))" } ?? ""),
                          systemImage: "star.fill").foregroundStyle(.orange)
                }
                if let open = card["open_now"].bool {
                    Text(open ? "Open" : "Closed").foregroundStyle(open ? Color.green : Color.red).fontWeight(.medium)
                }
                if let hours = card["hours_today"].string { Text(hours).foregroundStyle(.secondary) }
            }
            .font(.system(size: 11))
            if let address = card["address"].string {
                Text(address).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
            }
        }
    }
}

struct PlacesCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    /// The places as carousel tiles (the designs' picks: photo or name, one line of facts, Maps and
    /// Website), the category as the tile's line and a closed place said so.
    static func tiles(_ card: ViewCard) -> JSONValue {
        let items: [JSONValue] = card["items"].array.prefix(6).map { p in
            guard case .object(var o) = p else { return p }
            let sub = [p["category"].string, p["area"].string].compactMap { $0 }.joined(separator: " · ")
            if !sub.isEmpty, o["sub"] == nil { o["sub"] = .string(sub) }
            if p["open_now"].bool == false { o["state"] = .string("Closed now") }
            if let d = p["distance"].string { o["facts"] = .array([.string(d)]) }
            return .object(o)
        }
        return .object(["label": .string(card["title"].string ?? "Places"), "entity": .string("place"), "items": .array(items)])
    }
    var body: some View {
        if fit != .compact {
            ExploreSection(section: Self.tiles(card))
        } else {
            list
        }
    }
    private var list: some View {
        CardBox {
            CardHeader(icon: "mappin.and.ellipse", title: card["title"].string ?? "Places")
            ForEach(Array(card["items"].array.prefix(fit.rows(6)).enumerated()), id: \.offset) { i, p in
                if i > 0 { Divider().opacity(0.4) }
                HStack(spacing: 10) {
                    if let photo = p["photo_url"].url { CardImage(url: photo, size: 40, corner: 8, fill: true).frame(width: 40) }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(i + 1). \(p["name"].string ?? "")").font(.system(size: 12, weight: .medium)).lineLimit(1)
                        HStack(spacing: 6) {
                            if let r = p["rating"].double { Text("★ " + String(format: "%.1f", r)).foregroundStyle(.orange) }
                            if let c = p["category"].string { Text(c) }
                            if let price = p["price"].string { Text(price) }
                            if let d = p["distance"].string { Text(d) }
                            if let open = p["open_now"].bool { Text(open ? "Open" : "Closed").foregroundStyle(open ? Color.green : Color.red) }
                        }
                        .font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: 0)
                    openable(p["url"].url ?? mapsURL(p["name"].string, lat: p["lat"].double, lon: p["lon"].double))
                }
            }
            MoreRows(total: min(card["items"].array.count, 6), shown: fit.rows(6))
        }
    }
}

struct RouteCard: View {
    let card: ViewCard
    var body: some View {
        let icon: String = {
            switch card["mode"].string {
            case "walk", "walking": return "figure.walk"
            case "transit", "subway", "train": return "tram.fill"
            case "bike", "cycling": return "bicycle"
            default: return "car.fill"
            }
        }()
        CardBox {
            if let lat = card["dest_lat"].double ?? card["lat"].double, let lon = card["dest_lon"].double ?? card["lon"].double {
                MiniMap(lat: lat, lon: lon, title: card["destination"].string ?? "")
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: icon).font(.system(size: 14)).foregroundStyle(.secondary)
                if let minutes = card["minutes"].double {
                    Text(minutes >= 60 ? "\(Int(minutes) / 60) hr \(Int(minutes) % 60) min" : "\(Int(minutes)) min")
                        .font(.system(size: 22, weight: .semibold))
                }
                if let d = card["distance"].string { Text(d).font(.system(size: 12)).foregroundStyle(.secondary) }
                Spacer()
                openable(mapsURL(card["destination"].string, lat: card["dest_lat"].double, lon: card["dest_lon"].double))
            }
            Text("\(card["origin"].string ?? "Here") → \(card["destination"].string ?? "")").font(.system(size: 11.5)).lineLimit(2)
            HStack(spacing: 10) {
                if let leave = card["leave_by"].string { Label("Leave \(leave)", systemImage: "clock") }
                if let arrive = card["arrive_by"].string { Label("Arrive \(arrive)", systemImage: "flag.checkered") }
                if let traffic = card["traffic"].string { Text(traffic) }
            }
            .font(.system(size: 10.5)).foregroundStyle(.secondary)
            ForEach(Array(card["steps"].strings.prefix(5).enumerated()), id: \.offset) { _, s in
                Text("· " + s).font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
        }
    }
}

func lineColor(_ hex: String?) -> Color {
    guard var h = hex?.trimmingCharacters(in: CharacterSet(charactersIn: "#")), h.count == 6, let v = UInt32(h, radix: 16) else {
        return .gray
    }
    h = ""
    return Color(red: Double((v >> 16) & 0xFF) / 255, green: Double((v >> 8) & 0xFF) / 255, blue: Double(v & 0xFF) / 255)
}

struct TransitCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack {
                CardHeader(icon: "tram.fill", title: card["station"].string ?? "Departures")
                if let status = card["status"].string {
                    Text(status).font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(status.lowercased().contains("good") ? Color.green : Color.orange)
                }
            }
            ForEach(Array(card["departures"].array.prefix(6).enumerated()), id: \.offset) { _, d in
                HStack(spacing: 10) {
                    Text(d["line"].string ?? "").font(.system(size: 12, weight: .bold)).foregroundStyle(.white)
                        .frame(width: 24, height: 24).background(Circle().fill(lineColor(d["color"].string)))
                    VStack(alignment: .leading, spacing: 0) {
                        Text(d["destination"].string ?? "").font(.system(size: 12)).lineLimit(1)
                        if let note = d["note"].string { Text(note).font(.system(size: 11)).foregroundStyle(.orange).lineLimit(1) }
                    }
                    Spacer(minLength: 0)
                    if let m = d["minutes"].double {
                        Text(m < 1 ? "Now" : "\(Int(m)) min").font(.system(size: 13, weight: .semibold).monospacedDigit())
                    } else if let t = d["time"].string {
                        Text(t).font(.system(size: 12).monospacedDigit())
                    }
                }
            }
        }
    }
}

struct FlightCard: View {
    let card: ViewCard
    var body: some View {
        let status = card["status"].string ?? ""
        let late = status.lowercased().contains("delay") || status.lowercased().contains("cancel")
        CardBox {
            HStack {
                CardHeader(icon: "airplane", title: [card["airline"].string, card["number"].string].compactMap { $0 }.joined(separator: " "))
                Text(status).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(late ? Color.red : Color.green)
            }
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(card["origin"].string ?? "").font(.system(size: 22, weight: .semibold))
                    Text(card["departs"].string ?? "").font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
                Spacer()
                VStack(spacing: 3) {
                    Image(systemName: "airplane").font(.system(size: 12)).foregroundStyle(.secondary)
                    Bar(fraction: card["progress"].double ?? 0, tint: .accentColor, height: 4).frame(width: 70)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 1) {
                    Text(card["destination"].string ?? "").font(.system(size: 22, weight: .semibold))
                    Text(card["arrives"].string ?? "").font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 12) {
                if let t = card["terminal"].string { Text("Terminal \(t)") }
                if let g = card["gate"].string { Text("Gate \(g)") }
                if let d = card["delay"].string { Text(d).foregroundStyle(.red) }
            }
            .font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }
}

struct PackageCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack {
                CardHeader(icon: "shippingbox.fill", title: card["carrier"].string ?? "Package")
                openable(card["url"].url)
            }
            Text(card["item"].string ?? "").font(.system(size: 12.5, weight: .medium)).lineLimit(2)
            HStack(spacing: 8) {
                Text(card["status"].string ?? "").font(.system(size: 13, weight: .semibold))
                if let eta = card["eta"].string { Text(eta).font(.system(size: 11)).foregroundStyle(.secondary) }
            }
            let steps = card["steps"].array
            ForEach(Array(steps.prefix(6).enumerated()), id: \.offset) { i, s in
                HStack(alignment: .top, spacing: 8) {
                    VStack(spacing: 0) {
                        Circle().fill(s["done"].bool == false ? Color.primary.opacity(0.2) : Color.green).frame(width: 8, height: 8)
                        if i < min(steps.count, 6) - 1 { Rectangle().fill(Color.primary.opacity(0.15)).frame(width: 1.5, height: 16) }
                    }
                    .padding(.top, 3)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(s["text"].string ?? "").font(.system(size: 11)).lineLimit(2)
                        if let w = s["when"].string { Text(w).font(.system(size: 11.5)).foregroundStyle(.secondary) }
                    }
                }
            }
        }
    }
}

struct ContactCard: View {
    let card: ViewCard
    var body: some View {
        CardBox {
            HStack(spacing: 12) {
                if let photo = card["photo_url"].url {
                    CardImage(url: photo, size: 44, corner: 22, fill: true).frame(width: 44)
                } else {
                    Text(initials(card["name"].string)).font(.system(size: 15, weight: .semibold)).foregroundStyle(.white)
                        .frame(width: 44, height: 44).background(Circle().fill(Color.gray.gradient))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(card["name"].string ?? "").font(.system(size: 14, weight: .semibold))
                    if let s = card["subtitle"].string { Text(s).font(.system(size: 11)).foregroundStyle(.secondary) }
                }
            }
            HStack(spacing: 8) {
                if let phone = card["phone"].string, let url = URL(string: "tel:" + phone.filter { "+0123456789".contains($0) }) {
                    Link(destination: url) { Label(phone, systemImage: "phone.fill") }
                }
                if let email = card["email"].string, let url = URL(string: "mailto:" + email) {
                    Link(destination: url) { Label(email, systemImage: "envelope.fill").lineLimit(1) }
                }
            }
            .font(.system(size: 11))
            if let b = card["birthday"].string { Label("Birthday \(b)", systemImage: "gift").font(.system(size: 11)).foregroundStyle(.secondary) }
            if let n = card["note"].string { Text(n).font(.system(size: 11)).foregroundStyle(.secondary) }
        }
    }
}

func initials(_ name: String?) -> String {
    (name ?? "?").split(separator: " ").prefix(2).compactMap { $0.first }.map(String.init).joined().uppercased()
}

/// An outbound message waiting for the user's tap. Sending happens in the host app (it owns the
/// server client), through the `onSend` hook; the card itself never sends.
public struct DraftActions {
    public var onSend: ((String) -> Void)?
    public var onEdit: ((String) -> Void)?
    public init(onSend: ((String) -> Void)? = nil, onEdit: ((String) -> Void)? = nil) { self.onSend = onSend; self.onEdit = onEdit }
}

private struct DraftActionsKey: EnvironmentKey { static let defaultValue = DraftActions() }
public extension EnvironmentValues {
    var draftActions: DraftActions {
        get { self[DraftActionsKey.self] }
        set { self[DraftActionsKey.self] = newValue }
    }
}

struct DraftCard: View {
    let card: ViewCard
    @Environment(\.draftActions) private var actions
    @Environment(\.cardFit) private var fit
    var body: some View {
        let status = card["status"].string ?? "draft"
        let icon: String = {
            switch card["channel"].string?.lowercased() {
            case "email", "gmail", "mail": return "envelope.fill"
            case "imessage", "sms", "text", "messages": return "message.fill"
            default: return "paperplane.fill"
            }
        }()
        CardBox(tint: status == "sent" ? .green : nil) {
            HStack {
                CardHeader(icon: icon, title: (card["channel"].string ?? "Message") + " to " + (card["to"].string ?? ""))
                Text(status == "sent" ? "Sent" : "Draft").font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(status == "sent" ? Color.green : Color.orange)
            }
            if let subject = card["subject"].string { Text(subject).font(.system(size: 12.5, weight: .semibold)) }
            // Compact: the first lines of the message; the whole text is in the full card.
            Text(card["body"].string ?? "").font(.system(size: 12.5)).textSelection(.enabled)
                .lineLimit(fit == .compact ? 3 : nil)
                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.accentColor.opacity(0.10)))
            if status != "sent", let id = card["draft_id"].string, actions.onSend != nil {
                HStack {
                    Spacer()
                    if let edit = actions.onEdit { Button("Edit") { edit(id) }.buttonStyle(.bordered) }
                    Button { actions.onSend?(id) } label: { Label("Send", systemImage: "paperplane.fill") }
                        .buttonStyle(.borderedProminent)
                }
                .controlSize(.small)
            }
        }
    }
}

struct InboxCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    var body: some View {
        let items: [JSONValue] = card.kind == "message" ? [card.data] : card["items"].array
        CardBox {
            CardHeader(icon: "tray.full.fill", title: card["title"].string ?? (card.kind == "message" ? (card["channel"].string ?? "Message") : "Inbox"))
            ForEach(Array(items.prefix(fit.rows(6)).enumerated()), id: \.offset) { i, m in
                if i > 0 { Divider().opacity(0.4) }
                HStack(alignment: .top, spacing: 8) {
                    Circle().fill(m["unread"].bool == true ? Color.accentColor : Color.clear).frame(width: 6, height: 6).padding(.top, 5)
                    VStack(alignment: .leading, spacing: 1) {
                        HStack {
                            Text(m["from"].string ?? "").font(.system(size: 12, weight: .semibold)).lineLimit(1)
                            Spacer(minLength: 0)
                            Text(m["when"].string ?? "").font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                        if let s = m["subject"].string { Text(s).font(.system(size: 11.5)).lineLimit(1) }
                        if let p = m["preview"].string { Text(p).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2) }
                    }
                }
            }
            MoreRows(total: min(items.count, 6), shown: fit.rows(6))
        }
    }
}
