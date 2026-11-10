import AppKit
import SpeakeasyClient
import SpeakeasyCore
import SwiftUI

/// Home on the Mac (SPK-124 phase 4b): the main window's middle column, between the sidebar and the
/// conversation. The same `GET /voice/now` list as the iPhone and iPad, in the plugin's order, under
/// its header sentence. Clicking a card opens its conversation beside it; a question's choices
/// answer in place. The menu bar glyph gets its dot while a question waits.
@MainActor
final class MacNowModel: ObservableObject {
    static let shared = MacNowModel()

    @Published private(set) var home: NowHome?
    @Published private(set) var failed = false
    /// Answered or set aside here: hidden until the next fetch agrees.
    @Published private(set) var hidden: Set<String> = []
    /// The app's API (nil before pairing); replaced by the demo in screenshots.
    var api: () -> ServerClient? = { nil }
    var demo: NowHome?
    /// The menu bar's dot: a question is waiting.
    var onQuestions: (Int) -> Void = { _ in }
    private var loop: Task<Void, Never>?

    var visible: [NowItem] { pendingItems + (home?.items ?? []).filter { !hidden.contains($0.id) } }

    // MARK: Started from Home's composer (SPK-198)

    /// Something sent from Home's composer: a working card at once, until Home's own list carries
    /// its task or chat (or a minute passes: then Home's list is the truth).
    struct Pending: Equatable {
        let id: String
        let title: String
        let at: Date
        var runID: String?
        var sessionID: String?
    }
    @Published private(set) var pending: [Pending] = []
    nonisolated static let pendingKeptS: TimeInterval = 60

    func addPending(id: String, title: String, now: Date = Date()) {
        withAnimation(.spring(response: 0.42, dampingFraction: 0.9)) {
            pending.insert(Pending(id: id, title: title, at: now), at: 0)
        }
    }

    func pendingStarted(id: String, runID: String?, sessionID: String?) {
        guard let i = pending.firstIndex(where: { $0.id == id }) else { return }
        pending[i].runID = runID
        pending[i].sessionID = sessionID
        prunePending()
    }

    func dropPending(_ id: String) { withAnimation { pending.removeAll { $0.id == id } } }

    func prunePending(now: Date = Date()) {
        let keep = Self.unmatched(pending, items: home?.items ?? [], now: now, demo: demo != nil)
        if keep != pending { pending = keep }
    }

    nonisolated static func unmatched(_ pending: [Pending], items: [NowItem], now: Date, demo: Bool = false) -> [Pending] {
        pending.filter { p in
            if !demo && now.timeIntervalSince(p.at) > pendingKeptS { return false }
            return !items.contains { item in
                (p.runID != nil && item.runID == p.runID) || (p.sessionID != nil && item.sessionID == p.sessionID)
            }
        }
    }

    private var pendingItems: [NowItem] {
        pending.map { p in
            NowItem(id: "pending:" + p.id, kind: .working, sessionID: p.sessionID, runID: p.runID, title: p.title,
                    line: "Getting started", startedAt: p.at)
        }
    }

    func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 20_000_000_000)
            }
        }
    }

    func stop() { loop?.cancel(); loop = nil }

    func refresh() async {
        if let demo { home = demo; return }
        guard let api = api() else { return }
        do {
            let fresh = try await api.now()
            home = fresh; failed = false; hidden = []
            prunePending()
            onQuestions(fresh.counts.needsYou)
        } catch {
            failed = true
        }
    }

    @Published var responseError: String?

    private func respond(_ item: NowItem, line: String, _ send: @escaping (ServerClient) async throws -> Void) {
        let original = home?.items.first { $0.id == item.id }
        if let index = home?.items.firstIndex(where: { $0.id == item.id }) {
            home?.items[index].kind = .working
            home?.items[index].line = line
            home?.items[index].question = nil
            if let entry = home?.stream?.entries.firstIndex(where: { $0.itemID == item.id && $0.kind == .card }) {
                home?.stream?.entries[entry].widget = .confirmed
            }
        }
        if demo != nil { demo = home; return }
        Task {
            do {
                guard let api = api() else { throw URLError(.notConnectedToInternet) }
                try await send(api); await refresh()
            } catch {
                if let original, let index = home?.items.firstIndex(where: { $0.id == item.id }) { home?.items[index] = original }
                responseError = "That didn't go through. Try again."
            }
        }
    }
    func answer(_ item: NowItem, option: NowQuestion.Option) {
        respond(item, line: "You chose \u{201C}\(option.text)\u{201D}. Continuing…") { try await $0.nowAnswer(item.id, optionID: option.id) }
    }

    func approve(_ item: NowItem, choice: String) {
        respond(item, line: choice == "deny" ? "You didn't allow this." : "Allowed. Continuing…") { try await $0.nowApproval(item.id, choice: choice) }
    }

    /// A card's Dismiss (its menu): off Home to Earlier. There is no Clear-all (b72).
    func dismiss(_ item: NowItem) {
        hidden.insert(item.id)
        guard demo == nil, let api = api() else { return }
        Task { try? await api.nowDismiss(item.id); await refresh() }
    }
}

