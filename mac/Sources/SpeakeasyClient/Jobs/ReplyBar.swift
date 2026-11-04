import SwiftUI
import SpeakeasyCore
#if os(iOS)
import UIKit
#endif

// The job page's reply bar and the decision card's "Something else…" field (servers with
// `task_messages` only). What's typed goes to this job by run id, never through the voice's router,
// and shows as the user's own turn here (never as something the assistant said).

/// What the user sent to this job: their own bubbles on the right, with how each one is doing.
public struct SentMessages: View {
    @ObservedObject var model: VoicePanelModel
    var jobID: String
    var choiceTaskID: String?
    var limit = 3
    @Environment(\.jobTextScale) private var s

    public init(model: VoicePanelModel, jobID: String, choiceTaskID: String? = nil, limit: Int = 3) {
        self.model = model; self.jobID = jobID; self.choiceTaskID = choiceTaskID; self.limit = limit
    }

    public var body: some View {
        let mine = model.outbox.messages(forJob: jobID).filter { choiceTaskID == nil ? $0.choiceTaskID == nil : $0.choiceTaskID == choiceTaskID }
        if !mine.isEmpty {
            VStack(alignment: .trailing, spacing: 7 * s) {
                ForEach(mine.suffix(limit)) { m in row(m) }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
    }

    @ViewBuilder private func row(_ m: OutgoingMessage) -> some View {
        VStack(alignment: .trailing, spacing: 3 * s) {
            Text(m.text).font(.system(size: 12.5 * s))
                .foregroundStyle(Color.white)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .padding(.horizontal, 10 * s).padding(.vertical, 6 * s)
                .background(RoundedRectangle(cornerRadius: 12 * s, style: .continuous)
                    .fill(m.isFailed ? AnyShapeStyle(Color.primary.opacity(0.10)) : AnyShapeStyle(JobStyle.blue.opacity(m.isPending ? 0.6 : 0.92))))
                .overlay(RoundedRectangle(cornerRadius: 12 * s, style: .continuous)
                    .strokeBorder(m.isFailed ? JobStyle.red.opacity(0.55) : .clear, lineWidth: 1))
            status(m)
        }
        .padding(.leading, 40 * s)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private func status(_ m: OutgoingMessage) -> some View {
        switch m.state {
        case .queued:
            Label("Waiting to send", systemImage: "clock").labelStyle(SmallLabel(s: s)).foregroundStyle(.secondary)
        case .sending:
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text("Sending…").font(.system(size: 10.5 * s, weight: .medium))
            }
            .foregroundStyle(.secondary)
        case .sent(let how2):
            Label(how2 == .followUp ? "Sent · started a follow-up" : how2 == .answer ? "Sent as your answer" : "Sent",
                  systemImage: "checkmark").labelStyle(SmallLabel(s: s)).foregroundStyle(.secondary)
        case .failed(let message):
            VStack(alignment: .trailing, spacing: 4 * s) {
                Text(message).font(.system(size: 11 * s)).foregroundStyle(JobStyle.red)
                    .multilineTextAlignment(.trailing).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10 * s) {
                    Button("Discard") { model.discardMessage(m.id) }
                        .font(.system(size: 11 * s, weight: .medium)).foregroundStyle(.secondary)
                    Button { model.retryMessage(m.id) } label: {
                        Label("Retry", systemImage: "arrow.clockwise").font(.system(size: 11 * s, weight: .semibold))
                            .padding(.horizontal, 9 * s).frame(height: 22 * s)
                            .background(Capsule().fill(Color.primary.opacity(0.12)))
                    }
                    .foregroundStyle(Color.primary)
                    .accessibilityIdentifier("message-retry")
                }
                .buttonStyle(.plain)
            }
        }
    }

    struct SmallLabel: LabelStyle {
        var s: CGFloat
        func makeBody(configuration: Configuration) -> some View {
            HStack(spacing: 3) {
                configuration.icon.font(.system(size: 9 * s, weight: .bold))
                configuration.title.font(.system(size: 10.5 * s, weight: .medium))
            }
        }
    }
}

/// The bottom of a job page: a text field and a mic. In a call the mic is hold-to-talk to this job
/// ("Talking to … · release to stop"); otherwise it dictates into the field to review and send.
public struct TaskReplyBar: View {
    @ObservedObject var model: VoicePanelModel
    var job: Job
    @ObservedObject var dictation: ReplyDictation
    /// Draw what was sent above the field. The iPhone shows it in the page instead (the bar stays small).
    var showsSent = true
    @FocusState private var focused: Bool
    @Environment(\.jobTextScale) private var s
    /// Text in the field before dictation started (heard words are added after it).
    @State private var dictationBase = ""
    @State private var pressing = false

