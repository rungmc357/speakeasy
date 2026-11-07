import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// History › Board: the Hermes Kanban boards of the paired Hermes, read-only (servers with `boards`).
// The sidebar's Board row opens the picked board on the right as four columns (Ready, Running,
// Blocked, Done); a card opens the session it ran in. Refreshes every few seconds while open.

enum BoardLook {
    /// The count badge for what needs you (a count only; the label itself is `NeedsYouMark`): one
    /// deep red that keeps white text readable in light and dark.
    static let needsYou = Color(red: 0.72, green: 0.15, blue: 0.13)
    static func tint(_ column: KanbanCard.Column) -> Color {
        switch column {
        case .ready: return Color.primary.opacity(0.55)
        case .running: return Live.brass
        case .blocked: return Live.red
        case .done: return Live.green
        }
    }
    static func symbol(_ column: KanbanCard.Column) -> String {
        switch column {
        case .ready: return "tray.full"
        case .running: return "circle.dotted"
        case .blocked: return "hand.raised.fill"
        case .done: return "checkmark.circle.fill"
        }
    }
}

/// The sidebar's entry: "Board", with what's going on.
struct BoardSidebarRow: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var board: BoardModel
    @State private var hover = false

    var body: some View {
        let on = store.showingBoard
        Button { store.openBoard() } label: {
            HStack(spacing: 10) {
                Circle().fill(on ? Live.brass.opacity(0.22) : Color.primary.opacity(0.06)).frame(width: 26, height: 26)
                    .overlay(Image(systemName: "rectangle.split.3x1.fill").font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(on ? Live.brass : Color.primary.opacity(0.6)))
                VStack(alignment: .leading, spacing: 1) {
                    Text("Board").font(.system(size: 12.5, weight: .semibold))
                    Text(subtitle).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
                if board.needsYouCount > 0 {
                    Text("\(board.needsYouCount)").font(.system(size: 10.5, weight: .bold).monospacedDigit())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6).frame(height: 17)
                        .background(Capsule().fill(BoardLook.needsYou))
                        .accessibilityLabel("\(board.needsYouCount) need you")
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(on ? HistoryLook.selected : hover ? HistoryLook.hover : .clear))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(on ? Live.brass.opacity(0.32) : .clear, lineWidth: 0.75))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Board, \(subtitle)")
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier("history-board")
    }

    private var subtitle: String {
        guard board.loaded else { return "Your Hermes Kanban work" }
        guard let current = board.current else { return board.boards.isEmpty ? "No boards yet" : "Pick a board" }
        return "\(current.name) · \(current.subtitle)"
    }
}

/// The right side while Board is open.
struct BoardPane: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var board: BoardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header.padding(.horizontal, 28).padding(.top, 34)
            content
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .onAppear { board.startRefreshing() }
        .onDisappear { board.stopRefreshing() }
    }

    private var header: some View {
        HStack(alignment: .bottom, spacing: 10) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "rectangle.split.3x1.fill").font(.system(size: 10, weight: .bold))
                    Text("BOARD").tracking(1.0)
                }
                .font(.system(size: 10.5, weight: .bold)).foregroundStyle(Live.brass)
                Text(board.page?.name ?? board.current?.name ?? "Board").font(.system(size: 24, weight: .bold))
                    .lineLimit(1)
                Text("Your Hermes Kanban work. Open a card to see the chat it ran in.")
                    .font(.system(size: 12.5, weight: .medium)).foregroundStyle(Color.primary.opacity(0.6))
            }
            Spacer()
            if board.boards.count > 1 { picker }
        }
    }

    private var picker: some View {
        Menu {
            ForEach(board.boards) { b in
                Button { board.select(b.slug) } label: {
                    if b.slug == board.selected { Label("\(b.name) · \(b.subtitle)", systemImage: "checkmark") }
                    else { Text("\(b.name) · \(b.subtitle)") }
                }
            }
        } label: {
            HStack(spacing: 6) {
                Text("Switch board").font(.system(size: 12, weight: .semibold))
                Image(systemName: "chevron.up.chevron.down").font(.system(size: 9, weight: .bold))
            }
            .padding(.horizontal, 12).frame(height: 28)
            .background(Capsule().fill(Color.primary.opacity(0.07)))
        }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .accessibilityIdentifier("board-picker")
    }

    @ViewBuilder private var content: some View {
        if let page = board.page {
            BoardColumnsRow(store: store, board: board, page: page)
                .accessibilityIdentifier("board-columns")
        } else if let error = board.error {
            VStack(spacing: 8) {
                Text(error).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center)
                Button("Try again") { Task { await board.reload() } }.buttonStyle(.plain)
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(Live.brassText)
            }
            .frame(maxWidth: .infinity).padding(.top, 40)
        } else if board.loaded && board.boards.isEmpty {
            VStack(spacing: 10) {
                Image(systemName: "rectangle.split.3x1").font(.system(size: 30, weight: .light)).foregroundStyle(Live.brass.opacity(0.85))
                Text("No boards yet").font(.system(size: 14, weight: .semibold))
                Text("When your Hermes plans work on a Kanban board, it shows up here.")
                    .font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 360)
            }
            .frame(maxWidth: .infinity).padding(.top, 50)
        } else {
            HStack(spacing: 8) { LiveSpinner(size: 12); Text("Loading…").font(.system(size: 12)).foregroundStyle(.secondary) }
                .frame(maxWidth: .infinity).padding(.top, 40)
        }
    }
}

