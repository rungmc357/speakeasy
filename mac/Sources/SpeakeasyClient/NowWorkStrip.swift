import SwiftUI
#if os(iOS)
import UIKit
#endif
import SpeakeasyCore

/// Home's quiet island. Opening it reveals one live item at a time, never a second board.
public struct NowWorkStrip: View {
    public let items: [NowItem]
    public let background: [NowBackgroundItem]
    public var onOpen: (NowItem) -> Void
    public var onBackground: (NowBackgroundItem) -> Void
    public var onBoard: () -> Void
    /// The plugin's pill words ("3 need you · 5 running"); nil: counted here.
    public var pill: String?
    /// Home's top line (SPK-259): the strip is only the pill; the host draws `NowWorkStripPanel` over
    /// the thread while it's open (`onExpandedChange`), so opening it never moves the conversation.
    public var dropDown = false
    /// Bumped by the host to fold an open strip (the thread scrolled, a tap outside).
    public var collapseToken = 0
    /// Tells the host the strip opened or folded (to catch a tap outside it).
    public var onExpandedChange: ((Bool) -> Void)?
    @State private var ownExpanded = false
    @State private var pull: CGFloat = 0
    /// Home (iPhone/iPad) drives the drop-down itself: strip and Work panel follow the finger.
    @Environment(\.nowWorkDrop) private var drop
    private var hosted: NowWorkDropControl? { dropDown ? drop : nil }
    private var expanded: Bool {
        get { hosted?.isOpen ?? ownExpanded }
        nonmutating set { if hosted == nil { ownExpanded = newValue } }
    }
    @Environment(\.accessibilityReduceMotion) private var reduceMotion


    public init(items: [NowItem], background: [NowBackgroundItem], onOpen: @escaping (NowItem) -> Void,
                onBackground: @escaping (NowBackgroundItem) -> Void, onBoard: @escaping () -> Void, pill: String? = nil,
                dropDown: Bool = false, collapseToken: Int = 0, onExpandedChange: ((Bool) -> Void)? = nil) {
        self.pill = pill
        self.dropDown = dropDown; self.collapseToken = collapseToken; self.onExpandedChange = onExpandedChange
        self.items = items; self.background = background; self.onOpen = onOpen
        self.onBackground = onBackground; self.onBoard = onBoard
    }
    private var live: [NowItem] { items.filter { [.question, .working, .waiting, .stopped].contains($0.kind) } }
    private var needs: Int { live.filter { $0.kind == .question }.count }
    private var running: Int { live.filter { $0.kind == .working }.count + background.count }
    private var keys: [String] { live.map { "i:" + $0.id } + background.map { "b:" + $0.id } }
    /// What the pill says, read as one control.
    var pillLabel: String {
        if let pill { return pill }
        let counts = running > 0 ? "\(running) running" : "Work"
        return needs > 0 ? "\(counts) · \(needs) needs you" : counts
    }

