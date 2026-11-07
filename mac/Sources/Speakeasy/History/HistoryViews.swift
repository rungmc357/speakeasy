import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// The history window: every session and task on the left (Today / Yesterday / Earlier, search,
// a source filter, New chat), the open one on the right (its messages, tool calls folded under
// each answer, its helpers, anything it's waiting on), and a composer that types or speaks a
// message to that session by id. Same language as the panel's job page: warm near-black, brass,
// soft cards, a mood glow behind the title.

enum HistoryLook {
    static let window = Color.adaptive(dark: Color(red: 0.118, green: 0.110, blue: 0.102), light: Color(hex: 0xfbf8f2)) // #1e1c1a / warm paper
    static let sidebar = Color.adaptive(dark: Color(red: 0.090, green: 0.084, blue: 0.078), light: Color(hex: 0xf3ede2)) // #171514
    static let field = Color.primary.opacity(0.06)
    static let hover = Color.primary.opacity(0.045)
    static let selected = JobLook.brass.opacity(0.16)
    static let readable: CGFloat = 700
    static let sidebarWidth: CGFloat = 288

    static func mood(_ s: HistorySession.State) -> LiveMood {
        switch s {
        case .running: return .working
        case .needsYou: return .needsYou
        case .idle: return .quiet
        }
    }

    static func word(_ s: HistorySession.State) -> String {
        switch s { case .running: return "Working"; case .needsYou: return "Needs you"; case .idle: return "Conversation" }
    }

    /// "3:12 PM" today, "Tue" this week, "Oct 3" this year, "Oct 3, 2025" before.
    static func short(_ date: Date?, now: Date) -> String {
        guard let date else { return "" }
        let cal = Calendar.current
        let f = DateFormatter()
        if cal.isDate(date, inSameDayAs: now) { f.timeStyle = .short; f.dateStyle = .none }
        else if let days = cal.dateComponents([.day], from: cal.startOfDay(for: date), to: cal.startOfDay(for: now)).day, days < 7 {
            f.setLocalizedDateFormatFromTemplate("EEE")
        } else if cal.component(.year, from: date) == cal.component(.year, from: now) {
            f.setLocalizedDateFormatFromTemplate("MMM d")
        } else { f.setLocalizedDateFormatFromTemplate("MMM d, yyyy") }
        return f.string(from: date)
    }

    static func oneLine(_ s: String) -> String {
        s.split(whereSeparator: \.isNewline).joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }

    /// "Tue 3:12 PM".
    static func long(_ date: Date?, now: Date) -> String? {
        guard let date else { return nil }
        let f = DateFormatter()
        f.setLocalizedDateFormatFromTemplate(Calendar.current.isDate(date, inSameDayAs: now) ? "h:mm a" : "EEE MMM d h:mm a")
        return f.string(from: date)
    }
}

struct HistoryRootView: View {
    @ObservedObject var store: HistoryStore
    var voice: VoicePanelModel? = nil
    @State private var earlierOpen = false

    var body: some View {
        GeometryReader { geo in
            // Home (SPK-124 phase 4b): sidebar · Now · conversation; under 1,000 wide, Now · conversation.
            let wide = MacNowLayout.showsSidebar(width: geo.size.width, nowSupported: store.nowSupported)
            columns(sidebar: wide, width: geo.size.width)
                .overlay(alignment: .topLeading) {
                    if !wide {
                        Button { earlierOpen.toggle() } label: {
                            Label("Earlier", systemImage: "sidebar.left")
                        }
                        .buttonStyle(.borderless).padding(.leading, 84).padding(.top, 9)
                        .accessibilityIdentifier("mac-now-earlier")
                        .popover(isPresented: $earlierOpen, arrowEdge: .bottom) {
                            HistorySidebar(store: store).frame(width: 288, height: 560)
                                .background(HistoryLook.sidebar)
                        }
                    }
                }
        }
    }

    private var divider: some View {
        Rectangle().fill(Color.adaptive(dark: Color.black.opacity(0.45), light: Color.black.opacity(0.08))).frame(width: 1)
    }

    private func columns(sidebar: Bool, width: CGFloat) -> some View {
        HStack(spacing: 0) {
            if sidebar {
                HistorySidebar(store: store)
                    .frame(width: store.nowSupported ? 260 : HistoryLook.sidebarWidth)
                    .background(HistoryLook.sidebar)
                divider
            }
            if store.nowSupported {
                MacNowColumn(now: MacNowModel.shared, store: store, voice: voice)
                    .frame(width: sidebar ? 420 : MacNowLayout.nowWidth(windowWidth: width))
                    .background(HistoryLook.sidebar.opacity(0.5))
                divider
            }
            HistoryDetailPane(store: store)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(HistoryLook.window)
        }
        .ignoresSafeArea(.container, edges: .top)   // under the transparent title bar
        .foregroundStyle(.primary)
        .background(HistoryLook.window)
        .background(shortcuts)
    }

    /// ⌘F search (⌘N lives on the New chat button).
    private var shortcuts: some View {
        Button("") {
            if store.nowSupported { earlierOpen = true }
            store.focusSearch += 1
        }.keyboardShortcut("f", modifiers: .command).opacity(0).allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

// MARK: - Sidebar

struct HistorySidebar: View {
    @ObservedObject var store: HistoryStore
    @FocusState private var searchFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header.padding(.top, 34).padding(.horizontal, 14)
            search.padding(.horizontal, 12).padding(.top, 12)
            filterRow.padding(.horizontal, 14).padding(.top, 9).padding(.bottom, 6)
            if store.supported && store.schedulesSupported {
                ScheduledSidebarRow(store: store, schedules: store.schedules).padding(.horizontal, 6).padding(.bottom, 6)
            }
            if store.supported && store.boardsSupported {
                BoardSidebarRow(store: store, board: store.board).padding(.horizontal, 6).padding(.bottom, 6)
            }
            if store.supported && store.filesSupported {
                FilesSidebarRow(store: store, files: store.files).padding(.horizontal, 6).padding(.bottom, 6)
            }
            Rectangle().fill(Live.hairline).frame(height: 1)
            if !store.supported { updateNote } else { listBody }
        }
        .overlay(alignment: .bottom) { undoToast }
        .onChange(of: store.focusSearch) { _, _ in searchFocused = true }
    }

