import SwiftUI
import SpeakeasyCore

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
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Work").font(.title2); Spacer(); Button("Done") { dismiss() } }
            ScrollView {
                VStack(spacing: 10) {
                    ForEach(now.visible.filter { [.working, .waiting, .question, .stopped].contains($0.kind) }) { item in
                        MacNowCard(item: item, now: now) {
                            if let sid = item.sessionID { dismiss(); store.open(sid) }
                        }
                    }
                    ForEach(now.home?.background ?? []) { item in
                        Button { dismiss(); store.open(item.sessionID) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(AgentMarkdown.attributed(item.title)).font(.headline)
                                Text(AgentMarkdown.attributed(item.line)).font(.callout)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                        }.buttonStyle(.plain)
                    }
                }
            }
        }.padding(20).frame(minWidth: 340, idealWidth: 460, minHeight: 400)
    }
}