/// The four columns. They share the pane when they fit; on a narrower window the row scrolls a column
/// at a time with the next one peeking in, the cut-off edge fades, an arrow at that edge shows the next
/// column, and the board opens scrolled so a card that needs you is in view. When that column is
/// scrolled away, the arrow on its side becomes a red "1 needs you" button that brings it back.
struct BoardColumnsRow: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var board: BoardModel
    let page: KanbanBoardPage
    @State private var leading: Int?

    static let inset: CGFloat = 28, spacing: CGFloat = 12, minColumn: CGFloat = 150, peek: CGFloat = 44
    /// `--history-shots` only: scroll back to Ready.
    static let scrollToStartForShot = Notification.Name("speakeasy.board.scrollToStartForShot")
    static let scrollToEndForShot = Notification.Name("speakeasy.board.scrollToEndForShot")

    var body: some View {
        GeometryReader { geo in
            let layout = KanbanBoardLayout(width: geo.size.width, minColumn: Self.minColumn, spacing: Self.spacing,
                                           inset: Self.inset, peek: Self.peek)
            let first = layout.inView(leading: leading ?? 0)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: Self.spacing) {
                    ForEach(0..<page.columns.count, id: \.self) { i in
                        BoardColumnView(store: store, column: page.columns[i], now: board.now)
                            .frame(width: layout.columnWidth)
                            .id(i)
                    }
                }
                .scrollTargetLayout()
                .padding(.bottom, 20)
                .frame(minHeight: geo.size.height, alignment: .top)
            }
            .contentMargins(.leading, Self.inset, for: .scrollContent)
            .contentMargins(.trailing, layout.trailingMargin, for: .scrollContent)
            .scrollTargetBehavior(.viewAligned)
            // Leading anchor: without one, SwiftUI may scroll only as far as needed to show that column,
            // and the next one along is usually already in view, so the arrow wouldn't move (iPad did this).
            .scrollPosition(id: $leading, anchor: .leading)
            .scrollDisabled(!layout.scrolls)
            .mask(fadeMask(leading: first.lowerBound > 0, trailing: first.upperBound < KanbanBoardLayout.count))
            .overlay(alignment: .leading) {
                if layout.scrolls, first.lowerBound > 0 {
                    edgeButton(.leading, layout: layout, from: first.lowerBound)
                }
            }
            .overlay(alignment: .trailing) {
                if layout.scrolls, first.upperBound < KanbanBoardLayout.count {
                    edgeButton(.trailing, layout: layout, from: first.lowerBound)
                }
            }
            .onAppear { open(layout, force: true) }
            .onChange(of: layout.visible) { _, _ in open(layout, force: true) }
            .onChange(of: page.slug) { _, _ in open(layout, force: true) }
            .onChange(of: page.attentionIndex) { _, _ in open(layout, force: false) }
            .onReceive(NotificationCenter.default.publisher(for: Self.scrollToStartForShot)) { _ in leading = 0 }
            .onReceive(NotificationCenter.default.publisher(for: Self.scrollToEndForShot)) { _ in leading = layout.lastLeading }
        }
    }

    /// Scroll to where the board should open (a card that needs you in view) — always for a new board
    /// or window width, otherwise only when that card would be out of view.
    private func open(_ layout: KanbanBoardLayout, force: Bool) {
        let target = layout.openingLeading(for: page)
        if !force, layout.offscreenAttention(leading: leading ?? 0, page: page) == nil { return }
        // After this layout pass: setting the position while the row is being laid out is ignored.
        DispatchQueue.main.async { leading = target }
    }

    private func fadeMask(leading: Bool, trailing: Bool) -> some View {
        HStack(spacing: 0) {
            LinearGradient(colors: [.black.opacity(0), .black], startPoint: .leading, endPoint: .trailing)
                .frame(width: leading ? Self.inset + 8 : 0)
            Rectangle().fill(.black)
            LinearGradient(colors: [.black, .black.opacity(0)], startPoint: .leading, endPoint: .trailing)
                .frame(width: trailing ? Self.peek + 8 : 0)
        }
    }

    @ViewBuilder
    private func edgeButton(_ edge: KanbanBoardLayout.Edge, layout: KanbanBoardLayout, from start: Int) -> some View {
        let attention = layout.offscreenAttention(leading: start, page: page) == edge
        let next = edge == .leading ? max(0, start - 1) : min(layout.lastLeading, start + 1)
        let reveal = edge == .leading ? start - 1 : start + layout.visible
        let goal = attention ? layout.openingLeading(for: page) : next
        let name = page.columns[max(0, min(KanbanBoardLayout.count - 1, reveal))].title
        Button { withAnimation(.easeInOut(duration: 0.3)) { leading = goal } } label: {
            if attention {
                HStack(spacing: 4) {
                    if edge == .leading { Image(systemName: "chevron.left").font(.system(size: 9, weight: .bold)) }
                    Text("\(page.needsYouCount) need\(page.needsYouCount == 1 ? "s" : "") you")
                        .font(.system(size: 11, weight: .bold))
                    if edge == .trailing { Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)) }
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 10).frame(height: 26)
                .background(Capsule().fill(BoardLook.needsYou).shadow(color: .black.opacity(0.18), radius: 4, y: 1))
            } else {
                Image(systemName: edge == .leading ? "chevron.left" : "chevron.right")
                    .font(.system(size: 11, weight: .bold)).foregroundStyle(Color.primary.opacity(0.75))
                    .frame(width: 28, height: 28)
                    .background(Circle().fill(.regularMaterial).shadow(color: .black.opacity(0.15), radius: 4, y: 1))
                    .overlay(Circle().strokeBorder(Live.hairline, lineWidth: 0.5))
            }
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 8)
        // The red pointer sits level with the column titles so it never covers a card; the plain arrow
        // sits mid-height.
        .padding(.top, attention ? 4 : 0)
        .frame(maxHeight: .infinity, alignment: attention ? .top : .center)
        .help(attention ? "Show the card that needs you" : "Show \(name)")
        .accessibilityLabel(attention ? "Show the card that needs you, in \(page.column(.blocked).title)" : "Show \(name)")
        .accessibilityIdentifier(attention ? "board-needs-you-jump" : edge == .leading ? "board-scroll-back" : "board-scroll-more")
    }
}