    /// "Archived <title> · Undo" after an archive (6 s), or why a change didn't go through.
    @ViewBuilder private var undoToast: some View {
        if let undo = store.undoArchive {
            HStack(spacing: 10) {
                Text("Archived “\(undo.title)”").font(.system(size: 11.5)).lineLimit(1).truncationMode(.middle)
                Button("Undo") { store.undoLastArchive() }.buttonStyle(.plain)
                    .font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Live.brassText)
                    .accessibilityIdentifier("history-undo-archive")
            }
            .padding(.horizontal, 12).frame(height: 30)
            .background(Capsule().fill(.regularMaterial))
            .padding(.bottom, 12)
            .transition(.move(edge: .bottom).combined(with: .opacity))
        } else if let error = store.actionError {
            Text(error).font(.system(size: 11)).foregroundStyle(Live.red).padding(8)
                .background(Capsule().fill(.regularMaterial)).padding(.bottom, 12)
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("History").font(.system(size: 15, weight: .bold))
            Spacer()
            HistoryIconButton(symbol: "square.and.pencil", help: "New chat (⌘N)", prominent: store.target == .newChat) { store.newChat() }
                .keyboardShortcut("n", modifiers: .command)
                .accessibilityIdentifier("history-new-chat")
        }
    }

    private var search: some View {
        HStack(spacing: 7) {
            Image(systemName: "magnifyingglass").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
            TextField("Search sessions and messages", text: $store.searchText)
                .textFieldStyle(.plain).font(.system(size: 12.5))
                .focused($searchFocused)
                .onExitCommand { store.searchText = ""; searchFocused = false }
                .accessibilityIdentifier("history-search")
            if !store.searchText.isEmpty {
                Button { store.searchText = "" } label: {
                    Image(systemName: "xmark.circle.fill").font(.system(size: 12)).foregroundStyle(.tertiary)
                }
                .buttonStyle(.plain).help("Clear the search")
            } else {
                Text("⌘F").font(.system(size: 10, weight: .medium)).foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 10).frame(height: 30)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(HistoryLook.field))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
            .strokeBorder(searchFocused ? Live.brass.opacity(0.55) : Live.hairline, lineWidth: searchFocused ? 1 : 0.5))
    }

    private var filterRow: some View {
        HStack(spacing: 8) {
            // Only sources the person has sessions from; no menu at all when everything is from one place.
            if store.list.showsSourcePicker { sourceMenu }
            Spacer()
            if !store.list.activeQuery.isEmpty, !store.list.loading {
                let q = store.list.activeQuery
                Text(store.list.sessions.isEmpty ? "No matches" : "\(store.list.sessions.count)\(store.list.reachedEnd ? "" : "+") for “\(q)”")
                    .font(.system(size: 10.5)).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
            }
            if store.list.offersAutomatedMenu { automatedMenu }
        }
    }

    /// One small filter icon at the end of the row: a menu of what automated work the list shows
    /// (board workers, scheduled jobs, background helpers), remembered on this Mac. Filled and
    /// brass while something differs from the standard choice.
    private var automatedMenu: some View {
        let shown = store.list.shown
        return Menu {
            Section(HistoryAutomation.title) {
                ForEach(store.list.automatedMenu) { kind in
                    Toggle(isOn: Binding(get: { store.list.shown[kind.id] }, set: { store.setShown(kind.id, $0) })) {
                        Label(kind.label, systemImage: HistoryAutomation.symbol(kind: kind.id))
                    }
                    .accessibilityIdentifier("history-automated-\(kind.id)")
                }
            }
        } label: {
            Image(systemName: HistoryAutomation.symbol(shown))
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(shown.isStandard ? Color.secondary : Live.brassText)
                .frame(width: 22, height: 22)
                .contentShape(Rectangle())
        }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .help(HistoryAutomation.help)
        .accessibilityLabel(HistoryAutomation.accessibilityLabel(shown))
        .accessibilityIdentifier("history-automated-menu")
    }

    private var sourceMenu: some View {
            Menu {
                Button { store.setArchived(false); store.setSource(nil) } label: {
                    Label("All sources", systemImage: store.list.source == nil && !store.list.archived ? "checkmark" : "")
                }
                Divider()
                ForEach(store.list.shownFilters) { s in
                    Button { store.setArchived(false); store.setSource(s.id) } label: {
                        Label(s.label, systemImage: store.list.source == s.id && !store.list.archived ? "checkmark" : "")
                    }
                }
                Divider()
                Button { store.setArchived(!store.list.archived) } label: {
                    Label("Archived", systemImage: store.list.archived ? "checkmark" : "archivebox")
                }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: store.list.source == nil ? "line.3.horizontal.decrease" : HistoryList.symbol(for: store.list.source))
                        .font(.system(size: 10, weight: .semibold))
                    Text(sourceName).font(.system(size: 11.5, weight: .medium))
                    Image(systemName: "chevron.down").font(.system(size: 8, weight: .bold)).opacity(0.7)
                }
                .foregroundStyle(store.list.source == nil ? Color.secondary : Live.brassText)
                .padding(.horizontal, 9).frame(height: 22)
                .background(Capsule().fill(store.list.source == nil ? Color.primary.opacity(0.05) : Live.brass.opacity(0.16)))
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("Show sessions from one place")
            .accessibilityIdentifier("history-source-filter")
    }

    private var sourceName: String {
        if store.list.archived { return "Archived" }
        guard let id = store.list.source else { return "All sources" }
        return store.list.filterChoices.first { $0.id == id }?.label ?? id.capitalized
    }

    @ViewBuilder private var listBody: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                // No Pinned section: Geo removed pinning (SPK-124); a pinned chat sorts by time.
                ForEach(store.list.grouped(now: store.now, pins: false), id: \.0.id) { group, sessions in
                    Section {
                        ForEach(sessions) { s in row(s) }
                    } header: { sectionHeader(store.list.archived ? "Archived · " + group.rawValue : group.rawValue) }
                }
                if !store.list.archivedMatches.isEmpty {
                    Section {
                        ForEach(store.list.archivedMatches) { s in row(s) }
                    } header: { sectionHeader("Archived") }
                }
                footer.padding(.vertical, 12)
            }
            .padding(.bottom, 8)
        }
        .scrollIndicators(.automatic)
    }

    private func row(_ s: HistorySession) -> some View {
        HistoryRow(session: s, selected: !store.showingSchedules && !store.showingBoard && !store.showingFiles && store.target == .session(s.id), now: store.now,
                   query: store.list.activeQuery.isEmpty ? nil : store.list.activeQuery) { store.open(s.id) }
            .onAppear { if s.id == store.list.sessions.last?.id { store.loadMore() } }
            .contextMenu { HistoryRowMenu(session: s, store: store) }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased()).font(.system(size: 10, weight: .bold)).tracking(0.9)
            .foregroundStyle(.tertiary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 5)
            .background(HistoryLook.sidebar.opacity(0.97))
    }

    @ViewBuilder private var footer: some View {
        if store.list.loading {
            HStack(spacing: 7) { LiveSpinner(size: 11); Text("Loading…").font(.system(size: 11)).foregroundStyle(.secondary) }
                .frame(maxWidth: .infinity)
        } else if let error = store.list.error {
            VStack(spacing: 6) {
                Text(error).font(.system(size: 11)).foregroundStyle(Live.red).multilineTextAlignment(.center)
                Button("Try again") { store.retryList() }.buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(Live.brassText)
            }
            .frame(maxWidth: .infinity).padding(.horizontal, 16)
        } else if store.list.sessions.isEmpty {
            VStack(spacing: 6) {
                Image(systemName: store.list.isFiltered ? "magnifyingglass" : "tray").font(.system(size: 20)).foregroundStyle(.tertiary)
                Text(store.list.isFiltered ? "Nothing matches that." : "No sessions yet.").font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
                if store.list.isFiltered {
                    Button("Clear the search and filter") { store.searchText = ""; store.setSource(nil) }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(Live.brassText)
                }
            }
            .frame(maxWidth: .infinity).padding(.top, 30)
        } else if !store.list.reachedEnd {
            Button("Load more") { store.loadMore() }.buttonStyle(.plain).font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Live.brassText).frame(maxWidth: .infinity)
        }
    }

    private var updateNote: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: "arrow.down.circle").font(.system(size: 18)).foregroundStyle(Live.brass)
            Text("Update Speakeasy on your Hermes").font(.system(size: 13, weight: .semibold))
            Text("Your Hermes's Speakeasy plugin doesn't share history yet. Update it (Settings › Server) and your sessions show up here.")
                .font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(16)
    }
}

struct HistoryRow: View {
    let session: HistorySession
    var selected: Bool
    var now: Date
    var query: String?
    var action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                ZStack(alignment: .bottomTrailing) {
                    Circle().fill(selected ? Live.brass.opacity(0.22) : Color.primary.opacity(0.06)).frame(width: 26, height: 26)
                        .overlay(Image(systemName: HistoryList.symbol(for: session.source))
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(selected ? Live.brass : Color.primary.opacity(0.6)))
                    statusDot.offset(x: 2, y: 2)
                }
                .padding(.top, 1)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(session.title).font(.system(size: 12.5, weight: .semibold)).lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                            .foregroundStyle(Color.primary.opacity(selected ? 1 : 0.92))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .layoutPriority(1)
                        if session.archived {
                            Image(systemName: "archivebox").font(.system(size: 9)).foregroundStyle(.tertiary)
                                .accessibilityLabel("Archived")
                        }
                        Text(HistoryLook.short(session.lastActivity, now: now)).font(.system(size: 10.5).monospacedDigit())
                            .foregroundStyle(.tertiary).fixedSize()
                    }
                    if let line = session.match ?? (session.preview.isEmpty ? nil : session.preview) {
                        Text(highlighted(HistoryLook.oneLine(line))).font(.system(size: 11.5)).foregroundStyle(.secondary)
                            .lineLimit(session.match != nil ? 3 : 2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if session.state != .idle || !session.isSpeakeasy || session.agent != nil {
                        HStack(spacing: 6) {
                            if session.state == .needsYou { tag("Needs you", Live.amber) }
                            else if session.state == .running { tag("Working", Live.brassText) }
                            if let agent = session.agent {
                                Text(agent.name).font(.system(size: 10, weight: .semibold)).foregroundStyle(Live.brassText).lineLimit(1)
                            }
                            if !session.isSpeakeasy {
                                Text(session.sourceLabel).font(.system(size: 10, weight: .medium)).foregroundStyle(.tertiary).lineLimit(1)
                            }
                        }
                        .padding(.top, 1)
                    }
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(selected ? HistoryLook.selected : hover ? HistoryLook.hover : .clear))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(selected ? Live.brass.opacity(0.32) : .clear, lineWidth: 0.75))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 6)
        .onHover { hover = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(session.title), \(HistoryLook.word(session.state))")
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier("history-row-\(session.id)")
    }

    @ViewBuilder private var statusDot: some View {
        switch session.state {
        case .running: LiveDot(color: Live.brass, size: 7).overlay(Circle().stroke(HistoryLook.sidebar, lineWidth: 1.5))
        case .needsYou: Circle().fill(Live.amber).frame(width: 7, height: 7).overlay(Circle().stroke(HistoryLook.sidebar, lineWidth: 1.5))
        case .idle:
            if session.unread {
                Circle().fill(Color.accentColor).frame(width: 7, height: 7).overlay(Circle().stroke(HistoryLook.sidebar, lineWidth: 1.5))
                    .accessibilityLabel("Unread")
            }
        }
    }

    private func tag(_ text: String, _ color: Color) -> some View {
        Text(text).font(.system(size: 10, weight: .semibold)).foregroundStyle(color)
    }

    /// The search words in brass.
    private func highlighted(_ line: String) -> AttributedString {
        var s = AttributedString(line)
        guard let query, !query.isEmpty else { return s }
        var from = s.startIndex
        while let r = s[from...].range(of: query, options: [.caseInsensitive, .diacriticInsensitive]) {
            s[r].foregroundColor = Live.brassText
            s[r].font = .system(size: 11.5, weight: .semibold)
            from = r.upperBound
        }
        return s
    }
}

/// Right-click on a row: the same actions as the phone's long-press menu.
struct HistoryRowMenu: View {
    let session: HistorySession
    @ObservedObject var store: HistoryStore

    var body: some View {
        Button("Rename…") { HistoryRename.ask(session.title) { store.act(.rename($0), on: session.id) } }
        if session.unread { Button("Mark as Read") { store.act(.read, on: session.id) } }
        Divider()
        Button("Copy Link") {
            if let url = session.link {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(url.absoluteString, forType: .string)
            }
        }
        if session.state == .running, !session.runIDs.isEmpty {
            Button("Stop", role: .destructive) { store.act(.stop(session.runIDs), on: session.id) }
        }
        Divider()
        if session.archived {
            Button("Unarchive") { store.act(.archive(false), on: session.id) }
        } else {
            Button("Archive") { store.act(.archive(true), on: session.id) }
        }
    }
}

