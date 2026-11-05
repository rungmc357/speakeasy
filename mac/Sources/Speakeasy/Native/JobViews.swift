import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// The Mac panel's chrome around the shared job views (SpeakeasyClient/Jobs/JobViews.swift):
// the folded orb bar, the board's scrolling and key focus, and the job view's header.

extension JobStyle {
    /// Room a scrolling job area can take without pushing the panel off the screen.
    static var scrollCap: CGFloat {
        let screen = NSScreen.main?.visibleFrame.height ?? 800
        return max(260, screen - 260)
    }

    /// The Mac's review, draft and approval cards inside a job view.
    @MainActor static func renderers(_ model: VoicePanelModel) -> JobRenderers {
        JobRenderers(
            review: { review, heading in
                if let heading {
                    return AnyView(ImageReviewCard(review: review, shownCount: 1, model: model, inJob: true,
                                                   previewCap: JobCardStack.imageCap, heading: heading))
                }
                return AnyView(ImageReviewCard(review: review, shownCount: 1, model: model, inJob: true, previewCap: JobCardStack.imageCap))
            },
            draft: { AnyView(EmailDraftCard(draft: $0, model: model)) },
            approval: { a in AnyView(ApprovalRow(approval: a, task: model.state.tasks.last { $0.info.runID == a.runID },
                                                 busy: model.busy, onChoice: model.onApproval)) })
    }
}

// MARK: - The folded orb

/// The folded panel: the orb, plus the mic and hang-up so the call stays in reach.
struct OrbBar: View {
    @ObservedObject var model: VoicePanelModel
    var body: some View {
        let p = model.presentation
        HStack(spacing: 6) {
            Button(action: model.onToggleSlim) {
                JobOrb(state: model.orbState, size: 40, level: model.orbLevel,
                       mark: model.voicePhase == .hearing ? .listening : model.presentation.mark,
                       unreadAnswers: model.unreadAnswers, placement: "folded", hearing: model.voicePhase == .hearing)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .help(orbLabel)
            .accessibilityLabel(orbLabel)
            if model.state.connection != .paused {
                // Screen half and mic: sharing stays visible and stoppable while folded.
                MediaCapsule(model: model, presentation: p)
            } else if let label = p.pauseLabel {
                PauseButton(label: label, enabled: p.pauseEnabled, shortcutHint: model.pauseShortcutHint, action: model.onTogglePause)
                    .fixedSize()
            }
            EndButton(action: model.onEnd).fixedSize()
        }
        .padding(.horizontal, 8).padding(.vertical, 6)
        .frame(width: model.foldedWidth)
    }

    /// A request waiting for the screen is said on the orb (its line lives in the full panel).
    private var orbLabel: String {
        model.hold?.isWaiting == true ? "\(slimHoldSummary). Show the full panel" : "\(model.orbState.label). Open your jobs"
    }
}

// MARK: - Board

struct JobBoardView: View {
    @ObservedObject var model: VoicePanelModel

    /// Where each tile sits in the scrolling content, so the keyboard focus scrolls into view.
    @State private var tileFrames: [String: CGRect] = [:]

    /// The board's scroll room: what the screen allows, less the transcript above it when open.
    static func scrollHeight(_ model: VoicePanelModel) -> CGFloat {
        let transcript: CGFloat = model.captionExpanded ? 150 + model.extraHeight + 24 : 56
        return min(max(200, JobStyle.scrollCap - transcript), 520 + model.extraHeight)
    }

