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
    static let window = Color(red: 0.118, green: 0.110, blue: 0.102)     // #1e1c1a
    static let sidebar = Color(red: 0.090, green: 0.084, blue: 0.078)    // #171514
    static let field = Color.white.opacity(0.06)
    static let hover = Color.white.opacity(0.045)
    static let selected = JobLook.brass.opacity(0.16)
    static let readable: CGFloat = 700
    static let sidebarWidth: CGFloat = 288

    static func mood(_ s: HistoryStatus) -> LiveMood {
        switch s {
        case .running: return .working
        case .waiting: return .needsYou
        case .done: return .done
        case .failed: return .failed
        case .idle: return .quiet
        }
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
            Rectangle().fill(Color.black.opacity(0.45)).frame(width: 1)
            HistoryDetailPane(store: store)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(HistoryLook.window)
        }
        .ignoresSafeArea(.container, edges: .top)   // under the transparent title bar
        .environment(\.colorScheme, .dark)
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
            Rectangle().fill(Live.hairline).frame(height: 1)
            if !store.supported { updateNote } else { listBody }
        }
        .onChange(of: store.focusSearch) { _, _ in searchFocused = true }
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
            Menu {
                Button { store.setSource(nil) } label: { Label("All sources", systemImage: store.list.source == nil ? "checkmark" : "") }
                Divider()
                ForEach(store.list.sourceChoices) { s in
                    Button { store.setSource(s.id) } label: {
                        Label(s.count.map { "\(s.label)  \($0)" } ?? s.label, systemImage: store.list.source == s.id ? "checkmark" : "")
                    }
                }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: store.list.source == nil ? "line.3.horizontal.decrease" : HistorySource.symbol(for: store.list.source))
                        .font(.system(size: 10, weight: .semibold))
                    Text(store.list.source.map { HistorySource.label(for: $0) } ?? "All sources").font(.system(size: 11.5, weight: .medium))
                    Image(systemName: "chevron.down").font(.system(size: 8, weight: .bold)).opacity(0.7)
                }
                .foregroundStyle(store.list.source == nil ? Color.secondary : Live.brassText)
                .padding(.horizontal, 9).frame(height: 22)
                .background(Capsule().fill(store.list.source == nil ? Color.white.opacity(0.05) : Live.brass.opacity(0.16)))
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("Show sessions from one place")
            .accessibilityIdentifier("history-source-filter")
            Spacer()
            if let q = store.list.activeQuery, !store.list.loading {
                Text(store.list.sessions.isEmpty ? "No matches" : "\(store.list.sessions.count)\(store.list.reachedEnd ? "" : "+") for “\(q)”")
                    .font(.system(size: 10.5)).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
            }
        }
    }

    @ViewBuilder private var listBody: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                ForEach(store.list.grouped(now: store.now), id: \.0) { group, sessions in
                    Section {
                        ForEach(sessions) { s in
                            HistoryRow(session: s, selected: store.target == .session(s.id), now: store.now,
                                       query: store.list.activeQuery) { store.open(s.id) }
                                .onAppear { if s.id == store.list.sessions.last?.id { store.loadMore() } }
                        }
                    } header: {
                        Text(group.rawValue.uppercased()).font(.system(size: 10, weight: .bold)).tracking(0.9)
                            .foregroundStyle(.tertiary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 5)
                            .background(HistoryLook.sidebar.opacity(0.97))
                    }
                }
                footer.padding(.vertical, 12)
            }
            .padding(.bottom, 8)
        }
        .scrollIndicators(.automatic)
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
                    Circle().fill(selected ? Live.brass.opacity(0.22) : Color.white.opacity(0.06)).frame(width: 26, height: 26)
                        .overlay(Image(systemName: HistorySource.symbol(for: session.source))
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(selected ? Live.brass : Color.white.opacity(0.6)))
                    statusDot.offset(x: 2, y: 2)
                }
                .padding(.top, 1)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(session.title).font(.system(size: 12.5, weight: .semibold)).lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                            .foregroundStyle(Color.white.opacity(selected ? 1 : 0.92))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .layoutPriority(1)
                        Text(HistoryLook.short(session.sortDate, now: now)).font(.system(size: 10.5).monospacedDigit())
                            .foregroundStyle(.tertiary).fixedSize()
                    }
                    if let line = session.snippet ?? session.preview {
                        Text(highlighted(line)).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(session.snippet != nil ? 3 : 2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if session.status == .waiting || session.status == .running || session.source.map({ $0 != "speakeasy" }) == true {
                        HStack(spacing: 6) {
                            if session.status == .waiting { tag("Needs you", Live.amber) }
                            else if session.status == .running { tag("Working", Live.brassText) }
                            if let source = session.source, source != "speakeasy", let name = session.sourceName {
                                Text(name).font(.system(size: 10, weight: .medium)).foregroundStyle(.tertiary).lineLimit(1)
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
        .accessibilityLabel("\(session.title), \(session.status.word)")
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier("history-row-\(session.id)")
    }

    @ViewBuilder private var statusDot: some View {
        switch session.status {
        case .running: LiveDot(color: Live.brass, size: 7).overlay(Circle().stroke(HistoryLook.sidebar, lineWidth: 1.5))
        case .waiting: Circle().fill(Live.amber).frame(width: 7, height: 7).overlay(Circle().stroke(HistoryLook.sidebar, lineWidth: 1.5))
        case .failed: Circle().fill(Live.red).frame(width: 7, height: 7).overlay(Circle().stroke(HistoryLook.sidebar, lineWidth: 1.5))
        default: EmptyView()
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
                .background(Circle().fill(prominent ? Live.brass.opacity(0.18) : Color.white.opacity(hover ? 0.08 : 0.04)))
        }
        .buttonStyle(.plain).onHover { hover = $0 }.help(help).accessibilityLabel(help)
    }
}

// MARK: - Detail

struct HistoryDetailPane: View {
    @ObservedObject var store: HistoryStore

    var body: some View {
        VStack(spacing: 0) {
            switch store.target {
            case .none: placeholder
            case .newChat: HistoryNewChat(store: store)
            case .session(let id):
                if let detail = store.details[id] { HistorySessionView(store: store, detail: detail) }
                else if let error = store.detailErrors[id] { problem(error, id) }
                else { loading }
            }
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
    let detail: HistoryDetail

    var body: some View {
        let s = detail.session
        let thread = store.thread(detail)
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        header(s)
                        if detail.hasMore {
                            Text("Showing the newest messages. Older ones are in Hermes.")
                                .font(.system(size: 11)).foregroundStyle(.tertiary).frame(maxWidth: .infinity)
                        }
                        ForEach(thread) { m in
                            HistoryMessageView(message: m, sessionID: s.id, store: store).id(m.id)
                        }
                        if !detail.helpers.isEmpty { HistoryHelpersCard(helpers: detail.helpers, sessionID: s.id, store: store) }
                        ForEach(detail.approvals) { HistoryApprovalCard(approval: $0, store: store) }
                        ForEach(detail.questions) { HistoryQuestionCard(question: $0, store: store) }
                        if let error = store.answerError {
                            Text(error).font(.system(size: 11.5)).foregroundStyle(Live.red)
                        }
                        Color.clear.frame(height: 4).id("end")
                    }
                    .frame(maxWidth: HistoryLook.readable, alignment: .leading)
                    .padding(.horizontal, 28).padding(.top, 34).padding(.bottom, 18)
                    .frame(maxWidth: .infinity)
                }
                .background(alignment: .top) { MoodGlow(mood: HistoryLook.mood(s.status)).frame(height: 260).opacity(0.75) }
                .onAppear {
                    proxy.scrollTo("end", anchor: .bottom)
                    // Again once the thread has laid out (long sessions land on the newest message).
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onChange(of: thread.count) { _, _ in withAnimation(.snappy) { proxy.scrollTo("end", anchor: .bottom) } }
                .onChange(of: s.id) { _, _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { proxy.scrollTo("end", anchor: .bottom) }
                }
            }
            HistoryComposer(store: store, placeholder: "Message “\(TaskMessageCopy.shortName(s.title, limit: 34))”",
                            enabled: detail.canMessage)
        }
    }

    private func header(_ s: HistorySession) -> some View {
        let mood = HistoryLook.mood(s.status)
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                LiveDot(color: Live.tint(mood), size: 6, active: mood.isLive)
                Text(s.status.word.uppercased()).tracking(1.0)
                if let name = s.sourceName {
                    Text("·").foregroundStyle(.secondary)
                    Text(name.uppercased()).tracking(0.6).lineLimit(1).foregroundStyle(Color.white.opacity(0.55))
                }
                if let started = HistoryLook.long(s.startedAt, now: store.now) {
                    Text("·").foregroundStyle(.secondary)
                    Text(started.uppercased()).tracking(0.6).foregroundStyle(Color.white.opacity(0.55))
                }
            }
            .font(.system(size: 10.5, weight: .bold))
            .foregroundStyle(Live.tint(mood))
            Text(s.title).font(.system(size: 24, weight: .bold)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            let calls = detail.toolCallCount
            let count = detail.messages.count
            Text([count > 0 ? "\(count) message\(count == 1 ? "" : "s")" : nil,
                  calls > 0 ? "\(calls) tool call\(calls == 1 ? "" : "s")" : nil,
                  detail.helpers.isEmpty ? nil : "\(detail.helpers.count) helper\(detail.helpers.count == 1 ? "" : "s")"]
                .compactMap { $0 }.joined(separator: " · "))
                .font(.system(size: 12.5, weight: .medium)).foregroundStyle(Color.white.opacity(0.6))
        }
        .padding(.bottom, 4)
    }
}

struct HistoryMessageView: View {
    let message: HistoryMessage
    let sessionID: String
    @ObservedObject var store: HistoryStore

    var body: some View {
        switch message.role {
        case .user: user
        case .assistant: assistant
        case .system:
            Text(message.text).font(.system(size: 11.5)).foregroundStyle(.tertiary)
                .frame(maxWidth: .infinity, alignment: .center).multilineTextAlignment(.center)
        }
    }

    private var user: some View {
        VStack(alignment: .trailing, spacing: 4) {
            Text(message.text).font(.system(size: 13.5)).foregroundStyle(Color.white.opacity(0.96))
                .lineSpacing(2).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 13).padding(.vertical, 9)
                .background(UnevenRoundedRectangle(topLeadingRadius: 14, bottomLeadingRadius: 14, bottomTrailingRadius: 5,
                                                   topTrailingRadius: 14, style: .continuous).fill(Live.brass.opacity(0.2)))
                .overlay(UnevenRoundedRectangle(topLeadingRadius: 14, bottomLeadingRadius: 14, bottomTrailingRadius: 5,
                                                topTrailingRadius: 14, style: .continuous).strokeBorder(Live.brass.opacity(0.28), lineWidth: 0.5))
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
                if message.id.hasPrefix("local-") {
                    Button("Retry") { store.retry(clientID: String(message.id.dropFirst("local-".count))) }
                        .buttonStyle(.plain).foregroundStyle(Live.brassText).fontWeight(.semibold)
                }
            default:
                if message.isVoice { Image(systemName: "mic.fill").font(.system(size: 8.5)); Text("Voice message") }
                else if let s = message.source, !["typed", "speakeasy", "text"].contains(s.lowercased()) {
                    Image(systemName: HistorySource.symbol(for: s)).font(.system(size: 8.5)); Text(HistorySource.label(for: s))
                }
                if message.delivery == .sent { Image(systemName: "checkmark").font(.system(size: 8.5, weight: .bold)) }
                if let t = HistoryLook.long(message.at, now: store.now) { Text(t) }
            }
        }
        .font(.system(size: 10.5)).foregroundStyle(.tertiary)
    }

    private var assistant: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !message.toolCalls.isEmpty {
                HistoryToolCalls(items: message.toolCalls, key: "\(sessionID)|\(message.id)", store: store)
            }
            if !message.text.isEmpty {
                HStack(alignment: .top, spacing: 10) {
                    Circle().fill(Live.brass.opacity(0.18)).frame(width: 22, height: 22)
                        .overlay(Image(systemName: "sparkle").font(.system(size: 9.5, weight: .bold)).foregroundStyle(Live.brass))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(markdown(message.text)).font(.system(size: 13.5)).foregroundStyle(Color.white.opacity(0.9))
                            .lineSpacing(3).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
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
        let names = items.compactMap { $0.tool }.reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        let head = "Used \(items.count) tool\(items.count == 1 ? "" : "s")"
        guard !names.isEmpty else { return head }
        return head + " · " + names.prefix(3).joined(separator: ", ") + (names.count > 3 ? "…" : "")
    }

    private func toggle(_ k: String) {
        if store.openSections.contains(k) { store.openSections.remove(k) } else { store.openSections.insert(k) }
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
                    case .stopped: Circle().strokeBorder(Color.white.opacity(0.45), lineWidth: 1.3).frame(width: 8, height: 8)
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
                    if let goal = h.goal { label("Asked to"); Text(goal).font(.system(size: 11.5)).foregroundStyle(Color.white.opacity(0.75)).textSelection(.enabled) }
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

/// A tool approval this session is waiting on. The buttons answer by message, to this session.
struct HistoryApprovalCard: View {
    let approval: HistoryApproval
    @ObservedObject var store: HistoryStore
    var body: some View {
        NeedsYouCard(symbol: "hand.raised.fill", title: "Wants your OK") {
            Text(approval.description).font(.system(size: 13, weight: .medium)).fixedSize(horizontal: false, vertical: true)
            if let command = approval.command {
                Text(command).font(.system(size: 11, design: .monospaced)).foregroundStyle(Color.white.opacity(0.82))
                    .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8).padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(Color.black.opacity(0.32)))
            }
            HStack(spacing: 8) {
                if let sendID = approval.sendID {
                    // A message sent through Hermes waits on it: its own choices, never "always".
                    ForEach(approval.choices, id: \.self) { c in
                        HistoryPillButton(title: Self.label(c), primary: c == approval.choices.first) {
                            store.answer(c, sendID: sendID, choice: c)
                        }
                    }
                    .disabled(store.answering.contains(sendID))
                } else {
                    HistoryPillButton(title: "Approve", primary: true) { store.answer("yes") }
                    HistoryPillButton(title: "Deny") { store.answer("no") }
                }
                Spacer()
                if approval.sendID == nil {
                    Text("or type what to do instead").font(.system(size: 10.5)).foregroundStyle(.tertiary)
                }
            }
        }
        .accessibilityIdentifier("history-approval")
    }

    static func label(_ choice: String) -> String {
        switch choice {
        case "once": return "Approve once"
        case "session": return "Approve for this session"
        case "deny": return "Deny"
        default: return choice.prefix(1).uppercased() + choice.dropFirst()
        }
    }
}