/// The rename sheet as a plain alert with a field (AppKit: works from a context menu).
enum HistoryRename {
    @MainActor static func ask(_ current: String, done: @escaping (String) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Rename conversation"
        alert.informativeText = "The new name shows in every Hermes app."
        let field = NSTextField(string: current)
        field.frame = NSRect(x: 0, y: 0, width: 280, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Rename")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        if alert.runModal() == .alertFirstButtonReturn {
            let text = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty, text != current { done(String(text.prefix(100))) }
        }
    }
}

struct HistoryIconButton: View {
    var symbol: String
    var help: String
    var prominent = false
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 12.5, weight: .semibold))
                .foregroundStyle(prominent ? Live.brass : hover ? Color.primary : Color.secondary)
                .frame(width: 28, height: 28)
                .background(Circle().fill(prominent ? Live.brass.opacity(0.18) : Color.primary.opacity(hover ? 0.08 : 0.04)))
        }
        .buttonStyle(.plain).onHover { hover = $0 }.help(help).accessibilityLabel(help)
    }
}

// MARK: - Detail

struct HistoryDetailPane: View {
    @ObservedObject var store: HistoryStore

    var body: some View {
        VStack(spacing: 0) {
            if store.showingSchedules {
                SchedulesPane(store: store, schedules: store.schedules)
            } else if store.showingBoard {
                BoardPane(store: store, board: store.board)
            } else if store.showingFiles {
                FilesPane(store: store, files: store.files)
            } else {
            switch store.target {
            case .none: placeholder
            case .newChat: HistoryNewChat(store: store)
            case .session(let id):
                if let c = store.conversations[id] { HistorySessionView(store: store, conversation: c) }
                else if let error = store.detailErrors[id] { problem(error, id) }
                else { loading }
            }
            }
        }
        .overlay(alignment: .topTrailing) { agentInTitleBar }
    }

    /// In the window's title bar: New chat's agent chip, or the agent a chat talks to.
    @ViewBuilder private var agentInTitleBar: some View {
        if !store.showingSchedules {
            Group {
                if store.showsAgentChip { AgentChip(store: store) }
                else if case .session = store.target, let agent = store.openAgent { AgentBadge(agent: agent) }
            }
            .padding(.top, 9).padding(.trailing, 16)
        }
    }

    private var placeholder: some View {
        VStack(spacing: 10) {
            Image(systemName: "text.bubble").font(.system(size: 30, weight: .light)).foregroundStyle(Live.brass.opacity(0.8))
            Text("Pick a session, or start a new chat").font(.system(size: 14, weight: .semibold))
            Text("Everything you've asked Hermes, from any app, is on the left. Open one to see the work and pick it back up.")
                .font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 340)
            Button { store.newChat() } label: {
                Label("New chat", systemImage: "square.and.pencil").font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 12).frame(height: 28)
                    .background(Capsule().fill(Live.brass)).foregroundStyle(JobLook.brassInk)
            }
            .buttonStyle(.plain).padding(.top, 4)
            Text("⌘N").font(.system(size: 10.5)).foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var loading: some View {
        HStack(spacing: 8) { LiveSpinner(size: 13); Text("Opening…").font(.system(size: 12)).foregroundStyle(.secondary) }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func problem(_ error: String, _ id: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle").font(.system(size: 20)).foregroundStyle(Live.red)
            Text(error).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 360)
            Button("Try again") { store.load(id) }.buttonStyle(.plain).font(.system(size: 12, weight: .semibold)).foregroundStyle(Live.brassText)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct HistorySessionView: View {
    @ObservedObject var store: HistoryStore
    let conversation: HistoryConversation

    var body: some View {
        let c = conversation
        let thread = store.thread(c)
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        header(c, thread)
                        if c.hasMore {
                            Text("Showing the newest messages. Older ones stay in Hermes.")
                                .font(.system(size: 11)).foregroundStyle(.tertiary).frame(maxWidth: .infinity)
                        }
                        // Tasks: finished ones sit where they were asked, as one quiet line; only what's
                        // still going (or needs you) stays at the end, above the composer.
                        let layout = Self.layout(c, thread)
                        if layout.showsFinishedRow {
                            HistoryFinishedRow(jobs: layout.finished, key: "\(c.id)|finished", store: store) { open($0, c, thread, proxy) }
                        }
                        ForEach(layout.top, id: \.lineID) { job in
                            HistoryTaskLine(job: job, now: store.now, openable: Self.opensElsewhere(job, c)) { open(job, c, thread, proxy) }
                        }
                        let messages = thread.messages
                        let via = ChatLabel.platform(source: c.source, label: c.sourceLabel)
                        // Quiet times: a day divider when the day changes, a time after a pause.
                        let marks = ChatTimeline.marks(messages.map { ($0.id, ChatTimeline.began($0.at, tools: $0.tools)) }, now: store.now)
                        ForEach(Array(messages.enumerated()), id: \.element.id) { i, m in
                            let next = i + 1 < messages.count ? messages[i + 1] : nil
                            let mark = marks[m.id]
                            VStack(spacing: 12) {
                                if let mark { HistoryTimeMark(mark: mark) }
                                HistoryMessageView(message: m, sessionID: c.id, store: store,
                                                   via: ChatLabel.fromChat(m.id) ? via : nil,
                                                   lastInGroup: next?.role != m.role || next.map { ChatLabel.fromChat($0.id) } != ChatLabel.fromChat(m.id)
                                                       || (next.map { marks[$0.id] != nil } ?? false))
                            }
                                .padding(.top, mark == nil && i > 0 && messages[i - 1].role == m.role && m.role != .note ? -12 : 0)
                                .id(m.id)
                            ForEach(layout.after[m.id] ?? [], id: \.lineID) { job in
                                HistoryTaskLine(job: job, now: store.now, openable: Self.opensElsewhere(job, c)) { open(job, c, thread, proxy) }
                                    .transition(.opacity.combined(with: .move(edge: .bottom)))
                            }
                        }
                        ForEach(layout.pinned, id: \.lineID) { job in
                            HistoryTaskCard(job: job).transition(.opacity)
                        }
                        if !thread.helpers.isEmpty { HistoryHelpersCard(helpers: thread.helpers, sessionID: c.id, store: store) }
                        ForEach(store.waiting(thread)) { HistoryWaitingCard(waiting: $0, store: store) }
                        ForEach(c.queued) { HistoryQueuedBubble(message: $0, store: store) }
                        if let error = store.queuedError {
                            Text(error).font(.system(size: 11.5)).foregroundStyle(Live.red)
                        }
                        if let undo = store.undoQuestion {
                            HistoryUndoToast(text: "Question dismissed", undo: store.undoDismiss)
                                .id("undo-\(undo.id)").transition(.opacity)
                        }
                        if let error = store.answerError {
                            Text(error).font(.system(size: 11.5)).foregroundStyle(Live.red)
                        }
                        Color.clear.frame(height: 4).id("end")
                    }
                    .frame(maxWidth: HistoryLook.readable, alignment: .leading)
                    .padding(.horizontal, 28).padding(.top, 34).padding(.bottom, 18)
                    .frame(maxWidth: .infinity)
                }
                .background(alignment: .top) { MoodGlow(mood: HistoryLook.mood(c.state)).frame(height: 260).opacity(0.75) }
                .onAppear {
                    proxy.scrollTo("end", anchor: .bottom)
                    // Again once the thread has laid out (long sessions land on the newest message).
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onChange(of: thread.messages.count) { _, _ in withAnimation(.snappy) { proxy.scrollTo("end", anchor: .bottom) } }
                .onChange(of: c.id) { _, _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { proxy.scrollTo("end", anchor: .bottom) }
                }
            }
            HistoryComposer(store: store, placeholder: "Message “\(TaskMessageCopy.shortName(c.title, limit: 34))”", enabled: true)
        }
        .animation(.snappy(duration: 0.3), value: c.jobs.map { "\($0.runID ?? "")\($0.status)" })
    }

    static func layout(_ c: HistoryConversation, _ thread: HistoryThread) -> SessionTaskLayout {
        // A task waiting on an approval or a question shows as that card only (no twin above it).
        let asking = Set(c.approvals.map(\.runID) + c.waitingQuestions.compactMap(\.runID))
        return SessionTaskLayout(jobs: c.jobs, messages: thread.messages.map {
            .init(id: $0.id, isUser: $0.role == .user, at: $0.at, text: $0.text)
        }, hasMore: c.hasMore, asking: asking)
    }

    /// The task ran in its own session (a chat continued from Discord, Telegram…): its line opens that.
    static func opensElsewhere(_ job: WorkInfo, _ c: HistoryConversation) -> Bool {
        guard let sid = job.sessionID else { return false }
        return sid != c.id
    }

    /// A task line: its own session when it has one, else the reply it produced on this page.
    private func open(_ job: WorkInfo, _ c: HistoryConversation, _ thread: HistoryThread, _ proxy: ScrollViewProxy) {
        if Self.opensElsewhere(job, c), let sid = job.sessionID { store.open(sid); return }
        let layout = Self.layout(c, thread)
        guard let anchor = layout.after.first(where: { $0.value.contains { $0.runID == job.runID } })?.key,
              let i = thread.messages.firstIndex(where: { $0.id == anchor }) else { return }
        let reply = thread.messages[(i + 1)...].first { $0.role == .assistant }?.id ?? anchor
        withAnimation(.snappy) { proxy.scrollTo(reply, anchor: .top) }
    }

    private func header(_ c: HistoryConversation, _ thread: HistoryThread) -> some View {
        let mood = HistoryLook.mood(c.state)
        let users = thread.messages.filter { $0.role != .note && !$0.text.isEmpty }.count
        let calls = thread.toolCallCount
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                LiveDot(color: Live.tint(mood), size: 6, active: mood.isLive)
                Text(HistoryLook.word(c.state).uppercased()).tracking(1.0)
                Text("·").foregroundStyle(.secondary)
                Text(c.sourceLabel.uppercased()).tracking(0.6).lineLimit(1).foregroundStyle(Color.primary.opacity(0.55))
                if let started = HistoryLook.long(c.entries.first?.at, now: store.now) {
                    Text("·").foregroundStyle(.secondary)
                    Text(started.uppercased()).tracking(0.6).foregroundStyle(Color.primary.opacity(0.55))
                }
            }
            .font(.system(size: 10.5, weight: .bold))
            .foregroundStyle(Live.tint(mood))
            Text(c.card?.heading ?? c.title).font(.system(size: 24, weight: .bold)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            if let live = c.live, !c.jobs.contains(where: { !["completed", "failed", "cancelled"].contains($0.status) }) {
                HistoryLiveLine(live: live).padding(.top, 2)
            }
            Text([users > 0 ? "\(users) message\(users == 1 ? "" : "s")" : nil,
                  calls > 0 ? "\(calls) tool call\(calls == 1 ? "" : "s")" : nil,
                  thread.helpers.isEmpty ? nil : "\(thread.helpers.count) helper\(thread.helpers.count == 1 ? "" : "s")",
                  c.isSpeakeasy || c.automated ? nil : "replies show here, not in \(c.sourceLabel.components(separatedBy: " · ").first ?? c.sourceLabel)"]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.system(size: 12.5, weight: .medium)).foregroundStyle(Color.primary.opacity(0.6))
            // A task's own page: when it started, when it finished, how long it took.
            if c.sendVia == .task, let times = TaskTimes.header(c.jobs, now: store.now) {
                Label(times, systemImage: "clock").font(.system(size: 12, weight: .medium).monospacedDigit())
                    .foregroundStyle(Color.primary.opacity(0.55))
                    .accessibilityIdentifier("history-task-times")
            }
            if let ctx = c.context {
                // One line names the model and how full its context got; the model only when the
                // composer chip doesn't already say it.
                let showsModel = c.model.map { b in store.modelsSupported && (store.picker?.headerShows(b) ?? true) } ?? true
                ContextUseLine(context: ctx, text: showsModel ? ctx.modelAndUse : ctx.useSentence)
            } else if let badge = c.model, store.modelsSupported, store.picker?.headerShows(badge) ?? true { ModelWorkedLine(badge: badge) }
            if let usage = c.usage { HistoryUsageLine(usage: usage, sessionID: c.id, store: store) }
        }
        .padding(.bottom, 4)
    }
}