    public var body: some View {
        if !keys.isEmpty {
            VStack(spacing: 8) {
                ViewThatFits(in: .horizontal) {
                    if let pill {
                        // One line at any volume: the counts, never a cut-off title.
                        HStack(spacing: 8) {
                            Text(pill).fixedSize().foregroundStyle(needs > 0 ? Color.accentColor : Color.primary)
                            Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2.weight(.semibold)).accessibilityHidden(true)
                        }
                    }
                    HStack(spacing: 8) {
                        Text(running > 0 ? "\(running) running" : "Work").fixedSize()
                        if needs > 0 { Text("· \(needs) needs you").foregroundStyle(Color.accentColor).fixedSize() }
                        Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2.weight(.semibold)).accessibilityHidden(true)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(running > 0 ? "\(running) running" : "Work").fixedSize()
                            Spacer(minLength: 0)
                            Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2.weight(.semibold)).accessibilityHidden(true)
                        }
                        if needs > 0 { Text("\(needs) needs you").foregroundStyle(Color.accentColor).fixedSize(horizontal: false, vertical: true) }
                    }.padding(.vertical, 6)
                }
                .font(.footnote.weight(.semibold)).padding(.horizontal, 16).frame(minHeight: 36)
                .contentShape(Capsule())
                .onTapGesture { toggle() }
                .gesture(DragGesture(minimumDistance: 10).onChanged { value in
                    guard abs(value.translation.height) > abs(value.translation.width) else { return }
                    if let hosted { hosted.pullChanged(value.translation.height); return }
                    pull = max(0, min(value.translation.height, 156))
                }.onEnded { value in
                    if let hosted { hosted.pullEnded(value.translation.height, value.predictedEndTranslation.height); return }
                    let vertical = abs(value.translation.height) > abs(value.translation.width)
                    let open = vertical && value.translation.height > 120
                    withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.9)) {
                        if open { expanded = false }
                        else if vertical { expanded = value.translation.height > 35 }
                        pull = 0
                    }
                    if open { onBoard() }
                })
                // One control: its words, not a second "Go Down" element for the chevron. Combined
                // (not ignored) so the element keeps its children's on-screen frame under the
                // scroll-edge bar; an ignored container reported a frame below the drawn pill.
                .accessibilityElement(children: .combine)
                .accessibilityLabel(pillLabel)
                .accessibilityAddTraits(.isButton)
                .accessibilityAction { toggle() }
                .accessibilityAction(named: "Open work") { if let hosted { hosted.openAll() } else { onBoard() } }
                .accessibilityAction(named: "Close work") { if let hosted { hosted.close() } else if expanded { toggle() } }
                .accessibilityIdentifier("home-work-capsule")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                #if os(iOS)
                // Typing needs the thread: an open strip folds back to the pill when the keyboard
                // comes up, so it never covers the conversation above the composer.
                .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
                    if expanded { withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.9)) { expanded = false } }
                }
                #endif
                if !dropDown && (expanded || pull > 0) {
                    NowWorkStripPanel(items: items, background: background, onOpen: onOpen, onBackground: onBackground,
                                      onBoard: onBoard, onClose: { expanded = false })
                    .frame(height: expanded ? nil : pull, alignment: .top)
                    .clipped()
                }
            }
            .frame(maxWidth: .infinity)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: expanded ? CleanGrid.cardRadius : 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: expanded ? CleanGrid.cardRadius : 18, style: .continuous).strokeBorder(Color.primary.opacity(0.08)))
            .onChange(of: collapseToken) { _, _ in
                if expanded { withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.9)) { expanded = false } }
            }
            .onChange(of: expanded) { _, open in onExpandedChange?(open) }
        }
    }
    private func toggle() {
        if let hosted { hosted.tap(); return }
        withAnimation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 0.88)) { expanded.toggle() }
    }
}


/// Home's drop-down, driven by its host (SPK-231 top tracker): the pill reports taps and live pulls;
/// the host owns the detents (pill · strip · all work) and draws them over the thread.
public struct NowWorkDropControl {
    public var isOpen: Bool
    public var tap: () -> Void
    public var pullChanged: (CGFloat) -> Void
    /// Translation and predicted end translation.
    public var pullEnded: (CGFloat, CGFloat) -> Void
    public var openAll: () -> Void
    public var close: () -> Void
    public init(isOpen: Bool, tap: @escaping () -> Void, pullChanged: @escaping (CGFloat) -> Void,
                pullEnded: @escaping (CGFloat, CGFloat) -> Void, openAll: @escaping () -> Void, close: @escaping () -> Void) {
        self.isOpen = isOpen; self.tap = tap; self.pullChanged = pullChanged; self.pullEnded = pullEnded
        self.openAll = openAll; self.close = close
    }
}

private struct NowWorkDropKey: EnvironmentKey { static let defaultValue: NowWorkDropControl? = nil }
public extension EnvironmentValues {
    var nowWorkDrop: NowWorkDropControl? {
        get { self[NowWorkDropKey.self] }
        set { self[NowWorkDropKey.self] = newValue }
    }
}

/// One job as a slim row: glyph tile, title, one status line, a trailing state word.
public struct NowWorkRow: View {
    public enum Tone: Sendable { case needs, running, quiet }
    public let title: String
    public let line: String
    public let state: String
    public let tone: Tone
    public let image: String?
    public let symbol: String

    public init(title: String, line: String, state: String, tone: Tone, image: String?, symbol: String) {
        self.title = title; self.line = line; self.state = state; self.tone = tone
        self.image = image; self.symbol = symbol
    }
    public init(item: NowItem) {
        self.init(title: item.title, line: item.line, state: Self.state(item), tone: Self.tone(item),
                  image: item.thumbs.first, symbol: Self.symbol(item))
    }
    public init(background item: NowBackgroundItem) {
        let scheduled = NowWorkSections.isScheduled(item)
        self.init(title: item.title, line: item.line,
                  state: scheduled ? "Scheduled" : (Self.verb(item.line) ?? item.sourceLabel ?? "Running"),
                  tone: scheduled ? .quiet : .running, image: nil, symbol: scheduled ? "clock" : "square.stack")
    }

