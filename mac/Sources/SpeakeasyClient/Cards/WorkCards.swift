import SwiftUI
import SpeakeasyCore
#if os(macOS)
import AppKit
#else
import UIKit
#endif

// Work cards (Home v7): one issue with its mockup, a pull request explained, running work as quiet
// status lines, a website preview, a set of designed screens, and (in LifeCards) a day with its
// overlaps. The agent sends facts only (plugin views.py); these views own the layout, and every
// colour, corner and surface comes from `\.cardStyle`. Buttons that act (Fix conflicts, a next
// step) are the server's, drawn by the host's button row; links the card already holds (Diff,
// GitHub, Open preview) are the host's too (`CardLinks`).

// MARK: - Pictures

/// A task picture (its card number, through the host's loader) or an https picture, drawn at its
/// own shape. Missing or failed pictures take no space. Tapping opens it full screen.
struct CardPicture: View {
    let value: JSONValue
    var width: CGFloat? = nil
    var height: CGFloat? = nil
    var corner: CGFloat = 14
    var label: String = "Picture"
    /// Fill a fixed frame (tiles) rather than fit the picture's own shape.
    var fill = false
    @Environment(\.questionActions) private var actions
    @Environment(\.cardStyle) private var style
    @State private var image: PlatformPicture?
    @State private var open = false

    var body: some View {
        // A ZStack (not a Group) so the loader runs while nothing is drawn yet: an empty Group has
        // no view to attach `.task` to, and the picture would never load.
        ZStack {
            if let image {
                Button { open = true } label: {
                    picture(image)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(label)
                .accessibilityHint("Opens the full picture")
                #if os(iOS)
                .fullScreenCover(isPresented: $open) { CardPictureViewer(image: image, label: label) }
                #else
                .sheet(isPresented: $open) { CardPictureViewer(image: image, label: label).frame(minWidth: 520, minHeight: 520) }
                #endif
            } else {
                Color.clear.frame(width: 0, height: 0)
            }
        }
        .task(id: key) { await load() }
    }

    @ViewBuilder private func picture(_ image: PlatformPicture) -> some View {
        let shape = RoundedRectangle(cornerRadius: corner, style: .continuous)
        if fill {
            image.view.resizable().scaledToFill()
                .frame(width: width ?? tileWidth(image), height: height)
                .clipped().clipShape(shape)
                .overlay(shape.strokeBorder(style.hairline, lineWidth: 0.7))
        } else {
            image.view.resizable().scaledToFit()
                .frame(width: width, height: height)
                .clipShape(shape)
                .overlay(shape.strokeBorder(style.hairline, lineWidth: 0.7))
        }
    }

    /// A tile at the picture's own aspect for a fixed height (phone screens tall, pages wide).
    private func tileWidth(_ image: PlatformPicture) -> CGFloat? {
        guard let height, image.size.height > 0 else { return nil }
        return min(max(height * image.size.width / image.size.height, 110), 320)
    }

    private var key: String { value.string ?? "none" }

    private func load() async {
        guard image == nil else { return }
        var data: Data?
        if let n = value.double, let loader = actions.image {
            data = await loader(Int(n))
        } else if let url = value.url {
            data = try? await URLSession.shared.data(from: url).0
        }
        guard let data, let picture = PlatformPicture(data: data) else { return }
        image = picture
    }
}

struct PlatformPicture {
    #if os(macOS)
    let native: NSImage
    init?(data: Data) { guard let i = NSImage(data: data) else { return nil }; native = i }
    var view: Image { Image(nsImage: native) }
    #else
    let native: UIImage
    init?(data: Data) { guard let i = UIImage(data: data) else { return nil }; native = i }
    var view: Image { Image(uiImage: native) }
    #endif
    var size: CGSize { native.size }
}

struct CardPictureViewer: View {
    let image: PlatformPicture
    let label: String
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            image.view.resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityLabel(label)
            Button { dismiss() } label: {
                Image(systemName: "xmark").font(.system(size: 15, weight: .semibold)).foregroundStyle(.white)
                    .frame(width: 36, height: 36).background(Circle().fill(Color.white.opacity(0.18)))
                    .frame(width: 44, height: 44)
            }
            .buttonStyle(.plain).padding(12).accessibilityLabel("Close")
        }
    }
}