private extension HistoryConversation {
    var isSpeakeasy: Bool { source == "speakeasy" }
}

/// A board worker's card as the top of its chat: the issue and its one-line goal, a link to the
/// issue, and the instructions it was started with behind a collapsed row.
struct HistoryWorkerCard: View {
    let card: HistoryWorkerTask
    let key: String
    @ObservedObject var store: HistoryStore
    private var open: Bool { store.openSections.contains(key) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: HistoryList.symbol(for: "kanban")).font(.system(size: 10, weight: .semibold))
                Text("BOARD TASK").tracking(0.8)
                if let issue = card.issue { Text("·"); Text(issue).tracking(0.4) }
                Spacer(minLength: 0)
                if let url = card.url {
                    Link(destination: url) {
                        Label("Open issue", systemImage: "arrow.up.right.square").font(.system(size: 11, weight: .semibold))
                    }
                    .foregroundStyle(Live.brassText)
                    .accessibilityIdentifier("history-worker-issue-link")
                }
            }
            .font(.system(size: 10.5, weight: .bold)).foregroundStyle(.secondary)
            // The page's own title already reads "LAN-12 · <title>": the card adds the goal, not the title again.
            if let goal = card.goal {
                Text(goal).font(.system(size: 12.5)).foregroundStyle(Color.primary.opacity(0.7)).fixedSize(horizontal: false, vertical: true)
            }
            if !card.instructions.isEmpty {
                Button {
                    withAnimation(.snappy) {
                        if open { store.openSections.remove(key) } else { store.openSections.insert(key) }
                    }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)).rotationEffect(.degrees(open ? 90 : 0))
                        Text("Instructions").font(.system(size: 11.5, weight: .semibold))
                    }
                    .foregroundStyle(.secondary).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("history-worker-instructions")
                if open {
                    ScrollView {
                        Text(card.instructions).font(.system(size: 11, design: .monospaced)).foregroundStyle(Color.primary.opacity(0.7))
                            .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(maxHeight: 260)
                }
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(ChatInk.paper))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("history-worker-card")
    }
}

/// What a running session is doing now and what it has done so far (no Speakeasy task card to carry it).
struct HistoryLiveLine: View {
    let live: HistoryLive
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                LiveSpinner(size: 11)
                Text(live.now).font(.system(size: 13, weight: .semibold)).foregroundStyle(Live.brassText).lineLimit(2)
            }
            if let soFar = live.soFar {
                Text("So far: " + soFar).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(3)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("history-live-line")
    }
}

/// Labels for a chat from another app (Discord, Telegram…): its own turns say where they happened;
/// what was said from Speakeasy (linked entries `<session>-<id>`, local `local-…`) doesn't.
enum ChatLabel {
    static func platform(source: String, label: String) -> String? {
        let chats: Set<String> = ["discord", "telegram", "slack", "whatsapp", "signal", "matrix", "sms", "email", "imessage"]
        guard chats.contains(source.lowercased()) else { return nil }
        let name = label.split(separator: "·").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
        return name.isEmpty ? source.capitalized : name
    }
    static func fromChat(_ id: String) -> Bool { !id.contains("-") }

    /// An answer that reads better as a page than as a bubble: headings, code, lists or just long.
    static func isLongForm(_ text: String) -> Bool {
        if text.contains("```") || text.count > 520 { return true }
        let lines = text.split(separator: "\n")
        if lines.contains(where: { $0.range(of: #"^#{1,6} "#, options: .regularExpression) != nil }) { return true }
        return lines.filter { $0.hasPrefix("- ") || $0.hasPrefix("* ") }.count >= 3
    }
}

/// Speakeasy's mark on the last reply of a group: a brass disc with the waveform.
struct ChatMark: View {
    var body: some View {
        ZStack {
            Circle().fill(LinearGradient(colors: [Live.brass.opacity(0.85), Live.brass], startPoint: .top, endPoint: .bottom))
            Image(systemName: "waveform").font(.system(size: 10.5, weight: .bold)).foregroundStyle(ChatInk.onBrass)
        }
        .frame(width: 24, height: 24)
        .accessibilityHidden(true)
    }
}

enum ChatInk {
    static let onBrass = BrandPalette.onBrass
    static let paper = Color.adaptive(dark: Color.white.opacity(0.085), light: Color.white)
    static let otherApp = Color.primary.opacity(0.08)
}

struct HistoryMessageView: View {
    let message: HistoryThreadMessage
    let sessionID: String
    @ObservedObject var store: HistoryStore
    var via: String? = nil
    var lastInGroup = true

    private func bubble(mine: Bool) -> UnevenRoundedRectangle {
        let small: CGFloat = lastInGroup ? 5 : 16
        return UnevenRoundedRectangle(topLeadingRadius: 16, bottomLeadingRadius: mine ? 16 : small,
                                      bottomTrailingRadius: mine ? small : 16, topTrailingRadius: 16, style: .continuous)
    }

    var body: some View {
        switch message.role {
        case .user: user
        case .assistant: assistant
        case .note where message.card != nil:
            HistoryWorkerCard(card: message.card!, key: "\(sessionID)|\(message.id)|instructions", store: store)
        case .note:
            Text(message.text).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                .padding(.horizontal, 10).padding(.vertical, 3)
                .background(Capsule().fill(Color.primary.opacity(0.05)))
                .frame(maxWidth: .infinity, alignment: .center).multilineTextAlignment(.center)
        }
    }

    private var user: some View {
        Group {
            if let call = message.call { callCard(call) } else { userBubble }
        }
    }

    /// A request that came from a call: one card; what was said opens on tap.
    private func callCard(_ lines: [HistoryCallLine]) -> some View {
        let key = "\(sessionID)|call|\(message.id)"
        let open = store.openSections.contains(key)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "phone.fill").font(.system(size: 8.5, weight: .bold)).foregroundStyle(ChatInk.onBrass)
                    .frame(width: 18, height: 18).background(Circle().fill(Live.brass))
                Text("From a call").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            }
            Text(message.text).font(.system(size: 13.5, weight: .medium)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if lines.count > 1 || lines.first?.text != message.text {
                Button {
                    if open { store.openSections.remove(key) } else { store.openSections.insert(key) }
                } label: {
                    HStack(spacing: 4) {
                        Text(open ? "Hide what was said" : "What was said · \(lines.count) \(lines.count == 1 ? "line" : "lines")")
                        Image(systemName: "chevron.down").rotationEffect(.degrees(open ? 180 : 0))
                    }
                    .font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Live.brass)
                }
                .buttonStyle(.plain)
                if open {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(line.who == .you ? "You" : "Speakeasy").font(.system(size: 11, weight: .bold))
                                    .foregroundStyle(line.who == .you ? AnyShapeStyle(Live.brass) : AnyShapeStyle(.secondary))
                                    .frame(width: 62, alignment: .leading)
                                Text(line.text).font(.system(size: 12.5)).foregroundStyle(Color.primary.opacity(0.85))
                                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
        .padding(13)
        .frame(maxWidth: 460, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(ChatInk.paper))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Live.brass.opacity(0.4), lineWidth: 1))
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 90)
    }