    /// "Searching 6 places" → "Searching"; nil when the line doesn't start with a verb.
    public static func verb(_ line: String) -> String? {
        guard let first = line.split(separator: " ").first.map(String.init)?.trimmingCharacters(in: .punctuationCharacters),
              first.count > 4, first.lowercased().hasSuffix("ing"), first.first?.isUppercase == true else { return nil }
        return first
    }
    public static func state(_ item: NowItem) -> String {
        switch item.kind {
        case .question: return "Needs your OK"
        case .working: return verb(item.line) ?? "Working"
        case .waiting: return "Waiting"
        case .stopped: return "Stopped"
        case .newAnswer, .seenAnswer: return item.canceled ? "Canceled" : "Done"
        }
    }
    static func tone(_ item: NowItem) -> Tone {
        switch item.kind {
        case .question: return .needs
        case .working, .waiting: return .running
        default: return .quiet
        }
    }
    static func symbol(_ item: NowItem) -> String {
        switch item.kind {
        case .question: return "hand.raised"
        case .stopped: return "exclamationmark.triangle"
        case .waiting: return "hourglass"
        case .working: return "sparkles"
        default: return "checkmark"
        }
    }

    public var body: some View {
        HStack(spacing: CleanGrid.glyphGap) {
            tile
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.subheadline.weight(.semibold)).lineLimit(1)
                if !line.isEmpty {
                    Text(line).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(state).font(.footnote.weight(tone == .needs ? .semibold : .regular))
                .foregroundStyle(tone == .needs ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(.secondary))
                .lineLimit(1).fixedSize()
        }
        .padding(.vertical, 8)
        .frame(minHeight: NowWorkStripPanel.rowHeight)
        .contentShape(Rectangle())
    }

    @ViewBuilder private var tile: some View {
        let shape = RoundedRectangle(cornerRadius: 7, style: .continuous)
        let glyph = Image(systemName: symbol).font(.footnote.weight(.medium))
            .foregroundStyle(tone == .needs ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(.secondary))
        Group {
            if let image, let url = URL(string: image) {
                AsyncImage(url: url) { phase in
                    if let picture = phase.image { picture.resizable().scaledToFill() } else { glyph }
                }
            } else { glyph }
        }
        .frame(width: CleanGrid.glyphTile, height: CleanGrid.glyphTile)
        .background(shape.fill(Color.primary.opacity(0.07)))
        .clipShape(shape)
        .accessibilityHidden(true)
    }
}

/// What the drop-down lists, in the Work board's sections.
public struct NowWorkSections {
    public struct Section: Identifiable {
        public var id: String { title }
        public let title: String
        public let items: [NowItem]
        public let background: [NowBackgroundItem]
        public var count: Int { items.count + background.count }
    }
    public let sections: [Section]
    static func isScheduled(_ item: NowBackgroundItem) -> Bool {
        let s = item.sourceLabel?.lowercased() ?? ""
        return s.contains("schedul") || s.contains("cron")
    }
    public init(items: [NowItem], background: [NowBackgroundItem]) {
        let all = [
            Section(title: "Needs you", items: items.filter { $0.kind == .question || $0.kind == .stopped }, background: []),
            Section(title: "Running", items: items.filter { $0.kind == .working || $0.kind == .waiting },
                    background: background.filter { !Self.isScheduled($0) }),
            Section(title: "Scheduled", items: [], background: background.filter(Self.isScheduled)),
            Section(title: "Done today", items: items.filter { $0.kind == .newAnswer || $0.kind == .seenAnswer }, background: []),
        ]
        sections = all.filter { $0.count > 0 }
    }
    /// The strip: live work only (Needs you, Running, Scheduled), in that order.
    public var live: Section {
        let l = sections.filter { $0.title != "Done today" }
        return Section(title: "", items: l.flatMap(\.items), background: l.flatMap(\.background))
    }
}

/// The open strip: live jobs as one continuous vertical list of slim rows (no pages, no dots); it
/// scrolls past `maxRows`. Inside the pill's card (Mac), or the rows of Home's drop-down (iPhone/iPad).
public struct NowWorkStripPanel: View {
    public let items: [NowItem]
    public let background: [NowBackgroundItem]
    public var onOpen: (NowItem) -> Void
    public var onBackground: (NowBackgroundItem) -> Void
    public var onBoard: () -> Void
    /// Fold the strip (before opening a job).
    public var onClose: () -> Void
    public var maxRows: Int