    var body: some View {
        let sections = model.boardSections
        // The first tile reveals from the very top, so the greeting above it comes back into view too.
        let reveal = model.boardFocusID.flatMap { id in
            tileFrames[id].map { id == sections.all.first?.id ? CGRect(x: 0, y: 0, width: $0.maxX, height: $0.maxY) : $0 }
        }
        PanelScrollView(maxHeight: Self.scrollHeight(model), reveal: reveal) {
            VStack(alignment: .leading, spacing: 8) {
                BoardGreetingView(sections: sections, now: model.state.now)
                    .padding(.bottom, 4)
                // A newer plugin: a tile of its own, in the board's content (never over it). The card
                // watches the notice itself and draws nothing when there's nothing to say.
                PluginUpdateCard(notice: model.pluginUpdate, onUpdate: model.onPluginUpdate,
                                 onOpenJob: model.onOpenPluginUpdateJob, onCopy: model.onCopyPluginUpdate)
                // One label per section; the tiles under it never repeat it.
                if !sections.needsYou.isEmpty {
                    BoardSectionLabel(text: "Needs you", count: sections.needsYou.count, color: Live.amber)
                    ForEach(sections.needsYou) { job in tile(job) }
                }
                if !sections.newAnswers.isEmpty {
                    BoardSectionLabel(text: "New answers", count: sections.newAnswers.count, color: Tokens.blue)
                        .padding(.top, sections.needsYou.isEmpty ? 0 : 6)
                    ForEach(sections.newAnswers) { job in
                        AnswerTile(job: job, focused: model.boardFocusID == job.id,
                                   spokenNow: model.state.connection == .live ? model.state.exchange.cleaned.assistant : nil,
                                   onDismiss: { model.dismissAnswer(job) }) { model.onOpenJob(job.id) }
                            .modifier(TileFrame(id: job.id))
                    }
                }
                if !sections.running.isEmpty {
                    BoardSectionLabel(text: "Running", count: sections.running.count, color: Live.brass)
                        .padding(.top, sections.needsYou.isEmpty && sections.newAnswers.isEmpty ? 0 : 6)
                    let wide = JobDetailView.isWide(model)
                    let cols = wide && sections.running.count > 1 ? [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)]
                                                                 : [GridItem(.flexible())]
                    LazyVGrid(columns: cols, alignment: .leading, spacing: 8) {
                        ForEach(sections.running) { job in
                            RunningTile(job: job, model: model, focused: model.boardFocusID == job.id)
                                .modifier(BoardCardActions(job: job, model: model, hoverDismiss: true))
                                .modifier(TileFrame(id: job.id))
                        }
                    }
                }
                if !sections.doneToday.isEmpty {
                    BoardSectionLabel(text: "Done today", count: sections.doneToday.count, color: Live.green)
                        .padding(.top, sections.needsYou.isEmpty && sections.newAnswers.isEmpty && sections.running.isEmpty ? 0 : 6)
                    WrapLayout(spacing: 5) {
                        ForEach(sections.doneToday) { job in
                            DoneChip(job: job, focused: model.boardFocusID == job.id) { model.onOpenJob(job.id) }
                                .modifier(BoardCardActions(job: job, model: model))
                                .modifier(TileFrame(id: job.id))
                        }
                    }
                }
                if !FilmFrames.cleanShots {
                    Text(hint(sections)).font(.system(size: 11)).foregroundStyle(.tertiary).padding(.top, 4)
                }
            }
            .padding(.trailing, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .coordinateSpace(name: "board")
            .onPreferenceChange(TileFrameKey.self) { frames in
                MainActor.assumeIsolated { if frames != tileFrames { tileFrames = frames } }
            }
        }
    }

    private func hint(_ s: JobBoard.Sections) -> String {
        let pick = s.needsYou.contains { $0.stage == .needsYou(.decide) } ? " · 1–3 pick" : ""
        return "↑↓ move · ↩ open\(pick) · esc fold into the orb"
    }

    @ViewBuilder private func tile(_ job: Job) -> some View {
        JobTile(job: job, model: model, focused: model.boardFocusID == job.id)
            .modifier(BoardCardActions(job: job, model: model, hoverDismiss: true))
            .modifier(TileFrame(id: job.id))
    }
}

struct TileFrameKey: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue()) { $1 }
    }
}

/// Reports a tile's frame in the board's scrolling content.
struct TileFrame: ViewModifier {
    var id: String
    func body(content: Content) -> some View {
        content.background(GeometryReader { g in
            Color.clear.preference(key: TileFrameKey.self, value: [id: g.frame(in: .named("board"))])
        })
    }
}

// MARK: - Job view

struct JobDetailView: View {
    var job: Job
    @ObservedObject var model: VoicePanelModel

    /// Wide enough for the live block's two columns.
    static func isWide(_ model: VoicePanelModel) -> Bool { model.panelWidth >= 560 }

    var body: some View {
        let next = model.nextJobNeedingYou(after: job.id)
        let wide = Self.isWide(model)
        let live = LiveJobBlock.has(job)
        let mood = LiveMood.of(job)
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                AllJobsButton { model.onOpenJob(nil) }
                if model.state.connection.isInCall { TalkingToChip(job: job) }
                Spacer(minLength: 4)
                // The redesigned page's one Stop: up here, never also under the steps.
                if live, let task = job.activeTask, task.canStop, let run = task.info.runID {
                    HeaderStopButton { model.onStopTask(run) }
                }
                KeyCap("esc").help("Esc goes back to all jobs")
            }
            LiveJobTitle(job: job, title: title, line: line, wide: wide, extra: extra)
                .padding(.top, 2)
            // Compare hides the footer but keeps its room, so the panel never grows past the window.
            let footer: CGFloat = job.decisionTask.map { model.answers[$0.id] == nil } == true ? 76 : 0
            PanelScrollView(maxHeight: min(JobStyle.scrollCap, 560 + model.extraHeight) - footer) {
                VStack(alignment: .leading, spacing: 14) {
                    if live { LiveJobBlock(job: job, model: model, wide: wide) }
                    JobDetailBody(job: job, model: model, revision: UUID())
                        // The scroller hosts its content apart, so the environment is set inside it.
                        .environment(\.jobRenderers, JobStyle.renderers(model))
                        .environment(\.jobHostDrawsSteps, live && LiveJobBlock.drawsSteps(job))
                        .environment(\.jobHostShowsNow, live)
                }
                    .padding(.trailing, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            // The decision's buttons stay in reach however long the brief above them is.
            if let task = job.decisionTask, let d = task.info.decision, model.answers[task.id] == nil,
               !model.openSections.contains("\(task.id)|compare") {
                DecisionFooter(job: job, decision: d, model: model)
            }
            if model.taskMessagesEnabled {
                // Type to this job, or (in a call) hold the mic to talk to it alone.
                TaskReplyBar(model: model, job: job)
                    .padding(.top, 2)
                    .overlay(alignment: .top) { Divider().opacity(0.5).offset(y: -5) }
            }
            if let next {
                NextJobLink(next: next, after: job.stage.needsYou) { model.onOpenJob(next.id) }
            }
        }
        .padding(.horizontal, wide ? 20 : 14).padding(.top, 10).padding(.bottom, 12)
        .background(alignment: .top) { MoodGlow(mood: mood).frame(height: 230) }
        .environment(\.jobRenderers, JobStyle.renderers(model))
    }

