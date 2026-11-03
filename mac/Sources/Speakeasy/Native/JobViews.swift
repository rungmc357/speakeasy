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
            approval: { AnyView(ApprovalRow(approval: $0, busy: model.busy, onChoice: model.onApproval)) })
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
                JobOrb(state: model.orbState, size: 36, level: model.orbLevel, mark: model.presentation.mark,
                       unreadAnswers: model.unreadAnswers, placement: "folded").contentShape(Circle())
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
        PanelScrollView(maxHeight: Self.scrollHeight(model), reveal: model.boardFocusID.flatMap { tileFrames[$0] }) {
            VStack(alignment: .leading, spacing: 7) {
                Text(summary(sections)).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(.secondary)
                    .padding(.bottom, 1)
                if !sections.needsYou.isEmpty {
                    SectionLabel(text: "Needs you")
                    ForEach(sections.needsYou) { job in tile(job) }
                }
                if !sections.newAnswers.isEmpty {
                    SectionLabel(text: "New answers").padding(.top, sections.needsYou.isEmpty ? 0 : 4)
                    ForEach(sections.newAnswers) { job in
                        AnswerTile(job: job, focused: model.boardFocusID == job.id,
                                   onDismiss: { model.dismissAnswer(job) }) { model.onOpenJob(job.id) }
                            .modifier(TileFrame(id: job.id))
                    }
                }
                if !sections.running.isEmpty {
                    SectionLabel(text: "Running").padding(.top, sections.needsYou.isEmpty && sections.newAnswers.isEmpty ? 0 : 4)
                    ForEach(sections.running) { job in tile(job) }
                }
                if !sections.doneToday.isEmpty {
                    SectionLabel(text: "Done today").padding(.top, sections.needsYou.isEmpty && sections.newAnswers.isEmpty && sections.running.isEmpty ? 0 : 4)
                    WrapLayout(spacing: 5) {
                        ForEach(sections.doneToday) { job in
                            DoneChip(job: job, focused: model.boardFocusID == job.id) { model.onOpenJob(job.id) }
                                .modifier(TileFrame(id: job.id))
                        }
                    }
                }
                if !FilmFrames.cleanShots {
                    Text(hint(sections)).font(.system(size: 10)).foregroundStyle(.tertiary).padding(.top, 3)
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

    private func summary(_ s: JobBoard.Sections) -> String {
        var parts: [String] = []
        if !s.needsYou.isEmpty { parts.append("\(s.needsYou.count) need\(s.needsYou.count == 1 ? "s" : "") you") }
        if !s.newAnswers.isEmpty { parts.append(s.newAnswers.count == 1 ? "1 new answer" : "\(s.newAnswers.count) new answers") }
        if !s.running.isEmpty { parts.append("\(s.running.count) running") }
        if !s.doneToday.isEmpty { parts.append("\(s.doneToday.count) done today") }
        return parts.joined(separator: " · ")
    }

    private func hint(_ s: JobBoard.Sections) -> String {
        let pick = s.needsYou.contains { $0.stage == .needsYou(.decide) } ? " · 1–3 pick" : ""
        return "↑↓ move · ↩ open\(pick) · esc fold into the orb"
    }

    @ViewBuilder private func tile(_ job: Job) -> some View {
        JobTile(job: job, model: model, focused: model.boardFocusID == job.id)
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

    var body: some View {
        let next = model.nextJobNeedingYou(after: job.id)
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                AllJobsButton { model.onOpenJob(nil) }
                Spacer(minLength: 4)
                Text("esc").font(.system(size: 10, weight: .medium)).foregroundStyle(.tertiary)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Color.primary.opacity(0.15), lineWidth: 0.75))
                    .help("Esc goes back to all jobs")
                    .accessibilityHidden(true)
            }
            HStack(spacing: 5) {
                Image(systemName: "mic.fill").font(.system(size: 9, weight: .semibold))
                Text("Talking to: \(job.name)").font(.system(size: 11, weight: .semibold)).lineLimit(1)
            }
            .padding(.horizontal, 9).padding(.vertical, 3)
            .background(Capsule().fill(JobStyle.blue.opacity(0.35)))
            .foregroundStyle(JobStyle.blueInk)
            .help("What you say now goes to this job unless it's clearly about something else")
            if let answer = job.answerTask, let headline = JobAnswer.headline(answer.info) {
                // An answer: the chip above names what was asked about; the answer itself leads, big.
                Text(headline).font(.system(size: 16, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    Text(job.name).font(.system(size: 15, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                    let line = taskStatusLine(job.activeTask?.info ?? job.newest.info, now: model.state.now, assistantName: model.state.assistantName)
                    Text(job.stage.needsYou || job.setAside ? JobDetailBody.stageLine(job) : line.0).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            PanelScrollView(maxHeight: min(JobStyle.scrollCap, 560 + model.extraHeight)) {
                VStack(alignment: .leading, spacing: 12) {
                    JobDetailBody(job: job, model: model, revision: UUID())
                        // The scroller hosts its content apart, so the environment is set inside it.
                        .environment(\.jobRenderers, JobStyle.renderers(model))
                }
                    .padding(.trailing, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let next {
                Button { model.onOpenJob(next.id) } label: {
                    HStack(spacing: 4) {
                        Spacer(minLength: 0)
                        Text(job.stage.needsYou ? "After you decide · Next:" : "Next:").foregroundStyle(.secondary)
                        Text("\(next.name) ›").fontWeight(.semibold)
                        Spacer(minLength: 0)
                    }
                    .font(.system(size: 11.5)).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Open the next job that needs you")
            }
        }
        .padding(.horizontal, 14).padding(.top, 10).padding(.bottom, 12)
        .environment(\.jobRenderers, JobStyle.renderers(model))
    }
}