    @MainActor public init(model: VoicePanelModel, job: Job, showsSent: Bool = true, dictation: ReplyDictation? = nil) {
        self.model = model; self.job = job; self.showsSent = showsSent; self.dictation = dictation ?? .shared
    }

    private var draft: Binding<String> {
        Binding(get: { model.replyDrafts[job.id] ?? "" }, set: { model.replyDrafts[job.id] = $0.isEmpty ? nil : $0 })
    }
    private var holding: Bool { model.heldJobID == job.id }
    private var dictating: Bool { dictation.isListening(to: job.id) }
    private var canSend: Bool { TaskMessageRequest.clean(draft.wrappedValue) != nil }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8 * s) {
            if showsSent { SentMessages(model: model, jobID: job.id) }
            if holding {
                holdBanner
            } else if dictating {
                note("Listening… tap the mic to stop, then review and send.", tone: JobStyle.blueInk, symbol: "waveform")
            } else if case .denied(let why) = dictation.phase, dictation.jobID == job.id {
                note(why, tone: JobStyle.tileAmber, symbol: "mic.slash")
            } else if case .failed(let why) = dictation.phase, dictation.jobID == job.id {
                note(why, tone: JobStyle.tileAmber, symbol: "exclamationmark.circle")
            }
            HStack(alignment: .bottom, spacing: 8 * s) {
                field
                mic
                if !holding { send }
            }
        }
        .onAppear { takeFocusIfAsked() }
        .onChange(of: model.replyFocusJobID) { _, _ in takeFocusIfAsked() }
        .onChange(of: model.replyMicHolds) { _, holds in
            // The call ended while held: the hold is over (the server lets go on its own too).
            if !holds, holding { model.holdToTalk(job.id, false) }
        }
        .onDisappear {
            if holding { model.holdToTalk(job.id, false) }
            if dictating { dictation.stop() }
        }
    }

    private func takeFocusIfAsked() {
        guard model.replyFocusJobID == job.id else { return }
        model.replyFocusJobID = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { focused = true }
    }

    private var field: some View {
        ZStack(alignment: .leading) {
            if holding {
                Text("Speak now. Only this task hears it.").font(.system(size: 12.5 * s))
                    .foregroundStyle(.secondary).lineLimit(1)
                    .padding(.horizontal, 12 * s)
            } else {
                TextField(TaskMessageCopy.placeholder(job.name), text: draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13 * s))
                    .lineLimit(1...5)
                    .focused($focused)
                    .submitLabel(.send)
                    .onSubmit(submit)
                    .padding(.horizontal, 12 * s).padding(.vertical, 8 * s)
                    .accessibilityIdentifier("reply-field")
            }
        }
        .frame(minHeight: 34 * s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 17 * s, style: .continuous).fill(Color.primary.opacity(focused ? 0.11 : 0.08)))
        .overlay(RoundedRectangle(cornerRadius: 17 * s, style: .continuous)
            .strokeBorder(holding ? JobStyle.stopRed.opacity(0.7) : focused ? JobStyle.blueInk.opacity(0.55) : Color.primary.opacity(0.10),
                          lineWidth: holding || focused ? 1.2 : 0.75))
    }

    private var send: some View {
        Button(action: submit) {
            Image(systemName: "arrow.up").font(.system(size: 13 * s, weight: .bold))
                .frame(width: 34 * s, height: 34 * s)
                .foregroundStyle(canSend ? Color.white : Color.secondary)
                .background(Circle().fill(canSend ? JobStyle.blue : Color.primary.opacity(0.08)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(!canSend)
        .keyboardShortcut(.return, modifiers: .command)
        .help("Send to \(TaskMessageCopy.shortName(job.name))")
        .accessibilityLabel("Send to \(job.name)")
        .accessibilityIdentifier("reply-send")
    }

    @ViewBuilder private var mic: some View {
        let live = model.replyMicHolds
        let on = holding || dictating
        let icon = Image(systemName: on ? (holding ? "mic.fill" : "stop.fill") : "mic")
            .font(.system(size: 13.5 * s, weight: .semibold))
            .frame(width: 34 * s, height: 34 * s)
            .foregroundStyle(on ? Color.white : Color.primary.opacity(0.85))
            .background(Circle().fill(holding ? JobStyle.stopRed : dictating ? JobStyle.blue : Color.primary.opacity(0.10)))
            .overlay(Circle().strokeBorder(holding ? JobStyle.stopRed.opacity(0.35) : .clear, lineWidth: 6).scaleEffect(holding ? 1.25 : 1))
            .scaleEffect(holding ? 1.12 : 1)
            .animation(.spring(response: 0.25, dampingFraction: 0.7), value: holding)
            .contentShape(Circle())
        if live {
            // Hold to talk: press and hold, speak, release. A tap alone just shows how.
            icon
                .gesture(DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        guard !pressing else { return }
                        pressing = true
                        focused = false
                        haptic(.begin)
                        model.holdToTalk(job.id, true)
                    }
                    .onEnded { _ in
                        pressing = false
                        if holding { haptic(.end) }
                        model.holdToTalk(job.id, false)
                    })
                .help("Hold to talk to \(TaskMessageCopy.shortName(job.name)); release to stop")
                .accessibilityLabel(holding ? "Talking to \(job.name). Release to stop" : "Hold to talk to \(job.name)")
                .accessibilityAddTraits(.isButton)
                .accessibilityIdentifier("reply-hold")
        } else {
            Button {
                if !dictating { dictationBase = draft.wrappedValue }
                dictation.toggle(jobID: job.id) { heard in
                    let base = dictationBase.trimmingCharacters(in: .whitespaces)
                    draft.wrappedValue = base.isEmpty ? heard : base + " " + heard
                }
                if dictating { focused = true }
            } label: { icon }
            .buttonStyle(.plain)
            .help(dictating ? "Stop dictating" : "Dictate a message (on this device)")
            .accessibilityLabel(dictating ? "Stop dictating" : "Dictate a message")
            .accessibilityIdentifier("reply-dictate")
        }
    }

    private var holdBanner: some View {
        HStack(spacing: 8 * s) {
            PulseDot(color: JobStyle.stopRed)
            Text(TaskMessageCopy.holding(job.name)).font(.system(size: 12 * s, weight: .semibold)).lineLimit(2)
                .foregroundStyle(Color.primary).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11 * s).padding(.vertical, 7 * s)
        .background(RoundedRectangle(cornerRadius: 14 * s, style: .continuous).fill(JobStyle.stopRed.opacity(0.18)))
        .overlay(RoundedRectangle(cornerRadius: 14 * s, style: .continuous).strokeBorder(JobStyle.stopRed.opacity(0.45), lineWidth: 0.75))
        .accessibilityIdentifier("hold-banner")
    }

    private func note(_ text: String, tone: Color, symbol: String) -> some View {
        Label(text, systemImage: symbol).font(.system(size: 11.5 * s, weight: .medium)).foregroundStyle(tone)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func submit() {
        guard canSend else { return }
        if dictating { dictation.stop() }
        model.sendReply(jobID: job.id)
    }

    private enum Haptic { case begin, end }
    private func haptic(_ h: Haptic) {
        #if os(iOS)
        let g = UIImpactFeedbackGenerator(style: h == .begin ? .medium : .light)
        g.impactOccurred()
        #endif
    }
}

/// A small dot that breathes while the mic is held.
struct PulseDot: View {
    var color: Color
    @State private var on = false
    var body: some View {
        Circle().fill(color).frame(width: 8, height: 8)
            .overlay(Circle().stroke(color.opacity(0.5), lineWidth: 2).scaleEffect(on ? 2.2 : 1).opacity(on ? 0 : 1))
            .onAppear { withAnimation(.easeOut(duration: 1.1).repeatForever(autoreverses: false)) { on = true } }
    }
}

/// Screenshots: the next "Something else…" field drawn opens with this text.
@MainActor public final class SomethingElsePreview: ObservableObject {
    public static let shared = SomethingElsePreview()
    @Published public var text: String?
    public static var open: String? { get { shared.text } set { shared.text = newValue } }
}

/// Under a decision's options: "Something else…" opens a field; what's typed is the answer
/// (sent with `choice_task_id`, so it answers this decision and nothing else).
public struct SomethingElseField: View {
    @ObservedObject var model: VoicePanelModel
    var job: Job
    var task: TaskItem
    @State private var open = false
    @State private var text = ""
    @FocusState private var focused: Bool
    @Environment(\.jobTextScale) private var s

    public init(model: VoicePanelModel, job: Job, task: TaskItem, startOpen: Bool = false, text: String = "") {
        self.model = model; self.job = job; self.task = task
        _open = State(initialValue: startOpen); _text = State(initialValue: text)
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 6 * s) {
            SentMessages(model: model, jobID: job.id, choiceTaskID: task.id, limit: 1)
            if model.answers[task.id] == nil {
                if open {
                    HStack(alignment: .bottom, spacing: 7 * s) {
                        TextField("Something else…", text: $text, axis: .vertical)
                            .textFieldStyle(.plain).font(.system(size: 12.5 * s)).lineLimit(1...4)
                            .focused($focused).submitLabel(.send).onSubmit(submit)
                            .padding(.horizontal, 10 * s).padding(.vertical, 7 * s)
                            .background(RoundedRectangle(cornerRadius: 10 * s, style: .continuous).fill(Color.primary.opacity(0.08)))
                            .overlay(RoundedRectangle(cornerRadius: 10 * s, style: .continuous)
                                .strokeBorder(JobStyle.blueInk.opacity(focused ? 0.55 : 0.2), lineWidth: 1))
                            .accessibilityIdentifier("something-else-field")
                        let ready = TaskMessageRequest.clean(text) != nil
                        Button(action: submit) {
                            Text("Send").font(.system(size: 12 * s, weight: .semibold))
                                .padding(.horizontal, 12 * s).frame(height: 30 * s)
                                .foregroundStyle(ready ? Color.white : Color.secondary)
                                .background(Capsule().fill(ready ? JobStyle.blue : Color.primary.opacity(0.08)))
                        }
                        .buttonStyle(.plain).disabled(!ready)
                        .accessibilityIdentifier("something-else-send")
                    }
                    Text("Tell it what you want instead. It goes only to this decision.")
                        .font(.system(size: 10.5 * s)).foregroundStyle(.secondary)
                } else {
                    Button { open = true; DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { focused = true } } label: {
                        HStack(spacing: 6 * s) {
                            Image(systemName: "text.cursor").font(.system(size: 11 * s, weight: .semibold))
                            Text("Something else…").font(.system(size: 12 * s, weight: .semibold))
                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, 10 * s).frame(height: 32 * s)
                        .foregroundStyle(JobStyle.blueInk)
                        .background(RoundedRectangle(cornerRadius: 10 * s, style: .continuous).strokeBorder(Color.primary.opacity(0.14), style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Answer in your own words")
                    .accessibilityIdentifier("something-else")
                }
            }
        }
        .onReceive(SomethingElsePreview.shared.$text) { t in if let t { open = true; text = t } }
    }

    private func submit() {
        guard model.sendReply(jobID: job.id, text: text, choiceTaskID: task.id) else { return }
        text = ""; open = false
    }
}
