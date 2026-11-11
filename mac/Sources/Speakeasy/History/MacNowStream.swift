import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

struct MacNowThread: View {
    let stream: NowStream
    @ObservedObject var now: MacNowModel
    @ObservedObject var store: HistoryStore
    /// Scroll the thread to an entry (a job's card -> the result it posted further down).
    var onJump: (String) -> Void = { _ in }
    @State private var expanded: Set<String> = []

    var body: some View {
        let scoped = now.home?.call == nil ? now.visible : now.visible.filter(\.onCurrentCall)
        let byID = Dictionary(scoped.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        ForEach(stream.entries) { entry in
            switch entry.kind {
            case .result:
                // b73: what the job made (an email draft), posted at the bottom when it finished.
                if let id = entry.itemID, let item = byID[id], let run = entry.runID {
                    MacPostedResult(item: item, runID: run, now: now).id(entry.id)
                }
            case .ask:
                if let text = entry.text, let id = entry.itemID, byID[id] != nil { ask(text) }
            case .message where entry.role == .assistant && entry.itemID != nil:
                // SPK-314: a job that finished after you wrote something newer says its answer down
                // here too; its card keeps its place above. Gone with its card.
                if let id = entry.itemID, let item = byID[id], let text = entry.text { answerLine(text, item) }
            case .message:
                if entry.role == .user, !entry.files.isEmpty { sentFiles(entry.files) }
                if let text = entry.text {
                    if entry.role == .user { ask(text) } else { reply(text, entry.attribution) }
                }
            case .card:
                if let id = entry.itemID, let item = byID[id] {
                    if let result = entry.resultID {
                        // Done: its email is further down; a tap goes there.
                        card(item) { onJump(result) }
                    } else {
                        card(item)
                    }
                }
            case .group:
                let items = entry.itemIDs.compactMap { byID[$0] }
                if !items.isEmpty {
                    Button {
                        if expanded.contains(entry.id) { expanded.remove(entry.id) } else { expanded.insert(entry.id) }
                    } label: {
                        HStack {
                            Text(entry.line ?? "Earlier")
                            Spacer()
                            Image(systemName: expanded.contains(entry.id) ? "chevron.up" : "chevron.down")
                        }.font(.callout).padding(.vertical, 8)
                    }.buttonStyle(.plain).accessibilityIdentifier("mac-now-stretch")
                    if expanded.contains(entry.id) { ForEach(items) { card($0) } }
                }
            }
        }
        ForEach(now.visible.filter { $0.id.hasPrefix("pending:") }) { item in
            ask(item.title)
            card(item)
        }
    }
    /// What a Home message was sent with (SPK-220): one compact labeled tile per clip, picture or
    /// file, right-aligned above the words (the Mac has no poster for the iPhone's upload).
    private func sentFiles(_ files: [SentFile]) -> some View {
        HStack(spacing: 6) {
            Spacer(minLength: 32)
            ForEach(files) { file in
                Label(file.label, systemImage: file.symbol)
                    .font(.callout).lineLimit(1).truncationMode(.middle)
                    .padding(.horizontal, 10).padding(.vertical, 8)
                    .background(Color.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(file.spoken)
            }
        }.accessibilityIdentifier("mac-now-chat-files")
    }
    private func ask(_ text: String) -> some View {
        HStack {
            Spacer(minLength: 32)
            Text(text).font(.callout).padding(12)
                .background(Color.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }.accessibilityIdentifier("mac-now-ask")
    }
    /// The coordinator's reply in Home's chat: plain text on the left. No model under it (N13):
    /// Home's model lives in the composer chip; session threads keep per-message models.
    private func reply(_ text: String, _ attribution: ReplyAttribution?) -> some View {
        HStack {
            Text(AgentMarkdown.attributed(text)).font(.callout).textSelection(.enabled)
            .padding(12)
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            Spacer(minLength: 32)
        }.accessibilityIdentifier("mac-now-reply")
    }
    /// A finished job's answer at the bottom of the thread: the job's name over its answer, like a
    /// reply; clicking opens the job.
    private func answerLine(_ text: String, _ item: NowItem) -> some View {
        HStack {
            Button {
                if let sid = item.sessionID { store.open(sid) }
            } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Label(item.title, systemImage: item.kind == .stopped ? "exclamationmark.circle" : "checkmark.circle")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Text(AgentMarkdown.attributed(text)).font(.callout).multilineTextAlignment(.leading)
                }
                .padding(12)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            .buttonStyle(.plain)
            .help("Open \(item.title)")
            Spacer(minLength: 32)
        }.accessibilityIdentifier("mac-now-answer-line")
    }
    private func card(_ item: NowItem, open: (() -> Void)? = nil) -> some View {
        MacNowCard(item: item, now: now, focused: now.home?.call?.focus == item.id) {
            if let open { open() } else if let sid = item.sessionID { store.open(sid) }
        }
        // The model that did the work is a detail, not a line under every card (N13): on hover,
        // and on the job's own page.
        .help(item.workModel.map { "Worked on by \($0.label)" } ?? "")
        .id(item.id)
    }
}

struct MacNowRecent: View {
    @ObservedObject var now: MacNowModel
    @ObservedObject var store: HistoryStore
    @Environment(\.dismiss) private var dismiss
    @State private var rows: [NowRecentItem]?
    @State private var failed = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Recent").font(.title2); Spacer(); Button("Done") { dismiss() } }
            if let rows {
                if rows.isEmpty { Text("Nothing yet. What you ask for shows up here.").foregroundStyle(.secondary) }
                List(rows) { row in
                    Button {
                        if let sid = row.sessionID { dismiss(); store.open(sid) }
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(AgentMarkdown.attributed(row.title)).font(.headline)
                            Text(row.statusWords).font(.caption).foregroundStyle(.secondary)
                            if let line = row.line { Text(AgentMarkdown.attributed(line)).font(.callout).lineLimit(2) }
                        }.padding(.vertical, 4)
                    }.buttonStyle(.plain).disabled(row.sessionID == nil)
                }
            } else if failed {
                Text("Couldn't load Recent. Close this and try again.")
            } else { ProgressView() }
        }
        .padding(20).frame(minWidth: 340, idealWidth: 460, minHeight: 400)
        .task {
            if now.demo != nil {
                rows = now.visible.map { NowRecentItem(id: $0.id, title: $0.title, state: $0.kind == .working ? "running" : "completed",
                                                      at: $0.finishedAt ?? $0.startedAt ?? Date(), sessionID: $0.sessionID, line: $0.line) }
            } else if let api = now.api() {
                do { rows = try await api.nowRecent() } catch { failed = true }
            } else { rows = [] }
        }
    }
}

