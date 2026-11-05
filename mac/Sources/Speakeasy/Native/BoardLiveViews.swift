import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// The Mac board in the redesign's language: a greeting with where things stand, one label per
// section, running jobs as live tiles with their work drawn small, and an empty board that says
// hello and what to try.

struct BoardGreetingView: View {
    var sections: JobBoard.Sections
    var now: Date
    var body: some View {
        let summary = BoardGreeting.summary(sections)
        VStack(alignment: .leading, spacing: 2) {
            Text(BoardGreeting.greeting(hour: Calendar.current.component(.hour, from: now)))
                .font(.system(size: 19, weight: .bold))
            if !summary.isEmpty {
                Text(summary).font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// A section's one label: a coloured dot, the name in small caps, its count.
struct BoardSectionLabel: View {
    var text: String
    var count: Int
    var color: Color
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 5, height: 5)
            Text(text.uppercased()).font(.system(size: 10.5, weight: .bold)).tracking(0.9).foregroundStyle(Color.primary.opacity(0.62))
            Text("\(count)").font(.system(size: 10.5, weight: .semibold).monospacedDigit()).foregroundStyle(.tertiary)
        }
        .padding(.leading, 2)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// A running job: its name, a ticking clock, what it's doing, and its work drawn small.
struct RunningTile: View {
    let job: Job
    @ObservedObject var model: VoicePanelModel
    var focused: Bool
    @State private var hover = false

    var body: some View {
        let info = (job.activeTask ?? job.newest).info
        let waiting = info.status == "waiting_on_task"
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 7) {
                LiveDot(color: waiting ? Color.primary.opacity(0.4) : Live.brass, size: 6, active: !waiting)
                Text(job.name).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 4)
                if waiting {
                    JobPill(text: "Waiting", style: .grey)
                } else if let start = JobProgress.start(info) {
                    TimelineView(.periodic(from: .now, by: 1)) { ctx in
                        Text(Live.clock(ctx.date.timeIntervalSince(start))).font(.system(size: 11, weight: .medium).monospacedDigit())
                            .foregroundStyle(Live.brassText)
                    }
                }
            }
            Text(taskStatusLine(info, now: model.state.now, assistantName: model.state.assistantName).0)
                .font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
            if let glance = JobGlance.of(info) { MiniGlance(glance: glance) }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(hover ? 0.085 : 0.055)))
        .background(alignment: .topLeading) {
            RadialGradient(colors: [Live.brass.opacity(0.16), .clear], center: .topLeading, startRadius: 0, endRadius: 160)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .strokeBorder(focused ? JobStyle.blueInk : Color.primary.opacity(hover ? 0.14 : 0.07), lineWidth: focused ? 1.5 : 0.6))
        .contentShape(Rectangle())
        .onTapGesture { model.onOpenJob(job.id) }
        .onHover { hover = $0 }
        .help("Open “\(job.name)”")
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }
}

/// A job's hero in a line: dots, site marks, a diff's counts, a step bar, or the call it's on.
struct MiniGlance: View {
    var glance: JobGlance
    var body: some View {
        switch glance {
        case let .trials(passed, failed, total):
            HStack(spacing: 8) {
                HStack(spacing: 2.5) {
                    let shown = min(total, 25)
                    ForEach(0..<shown, id: \.self) { i in
                        let at = Double(i) / Double(shown) * Double(total)
                        Circle().fill(at < Double(passed) ? Live.green : at < Double(passed + failed) ? Live.red : Color.primary.opacity(0.12))
                            .frame(width: 6, height: 6)
                    }
                }
                Spacer(minLength: 0)
                Text("\(passed)/\(total) passed").font(.system(size: 11, weight: .medium).monospacedDigit()).foregroundStyle(.secondary).fixedSize()
            }
        case let .sources(count, sites):
            HStack(spacing: 8) {
                HStack(spacing: -5) { ForEach(Array(sites.enumerated()), id: \.offset) { i, s in SiteBadge(site: s, size: 18).zIndex(Double(i)) } }
                Text("\(count) \(count == 1 ? "source" : "sources") read").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
        case let .diff(files, added, removed):
            HStack(spacing: 8) {
                Text("\(files) \(files == 1 ? "file" : "files")").foregroundStyle(.secondary)
                Text("+\(added)").foregroundStyle(Live.green)
                Text("−\(removed)").foregroundStyle(Live.red)
                Spacer(minLength: 0)
            }
            .font(.system(size: 11, weight: .semibold).monospacedDigit())
        case let .steps(index, count):
            HStack(spacing: 8) {
                StepBar(done: index - 1, total: count)
                Text("Step \(index) of \(count)").font(.system(size: 11, weight: .medium).monospacedDigit()).foregroundStyle(.secondary).fixedSize()
            }
        case let .now(text):
            HStack(spacing: 6) {
                LiveSpinner(size: 9)
                Text(text).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

/// Nothing on the board: a greeting, and a few things to try (said, or typed in a call).
struct EmptyBoardView: View {
    var now: Date
    var inCall: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(BoardGreeting.greeting(hour: Calendar.current.component(.hour, from: now)))
                    .font(.system(size: 21, weight: .bold))
                Text(inCall ? "Nothing running. Say what you need and it starts here." : "Nothing running. Start a call and say what you need.")
                    .font(.system(size: 12.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            BoardSectionLabel(text: "Try saying", count: BoardGreeting.suggestions.count, color: Live.brass).padding(.top, 4)
            VStack(alignment: .leading, spacing: 6) {
                ForEach(BoardGreeting.suggestions, id: \.self) { SuggestionRow(text: $0) }
            }
        }
        .padding(.horizontal, 2)
    }
}

struct SuggestionRow: View {
    var text: String
    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: "quote.opening").font(.system(size: 10, weight: .bold)).foregroundStyle(Live.brass)
            Text(text).font(.system(size: 12.5)).foregroundStyle(.primary).lineLimit(2)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11).frame(minHeight: 34)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.primary.opacity(0.06), lineWidth: 0.5))
        .help("Say this in a call")
    }
}