    private var userBubble: some View {
        VStack(alignment: .trailing, spacing: 4) {
            if let via { Text("You, in \(via)").font(.system(size: 10.5, weight: .semibold)).foregroundStyle(.tertiary) }
            if message.isVoiceNote && via == nil {
                MacVoiceNoteBubble(message: message, shape: bubble(mine: true))
                    .help(exactTime ?? "")
            } else {
            VStack(alignment: .leading, spacing: 6) {
                if message.voice {
                    HStack(spacing: 2) {
                        Image(systemName: "mic.fill").font(.system(size: 9, weight: .bold)).padding(.trailing, 4)
                        ForEach(Array([0.35, 0.6, 0.9, 0.5, 0.75, 1, 0.65, 0.4, 0.8, 0.55, 0.95, 0.45, 0.7, 0.3].enumerated()), id: \.offset) { _, h in
                            Capsule().frame(width: 2, height: 3 + 12 * h)
                        }
                    }
                    .foregroundStyle(ChatInk.onBrass.opacity(0.75))
                    .accessibilityHidden(true)
                }
                Text(message.text).font(.system(size: 13.5))
                    .foregroundStyle(via == nil ? ChatInk.onBrass : Color.primary.opacity(0.95))
                    .lineSpacing(2).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 13).padding(.vertical, 9)
            .background(bubble(mine: true).fill(via == nil ? AnyShapeStyle(Live.brass) : AnyShapeStyle(ChatInk.otherApp)))
            .opacity(message.delivery == .sending ? 0.7 : 1)
            .help(exactTime ?? "")
            }
            if message.voice || message.delivery != nil { caption }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 90)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var caption: some View {
        HStack(spacing: 5) {
            switch message.delivery {
            case .sending?:
                LiveSpinner(size: 9); Text("Sending…")
            case .failed(let why)?:
                Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Live.red)
                Text(why).foregroundStyle(Live.red).lineLimit(1)
                if let id = message.clientID {
                    Button("Retry") { store.retry(clientID: id) }
                        .buttonStyle(.plain).foregroundStyle(Live.brassText).fontWeight(.semibold)
                }
            default:
                if message.voice && !message.isVoiceNote { Image(systemName: "mic.fill").font(.system(size: 8.5)); Text("Voice message") }
                if message.delivery == .sent { Image(systemName: "checkmark").font(.system(size: 8.5, weight: .bold)); Text("Sent") }
                if let receipt = store.steerReceipt(message.text, at: message.at, session: sessionID) {
                    Image(systemName: receipt.symbol).font(.system(size: 8.5, weight: .bold))
                    Text(receipt.tag()).foregroundStyle(receipt.state == .seen ? AnyShapeStyle(Live.brassText) : AnyShapeStyle(.tertiary))
                }
            }
        }
        .font(.system(size: 10.5)).foregroundStyle(.tertiary)
    }

    private var assistant: some View {
        let long = ChatLabel.isLongForm(message.text)
        return VStack(alignment: .leading, spacing: 8) {
            if !message.tools.isEmpty {
                HistoryToolCalls(items: message.tools, key: "\(sessionID)|\(message.id)", store: store)
                    .padding(.leading, 34)
            }
            if !message.text.isEmpty {
                HStack(alignment: .bottom, spacing: 10) {
                    ZStack { if lastInGroup { ChatMark() } }.frame(width: 24)
                    VStack(alignment: .leading, spacing: 4) {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(Array(MessageBlock.split(message.text).enumerated()), id: \.offset) { _, block in
                                switch block {
                                case .text(let prose):
                                    Text(markdown(prose)).font(.system(size: 13.5)).foregroundStyle(Color.primary.opacity(0.9))
                                        .lineSpacing(3).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                                case .code(let code, let language):
                                    HistoryCodeBlock(code: code, language: language)
                                }
                            }
                        }
                        .frame(maxWidth: long ? .infinity : nil, alignment: .leading)
                        .padding(.horizontal, long ? 16 : 13).padding(.vertical, long ? 13 : 9)
                        .background(bubble(mine: false).fill(ChatInk.paper).shadow(color: .black.opacity(0.06), radius: 4, y: 1))
                        .overlay(bubble(mine: false).strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5))
                        .help(exactTime ?? "")
                        if lastInGroup, let via {
                            HStack(spacing: 5) {
                                Image(systemName: "arrow.turn.down.right"); Text("Replied in \(via)")
                            }
                            .font(.system(size: 10.5)).foregroundStyle(.tertiary).padding(.leading, 6)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.trailing, ChatLabel.isLongForm(message.text) ? 0 : 90)
    }

    private func markdown(_ s: String) -> AttributedString {
        AgentMarkdown.attributed(s)
    }

    /// The message's own time, on hover ("9:41 PM", "Yesterday, 9:41 PM").
    private var exactTime: String? { message.at.map { ChatTimeline.exact($0, now: store.now) } }
}

/// A quiet time in the thread: a day divider ("Today  9:41 PM" between hairlines) or, after a
/// pause, just the time.
struct HistoryTimeMark: View {
    let mark: ChatTimeline.Mark
    var body: some View {
        HStack(spacing: 10) {
            if mark.day != nil { Rectangle().fill(Color.primary.opacity(0.08)).frame(height: 0.5) }
            (Text(mark.day.map { "\($0)  " } ?? "").font(.system(size: 10.5, weight: .semibold))
             + Text(mark.time).font(.system(size: 10.5)))
                .foregroundStyle(.tertiary).fixedSize()
            if mark.day != nil { Rectangle().fill(Color.primary.opacity(0.08)).frame(height: 0.5) }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, mark.day != nil ? 6 : 2)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(mark.day != nil ? "history-day-divider" : "history-time-mark")
    }
}

/// A fenced code block in an answer: monospaced, unwrapped (scrolls sideways), language + Copy.
struct HistoryCodeBlock: View {
    let code: String
    let language: String?
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language ?? "code").font(.system(size: 10.5, weight: .semibold)).foregroundStyle(.secondary)
                Spacer()
                Button(copied ? "Copied" : "Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { copied = false }
                }
                .buttonStyle(.plain).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Live.brassText)
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            Divider().overlay(Live.hairline)
            ScrollView(.horizontal, showsIndicators: true) {
                Text(code).font(.system(size: 12, design: .monospaced)).textSelection(.enabled).fixedSize().padding(10)
            }
        }
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Live.card))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
    }
}

/// The tool calls behind one answer, folded to one line ("Used 4 tools · read_file, terminal");
/// open, every call as the job page draws them (each opens to its raw call).
struct HistoryToolCalls: View {
    let items: [JobActivityItem]
    let key: String
    @ObservedObject var store: HistoryStore

    var body: some View {
        let open = store.openSections.contains(key)
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(Live.reduceMotion ? nil : .snappy(duration: 0.22)) { toggle(key) }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "wrench.and.screwdriver").font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Live.brass)
                    Text(summary).font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary).lineLimit(1)
                    if items.contains(where: { $0.status == .failed }) {
                        Text("\(items.filter { $0.status == .failed }.count) failed")
                            .font(.system(size: 11, weight: .medium)).foregroundStyle(Live.red)
                    }
                    if items.contains(where: { $0.status == .running }) { LiveSpinner(size: 10) }
                    Spacer(minLength: 4)
                    if let when = ToolCallLog.whenLine(items, now: store.now) {
                        Text(when).font(.system(size: 10.5).monospacedDigit()).foregroundStyle(.tertiary).lineLimit(1).fixedSize()
                    }
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
                .padding(.horizontal, 10).frame(height: 32).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(open ? "Fold the tool calls" : "Show every tool call behind this answer")
            .accessibilityIdentifier("history-tool-calls")
            if open {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(items) { item in
                        Divider().overlay(Live.hairline.opacity(0.6)).padding(.leading, 40)
                        ToolCallRow(item: item, live: item.status == .running, now: store.now,
                                    open: store.openSections.contains("\(key)|\(item.id)")) {
                            withAnimation(Live.reduceMotion ? nil : .snappy(duration: 0.22)) { toggle("\(key)|\(item.id)") }
                        }
                    }
                }
                .padding(.horizontal, 2).padding(.bottom, 3)
                .transition(.opacity)
            }
        }
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Live.card))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
        .padding(.leading, 32)
    }

    private var summary: String {
        let words = ToolCallLog.plainSummary(items)
        return words.isEmpty ? "Used \(items.count) tool\(items.count == 1 ? "" : "s")" : words
    }

    private func toggle(_ k: String) {
        if store.openSections.contains(k) { store.openSections.remove(k) } else { store.openSections.insert(k) }
    }
}

/// One of the session's tasks, updated in place by the live feed: what it's doing now, a real
/// progress bar (only with a step count), the outcome. The phone's session page draws the same card.
extension WorkInfo {
    /// A stable row id for a task line/card.
    var lineID: String { runID ?? (title ?? "") + (startedAt.map { "\($0.timeIntervalSince1970)" } ?? "") }
}

