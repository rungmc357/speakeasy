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

    var visible: [NowItem] { (home?.items ?? []).filter { !hidden.contains($0.id) } }

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
            onQuestions(fresh.counts.needsYou)
        } catch {
            failed = true
        }
    }

    func answer(_ item: NowItem, option: NowQuestion.Option) {
        hidden.insert(item.id)
        guard demo == nil, let api = api() else { return }
        Task { try? await api.nowAnswer(item.id, optionID: option.id); await refresh() }
    }

    func approve(_ item: NowItem, choice: String) {
        hidden.insert(item.id)
        guard demo == nil, let api = api() else { return }
        Task { try? await api.nowApproval(item.id, choice: choice); await refresh() }
    }

    func clearSeen() {
        for i in visible where i.kind == .seenAnswer { hidden.insert(i.id) }
        guard demo == nil, let api = api() else { return }
        Task { _ = try? await api.nowClearSeen(); await refresh() }
    }
}

struct MacNowColumn: View {
    @ObservedObject var now: MacNowModel
    @ObservedObject var store: HistoryStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(now.home?.sentence ?? (now.failed ? "Can't reach Hermes right now." : "All quiet."))
                    .font(.title2.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 4)
                    .accessibilityAddTraits(.isHeader)
                if let summary = now.home?.callSummary {
                    callSummary(summary)
                }
                ForEach(now.visible) { item in
                    MacNowCard(item: item, now: now, selected: isOpen(item), focused: now.home?.call?.focus == item.id) {
                        if let sid = item.sessionID { store.open(sid) }
                    }
                }
                if now.visible.contains(where: { $0.kind == .seenAnswer }) {
                    Button("Clear seen") { now.clearSeen() }
                        .buttonStyle(.link).font(.callout).padding(.top, 2)
                }
                if let n = now.home?.backgroundRunning, n > 0 {
                    Label("\(n) running on \(n == 1 ? "its" : "their") own", systemImage: "gearshape.2")
                        .font(.callout).foregroundStyle(.secondary).padding(.top, 4)
                }
            }
            .padding(.horizontal, 18).padding(.top, 44).padding(.bottom, 18)
        }
        .onAppear { now.start() }
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
            (Text(s.started + " ").foregroundStyle(.secondary)
             + (s.youSaid.map { Text("You said: " + $0).fontWeight(.medium) } ?? Text("")))
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
                Text(item.title).font(.headline).lineLimit(1)
                Spacer(minLength: 6)
                if let meta { Text(meta).font(.caption).foregroundStyle(focused || item.onCall == .asking ? JobLook.brass : .secondary) }
            }
            if !item.line.isEmpty {
                Text(item.line).font(.callout).foregroundStyle(.secondary).lineLimit(3)
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
            HStack(spacing: 6) {
                ForEach(Array(q.options.prefix(3).enumerated()), id: \.element.id) { i, option in
                    if i == (q.recommended ?? -1) {
                        Button(option.text) { now.answer(item, option: option) }.buttonStyle(.borderedProminent).tint(JobLook.brass)
                    } else {
                        Button(option.text) { now.answer(item, option: option) }.buttonStyle(.bordered)
                    }
                }
            }
            .controlSize(.small).lineLimit(1)
            if let lock = item.lockLine {
                Label(lock, systemImage: "lock.fill").font(.caption).foregroundStyle(.secondary)
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
        case .question, .newAnswer: return .accentColor
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
