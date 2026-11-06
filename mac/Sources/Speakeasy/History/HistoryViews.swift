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

    var body: some View {
        HStack(spacing: 0) {
            HistorySidebar(store: store)
                .frame(width: HistoryLook.sidebarWidth)
                .background(HistoryLook.sidebar)
            Rectangle().fill(Color.adaptive(dark: Color.black.opacity(0.45), light: Color.black.opacity(0.08))).frame(width: 1)
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
        Button("") { store.focusSearch += 1 }.keyboardShortcut("f", modifiers: .command).opacity(0).allowsHitTesting(false)
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
        }
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
                if !store.list.pinned.isEmpty {
                    Section {
                        ForEach(store.list.pinned) { s in row(s) }
                    } header: { sectionHeader("Pinned") }
                }
                ForEach(store.list.grouped(now: store.now), id: \.0.id) { group, sessions in
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
        HistoryRow(session: s, selected: !store.showingSchedules && store.target == .session(s.id), now: store.now,
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
                        if session.pinned {
                            Image(systemName: "pin.fill").font(.system(size: 8.5)).foregroundStyle(.tertiary)
                                .accessibilityLabel("Pinned")
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
        Button(session.pinned ? "Unpin" : "Pin") { store.act(.pin(!session.pinned), on: session.id) }
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
                        ForEach(thread.messages) { m in
                            HistoryMessageView(message: m, sessionID: c.id, store: store).id(m.id)
                        }
                        // A task waiting on an approval or a question shows as that card only (no twin above it).
                        let asking = Set(c.approvals.map(\.runID) + c.waitingQuestions.compactMap(\.runID))
                        let cards = c.jobs.filter { !(asking.contains($0.runID ?? "") && $0.status != "running") }
                        ForEach(Array(cards.suffix(4).enumerated()), id: \.offset) { _, job in
                            HistoryTaskCard(job: job)
                        }
                        if !thread.helpers.isEmpty { HistoryHelpersCard(helpers: thread.helpers, sessionID: c.id, store: store) }
                        ForEach(thread.waiting) { HistoryWaitingCard(waiting: $0, store: store) }
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
            Text(c.title).font(.system(size: 24, weight: .bold)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            Text([users > 0 ? "\(users) message\(users == 1 ? "" : "s")" : nil,
                  calls > 0 ? "\(calls) tool call\(calls == 1 ? "" : "s")" : nil,
                  thread.helpers.isEmpty ? nil : "\(thread.helpers.count) helper\(thread.helpers.count == 1 ? "" : "s")",
                  c.isSpeakeasy ? nil : "replies show here, not in \(c.sourceLabel.components(separatedBy: " · ").first ?? c.sourceLabel)"]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.system(size: 12.5, weight: .medium)).foregroundStyle(Color.primary.opacity(0.6))
            if let badge = c.model, store.modelsSupported, store.picker?.headerShows(badge) ?? true { ModelWorkedLine(badge: badge) }
        }
        .padding(.bottom, 4)
    }
}

private extension HistoryConversation {
    var isSpeakeasy: Bool { source == "speakeasy" }
}

struct HistoryMessageView: View {
    let message: HistoryThreadMessage
    let sessionID: String
    @ObservedObject var store: HistoryStore

    var body: some View {
        switch message.role {
        case .user: user
        case .assistant: assistant
        case .note:
            Text(message.text).font(.system(size: 11.5)).foregroundStyle(.tertiary)
                .frame(maxWidth: .infinity, alignment: .center).multilineTextAlignment(.center)
        }
    }

    private var user: some View {
        VStack(alignment: .trailing, spacing: 4) {
            Text(message.text).font(.system(size: 13.5)).foregroundStyle(Color.primary.opacity(0.96))
                .lineSpacing(2).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 13).padding(.vertical, 9)
                .background(UnevenRoundedRectangle(topLeadingRadius: 14, bottomLeadingRadius: 14, bottomTrailingRadius: 5,
                                                   topTrailingRadius: 14, style: .continuous).fill(Live.brass.opacity(0.2)))
                .overlay(UnevenRoundedRectangle(topLeadingRadius: 14, bottomLeadingRadius: 14, bottomTrailingRadius: 5,
                                                topTrailingRadius: 14, style: .continuous).strokeBorder(Live.brass.opacity(0.28), lineWidth: 0.5))
                .opacity(message.delivery == .sending ? 0.7 : 1)
            caption
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 60)
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
                if message.voice { Image(systemName: "mic.fill").font(.system(size: 8.5)); Text("Voice message") }
                if message.delivery == .sent { Image(systemName: "checkmark").font(.system(size: 8.5, weight: .bold)); Text("Sent") }
                if let t = HistoryLook.long(message.at, now: store.now) { Text(t) }
            }
        }
        .font(.system(size: 10.5)).foregroundStyle(.tertiary)
    }

    private var assistant: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !message.tools.isEmpty {
                HistoryToolCalls(items: message.tools, key: "\(sessionID)|\(message.id)", store: store)
            }
            if !message.text.isEmpty {
                HStack(alignment: .top, spacing: 10) {
                    Circle().fill(Live.brass.opacity(0.18)).frame(width: 22, height: 22)
                        .overlay(Image(systemName: "sparkle").font(.system(size: 9.5, weight: .bold)).foregroundStyle(Live.brass))
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(MessageBlock.split(message.text).enumerated()), id: \.offset) { _, block in
                            switch block {
                            case .text(let prose):
                                Text(markdown(prose)).font(.system(size: 13.5)).foregroundStyle(Color.primary.opacity(0.9))
                                    .lineSpacing(3).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            case .code(let code, let language):
                                HistoryCodeBlock(code: code, language: language)
                            }
                        }
                        if let t = HistoryLook.long(message.at, now: store.now) {
                            Text(t).font(.system(size: 10.5)).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.trailing, 24)
    }

    private func markdown(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
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
            if running, let now = job.shortStatus ?? job.activity.last?.text, !now.isEmpty {
                HStack(spacing: 6) {
                    LiveDot(color: Live.brass, size: 6)
                    Text(now).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            if job.status == "failed", let why = job.failureReason ?? job.failure?.text {
                Text(why).font(.system(size: 12)).foregroundStyle(Live.red).lineLimit(3)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(tint.opacity(running ? 0.4 : 0.15), lineWidth: 0.75))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("history-task-card")
    }

    private var word: String {
        switch job.status {
        case "completed": return "Done"
        case "failed": return "Didn't finish"
        case "cancelled": return "Stopped"
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
                     title: waiting.kind == .approval ? "Wants your OK" : "Asked you") {
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
            Text(waiting.kind == .approval ? "Or type what to do instead." : "Or answer in your own words below.")
                .font(.system(size: 10.5)).foregroundStyle(.tertiary)
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

struct NeedsYouCard<Content: View>: View {
    var symbol: String
    var title: String
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Image(systemName: symbol).font(.system(size: 10.5, weight: .semibold))
                Text(title.uppercased()).font(.system(size: 10.5, weight: .bold)).tracking(0.8)
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
                circle("arrow.up", help: "Send (↩)", fill: empty ? Color.primary.opacity(0.06) : Live.brass,
                       ink: empty ? Color.secondary : JobLook.brassInk) { store.send() }
                    .disabled(empty || !enabled)
                    .accessibilityIdentifier("history-send")
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
            Text(heard.isEmpty ? (asking ? "Asking for the microphone…" : "Listening… say your message.") : heard)
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
                circle("stop.fill", help: "Stop (↩)", fill: Live.brass, ink: JobLook.brassInk) { store.tapMic() }
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
                    Text(store.voice.micVisible ? "↩ send · ⌥↩ new line · replies show here only"
                                                : "↩ send · ⌥↩ new line · voice messages pause while you're on a call")
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
