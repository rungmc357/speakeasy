import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

struct MacNowThread: View {
    let stream: NowStream
    @ObservedObject var now: MacNowModel
    @ObservedObject var store: HistoryStore
    @State private var expanded: Set<String> = []

    var body: some View {
        let scoped = now.home?.call == nil ? now.visible : now.visible.filter(\.onCurrentCall)
        let byID = Dictionary(scoped.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        ForEach(stream.entries) { entry in
            switch entry.kind {
            case .ask:
                if let text = entry.text, let id = entry.itemID, byID[id] != nil { ask(text) }
            case .card:
                if let id = entry.itemID, let item = byID[id] { card(item) }
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
    private func ask(_ text: String) -> some View {
        HStack {
            Spacer(minLength: 32)
            Text(text).font(.callout).padding(12)
                .background(Color.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 16))
        }.accessibilityIdentifier("mac-now-ask")
    }
    private func card(_ item: NowItem) -> some View {
        MacNowCard(item: item, now: now, focused: now.home?.call?.focus == item.id) {
            if let sid = item.sessionID { store.open(sid) }
        }.id(item.id)
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
        }.padding(18).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
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