struct MacNowWorkList: View {
    @ObservedObject var now: MacNowModel
    @ObservedObject var store: HistoryStore
    var onClose: () -> Void
    @State private var category = "Needs you"
    private var categories: [String] { ["Needs you", "Running", "Done today"].filter { !items($0).isEmpty || ($0 == "Running" && !(now.home?.background.isEmpty ?? true)) } }
    private var current: String { categories.contains(category) ? category : categories.first ?? "Running" }
    private func items(_ section: String) -> [NowItem] {
        now.visible.filter {
            switch section {
            case "Needs you": return $0.kind == .question || $0.kind == .stopped
            case "Running": return $0.kind == .working || $0.kind == .waiting
            default: return $0.kind == .newAnswer || $0.kind == .seenAnswer
            }
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Work").font(.title2); Spacer(); Button("Collapse Work", action: onClose) }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack {
                    ForEach(categories, id: \.self) { name in
                        Button(name) { category = name }.buttonStyle(.bordered)
                            .tint(current == name ? JobLook.brass : .secondary)
                    }
                }
            }
            if categories.isEmpty { Text("All caught up.").foregroundStyle(.secondary); Spacer() }
            else {
                ScrollView(.horizontal) {
                    LazyHStack(alignment: .top, spacing: 12) {
                        ForEach(items(current)) { item in
                            ScrollView {
                                VStack(spacing: 12) {
                                    MacNowCard(item: item, now: now) { onClose(); if let sid = item.sessionID { store.open(sid) } }
                                    MacHomeWorkAnswer(item: item, now: now)
                                    Button("See the work") { onClose(); if let sid = item.sessionID { store.open(sid) } }
                                }.padding(4)
                            }.containerRelativeFrame(.horizontal).id(item.id)
                        }
                        if current == "Running" {
                            ForEach(now.home?.background ?? []) { item in
                                VStack(alignment: .leading, spacing: 12) {
                                    Text(AgentMarkdown.attributed(item.title)).font(.headline)
                                    Text(AgentMarkdown.attributed(item.line)).font(.callout)
                                    Button("See the work") { onClose(); store.open(item.sessionID) }
                                    Spacer()
                                }.containerRelativeFrame(.horizontal)
                            }
                        }
                    }.scrollTargetLayout()
                }.scrollTargetBehavior(.viewAligned).defaultScrollAnchor(.leading)
            }
        }.padding(16).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct MacHomeWorkAnswer: View {
    let item: NowItem
    @ObservedObject var now: MacNowModel
    @State private var info: WorkInfo?
    @State private var failed = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let info {
                if let run = item.runID, !info.images.isEmpty {
                    PictureGallery(images: info.images, identity: run) { number in
                        try? await now.api()?.image(runID: run, index: number)
                    }
                }
                ForEach(info.views.filter { $0.kind != "question" }) { ViewCardView($0) }
                if let full = info.result?.full { Text(AgentMarkdown.attributed(full)).textSelection(.enabled) }
            } else if failed {
                Button("Couldn't load the answer. Try again") { Task { await load() } }
            }
        }.task(id: item.updatedAt) { await load() }
    }
    private func load() async {
        guard now.demo == nil, let run = item.runID, let api = now.api() else { return }
        failed = false
        do { info = try await api.work(runID: run); failed = info == nil } catch { failed = true }
    }
}