/// A finished task in the thread where it was asked: one quiet line, "✓ Fixed the login redirect · Done 9:41 PM ›".
struct HistoryTaskLine: View {
    let job: WorkInfo
    let now: Date
    var openable: Bool
    var action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 10.5, weight: .bold)).foregroundStyle(tint).accessibilityHidden(true)
                Text(TaskLineCopy.line(job, now: now)).font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(Color.primary.opacity(hover ? 0.75 : 0.5)).lineLimit(1).truncationMode(.middle)
                if openable {
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Color.primary.opacity(0.3)).accessibilityHidden(true)
                }
            }
            .padding(.horizontal, 10).frame(height: 24)
            .background(Capsule().fill(Color.primary.opacity(hover ? 0.07 : 0.04)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .frame(maxWidth: .infinity)
        .help(openable ? "Open this task" : "Show the reply")
        .accessibilityLabel(TaskLineCopy.line(job, now: now))
        .accessibilityIdentifier("history-task-line")
    }

    private var icon: String {
        if TaskLineCopy.questionNote(job) != nil { return "questionmark.bubble" }
        switch job.status {
        case "failed": return "exclamationmark.triangle.fill"
        case "cancelled": return "stop.fill"
        default: return "checkmark"
        }
    }
    private var tint: Color {
        if TaskLineCopy.questionNote(job) != nil { return .secondary }
        switch job.status {
        case "failed": return Live.red
        case "cancelled": return .secondary
        default: return Live.green
        }
    }
}

/// The chat's finished tasks that aren't on this page: one collapsed row, opened on tap.
struct HistoryFinishedRow: View {
    let jobs: [WorkInfo]
    let key: String
    @ObservedObject var store: HistoryStore
    var open: (WorkInfo) -> Void

    var body: some View {
        let isOpen = store.openSections.contains(key)
        VStack(spacing: 6) {
            Button {
                withAnimation(.snappy) { if isOpen { store.openSections.remove(key) } else { store.openSections.insert(key) } }
            } label: {
                HStack(spacing: 5) {
                    Text(TaskLineCopy.finishedRow(jobs.count)).font(.system(size: 11.5, weight: .semibold))
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold))
                        .rotationEffect(.degrees(isOpen ? 90 : 0))
                }
                .foregroundStyle(Color.primary.opacity(0.5))
                .padding(.horizontal, 10).frame(height: 24)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("history-finished-row")
            if isOpen {
                ForEach(jobs, id: \.lineID) { job in
                    HistoryTaskLine(job: job, now: store.now, openable: job.sessionID != nil && store.target != .session(job.sessionID!)) { open(job) }
                }
            }
        }
        .frame(maxWidth: .infinity)
    }
}

struct HistoryTaskCard: View {
    let job: WorkInfo
    private var running: Bool { !["completed", "failed", "cancelled"].contains(job.status) }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: icon).font(.system(size: 11, weight: .bold)).foregroundStyle(tint).accessibilityHidden(true)
                Text(job.title ?? job.summary ?? "Task").font(.system(size: 13, weight: .semibold)).lineLimit(2)
                Spacer(minLength: 6)
                Text(word).font(.system(size: 11, weight: .semibold)).foregroundStyle(tint)
            }
            // What it is doing now, from its real steps (the plugin's live line), then what it has done
            // so far. Never the opening plan: that was a guess made at handoff.
            if running, let now = Self.nowLine(job) {
                HStack(spacing: 6) {
                    LiveDot(color: Live.brass, size: 6)
                    Text(now).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2)
                }
                .accessibilityIdentifier("history-task-now-line")
            }
            if running, let soFar = Self.soFar(job) {
                Text("So far: \(soFar)").font(.system(size: 11.5)).foregroundStyle(.tertiary).lineLimit(2)
                    .accessibilityIdentifier("history-task-so-far")
            }
            if job.status == "failed", let why = job.failureReason ?? job.failure?.text {
                Text(why).font(.system(size: 12)).foregroundStyle(Live.red).lineLimit(3)
            }
            if let ctx = job.context { ContextUseLine(context: ctx, text: ctx.line, size: 10.5) }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(tint.opacity(running ? 0.4 : 0.15), lineWidth: 0.75))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("history-task-card")
    }

    static func nowLine(_ job: WorkInfo) -> String? { TaskLiveLine.now(job) }
    static func soFar(_ job: WorkInfo) -> String? { TaskLiveLine.soFar(job) }

    /// A finished card says when: "Done 9:05 PM".
    private var word: String {
        switch job.status {
        case "completed", "failed", "cancelled": return TaskLineCopy.outcome(job)
        case "waiting_for_approval": return "Needs your OK"
        default: return "Working"
        }
    }
    private var icon: String {
        switch job.status {
        case "completed": return "checkmark.circle.fill"
        case "failed": return "exclamationmark.triangle.fill"
        case "cancelled": return "stop.circle.fill"
        case "waiting_for_approval": return "hand.raised.fill"
        default: return "sparkles"
        }
    }
    private var tint: Color {
        switch job.status {
        case "completed": return Live.green
        case "failed", "waiting_for_approval": return Live.red
        case "cancelled": return .secondary
        default: return Live.brass
        }
    }
}

struct HistoryHelpersCard: View {
    let helpers: [JobSubagent]
    let sessionID: String
    @ObservedObject var store: HistoryStore

    var body: some View {
        let working = helpers.filter(\.isRunning).count
        LiveCard(title: helpers.count == 1 ? "Helper" : "Helpers", symbol: "person.2.fill",
                 trailing: working > 0 ? "\(working) of \(helpers.count) working" : "\(helpers.count)", padding: 4) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(helpers.enumerated()), id: \.element.id) { i, h in
                    if i > 0 { Divider().overlay(Live.hairline).padding(.leading, 30) }
                    row(h)
                }
            }
        }
        .accessibilityIdentifier("history-helpers")
    }

    private func row(_ h: JobSubagent) -> some View {
        let key = "\(sessionID)|helper:\(h.id)"
        let open = store.openSections.contains(key)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Group {
                    switch h.status {
                    case .running: LiveDot(color: Live.brass, size: 7)
                    case .done: Circle().fill(Live.green).frame(width: 7, height: 7)
                    case .failed: Circle().fill(Live.red).frame(width: 7, height: 7)
                    case .stopped: Circle().strokeBorder(Color.primary.opacity(0.45), lineWidth: 1.3).frame(width: 8, height: 8)
                    }
                }
                .frame(width: 14)
                VStack(alignment: .leading, spacing: 1) {
                    Text(h.title).font(.system(size: 12.5, weight: .semibold)).lineLimit(2)
                    if let line = h.line, !open || h.isRunning {
                        Text(line).font(.system(size: 11.5)).foregroundStyle(h.status == .failed ? Live.red.opacity(0.9) : .secondary).lineLimit(1)
                    }
                }
                Spacer(minLength: 6)
                if let d = h.elapsed(now: store.now) {
                    Text(Live.clock(d)).font(.system(size: 10.5).monospacedDigit()).foregroundStyle(.secondary)
                }
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(open ? 90 : 0))
            }
            .padding(.horizontal, 8).padding(.vertical, 8).contentShape(Rectangle())
            .onTapGesture {
                withAnimation(Live.reduceMotion ? nil : .snappy(duration: 0.22)) {
                    if open { store.openSections.remove(key) } else { store.openSections.insert(key) }
                }
            }
            if open {
                VStack(alignment: .leading, spacing: 6) {
                    if let goal = h.goal { label("Asked to"); Text(goal).font(.system(size: 11.5)).foregroundStyle(Color.primary.opacity(0.75)).textSelection(.enabled) }
                    if let summary = h.summary {
                        label(h.status == .failed ? "What went wrong" : "What it found")
                        Text(summary).font(.system(size: 12)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                    if !h.activity.isEmpty {
                        label(h.activity.count == 1 ? "Its step" : "Its steps")
                        ForEach(h.activity.reversed()) { item in
                            ToolCallRow(item: item, live: h.isRunning && item.status == .running, now: store.now,
                                        open: store.openSections.contains("\(key)|\(item.id)")) {
                                let k = "\(key)|\(item.id)"
                                if store.openSections.contains(k) { store.openSections.remove(k) } else { store.openSections.insert(k) }
                            }
                        }
                        .padding(.leading, -8)
                    }
                }
                .padding(.leading, 32).padding(.trailing, 4).padding(.bottom, 10)
                .transition(.opacity)
            }
        }
    }

    private func label(_ t: String) -> some View {
        Text(t.uppercased()).font(.system(size: 9.5, weight: .semibold)).tracking(0.6).foregroundStyle(.tertiary)
    }
}

/// Something the session waits on: an approval or a question (a task's, or one a message sent
/// through Hermes hit). Buttons answer it; typing below answers in your own words.
struct HistoryWaitingCard: View {
    let waiting: HistoryWaiting
    @ObservedObject var store: HistoryStore

    var body: some View {
        let busy = store.answering.contains(waiting.id)
        NeedsYouCard(symbol: waiting.kind == .approval ? "hand.raised.fill" : "questionmark.bubble.fill",
                     title: waiting.kind == .approval ? "Wants your OK" : "Asked you",
                     dismiss: waiting.questionRunID == nil ? nil : { withAnimation(.snappy) { store.dismiss(waiting) } }) {
            Text(waiting.text).font(.system(size: 13, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if let command = waiting.command {
                Text(command).font(.system(size: 11, design: .monospaced)).foregroundStyle(Color.primary.opacity(0.82))
                    .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8).padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(Live.well))
            }
            if !waiting.choices.isEmpty {
                WrapLayout(spacing: 6) {
                    ForEach(Array(waiting.choices.enumerated()), id: \.offset) { i, c in
                        HistoryPillButton(title: Self.label(c, kind: waiting.kind, task: waiting.runID != nil), primary: waiting.kind == .approval && i == 0) {
                            store.answer(waiting, choice: c)
                        }
                    }
                }
                .disabled(busy)
                .opacity(busy ? 0.6 : 1)
            }
            if waiting.kind == .question && waiting.questionRunID != nil {
                Button { store.reply(to: waiting) } label: {
                    Label("Reply…", systemImage: "arrowshape.turn.up.left").font(.system(size: 11.5, weight: .semibold))
                }
                .buttonStyle(.plain).foregroundStyle(Live.brassText)
                .accessibilityIdentifier("history-question-reply")
            } else {
                Text(waiting.kind == .approval ? "Or type what to do instead." : "Or answer in your own words below.")
                    .font(.system(size: 10.5)).foregroundStyle(.tertiary)
            }
        }
        .accessibilityIdentifier(waiting.kind == .approval ? "history-approval" : "history-question")
    }