/// An app's own mark (Linear, GitHub, a website): a small tile with a glyph.
struct SourceMark: View {
    let symbol: String
    var color: Color
    var body: some View {
        Image(systemName: symbol).font(.system(size: 12, weight: .bold)).foregroundStyle(.white)
            .frame(width: 28, height: 28)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(color))
            .accessibilityHidden(true)
    }
    static func issue(_ source: String?) -> SourceMark {
        switch source {
        case "linear": return SourceMark(symbol: "line.3.horizontal.decrease", color: Color(hex: 0x5e6ad2))
        case "github": return SourceMark(symbol: "smallcircle.filled.circle", color: Color(hex: 0x24292f))
        case "jira": return SourceMark(symbol: "square.stack.3d.up.fill", color: Color(hex: 0x1868db))
        default: return SourceMark(symbol: "checklist", color: Color(hex: 0x5b5f66))
        }
    }
    static let pr = SourceMark(symbol: "arrow.triangle.pull", color: Color(hex: 0x24292f))
    static let site = SourceMark(symbol: "globe", color: Color(hex: 0x2a2d33))
}

func sourceName(_ source: String?) -> String? {
    switch source {
    case "linear": return "Linear"
    case "github": return "GitHub"
    case "jira": return "Jira"
    default: return nil
    }
}

// MARK: - Issue