struct HistoryQuestionCard: View {
    let question: HistoryQuestion
    @ObservedObject var store: HistoryStore
    var body: some View {
        NeedsYouCard(symbol: "questionmark.bubble.fill", title: "Asked you") {
            Text(question.question).font(.system(size: 13, weight: .medium)).fixedSize(horizontal: false, vertical: true)
            if !question.options.isEmpty {
                WrapLayout(spacing: 6) {
                    ForEach(Array(question.options.enumerated()), id: \.offset) { i, o in
                        HistoryPillButton(title: o, primary: false) { store.answer(o, sendID: question.sendID, choice: o) }
                    }
                }
            }
            Text("Or answer in your own words below.").font(.system(size: 10.5)).foregroundStyle(.tertiary)
        }
        .accessibilityIdentifier("history-question")
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
                .background(Capsule().fill(primary ? Live.brass.opacity(hover ? 1 : 0.92) : Color.white.opacity(hover ? 0.12 : 0.07)))
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
                Text("What should Hermes work on?").font(.system(size: 24, weight: .bold))
                Text(store.voice.micVisible
                     ? "Type it, or tap the mic and say it. It starts as its own session, and the reply shows up here."
                     : "Type it. It starts as its own session, and the reply shows up here.")
                    .font(.system(size: 13)).foregroundStyle(Color.white.opacity(0.62)).fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: HistoryLook.readable, alignment: .leading)
            .padding(.horizontal, 28)
            .frame(maxWidth: .infinity)
            HistoryComposer(store: store, placeholder: "Ask Hermes anything…", enabled: !store.startingChat, inline: true)
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

/// Type (↩ sends, ⌥↩ new line) or, with no call live, record a voice message: speak, stop, read
/// and fix what it heard, send. Goes to the open session by id.
struct HistoryComposer: View {
    @ObservedObject var store: HistoryStore
    var placeholder: String
    var enabled: Bool
    /// New chat: sits in the middle of the page, no bar behind it.
    var inline = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            switch store.voice.phase {
            case .asking, .recording, .finishing: recording
            case .review(let text), .sending(let text): review(text)
            case .idle, .problem: typing
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
        return HStack(alignment: .bottom, spacing: 8) {
            TextField(placeholder, text: Binding(get: { store.draft }, set: { store.draft = $0 }), axis: .vertical)
                .textFieldStyle(.plain).font(.system(size: 13.5)).lineLimit(1...8)
                .focused($focused)
                .disabled(!enabled)
                .onSubmit { store.sendTyped() }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .frame(minHeight: 40)
                .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(HistoryLook.field))
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .strokeBorder(focused ? Live.brass.opacity(0.5) : Live.hairline, lineWidth: focused ? 1 : 0.5))
                .accessibilityIdentifier("history-composer")
            if store.voice.micVisible && empty {
                circle("mic.fill", help: "Record a voice message", fill: Color.white.opacity(0.08), ink: .primary) { store.tapMic() }
                    .disabled(!enabled)
                    .accessibilityIdentifier("history-mic")
            }
            circle("arrow.up", help: "Send (↩)", fill: empty ? Color.white.opacity(0.06) : Live.brass,
                   ink: empty ? Color.secondary : JobLook.brassInk) { store.sendTyped() }
                .disabled(empty || !enabled)
                .accessibilityIdentifier("history-send")
        }
    }

    private var recording: some View {
        let heard: String = {
            switch store.voice.phase { case .recording(let h, _), .finishing(let h): return h; default: return "" }
        }()
        let finishing: Bool = { if case .finishing = store.voice.phase { return true }; return false }()
        let asking: Bool = { if case .asking = store.voice.phase { return true }; return false }()
        return VStack(alignment: .leading, spacing: 8) {
            Text(heard.isEmpty ? (asking ? "Asking for the microphone…" : "Listening… say your message.") : heard)
                .font(.system(size: 13.5)).foregroundStyle(heard.isEmpty ? Color.secondary : Color.white.opacity(0.92))
                .italic(heard.isEmpty).lineLimit(5).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 10) {
                HStack(spacing: 6) {
                    if finishing { LiveSpinner(size: 10) } else { LiveDot(color: Live.red, size: 7, active: !asking) }
                    Text(finishing ? "Finishing" : Live.clock(store.voice.elapsed(now: store.now)))
                        .font(.system(size: 11.5, weight: .semibold).monospacedDigit())
                        .foregroundStyle(finishing ? Color.secondary : Live.red)
                }
                LevelBars(level: store.level, active: store.voice.isRecording, now: store.now).frame(height: 18)
                Spacer(minLength: 6)
                circle("xmark", help: "Discard (esc)", fill: Color.white.opacity(0.08), ink: .secondary) { store.cancelVoice() }
                    .keyboardShortcut(.cancelAction)
                circle("stop.fill", help: "Stop (↩)", fill: Live.brass, ink: JobLook.brassInk) { store.stopRecording() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!store.voice.isRecording)
                    .accessibilityIdentifier("history-stop")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Live.red.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Live.red.opacity(0.35), lineWidth: 0.75))
        .accessibilityIdentifier("history-recording")
    }

    private func review(_ text: String) -> some View {
        let sending: Bool = { if case .sending = store.voice.phase { return true }; return false }()
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "mic.fill").font(.system(size: 9.5, weight: .bold))
                Text("VOICE MESSAGE · CHECK THE WORDS").font(.system(size: 10, weight: .bold)).tracking(0.7)
            }
            .foregroundStyle(Live.brassText)
            TextField("", text: Binding(get: { text }, set: { store.editVoice($0) }), axis: .vertical)
                .textFieldStyle(.plain).font(.system(size: 13.5)).lineLimit(1...8)
                .focused($focused)
                .onSubmit { store.sendVoice() }
                .disabled(sending)
            HStack(spacing: 8) {
                Spacer()
                HistoryPillButton(title: "Discard") { store.cancelVoice() }.keyboardShortcut(.cancelAction)
                HistoryPillButton(title: sending ? "Sending…" : "Send", primary: true) { store.sendVoice() }
                    .disabled(sending || TaskMessageRequest.clean(text) == nil)
                    .accessibilityIdentifier("history-send-voice")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Live.brass.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Live.brass.opacity(0.45), lineWidth: 0.75))
        .onAppear { focused = true }
        .accessibilityIdentifier("history-voice-review")
    }

    @ViewBuilder private var hint: some View {
        HStack(spacing: 6) {
            if case .problem(let why) = store.voice.phase {
                Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Live.red)
                Text(why).foregroundStyle(Live.red).lineLimit(2)
                Button("Try again") { store.tapMic() }.buttonStyle(.plain).foregroundStyle(Live.brassText).fontWeight(.semibold)
            } else if !enabled && store.target != .newChat {
                Text("This session can't take messages.")
            } else {
                switch store.voice.phase {
                case .recording, .asking: Text("Transcribed on this Mac. Nothing is sent until you press Send.")
                case .review: Text("↩ send · esc discard · replies show here only")
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