struct MacNowColumn: View {
    @ObservedObject var now: MacNowModel
    @ObservedObject var store: HistoryStore
    var voice: VoicePanelModel? = nil
    @ObservedObject var composer = MacHomeComposerModel.shared
    @State private var recentOpen = false
    @State private var workOpen = false
    @State private var focusID: String?

    var body: some View {
        VStack(spacing: 0) {
            if now.home?.stream != nil {
                NowWorkStrip(items: now.visible, background: now.home?.background ?? [], onOpen: { item in
                    if item.kind == .question { focusID = nil; focusID = item.id }
                    else if let sid = item.sessionID { store.open(sid) }
                }, onBackground: { store.open($0.sessionID) }, onBoard: { workOpen = true }, pill: now.home?.pill)
                    .padding(.horizontal, 18).padding(.top, 12)
            }
            ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    // No counting header (build 69): only "can't reach", or (SPK-198) a small, quiet
                    // "All caught up." with a few things to try that fill the composer.
                    if now.failed && now.home == nil {
                        Text("Can't reach Hermes right now.").font(.title3.weight(.semibold)).padding(.bottom, 4)
                    } else if now.home?.caughtUp ?? false, now.visible.isEmpty {
                        MacNowEmpty { composer.fill($0) }
                    }
                    if let summary = now.home?.callSummary {
                        callSummary(summary)
                    }
                    if let stream = now.home?.stream {
                        MacNowThread(stream: stream, now: now, store: store,
                                     onJump: { id in withAnimation { proxy.scrollTo(id, anchor: .center) } })
                    } else {
                        ForEach(now.visible) { item in
                            MacNowCard(item: item, now: now, selected: isOpen(item), focused: now.home?.call?.focus == item.id) {
                                if let sid = item.sessionID { store.open(sid) }
                            }
                        }
                    }
                    if now.home?.stream == nil, let n = now.home?.backgroundRunning, n > 0 {
                        Label("\(n) running on \(n == 1 ? "its" : "their") own", systemImage: "gearshape.2")
                            .font(.callout).foregroundStyle(.secondary).padding(.top, 4)
                    }
                }
                    Button("Recent") { recentOpen = true }
                        .buttonStyle(.link).accessibilityIdentifier("mac-now-recent")
                .padding(.horizontal, 18).padding(.top, 12).padding(.bottom, 18)
            }
            .defaultScrollAnchor(now.home?.stream != nil ? .bottom : .top)
            .onChange(of: focusID) { _, id in
                if let id { withAnimation { proxy.scrollTo(id, anchor: .center) } }
            }
            }
            .overlay {
                if workOpen {
                    MacNowWorkList(now: now, store: store, onClose: { workOpen = false })
                        .background(HistoryLook.window)
                }
            }
            // Home's bottom bar (SPK-198): the composer with the round call button beside it; on a
            // call it becomes the call's dock.
            MacHomeBottomBar(voice: voice, composer: composer)
        }
        .onAppear { now.start() }
        .sheet(isPresented: $recentOpen) { MacNowRecent(now: now, store: store) }
        .alert("Couldn't send", isPresented: Binding(get: { now.responseError != nil }, set: { if !$0 { now.responseError = nil } })) {
            Button("Close") { now.responseError = nil }
        } message: { Text(now.responseError ?? "Try again.") }
        .accessibilityIdentifier("mac-now")
    }

    private func isOpen(_ item: NowItem) -> Bool {
        guard let sid = item.sessionID, case .session(let open) = store.target else { return false }
        return sid == open
    }

    private func callSummary(_ s: NowCallSummary) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label("Your call", systemImage: "phone.fill").font(.headline)
                Spacer()
                Text("\(s.minutes) min").font(.callout).foregroundStyle(.secondary)
            }
            Text(s.summaryLine).foregroundStyle(.secondary)
                .font(.callout).fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.05)))
        .onTapGesture { if let sid = s.sessionID { store.open(sid) } }
    }
}