struct BoardColumnView: View {
    @ObservedObject var store: HistoryStore
    let column: KanbanColumn
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: BoardLook.symbol(column.key)).font(.system(size: 10.5, weight: .bold))
                    .foregroundStyle(BoardLook.tint(column.key))
                Text(column.title.uppercased()).font(.system(size: 10.5, weight: .bold)).tracking(0.9)
                Text("\(column.count)").font(.system(size: 10.5, weight: .semibold).monospacedDigit()).foregroundStyle(.secondary)
                Spacer()
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(column.title), \(column.count)")
            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: 8) {
                    if column.cards.isEmpty {
                        Text(column.key.emptyLine).font(.system(size: 11.5)).foregroundStyle(.tertiary)
                            .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                            .padding(.horizontal, 8).frame(maxWidth: .infinity).padding(.vertical, 18)
                            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(Live.hairline, style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
                    }
                    ForEach(column.cards) { card in
                        BoardCardView(card: card, now: now) { id in store.open(id) }
                    }
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.035)))
        .accessibilityIdentifier("board-column-\(column.key.rawValue)")
    }
}

struct BoardCardView: View {
    let card: KanbanCard
    let now: Date
    let open: (String) -> Void
    @State private var hover = false

    var body: some View {
        Button { if let id = card.sessionID { open(id) } } label: {
            VStack(alignment: .leading, spacing: 6) {
                if card.needsYou { NeedsYouMark() }
                Text(card.title).font(.system(size: 12.5, weight: .semibold)).multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .opacity(card.column == .done ? 0.8 : 1)
                if let reason = card.reason {
                    Text(reason).font(.system(size: 11.5)).foregroundStyle(Color.primary.opacity(0.72)).lineLimit(4)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let summary = card.summary {
                    Text(summary).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let note = card.note {
                    Text(note).font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
                HStack(spacing: 5) {
                    if card.column == .running { LiveSpinner(size: 9) }
                    Text(card.timeLine(now: now)).font(.system(size: 11, weight: .medium).monospacedDigit())
                        .foregroundStyle(card.column == .running ? Live.brassText : Color.secondary)
                    Spacer(minLength: 0)
                    if card.sessionID != nil {
                        Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(hover && card.sessionID != nil ? Live.card.opacity(0.7) : Live.card))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(card.needsYou ? NeedsYouMark.color.opacity(0.45) : Live.hairline, lineWidth: card.needsYou ? 1 : 0.5))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .allowsHitTesting(card.sessionID != nil)
        .onHover { hover = $0 }
        .help(card.sessionID != nil ? "Open the chat this card ran in" : "")
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibility)
        .accessibilityIdentifier("board-card-\(card.id)")
    }

    private var accessibility: String {
        var parts = [card.title]
        if card.needsYou { parts.insert("Needs you", at: 0) }
        if let reason = card.reason { parts.append(reason) }
        parts.append(card.timeLine(now: now))
        return parts.joined(separator: ", ")
    }
}