    static func label(_ choice: String, kind: HistoryWaiting.Kind, task: Bool = false) -> String {
        guard kind == .approval else { return choice }
        switch choice {
        case "yes", "once": return "Approve"
        case "session": return task ? "Approve for this task" : "Approve for this session"
        case "no", "deny": return "Deny"
        default: return choice.prefix(1).uppercased() + choice.dropFirst()
        }
    }
}

/// "Question dismissed · Undo", for a few seconds after a ✕.
struct HistoryUndoToast: View {
    var text: String
    var undo: () -> Void
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.circle").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            Text(text).font(.system(size: 12)).foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Button("Undo", action: undo).buttonStyle(.plain)
                .font(.system(size: 12, weight: .semibold)).foregroundStyle(Live.brassText)
                .accessibilityIdentifier("history-question-undo")
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Live.well))
    }
}

struct NeedsYouCard<Content: View>: View {
    var symbol: String
    var title: String
    /// The header's ✕ (a decision card's Dismiss); nil hides it.
    var dismiss: (() -> Void)? = nil
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Image(systemName: symbol).font(.system(size: 10.5, weight: .semibold))
                Text(title.uppercased()).font(.system(size: 10.5, weight: .bold)).tracking(0.8)
                if let dismiss {
                    Spacer(minLength: 8)
                    Button(action: dismiss) {
                        Image(systemName: "xmark").font(.system(size: 10, weight: .bold))
                            .frame(width: 20, height: 20).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .help("Dismiss. Nothing is sent to the agent.")
                    .accessibilityLabel("Dismiss")
                    .accessibilityIdentifier("history-question-dismiss")
                }
            }
            .foregroundStyle(Live.amber)
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.amber.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Live.amber.opacity(0.3), lineWidth: 0.75))
    }
}

struct HistoryPillButton: View {
    var title: String
    var primary = false
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                .padding(.horizontal, 12).frame(height: 26)
                .background(Capsule().fill(primary ? Live.brass.opacity(hover ? 1 : 0.92) : Color.primary.opacity(hover ? 0.12 : 0.07)))
                .foregroundStyle(primary ? JobLook.brassInk : Color.primary)
        }
        .buttonStyle(.plain).onHover { hover = $0 }
    }
}

// MARK: - New chat

struct HistoryNewChat: View {
    @ObservedObject var store: HistoryStore
    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 40)
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Image(systemName: "square.and.pencil").font(.system(size: 10.5, weight: .bold))
                    Text("NEW CHAT").font(.system(size: 10.5, weight: .bold)).tracking(1.0)
                }
                .foregroundStyle(Live.brass)
                Text("What should \(store.agentsSupported ? (store.newChatAgentInfo?.name ?? "Hermes") : "Hermes") work on?")
                    .font(.system(size: 24, weight: .bold))
                Text(store.voice.micVisible
                     ? "Type it, or tap the mic and say it. It starts as its own session, and the reply shows up here."
                     : "Type it. It starts as its own session, and the reply shows up here.")
                    .font(.system(size: 13)).foregroundStyle(Color.primary.opacity(0.62)).fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: HistoryLook.readable, alignment: .leading)
            .padding(.horizontal, 28)
            .frame(maxWidth: .infinity)
            HistoryComposer(store: store, placeholder: "Ask \(store.agentsSupported ? (store.newChatAgentInfo?.name ?? "Hermes") : "Hermes") anything…",
                            enabled: !store.startingChat, inline: true)
                .padding(.top, 8)
            Group {
                if store.startingChat {
                    HStack(spacing: 7) { LiveSpinner(size: 11); Text("Starting…").font(.system(size: 12)).foregroundStyle(.secondary) }
                } else if let error = store.newChatError {
                    Text(error).font(.system(size: 12)).foregroundStyle(Live.red)
                }
            }
            .frame(maxWidth: HistoryLook.readable, alignment: .leading).padding(.horizontal, 28)
            Spacer(minLength: 40)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(alignment: .top) { MoodGlow(mood: .working).frame(height: 260).opacity(0.6) }
    }
}

// MARK: - Composer

/// Type (↩ sends, ⌥↩ new line) or, with no call live, record a voice message: tap the mic, speak,
/// tap Stop; the words land in the field to read and fix, then Send. Goes to the open session by id.
struct HistoryComposer: View {
    @ObservedObject var store: HistoryStore
    var placeholder: String
    var enabled: Bool
    /// New chat: sits in the middle of the page, no bar behind it.
    var inline = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            switch store.voice.flow.phase {
            case .asking, .recording, .transcribing: recording
            default: typing
            }
            hint
        }
        .frame(maxWidth: HistoryLook.readable + 8)
        .padding(.horizontal, inline ? 28 : 24).padding(.top, 10).padding(.bottom, 14)
        .frame(maxWidth: .infinity)
        .background(inline ? Color.clear : HistoryLook.window.opacity(0.96))
        .overlay(alignment: .top) { if !inline { Rectangle().fill(Live.hairline).frame(height: 1) } }
        .onChange(of: store.focusComposer) { _, _ in focused = true }
    }

    private var typing: some View {
        let empty = TaskMessageRequest.clean(store.draft) == nil
        let reviewing = store.voice.reviewing
        return VStack(alignment: .leading, spacing: 7) {
            if let replying = store.replyingTo {
                HStack(spacing: 6) {
                    Image(systemName: "arrowshape.turn.up.left.fill").font(.system(size: 9.5, weight: .bold))
                    Text("Answering: \(replying.text)").font(.system(size: 11, weight: .semibold)).lineLimit(1)
                    Spacer()
                    Button { store.cancelReply() } label: { Image(systemName: "xmark").font(.system(size: 9.5, weight: .bold)) }
                        .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("Stop answering")
                }
                .foregroundStyle(Live.brassText)
                .padding(.horizontal, 6)
                .accessibilityIdentifier("history-replying-to")
            }
            if reviewing {
                HStack(spacing: 6) {
                    Image(systemName: "mic.fill").font(.system(size: 9.5, weight: .bold))
                    Text("VOICE MESSAGE · CHECK THE WORDS, THEN SEND").font(.system(size: 10, weight: .bold)).tracking(0.7)
                    Spacer()
                    Button("Discard") { store.cancelVoice(); store.draft = store.voice.flow.draftBefore }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                        .keyboardShortcut(.cancelAction)
                }
                .foregroundStyle(Live.brassText)
                .padding(.horizontal, 6)
            }
            if store.slashOpen {
                SlashSkillList(store: store).transition(.opacity.combined(with: .move(edge: .bottom)))
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField(placeholder, text: Binding(get: { store.draft }, set: { store.draft = $0 }), axis: .vertical)
                    .textFieldStyle(.plain).font(.system(size: 13.5)).lineLimit(1...8)
                    .focused($focused)
                    .disabled(!enabled)
                    .onSubmit { store.send() }
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .frame(minHeight: 40)
                    .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(reviewing ? Live.brass.opacity(0.08) : HistoryLook.field))
                    .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .strokeBorder(reviewing ? Live.brass.opacity(0.5) : focused ? Live.brass.opacity(0.5) : Live.hairline,
                                      lineWidth: focused || reviewing ? 1 : 0.5))
                    .accessibilityIdentifier("history-composer")
                    .onKeyPress(keys: [.upArrow, .downArrow, .tab, .escape]) { press in
                        guard store.slashOpen else { return .ignored }
                        switch press.key {
                        case .upArrow: store.slash.move(-1)
                        case .downArrow: store.slash.move(1)
                        case .escape: store.slash.dismiss()
                        default: if let skill = store.slash.highlightedSkill { store.pickSkill(skill) }
                        }
                        return .handled
                    }
                    .onChange(of: store.draft) { _, new in store.slash.update(text: store.skillsSupported ? new : "") }
                if store.voice.micVisible && (empty || reviewing) {
                    circle(reviewing ? "arrow.counterclockwise" : "mic.fill",
                           help: reviewing ? "Record again" : "Record a voice message",
                           fill: Color.primary.opacity(0.08), ink: .primary) {
                        if reviewing { store.cancelVoice(); store.draft = store.voice.flow.draftBefore }
                        store.tapMic()
                    }
                    .disabled(!enabled)
                    .accessibilityIdentifier("history-mic")
                }
                circle("arrow.up", help: store.asksSendMode ? "Send (↩): steer now or queue for after" : "Send (↩)", fill: empty ? Color.primary.opacity(0.06) : Live.brass,
                       ink: empty ? Color.secondary : JobLook.brassInk) { store.send() }
                    .disabled(empty || !enabled)
                    .accessibilityIdentifier("history-send")
                    .popover(isPresented: Binding(get: { store.choosingSendMode && !inline },
                                                  set: { if !$0 { store.choosingSendMode = false } }),
                             arrowEdge: .top) {
                        HistorySendModeChoices(store: store)
                    }
            }
        }
    }

    private var recording: some View {
        let flow = store.voice.flow
        let heard = flow.fieldText
        let phase = flow.phase
        let finishing = phase == .transcribing
        let asking = phase == .asking
        return VStack(alignment: .leading, spacing: 8) {
            Text(store.voice.sendsOnStop ? (asking ? "Asking for the microphone…" : "Recording… press Send (↩) when you're done.")
                 : heard.isEmpty ? (asking ? "Asking for the microphone…" : "Listening… say your message.") : heard)
                .font(.system(size: 13.5)).foregroundStyle(heard.isEmpty ? Color.secondary : Color.primary.opacity(0.92))
                .italic(heard.isEmpty).lineLimit(5).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 10) {
                HStack(spacing: 6) {
                    if finishing || asking { LiveSpinner(size: 10) } else { LiveDot(color: Live.red, size: 7) }
                    Text(finishing ? "Finishing" : asking ? "Waiting" : VoiceMessageFlow.clock(flow.elapsed(now: store.now) ?? 0))
                        .font(.system(size: 11.5, weight: .semibold).monospacedDigit())
                        .foregroundStyle(finishing || asking ? Color.secondary : Live.red)
                }
                LevelBars(level: store.level, active: flow.isRecording, now: store.now).frame(height: 18)
                Spacer(minLength: 6)
                circle("xmark", help: "Discard (esc)", fill: Color.primary.opacity(0.08), ink: .secondary) { store.cancelVoice() }
                    .keyboardShortcut(.cancelAction)
                circle(store.voice.sendsOnStop ? "arrow.up" : "stop.fill", help: store.voice.sendsOnStop ? "Send voice message (↩)" : "Stop (↩)",
                       fill: Live.brass, ink: JobLook.brassInk) { store.tapMic() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!flow.isRecording)
                    .accessibilityIdentifier("history-stop")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Live.red.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Live.red.opacity(0.35), lineWidth: 0.75))
        .accessibilityIdentifier("history-recording")
    }

    @ViewBuilder private var hint: some View {
        HStack(spacing: 6) {
            if store.picker != nil && store.voice.problem == nil && !store.voice.flow.isRecording {
                ModelChip(store: store).padding(.trailing, 4)
            }
            if let why = store.modelError, store.voice.problem == nil {
                Text(why).foregroundStyle(Live.red).lineLimit(1)
            } else if let why = store.voice.problem {
                Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Live.red)
                Text(why).foregroundStyle(Live.red).lineLimit(2)
                if why != VoiceMessageFlow.nothingHeardText {
                    Button("Open Settings") {
                        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
                    }
                    .buttonStyle(.plain).foregroundStyle(Live.brassText).fontWeight(.semibold)
                }
            } else {
                switch store.voice.flow.phase {
                case .recording, .asking, .transcribing: Text("Transcribed on this Mac. Nothing is sent until you press Send.")
                case .reviewing: Text("↩ send as a voice message · esc discard · replies show here only")
                default:
                    if let skill = store.draftSkill {
                        Image(systemName: "sparkles").foregroundStyle(Live.brassText)
                        Text("Uses your \(skill.command) skill · ↩ send").foregroundStyle(Live.brassText)
                    } else {
                        Text(store.voice.micVisible ? "↩ send · ⌥↩ new line · replies show here only"
                                                    : "↩ send · ⌥↩ new line · voice messages pause while you're on a call")
                    }
                }
            }
            Spacer()
        }
        .font(.system(size: 10.5)).foregroundStyle(.tertiary)
        .padding(.horizontal, 6)
    }

    private func circle(_ symbol: String, help: String, fill: Color, ink: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 13, weight: .bold)).foregroundStyle(ink)
                .frame(width: 34, height: 34).background(Circle().fill(fill))
        }
        .buttonStyle(.plain).help(help).accessibilityLabel(help)
    }
}