struct MacNowCard: View {
    let item: NowItem
    @ObservedObject var now: MacNowModel
    var selected = false
    var focused = false
    let onOpen: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: symbol).foregroundStyle(tint).frame(width: 18)
                Text(AgentMarkdown.attributed(item.title)).font(.headline).lineLimit(1)
                Spacer(minLength: 6)
                if let meta { Text(meta).font(.caption).foregroundStyle(focused || item.onCall == .asking ? JobLook.brass : .secondary) }
            }
            if !item.line.isEmpty {
                Text(AgentMarkdown.attributed(item.line)).font(.callout).foregroundStyle(.secondary).lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, 26)
            }
            if let q = item.question { choices(q).padding(.leading, 26).padding(.top, 2) }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .fill(selected ? HistoryLook.selected : Color.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .strokeBorder(focused ? JobLook.brass : selected ? JobLook.brass.opacity(0.4) : .clear,
                          lineWidth: focused ? 1.5 : 1))
        .opacity(item.kind == .seenAnswer && !selected ? 0.8 : 1)
        .contentShape(Rectangle())
        .onTapGesture(perform: onOpen)
        // No Clear on Home (b72): a card leaves from its own menu, to Earlier.
        .contextMenu { Button("Dismiss") { now.dismiss(item) } }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("mac-now-\(item.id)")
    }

    @ViewBuilder private func choices(_ q: NowQuestion) -> some View {
        if q.shape == .approval {
            HStack {
                Button("Allow once") { now.approve(item, choice: "once") }
                Button("Deny") { now.approve(item, choice: "deny") }
            }
            .controlSize(.small)
        } else {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 6) { optionButtons(q) }.fixedSize(horizontal: true, vertical: false)
                VStack(alignment: .leading, spacing: 6) { optionButtons(q) }
            }
            .controlSize(.small)
            if let lock = item.lockLine {
                Label(lock, systemImage: "lock.fill").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private func optionButtons(_ q: NowQuestion) -> some View {
        ForEach(Array(q.options.prefix(3).enumerated()), id: \.element.id) { i, option in
            if i == (q.recommended ?? -1) {
                Button(option.text) { now.answer(item, option: option) }
                    .buttonStyle(.borderedProminent).tint(JobLook.brass)
            } else {
                Button(option.text) { now.answer(item, option: option) }.buttonStyle(.bordered)
            }
        }
    }

    private var symbol: String {
        switch item.kind {
        case .question: return "questionmark.circle.fill"
        case .stopped: return "exclamationmark.triangle.fill"
        case .working: return "circle.dotted"
        case .waiting: return "hourglass"
        case .newAnswer: return "checkmark.circle.fill"
        case .seenAnswer: return "checkmark.circle"
        }
    }

    private var tint: Color {
        switch item.kind {
        case .question, .newAnswer: return JobLook.brass
        case .stopped: return .orange
        default: return .secondary
        }
    }

    private var meta: String? {
        if item.onCall == .asking { return "Asking now" }
        if focused { return "Talking about this" }
        guard let d = item.finishedAt ?? item.updatedAt ?? item.startedAt else { return nil }
        let mins = Int(Date().timeIntervalSince(d) / 60)
        return mins < 1 ? "now" : mins < 60 ? "\(mins) min ago" : "\(mins / 60) hr ago"
    }
}