/// A finished job's result posted at the bottom of Home's thread (b73): the email it drafted, with
/// its own Send · Edit · Copy. The job's card further up says done and links here.
private struct MacPostedResult: View {
    let item: NowItem
    let runID: String
    @ObservedObject var now: MacNowModel
    @Environment(\.cardStyle) private var style
    @State private var card: ViewCard?
    @State private var bump = 0

    var body: some View {
        Group {
            if let card {
                // The job's done line is on its card where it was asked: the email comes alone.
                ViewCardView(card)
                    .environment(\.cardSend, { action in await send(action, card: card) })
                    .cardSurface(style)
                    .frame(maxWidth: 520, alignment: .leading)
                    .accessibilityIdentifier("mac-now-result")
            }
        }
        .task(id: "\(item.updatedAt?.timeIntervalSince1970 ?? 0)|\(bump)") { await load() }
    }

    private func load() async {
        guard now.demo == nil, let api = now.api() else { return }
        if let info = try? await api.work(runID: runID), let pick = ResultCardPick.pick(info.views) { card = pick }
    }

    private func send(_ action: CardAction, card: ViewCard) async -> Bool {
        guard let api = now.api() else { return false }
        let value: Any? = { if case .null = action.value { return nil }; return action.value.foundation }()
        let took: Bool
        do {
            _ = try await api.cardAction(interactionID: nil, runID: runID, view: card.number - 1, action: action.id, value: value)
            took = true
        } catch { took = false }
        try? await Task.sleep(for: .milliseconds(400))
        bump += 1   // the server settled the new draft: draw it
        return took
    }
}
