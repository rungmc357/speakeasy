import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// Helpers a job started (servers with `job_subagents`): one compact row each (its name, a status
// dot, what it's doing now or how it ended, how long), a click opening its answer and its own
// steps right there. Only what the server sent; no section when it started none.

struct HelpersCard: View {
    let job: Job
    let info: WorkInfo
    @ObservedObject var model: VoicePanelModel
    /// `GET /voice/work/{run_id}`: each helper with its own steps (newest load wins).
    @State private var full: [JobSubagent] = []

    static func key(job: String, helper: String) -> String { "\(job)|helper:\(helper)" }

    var body: some View {
        let helpers = info.subagents.map { h in h.merged(with: full.first { $0.id == h.id }) }
        LiveCard(title: helpers.count == 1 ? "Helper" : "Helpers", symbol: "person.2.fill",
                 trailing: info.helpersSummary, padding: 4) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(helpers.enumerated()), id: \.element.id) { i, helper in
                    if i > 0 { Divider().overlay(Live.hairline).padding(.leading, 30) }
                    HelperRowMac(helper: helper, now: model.state.now,
                                 open: model.openSections.contains(Self.key(job: job.id, helper: helper.id)), model: model, jobID: job.id) {
                        withAnimation(Live.reduceMotion ? nil : .snappy(duration: 0.22)) {
                            model.toggleSection(Self.key(job: job.id, helper: helper.id))
                        }
                    }
                }
            }
        }
        .accessibilityIdentifier("job-helpers")
        .task(id: loadKey) { await load() }
    }

    /// Reload while a helper is open, as its steps land.
    private var loadKey: String {
        let open = info.subagents.filter { model.openSections.contains(Self.key(job: job.id, helper: $0.id)) }
        guard !open.isEmpty else { return "" }
        return "\(info.runID ?? "")|" + open.map { "\($0.id):\($0.stepsTotal ?? 0):\($0.status.rawValue)" }.joined(separator: ",")
    }

    private func load() async {
        guard !loadKey.isEmpty, let run = info.runID else { return }
        if let got = await model.loadSubagents(run), !got.isEmpty { full = got }
    }
}

struct HelperRowMac: View {
    let helper: JobSubagent
    var now: Date
    var open: Bool
    @ObservedObject var model: VoicePanelModel
    var jobID: String
    var toggle: () -> Void
    @State private var hover = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 10) {
                dot.frame(width: 14)
                VStack(alignment: .leading, spacing: 1) {
                    Text(helper.title).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(.primary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    // Open, its answer is spelled out below: the row keeps only what it's doing now.
                    if let line = helper.line, !open || helper.isRunning {
                        Text(line).font(.system(size: 11.5)).foregroundStyle(helper.status == .failed ? Live.red.opacity(0.9) : .secondary)
                            .lineLimit(1).truncationMode(.tail)
                    }
                }
                Spacer(minLength: 6)
                if let d = helper.elapsed(now: now) {
                    Text(Live.clock(d)).font(.system(size: 10.5).monospacedDigit()).foregroundStyle(.secondary).fixedSize()
                }
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)).foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(open ? 90 : 0))
            }
            .padding(.horizontal, 8).padding(.vertical, 8)
            .contentShape(Rectangle())
            .onTapGesture(perform: toggle)
            if open { expanded.padding(.leading, 32).padding(.trailing, 4).padding(.bottom, 10).transition(.opacity) }
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.white.opacity(hover ? 0.045 : 0)))
        .onHover { hover = $0 }
        .help(open ? "Fold" : "Show what this helper did")
        .accessibilityElement(children: open ? .contain : .combine)
        .accessibilityLabel("\(helper.title), \(helper.statusWord)")
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("helper-\(helper.id)")
    }

    @ViewBuilder private var dot: some View {
        switch helper.status {
        case .running: LiveDot(color: Live.brass, size: 7)
        case .done: Circle().fill(Live.green).frame(width: 7, height: 7)
        case .failed: Circle().fill(Live.red).frame(width: 7, height: 7)
        case .stopped: Circle().strokeBorder(Color.white.opacity(0.45), lineWidth: 1.3).frame(width: 8, height: 8)
        }
    }

    private var expanded: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let goal = helper.goal {
                label("Asked to")
                Text(goal).font(.system(size: 11.5)).foregroundStyle(Color.white.opacity(0.75))
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            if let summary = helper.summary {
                label(helper.status == .failed ? "What went wrong" : "What it found")
                Text(summary).font(.system(size: 12)).foregroundStyle(Color.white.opacity(0.9))
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            } else if let note = helper.note, !helper.isRunning {
                Text(note).font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            if !helper.activity.isEmpty {
                label(helper.activity.count == 1 ? "Its step" : "Its steps")
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(helper.activity.reversed()) { item in
                        ToolCallRow(item: item, live: helper.isRunning && item.status == .running, now: now,
                                    open: model.openSections.contains(ToolCallLog.rowKey(job: "\(jobID)|\(helper.id)", item: item.id))) {
                            model.toggleSection(ToolCallLog.rowKey(job: "\(jobID)|\(helper.id)", item: item.id))
                        }
                    }
                }
                .padding(.leading, -8)
            } else if helper.isRunning {
                Text("Its steps show here as it works.").font(.system(size: 11)).foregroundStyle(.tertiary)
            }
        }
    }

    private func label(_ text: String) -> some View {
        Text(text.uppercased()).font(.system(size: 9.5, weight: .semibold)).tracking(0.6).foregroundStyle(.tertiary)
    }
}
