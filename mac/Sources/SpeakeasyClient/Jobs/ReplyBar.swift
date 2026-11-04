import SwiftUI
import SpeakeasyCore
#if os(iOS)
import UIKit
#endif

// The job page's reply bar and the decision card's "Something else…" field (servers with
// `task_messages` only). What's typed goes to this job by run id, never through the voice's router,
// and shows as the user's own turn here (never as something the assistant said).

private struct ReplyBarAnswersKey: EnvironmentKey { static let defaultValue = false }
public extension EnvironmentValues {
    /// Touch pages with a reply bar: the bar is the one place to type, so a waiting decision's
    /// "Something else…" moves into it (the card drops its own field).
    var jobReplyBarAnswers: Bool {
        get { self[ReplyBarAnswersKey.self] }
        set { self[ReplyBarAnswersKey.self] = newValue }
    }
}

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
                .foregroundStyle(m.isFailed ? Color.primary : JobStyle.onBlue)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .padding(.horizontal, (JobLook.touch ? 12 : 10) * s).padding(.vertical, (JobLook.touch ? 7 : 6) * s)
                .background(RoundedRectangle(cornerRadius: (JobLook.touch ? 15 : 12) * s, style: .continuous)
                    .fill(m.isFailed ? AnyShapeStyle(Color.primary.opacity(0.10)) : AnyShapeStyle(JobStyle.blue.opacity(m.isPending ? 0.6 : 0.92))))
                .overlay(RoundedRectangle(cornerRadius: (JobLook.touch ? 15 : 12) * s, style: .continuous)
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
    @Environment(\.jobReplyBarAnswers) private var answersDecision
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
    /// Touch, a decision waiting and no in-card field: what's typed here is "Something else…"
    /// (sent with `choice_task_id`, so it answers that decision and nothing else).
    private var openChoice: TaskItem? {
        guard JobLook.touch, answersDecision, let t = job.decisionTask, t.info.decision != nil,
              model.answers[t.id] == nil else { return nil }
        return t
    }
    private var placeholder: String {
        guard JobLook.touch else { return TaskMessageCopy.placeholder(job.name) }
        return openChoice != nil ? TaskMessageCopy.somethingElse : "Message this job"
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8 * s) {
            if showsSent { SentMessages(model: model, jobID: job.id) }
            if holding {
                holdBanner
            }
            HStack(alignment: .bottom, spacing: (JobLook.touch ? 4 : 8) * s) {
                field
                // Typing only. In a call you just talk (the voice says which task it went to); outside
                // a call the keyboard's own dictation types into the field.
                send.padding(JobLook.touch ? 2 * s : 0)
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
                // Touch: the page's title already names the job, so the field just says what it's for.
                TextField(placeholder, text: draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13 * s))
                    .lineLimit(1...5)
                    .focused($focused)
                    .submitLabel(.send)
                    .onSubmit(submit)
                    .padding(.horizontal, 12 * s).padding(.vertical, 8 * s)
                    .accessibilityIdentifier("reply-field")
                    .accessibilityHint(openChoice != nil ? "Answers the decision in your own words" : "")
            }
        }
        .frame(minHeight: 34 * s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .modifier(FieldFrame(focused: focused, holding: holding, s: s))
    }

    private var send: some View {
        Button(action: submit) {
            Image(systemName: "arrow.up").font(.system(size: 13 * s, weight: .bold))
                .frame(width: (JobLook.touch ? 30 : 34) * s, height: (JobLook.touch ? 30 : 34) * s)
                .foregroundStyle(canSend ? JobStyle.onBlue : Color.secondary)
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

    private var holdButton: some View {
        HStack(spacing: 6 * s) {
            Image(systemName: holding ? "waveform" : "mic.fill").font(.system(size: 12.5 * s, weight: .semibold))
            Text(holding ? "Release to send" : "Hold to talk").font(.system(size: 12.5 * s, weight: .semibold))
                .lineLimit(1).fixedSize()
        }
        .padding(.horizontal, 13 * s)
        .frame(height: 34 * s)
        .foregroundStyle(holding ? Color.white : JobStyle.blueInk)
        .background(Capsule().fill(holding ? JobStyle.stopRed : JobStyle.blue.opacity(0.18)))
        .overlay(Capsule().strokeBorder(holding ? JobStyle.stopRed.opacity(0.35) : JobStyle.blueInk.opacity(0.35), lineWidth: holding ? 5 : 0.75))
        .scaleEffect(holding ? 1.06 : 1)
        .animation(.spring(response: 0.25, dampingFraction: 0.7), value: holding)
        .contentShape(Capsule())
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
        .help("Hold to talk to \(TaskMessageCopy.shortName(job.name)) only; release to send")
        .accessibilityLabel(holding ? "Talking to \(job.name). Release to send" : "Hold to talk to \(job.name) only")
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("reply-hold")
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
        model.sendReply(jobID: job.id, choiceTaskID: openChoice?.id)
    }

    private enum Haptic { case begin, end }
    private func haptic(_ h: Haptic) {
        #if os(iOS)
        let g = UIImpactFeedbackGenerator(style: h == .begin ? .medium : .light)
        g.impactOccurred()
        #endif
    }
}

/// The reply field's own frame on the Mac. On touch the page draws one glass capsule around the
/// field and the send button (like Messages), so the field itself is bare.
private struct FieldFrame: ViewModifier {
    var focused: Bool
    var holding: Bool
    var s: CGFloat
    func body(content: Content) -> some View {
        if JobLook.touch {
            content
        } else {
            content
                .background(RoundedRectangle(cornerRadius: 17 * s, style: .continuous).fill(Color.primary.opacity(focused ? 0.11 : 0.08)))
                .overlay(RoundedRectangle(cornerRadius: 17 * s, style: .continuous)
                    .strokeBorder(holding ? JobStyle.stopRed.opacity(0.7) : focused ? JobStyle.blueInk.opacity(0.55) : Color.primary.opacity(0.10),
                                  lineWidth: holding || focused ? 1.2 : 0.75))
        }
    }
}

/// Touch: Liquid Glass where the system has it (iOS 26), a thin material before.
public struct TouchGlass<S: Shape>: ViewModifier {
    var shape: S
    var interactive: Bool
    public init(_ shape: S, interactive: Bool = true) { self.shape = shape; self.interactive = interactive }
    public func body(content: Content) -> some View {
        #if os(iOS)
        if #available(iOS 26, *) {
            content.glassEffect(interactive ? .regular.interactive() : .regular, in: shape)
        } else {
            content.background(shape.fill(.ultraThinMaterial)).overlay(shape.stroke(Color.white.opacity(0.08), lineWidth: 0.5))
        }
        #else
        content.background(shape.fill(.ultraThinMaterial))
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

/// "Something else…" closed: a dashed outline on the Mac; on touch a quiet row like the options.
private struct SomethingElseFrame: View {
    var s: CGFloat
    var body: some View {
        if JobLook.touch {
            RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.06))
        } else {
            RoundedRectangle(cornerRadius: 10 * s, style: .continuous).strokeBorder(Color.primary.opacity(0.14), style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
        }
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
    var startOpen: Bool
    var text: String
    @Environment(\.jobTextScale) private var s

    public init(model: VoicePanelModel, job: Job, task: TaskItem, startOpen: Bool = false, text: String = "") {
        self.model = model; self.job = job; self.task = task; self.startOpen = startOpen; self.text = text
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 6 * s) {
            SentMessages(model: model, jobID: job.id, choiceTaskID: task.id, limit: 1)
            if model.answers[task.id] == nil {
                SomethingElseInput(startOpen: startOpen, text: text) { model.sendReply(jobID: job.id, text: $0, choiceTaskID: task.id) }
            }
        }
    }
}

/// The "Something else…" row and the field it opens. `send` says whether it went.
public struct SomethingElseInput: View {
    var send: (String) -> Bool
    @State private var open = false
    @State private var text = ""
    @FocusState private var focused: Bool
    @Environment(\.jobTextScale) private var s

    public init(startOpen: Bool = false, text: String = "", send: @escaping (String) -> Bool) {
        self.send = send
        _open = State(initialValue: startOpen); _text = State(initialValue: text)
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 6 * s) {
            if open && JobLook.touch {
                // One glass field with the send button inside it, like the page's reply bar.
                let ready = TaskMessageRequest.clean(text) != nil
                HStack(alignment: .bottom, spacing: 4 * s) {
                    TextField("Something else…", text: $text, axis: .vertical)
                        .textFieldStyle(.plain).font(.system(size: 12.5 * s)).lineLimit(1...4)
                        .focused($focused).submitLabel(.send).onSubmit(submit)
                        .padding(.leading, 14 * s).padding(.vertical, 9 * s)
                        .frame(minHeight: 34 * s)
                        .accessibilityIdentifier("something-else-field")
                    Button(action: submit) {
                        Image(systemName: "arrow.up").font(.system(size: 12 * s, weight: .bold))
                            .frame(width: 28 * s, height: 28 * s)
                            .foregroundStyle(ready ? JobStyle.onBlue : Color.secondary)
                            .background(Circle().fill(ready ? JobStyle.blue : Color.primary.opacity(0.08)))
                    }
                    .buttonStyle(.plain).disabled(!ready)
                    .padding(3 * s)
                    .accessibilityLabel("Send")
                    .accessibilityIdentifier("something-else-send")
                }
                .modifier(TouchGlass(RoundedRectangle(cornerRadius: 19 * s, style: .continuous)))
                Text("Goes only to this decision.")
                    .font(.system(size: 10.5 * s)).foregroundStyle(.secondary).padding(.leading, 4)
            } else if open {
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
                            .foregroundStyle(ready ? JobStyle.onBlue : Color.secondary)
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
                    .padding(.horizontal, (JobLook.touch ? 12 : 10) * s).frame(minHeight: (JobLook.touch ? 36 : 32) * s)
                    .foregroundStyle(JobLook.touch ? Color.secondary : JobStyle.blueInk)
                    .background { SomethingElseFrame(s: s) }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Answer in your own words")
                .accessibilityIdentifier("something-else")
            }
        }
        .onReceive(SomethingElsePreview.shared.$text) { t in if let t { open = true; text = t } }
    }

    private func submit() {
        guard send(text) else { return }
        text = ""; open = false
    }
}