/// The "/" list over the composer: the user's Hermes skills matching what's typed after the slash.
/// Click a row, or ↑↓ then ↩/tab; esc closes it. Picking puts "/command " in the field.
struct SlashSkillList: View {
    @ObservedObject var store: HistoryStore

    var body: some View {
        let menu = store.slash
        VStack(alignment: .leading, spacing: 0) {
            Text("SKILLS").font(.system(size: 10, weight: .bold)).tracking(0.7).foregroundStyle(.tertiary)
                .padding(.horizontal, 12).padding(.top, 9).padding(.bottom, 4)
            ForEach(Array(menu.matches.enumerated()), id: \.element.id) { index, skill in
                Button { store.pickSkill(skill) } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("/" + skill.command).font(.system(size: 13, weight: .semibold, design: .monospaced))
                            .foregroundStyle(index == menu.highlighted ? Live.brassText : Color.primary)
                            .lineLimit(1).layoutPriority(1)
                        Text(skill.description).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(index == menu.highlighted ? Live.brass.opacity(0.14) : Color.clear))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .onHover { if $0 { store.slash.highlighted = index } }
                .accessibilityIdentifier("slash-skill-\(skill.command)")
            }
            Text("↑↓ choose · ↩ or tab pick · esc close").font(.system(size: 10.5)).foregroundStyle(.tertiary)
                .padding(.horizontal, 12).padding(.top, 4).padding(.bottom, 8)
        }
        .padding(.horizontal, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(HistoryLook.window))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.75))
        .shadow(color: .black.opacity(0.10), radius: 10, y: 2)
        .accessibilityIdentifier("slash-skills")
    }
}

/// A short live level meter: the newest level on the right, recent ones trailing off left.
struct LevelBars: View {
    var level: Float
    var active: Bool
    var now: Date
    @State private var history: [Float] = Array(repeating: 0, count: 28)
    var body: some View {
        HStack(alignment: .center, spacing: 2.5) {
            ForEach(Array(history.enumerated()), id: \.offset) { i, v in
                Capsule().fill(Live.red.opacity(0.45 + Double(i) / Double(history.count) * 0.5))
                    .frame(width: 2.5, height: max(3, CGFloat(v) * 18))
            }
        }
        .frame(maxHeight: .infinity)
        .onChange(of: now) { _, _ in
            guard active else { return }
            history.removeFirst(); history.append(level)
        }
        .onAppear { if level > 0 { history = (0..<28).map { i in Float(0.15 + 0.7 * abs(sin(Double(i) * 0.7))) * level } } }
        .accessibilityHidden(true)
    }
}


/// A voice note on the Mac, like a Telegram/Discord one: play, waveform, length, and what Hermes
/// heard under it once written down. Plays only where it was recorded (the clip stays on this Mac).
struct MacVoiceNoteBubble<S: Shape>: View {
    let message: HistoryThreadMessage
    let shape: S
    @ObservedObject private var player = VoiceClipPlayer.shared

    private static var fallback: [Double] { [0.35, 0.6, 0.9, 0.5, 0.75, 1, 0.65, 0.4, 0.8, 0.55, 0.95, 0.45, 0.7, 0.3, 0.6, 0.85, 0.5, 0.35, 0.7, 0.9, 0.45, 0.6] }
    private var clip: String? { message.voiceClip }
    private var playable: Bool { clip.flatMap { VoiceClipStore.audioURL($0) } != nil }
    private var playing: Bool { clip != nil && player.playing == clip }
    private var bars: [Double] {
        let saved = clip.flatMap { VoiceClipStore.meta($0)?.levels } ?? []
        return saved.count >= 8 ? saved : Self.fallback
    }
    private var sending: Bool { message.delivery == .sending }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 9) {
                if playable, let clip {
                    Button { player.toggle(clip) } label: {
                        Image(systemName: playing ? "pause.fill" : "play.fill").font(.system(size: 10, weight: .bold))
                            .foregroundStyle(Live.brass).frame(width: 24, height: 24).background(Circle().fill(ChatInk.onBrass))
                    }
                    .buttonStyle(.plain).help(playing ? "Pause" : "Play").accessibilityLabel(playing ? "Pause voice message" : "Play voice message")
                    .accessibilityIdentifier("history-voice-play")
                } else {
                    Image(systemName: "mic.fill").font(.system(size: 9, weight: .bold)).foregroundStyle(ChatInk.onBrass)
                        .frame(width: 24, height: 24).background(Circle().fill(ChatInk.onBrass.opacity(0.14)))
                }
                HStack(spacing: 2) {
                    ForEach(Array(bars.enumerated()), id: \.offset) { i, h in
                        Capsule().fill(ChatInk.onBrass.opacity(playing && Double(i) / Double(bars.count) < player.progress ? 0.95 : 0.55))
                            .frame(width: 2, height: 3 + 14 * CGFloat(max(0, min(1, h))))
                    }
                }
                .accessibilityHidden(true)
                if let d = message.voiceDuration {
                    Text(VoiceMessageFlow.clock(playing ? d * player.progress : d))
                        .font(.system(size: 11, weight: .semibold).monospacedDigit()).foregroundStyle(ChatInk.onBrass.opacity(0.8))
                }
            }
            if !message.text.isEmpty {
                Text(message.text).font(.system(size: 13.5)).foregroundStyle(ChatInk.onBrass)
                    .lineSpacing(2).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            } else if sending {
                Text("Writing it down…").font(.system(size: 12)).foregroundStyle(ChatInk.onBrass.opacity(0.65))
            } else if message.delivery == nil || message.delivery == .sent {
                Text("Your Hermes got the recording but couldn't write it down.").font(.system(size: 12))
                    .foregroundStyle(ChatInk.onBrass.opacity(0.7)).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 13).padding(.vertical, 9)
        .frame(minWidth: 180, alignment: .leading)
        .background(shape.fill(Live.brass))
        .opacity(sending ? 0.7 : 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(message.text.isEmpty ? "You, voice message" : "You, voice message: \(message.text)")
        .accessibilityIdentifier("history-voice-note")
    }
}