    public init(items: [NowItem], background: [NowBackgroundItem], onOpen: @escaping (NowItem) -> Void,
                onBackground: @escaping (NowBackgroundItem) -> Void, onBoard: @escaping () -> Void,
                onClose: @escaping () -> Void, maxRows: Int = 4) {
        self.items = items; self.background = background; self.onOpen = onOpen
        self.onBackground = onBackground; self.onBoard = onBoard; self.onClose = onClose; self.maxRows = maxRows
    }
    public static let rowHeight: CGFloat = 56
    public static func height(rows: Int, maxRows: Int = 4) -> CGFloat {
        // Past the limit, show a half row so the list reads as scrollable.
        let shown = rows > maxRows ? CGFloat(maxRows) + 0.5 : CGFloat(max(rows, 1))
        return shown * rowHeight + 4
    }
    public static func rows(items: [NowItem], background: [NowBackgroundItem]) -> Int {
        NowWorkSections(items: items, background: background).live.count
    }

    public var body: some View {
        let live = NowWorkSections(items: items, background: background).live
        ScrollView(.vertical) {
            NowWorkRowList(items: live.items, background: live.background,
                           onOpen: { onClose(); onOpen($0) }, onBackground: { onClose(); onBackground($0) })
                .padding(.top, 2)
        }
        .scrollIndicators(.hidden)
        // Always bounces: on Home, pulling the strip past its top opens all work.
        .scrollBounceBehavior(.always, axes: .vertical)
        .frame(height: Self.height(rows: live.count, maxRows: maxRows))
        .accessibilityIdentifier("home-work-strip")
    }
}

/// Rows with hairlines between them (inset past the glyph), each a button that opens its job.
public struct NowWorkRowList: View {
    let items: [NowItem]
    let background: [NowBackgroundItem]
    let onOpen: (NowItem) -> Void
    let onBackground: (NowBackgroundItem) -> Void
    public init(items: [NowItem], background: [NowBackgroundItem],
                onOpen: @escaping (NowItem) -> Void, onBackground: @escaping (NowBackgroundItem) -> Void) {
        self.items = items; self.background = background; self.onOpen = onOpen; self.onBackground = onBackground
    }
    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.element.id) { n, item in
                Button { onOpen(item) } label: { NowWorkRow(item: item) }
                    .buttonStyle(.plain)
                    .overlay(alignment: .top) { if n > 0 { divider } }
                    .accessibilityIdentifier("home-work-item")
            }
            ForEach(Array(background.enumerated()), id: \.element.id) { n, item in
                Button { onBackground(item) } label: { NowWorkRow(background: item) }
                    .buttonStyle(.plain)
                    .overlay(alignment: .top) { if n > 0 || !items.isEmpty { divider } }
                    .accessibilityIdentifier("home-work-item")
            }
        }
        .padding(.horizontal, CleanGrid.margin)
    }
    private var divider: some View {
        Rectangle().fill(Color.primary.opacity(0.1)).frame(height: 0.5)
            .padding(.leading, CleanGrid.glyphTile + CleanGrid.glyphGap)
    }
}

/// All work: one continuous vertical list in the board's sections (Needs you · Running · Scheduled ·
/// Done today). The host gives it its glass, height, scrolling and handle.
public struct NowWorkAllList: View {
    let items: [NowItem]
    let background: [NowBackgroundItem]
    let onOpen: (NowItem) -> Void
    let onBackground: (NowBackgroundItem) -> Void
    public init(items: [NowItem], background: [NowBackgroundItem],
                onOpen: @escaping (NowItem) -> Void, onBackground: @escaping (NowBackgroundItem) -> Void) {
        self.items = items; self.background = background; self.onOpen = onOpen; self.onBackground = onBackground
    }
    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(NowWorkSections(items: items, background: background).sections) { section in
                HStack {
                    Text(section.title).font(.footnote.weight(.semibold))
                        .foregroundStyle(section.title == "Needs you" ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(.secondary))
                    Spacer()
                    Text("\(section.count)").font(.footnote).foregroundStyle(.tertiary)
                }
                .padding(.horizontal, CleanGrid.margin).padding(.top, 12).padding(.bottom, 2)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(section.title)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("home-work-section")
                NowWorkRowList(items: section.items, background: section.background,
                               onOpen: onOpen, onBackground: onBackground)
            }
        }
        .padding(.bottom, CleanGrid.s)
    }
}
