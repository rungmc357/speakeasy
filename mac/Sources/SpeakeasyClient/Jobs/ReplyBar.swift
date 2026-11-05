import SwiftUI
import SpeakeasyCore

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
            if !m.attachments.isEmpty {
                HStack(spacing: 6 * s) {
                    ForEach(m.attachments) { a in SentAttachmentTile(attachment: a, image: model.outgoingThumbnail(a.id), s: s) }
                }
                .opacity(m.isPending ? 0.7 : 1)
            }
            if !m.text.isEmpty { bubble(m) }
            status(m)
        }
        .padding(.leading, 40 * s)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private func bubble(_ m: OutgoingMessage) -> some View {
        Group {
            Text(m.text).font(.system(size: 12.5 * s))
                .foregroundStyle(m.isFailed ? Color.primary : JobStyle.onBlue)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .padding(.horizontal, (JobLook.touch ? 12 : 10) * s).padding(.vertical, (JobLook.touch ? 7 : 6) * s)
                .background(RoundedRectangle(cornerRadius: (JobLook.touch ? 15 : 12) * s, style: .continuous)
                    .fill(m.isFailed ? AnyShapeStyle(Color.primary.opacity(0.10)) : AnyShapeStyle(JobStyle.blue.opacity(m.isPending ? 0.6 : 0.92))))
                .overlay(RoundedRectangle(cornerRadius: (JobLook.touch ? 15 : 12) * s, style: .continuous)
                    .strokeBorder(m.isFailed ? JobStyle.red.opacity(0.55) : .clear, lineWidth: 1))
        }
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

/// A picture or file a sent message carried, above its bubble: the thumbnail the composer kept, else a symbol.
struct SentAttachmentTile: View {
    let attachment: OutgoingAttachment
    let image: CGImage?
    let s: CGFloat

    var body: some View {
        let side = 56 * s
        Group {
            if let image {
                Image(decorative: image, scale: 1).resizable().scaledToFill()
            } else {
                VStack(spacing: 3 * s) {
                    Image(systemName: attachment.kind == .picture ? "photo" : "doc.text").font(.system(size: 16 * s))
                    Text(attachment.name).font(.system(size: 9 * s)).lineLimit(1).truncationMode(.middle)
                }
                .padding(4 * s)
                .foregroundStyle(.secondary)
            }
        }
        .frame(width: side, height: side)
        .background(Color.primary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 10 * s, style: .continuous))
        .accessibilityLabel("Sent \(attachment.name)")
    }
}

/// What a composer around `TaskReplyBar` adds (the phone's attachments, see its `Attachments`
/// model): a leading view in the field's row (the + button), a view above the row (the tray), and
/// the attachments to claim when the message goes. Lets a bar send with no words when one is ready.
public struct ReplyBarExtras {
    public var leading: AnyView
    public var above: AnyView
    /// At least one attachment is ready to go (the bar may send with an empty field).
    public var ready: Bool
    /// Something is still uploading: hold the send.
    public var busy: Bool
    /// Claims what is ready for the message being sent (and clears the tray).
    public var take: () -> [OutgoingAttachment]
    public init(leading: AnyView, above: AnyView, ready: Bool, busy: Bool, take: @escaping () -> [OutgoingAttachment]) {
        self.leading = leading; self.above = above; self.ready = ready; self.busy = busy; self.take = take
    }
}

/// The bottom of a job page: a text field and a send button. Typing only: in a call you just talk,
/// and the keyboard's own dictation types into the field.
public struct TaskReplyBar: View {
    @ObservedObject var model: VoicePanelModel
    var job: Job
    /// Draw what was sent above the field. The iPhone shows it in the page instead (the bar stays small).
    var showsSent = true
    /// Attachments around the field (the phone); nil keeps the plain bar.
    var extras: ReplyBarExtras?
    @FocusState private var focused: Bool
    @Environment(\.jobTextScale) private var s
    @Environment(\.jobReplyBarAnswers) private var answersDecision

    @MainActor public init(model: VoicePanelModel, job: Job, showsSent: Bool = true, extras: ReplyBarExtras? = nil) {
        self.model = model; self.job = job; self.showsSent = showsSent; self.extras = extras
    }

    private var draft: Binding<String> {
        Binding(get: { model.replyDrafts[job.id] ?? "" }, set: { model.replyDrafts[job.id] = $0.isEmpty ? nil : $0 })
    }
    private var canSend: Bool {
        if extras?.busy == true { return false }
        // A spoken-choice answer takes words only (the server refuses attachments there).
        return TaskMessageRequest.clean(draft.wrappedValue) != nil || (extras?.ready == true && openChoice == nil)
    }
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
            if let extras { extras.above }
            HStack(alignment: .bottom, spacing: (JobLook.touch ? 4 : 8) * s) {
                if let extras { extras.leading }
                field
                // Typing only. In a call you just talk (the voice says which task it went to); outside
                // a call the keyboard's own dictation types into the field.
                send.padding(JobLook.touch ? 2 * s : 0)
            }
        }
        .onAppear { takeFocusIfAsked() }
        .onChange(of: model.replyFocusJobID) { _, _ in takeFocusIfAsked() }
    }

    private func takeFocusIfAsked() {
        guard model.replyFocusJobID == job.id else { return }
        model.replyFocusJobID = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { focused = true }
    }

    private var field: some View {
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
            .frame(minHeight: 34 * s)
            .frame(maxWidth: .infinity, alignment: .leading)
            .modifier(FieldFrame(focused: focused, s: s))
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

    private func submit() {
        guard canSend else { return }
        let choice = openChoice?.id
        // The tray empties as the message is queued; the sent bubble shows what it carried (and Retry
        // resends the same ids with the same client id).
        let carried = choice == nil && model.replyTarget(job) != nil ? (extras?.take() ?? []) : []
        model.sendReply(jobID: job.id, choiceTaskID: choice, attachments: carried)
    }
}

/// The reply field's own frame on the Mac. On touch the page draws one glass capsule around the
/// field and the send button (like Messages), so the field itself is bare.
private struct FieldFrame: ViewModifier {
    var focused: Bool
    var s: CGFloat
    func body(content: Content) -> some View {
        if JobLook.touch {
            content
        } else {
            content
                .background(RoundedRectangle(cornerRadius: 17 * s, style: .continuous).fill(Color.primary.opacity(focused ? 0.11 : 0.08)))
                .overlay(RoundedRectangle(cornerRadius: 17 * s, style: .continuous)
                    .strokeBorder(focused ? JobStyle.blueInk.opacity(0.55) : Color.primary.opacity(0.10),
                                  lineWidth: focused ? 1.2 : 0.75))
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