    /// An answer leads with what it found; anything else with the job's name.
    private var title: String {
        if let answer = job.answerTask, let headline = JobAnswer.headline(answer.info) { return headline }
        return job.name
    }

    private var line: String? {
        if job.answerTask != nil { return job.name }
        if let picked = JobDetailBody.pickedLine(job, model: model) { return picked }
        if job.stage.needsYou || job.setAside { return JobDetailBody.stageLine(job) }
        if job.stage == .failed { return nil }   // the body says what went wrong
        return taskStatusLine(job.activeTask?.info ?? job.newest.info, now: model.state.now, assistantName: model.state.assistantName).0
    }

    /// Next to a finished job's mood: how long it took.
    private var extra: String? {
        guard !LiveMood.of(job).isLive else { return nil }
        return JobProgress.elapsed(job.newest.info, now: model.state.now).map { "took \($0)" }
    }
}

/// "Talking to: <job>" while a call is open: what's said now goes to this job.
struct TalkingToChip: View {
    var job: Job
    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "mic.fill").font(.system(size: 8.5, weight: .semibold))
            Text("Talking to this job").font(.system(size: 10.5, weight: .semibold)).lineLimit(1)
        }
        .padding(.horizontal, 8).frame(height: 22)
        .background(Capsule().fill(JobStyle.blue.opacity(0.18)))
        .foregroundStyle(JobStyle.blueInk)
        .help("What you say now goes to “\(job.name)” unless it's clearly about something else")
    }
}

struct HeaderStopButton: View {
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Label("Stop", systemImage: "stop.fill").font(.system(size: 11, weight: .semibold))
                .labelStyle(.titleAndIcon)
                .foregroundStyle(hover ? Color.white : JobStyle.red)
                .padding(.horizontal, 9).frame(height: 22)
                .background(Capsule().fill(JobStyle.stopRed.opacity(hover ? 0.55 : 0.2)))
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Stop this task. The call and other jobs continue.")
    }
}

struct KeyCap: View {
    var key: String
    init(_ key: String) { self.key = key }
    var body: some View {
        Text(key).font(.system(size: 10.5, weight: .medium)).foregroundStyle(.secondary)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Color.primary.opacity(0.15), lineWidth: 0.75))
            .accessibilityHidden(true)
    }
}

struct NextJobLink: View {
    var next: Job
    var after: Bool
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Spacer(minLength: 0)
                Text(after ? "After you decide · Next:" : "Next:").foregroundStyle(.secondary)
                Text("\(next.name) ›").fontWeight(.semibold).foregroundStyle(hover ? JobStyle.blueInk : .primary)
                Spacer(minLength: 0)
            }
            .font(.system(size: 11.5)).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Open the next job that needs you")
    }
}


/// Pinned under a decision's scrolling brief: Approve the recommendation (when the plugin allows it)
/// and a numbered button per option, so a long brief never pushes the choice off the panel.
struct DecisionFooter: View {
    var job: Job
    var decision: Decision
    @ObservedObject var model: VoicePanelModel
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if decision.approvable, let rec = decision.recommendedOption {
                TileButton(title: "Approve: \(rec.text)", primary: true) { model.approve(job) }
                    .lineLimit(1)
            }
            HStack(spacing: 5) {
                Text("Pick").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
                ForEach(Array(decision.options.prefix(4).enumerated()), id: \.offset) { i, o in
                    Button { model.pick(i + 1, in: job) } label: {
                        HStack(spacing: 4) {
                            NumberBadge(number: i + 1)
                            Text(o.text).font(.system(size: 11.5, weight: .medium)).lineLimit(1).truncationMode(.tail)
                        }
                        .padding(.horizontal, 6).frame(height: 26).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .fill(i == decision.recommended ? JobStyle.blue.opacity(0.22) : Color.primary.opacity(0.07)))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(o.text)
                }
            }
        }
        .padding(.top, 4)
        .overlay(alignment: .top) { Divider().opacity(0.5).offset(y: -4) }
    }
}