/// One tracker issue: its facts on the left and its mockup at its own shape on the right, never
/// cropped into a banner and never under the title.
struct IssueCard: View {
    let card: ViewCard
    @Environment(\.cardStyle) private var style
    @Environment(\.cardFit) private var fit
    var body: some View {
        CardBox {
            HStack(spacing: 10) {
                SourceMark.issue(card["source"].string)
                Text([sourceName(card["source"].string), card["ref"].string].compactMap { $0 }.joined(separator: " · "))
                    .font(.subheadline.weight(.semibold)).foregroundStyle(style.ink).lineLimit(1)
                Spacer(minLength: 8)
                if let when = EventTimes.stamp(card["updated"].string) { Text(when).font(.footnote).foregroundStyle(style.faint).lineLimit(1) }
            }
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 10) {
                    Text(card["title"].string ?? "").font(.title3.weight(.semibold)).foregroundStyle(style.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    if let state = card["state"].string {
                        CardTag(icon: "circle.lefthalf.filled", text: state, tint: style.tone(card["tone"].string ?? "warn"))
                    }
                    VStack(alignment: .leading, spacing: 7) {
                        let who = [card["assignee"].string, card["priority"].string.map { "\($0) priority" }].compactMap { $0 }
                        if !who.isEmpty { CardFactLine(icon: "person.fill", text: who.joined(separator: " · ")) }
                        let labels = card["labels"].strings
                        if !labels.isEmpty { CardFactLine(icon: "tag.fill", text: labels.joined(separator: ", ")) }
                        if let note = card["note"].string, fit != .compact { CardFactLine(icon: "text.bubble", text: note) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if card["image"] != .null {
                    CardPicture(value: card["image"], width: fit == .compact ? 76 : 104,
                                label: "Mockup for \(card["title"].string ?? "the issue")")
                }
            }
        }
    }
}

// MARK: - Pull request

struct PRCard: View {
    let card: ViewCard
    @Environment(\.cardStyle) private var style
    @Environment(\.cardFit) private var fit
    var body: some View {
        CardBox {
            HStack(spacing: 10) {
                SourceMark.pr
                // "owner/repo · #418"; when that can't fit beside the time, the repo's own name (an
                // owner like NousResearch cut to "NousResearch/he…" said nothing).
                // A title that already names the number ("Checks on #147753") keeps it there only.
                let number = PRCard.headerNumber(card)
                let repo = card["repo"].string
                ViewThatFits(in: .horizontal) {
                    ForEach([repo, repo?.split(separator: "/").last.map(String.init)].compactMap { $0 }.removingDuplicates() + [""], id: \.self) { name in
                        Text([name.isEmpty ? nil : name, number].compactMap { $0 }.joined(separator: " · "))
                            .font(.subheadline.weight(.semibold)).foregroundStyle(style.ink).lineLimit(1)
                            .fixedSize(horizontal: name != "", vertical: false)
                    }
                }
                Spacer(minLength: 8)
                if let when = EventTimes.stamp(card["when"].string) { Text(when).font(.footnote).foregroundStyle(style.faint).lineLimit(1) }
            }
            Text(card["title"].string ?? "").font(.title3.weight(.semibold)).foregroundStyle(style.ink)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                if let label = PRState.label(card["state"].string) {
                    CardTag(icon: stateIcon, text: label, tint: style.tone(PRState.tone(card["state"].string)))
                }
                if let files = card["files"].double {
                    CardTag(icon: "plus.forwardslash.minus", text: "\(Int(files)) file\(files == 1 ? "" : "s")")
                }
            }
            if let reason = card["reason"].string {
                Text(reason).font(.subheadline).foregroundStyle(style.secondary).fixedSize(horizontal: false, vertical: true)
            }
            let checks = card["checks"].array
            if !checks.isEmpty, fit != .compact {
                let rows = Self.checkRows(checks, total: card["checks_total"].double.map(Int.init), restState: card["checks_rest"].string)
                // Every check in one state ("didn't run" four times): the icon and the count line say it once.
                let states = Set(checks.map { $0["state"].string ?? "" })
                let same = states.count == 1
                let total = max(Int(card["checks_total"].double ?? 0), checks.count)
                let restSame = total == checks.count || card["checks_rest"].string == states.first
                if same, restSame, let only = states.first, ["skipped", "passed"].contains(only), total > 2 {
                    // Fifteen checks that all skipped (or all passed) are one fact, not a list of names.
                    HStack(spacing: 8) {
                        Image(systemName: only == "passed" ? "checkmark.circle.fill" : "minus.circle.fill")
                            .foregroundStyle(only == "passed" ? style.tone("good") : style.faint)
                        Text("All \(total) \(only)").font(.subheadline.weight(.medium)).foregroundStyle(style.ink)
                        Spacer(minLength: 0)
                    }
                    .cardWell(style)
                } else {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(Array(rows.shown.enumerated()), id: \.offset) { _, check in checkRow(check, word: !same) }
                    if let rest = rows.rest {
                        Text(rest).font(.footnote).foregroundStyle(style.faint)
                    }
                }
                .cardWell(style)
                }
            }
        }
    }
    static func headerNumber(_ card: ViewCard) -> String? {
        guard let n = card["number"].string else { return nil }
        let tag = "#" + n
        let title: String = card["title"].string ?? ""
        return title.contains(tag) ? nil : tag
    }
    /// Checks as the card lists them: what failed or is still running first, at most five; the rest
    /// as one line ("10 more skipped"). Fifteen identical "Skipped" rows were a wall, not news.
    /// *total*: how many the PR has when the server sent only some (``checks_total``), and
    /// *restState* the one state all the unsent ones share.
    static func checkRows(_ checks: [JSONValue], cap: Int = 4, total: Int? = nil, restState: String? = nil) -> (shown: [JSONValue], rest: String?) {
        let order = ["failed": 0, "running": 1, "pending": 2, "passed": 3, "skipped": 4]
        let sorted = checks.enumerated().sorted {
            (order[$0.element["state"].string ?? ""] ?? 5, $0.offset) < (order[$1.element["state"].string ?? ""] ?? 5, $1.offset)
        }.map(\.element)
        let all = max(total ?? 0, sorted.count)
        guard all > cap + 1 else { return (sorted, nil) }
        let shown = Array(sorted.prefix(cap)), left = sorted.dropFirst(cap)
        var states = Set(left.map { $0["state"].string ?? "" })
        if all > sorted.count { states.insert(restState ?? "") }
        let word = states.count == 1 && !(states.first ?? "").isEmpty ? " " + (states.first ?? "") : ""
        return (shown, "\(all - shown.count) more\(word)")
    }
    private var stateIcon: String {
        switch card["state"].string {
        case "conflicts": return "exclamationmark.triangle.fill"
        case "failing": return "xmark.octagon.fill"
        case "merged": return "arrow.triangle.merge"
        case "closed": return "xmark.circle.fill"
        case "draft": return "pencil"
        default: return "checkmark.circle.fill"
        }
    }
    private func checkRow(_ check: JSONValue, word: Bool = true) -> some View {
        let state = check["state"].string ?? ""
        let (icon, tint): (String, Color) = {
            switch state {
            case "passed": return ("checkmark.circle.fill", style.good)
            case "failed": return ("xmark.circle.fill", style.bad)
            case "running", "pending": return ("circle.dotted", style.faint)
            default: return ("minus.circle.fill", style.faint)
            }
        }()
        let note = check["note"].string ?? (state == "skipped" ? "didn't run" : state)
        return HStack(spacing: 8) {
            Image(systemName: icon).font(.footnote).foregroundStyle(tint)
            Text(check["name"].string ?? "").font(.footnote.weight(.medium)).foregroundStyle(style.ink)
            if word || check["note"].string != nil {
                Text(note).font(.footnote).foregroundStyle(style.secondary).lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(word ? "" : note)
    }
}

// MARK: - Running work

/// Running work as quiet status lines: the one that needs the user first, a still dot beside the
/// rest. Never a ring, spinner or percent.
struct JobsCard: View {
    let card: ViewCard
    @Environment(\.cardStyle) private var style
    @Environment(\.cardFit) private var fit
    @Environment(\.cardOpenTask) private var openTask
    var body: some View {
        let items = card["items"].array.enumerated().sorted { a, b in
            (a.element["needs_you"].bool == true ? 0 : 1, a.offset) < (b.element["needs_you"].bool == true ? 0 : 1, b.offset)
        }.map(\.element)
        let shown = Array(items.prefix(fit.rows(8)))
        CardBox {
            if let title = card["title"].string { CardHeader(icon: "list.bullet.rectangle", title: title, trailing: "\(items.count)") }
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(shown.enumerated()), id: \.offset) { i, item in
                    if i > 0 { Rectangle().fill(style.hairline).frame(height: 0.7) }
                    row(item)
                }
            }
            MoreRows(total: items.count, shown: shown.count)
        }
    }
    private func row(_ item: JSONValue) -> some View {
        let needs = item["needs_you"].bool == true
        return VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(item["name"].string ?? "").font(.subheadline.weight(.semibold)).foregroundStyle(style.ink).lineLimit(2)
                Spacer(minLength: 8)
                if let t = item["elapsed"].string { Text(t).font(.footnote).foregroundStyle(style.faint).lineLimit(1) }
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if needs { Circle().fill(style.needsYou).frame(width: 6, height: 6).frame(width: 12) } else { CardLiveDot() }
                Text(item["status"].string ?? "").font(.footnote).foregroundStyle(needs ? style.needsYou : style.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if needs, let ask = item["ask"].string {
                if let openTask, let id = item["task_id"].string {
                    CardPillButton(ask, kind: .primary) { openTask(id) }.padding(.top, 6)
                        .accessibilityIdentifier("jobs-ask")
                } else {
                    Text(ask).font(.footnote.weight(.semibold)).foregroundStyle(style.needsYou)
                }
            }
        }
        .padding(.vertical, 12)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Website

struct SiteCard: View {
    let card: ViewCard
    @Environment(\.cardStyle) private var style
    var body: some View {
        CardBox {
            if card["image"] != .null {
                CardPicture(value: card["image"], label: "Screenshot of \(card["title"].string ?? host)")
                    .frame(maxWidth: .infinity)
            }
            HStack(spacing: 10) {
                SourceMark.site
                VStack(alignment: .leading, spacing: 1) {
                    Text(card["title"].string ?? host).font(.subheadline.weight(.semibold)).foregroundStyle(style.ink).lineLimit(1)
                    Text(card["sub"].string ?? host).font(.footnote).foregroundStyle(style.secondary).lineLimit(2)
                }
                Spacer(minLength: 0)
            }
        }
    }
    private var host: String {
        guard let url = card["url"].url else { return "Website" }
        return (url.host ?? "") + (url.path == "/" ? "" : url.path)
    }
}

// MARK: - Mockups

/// Screens a task designed: tiles at their own shape to swipe through, the caption under each
/// picture (never over it). The next steps are the server's buttons under the card.
struct MockupsCard: View {
    let card: ViewCard
    @Environment(\.cardStyle) private var style
    @Environment(\.cardFit) private var fit
    var body: some View {
        let items = card["items"].array
        CardBox {
            // The screens show how many there are and that they scroll: no count, no "Swipe".
            CardHeader(icon: "rectangle.stack", title: card["title"].string ?? "Screens", trailing: nil)
            if let note = card["note"].string, !MockupsCard.restates(note, count: items.count) {
                Text(note).font(.subheadline).foregroundStyle(style.secondary).fixedSize(horizontal: false, vertical: true)
            }
            ScrollView(.horizontal) {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(Array(items.enumerated()), id: \.offset) { i, item in
                        VStack(alignment: .leading, spacing: 6) {
                            CardPicture(value: item["image"], height: fit == .compact ? 170 : 248, corner: 18,
                                        label: item["caption"].string ?? "Screen \(i + 1)", fill: true)
                            if let caption = item["caption"].string {
                                Text(caption).font(.footnote.weight(.medium)).foregroundStyle(style.secondary).lineLimit(2)
                                    .frame(width: 132, alignment: .leading)
                            }
                        }
                    }
                }
            }
            .scrollIndicators(.hidden)
        }
    }
}

extension MockupsCard {
    /// A note that only counts the screens ("Three Home screens, light theme") says what the row shows;
    /// one with something else to say ("Spacing tightened per your note") stays.
    static func restates(_ note: String, count: Int) -> Bool {
        let words = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
        let n = count < words.count ? words[count] : "\(count)"
        let low = note.lowercased()
        return low.range(of: #"^(\#(n)|\#(count))\b"#, options: .regularExpression) != nil && note.count <= 48
    }
}

// MARK: - Host hooks

public struct CardOpenTaskKey: EnvironmentKey { public static let defaultValue: ((String) -> Void)? = nil }
/// Runs one of the card's own server actions from inside the card (a day's Move sits by its overlap).
public struct CardRunActionKey: EnvironmentKey { public static let defaultValue: ((String) -> Void)? = nil }
public extension EnvironmentValues {
    /// Opens a task by id (a running job's "Pick a headline"); nil: the ask shows as words.
    var cardOpenTask: ((String) -> Void)? {
        get { self[CardOpenTaskKey.self] }
        set { self[CardOpenTaskKey.self] = newValue }
    }
    var cardRunAction: ((String) -> Void)? {
        get { self[CardRunActionKey.self] }
        set { self[CardRunActionKey.self] = newValue }
    }
}

/// Links the card already holds, drawn as buttons by the host's row (after the server's actions).
public enum CardLinks {
    public struct Link: Equatable, Identifiable, Sendable {
        public let id: String
        public let label: String
        public let symbol: String?
        public let url: URL
    }
    /// Kinds whose server actions the card draws inside itself (the host row leaves them out).
    public static let inCardActions: Set<String> = ["calendar_day"]

    public static func links(_ card: ViewCard) -> [Link] {
        switch card.kind {
        case "pr":
            guard let url = card["url"].url else { return [] }
            let diff = card["diff_url"].url ?? (url.host?.hasSuffix("github.com") == true ? url.appendingPathComponent("files") : nil)
            return (diff.map { [Link(id: "diff", label: "Diff", symbol: nil, url: $0)] } ?? [])
                + [Link(id: "github", label: url.host?.hasSuffix("github.com") == true ? "GitHub" : "Open", symbol: "arrow.up.right", url: url)]
        case "issue":
            guard let url = card["url"].url else { return [] }
            return [Link(id: "open", label: sourceName(card["source"].string).map { "Open in \($0)" } ?? "Open",
                         symbol: "arrow.up.right", url: url)]
        case "site":
            guard let url = card["url"].url else { return [] }
            let preview = (card["sub"].string ?? "").lowercased().contains("preview")
            return [Link(id: "open", label: preview ? "Open preview" : "Open site", symbol: "arrow.up.right", url: url)]
        default:
            return []
        }
    }
}

extension Array where Element: Hashable {
    func removingDuplicates() -> [Element] { var seen = Set<Element>(); return filter { seen.insert($0).inserted } }
}
