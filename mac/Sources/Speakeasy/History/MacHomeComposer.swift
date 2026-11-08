import AppKit
import SpeakeasyClient
import SpeakeasyCore
import SwiftUI
import UniformTypeIdentifiers

// Home's composer on the Mac (SPK-198), matching the iPhone and iPad: the session page's composer at
// the bottom of the Now column, with the round call button beside it. Typing (↩) or a voice message
// starts a NEW chat and task at once; it shows on Home as a working card and Home stays where it is
// (nothing opens beside it). On a call the bar becomes the call's dock.

@MainActor
final class MacHomeComposerModel: ObservableObject {
    static let shared = MacHomeComposerModel(now: .shared)

    @Published var draft = ""
    /// Bumped to put the cursor in the field (a "Try asking" chip filled it).
    @Published private(set) var focusToken = 0
    @Published private(set) var sending = false
    /// Why the last send didn't go (the words are back in the field; a voice message can be retried).
    @Published var notice: String?
    @Published private(set) var failedClip: Clip?
    /// Recording a voice message: when it started (nil = not recording). `asking` = waiting on the mic prompt.
    @Published private(set) var recordingSince: Date?
    @Published private(set) var asking = false
    @Published private(set) var level: Float = 0
    /// Servers with `new_voice_notes`: the mic shows.
    @Published var voiceNotesSupported = false
    /// Plugins with `home_chat`: Home is a chat with a coordinator (it replies; jobs only for real work).
    var homeChat = false
    /// Home's model menu (plugins with `work` in `/voice/home/model`): the model jobs from Home run
    /// on, and the chat's own model as a secondary choice.
    @Published var homeModel: HomeModelStatus?

    func loadHomeModel() async {
        guard let api = api() else { return }   // older plugins: no route, no menu
        if let status = try? await api.homeModel() { homeModel = status }
    }

    /// nil = back to the default (Hermes' default model for jobs; Auto for the chat).
    func chooseHomeModel(work: Bool, _ id: String?) {
        guard let api = api() else { return }
        Task {
            do { homeModel = work ? try await api.setHomeWorkModel(id) : try await api.setHomeModel(id) }
            catch { notice = "Couldn't change the model." }
        }
    }

    struct Pick: Identifiable {
        let id = UUID()
        let url: URL
        var encoded: EncodedAttachment?
        var serverID: String?
        var progress: Double = 0
        var failure: String?
    }
    @Published private(set) var picks: [Pick] = []
    private var sendID: String?
    private var lastSentText: String?
    var canSend: Bool {
        !sending && !recording && (TaskMessageRequest.clean(draft) != nil || !picks.isEmpty)
            && picks.allSatisfy { $0.serverID != nil && $0.failure == nil }
    }

    func chooseFiles() {
        guard !sending, !recording else { return }
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.image, .mpeg4Movie, .quickTimeMovie, .pdf, .plainText, .json]
        panel.message = "Add a clip, picture or document. Videos: up to 10 MB and 2 minutes."
        panel.begin { [weak self] result in
            guard result == .OK else { return }
            Task { @MainActor in self?.addFiles(panel.urls) }
        }
    }

    func addFiles(_ urls: [URL]) {
        guard !sending, !recording else { return }
        for url in urls {
            guard picks.count < AttachmentPolicy.maxAttachments else {
                notice = "You can attach up to three items."; break
            }
            let pick = Pick(url: url)
            picks.append(pick)
            retryPick(pick.id)
        }
        sendID = nil
    }

    func removePick(_ id: UUID) {
        guard !sending, let pick = picks.first(where: { $0.id == id }) else { return }
        picks.removeAll { $0.id == id }; sendID = nil
        if let serverID = pick.serverID, let api = api() {
            Task { try? await api.removeStagedAttachment(id: serverID) }
        }
    }

    func retryPick(_ id: UUID) {
        guard let index = picks.firstIndex(where: { $0.id == id }), picks[index].serverID == nil else { return }
        picks[index].failure = nil; picks[index].progress = 0
        let url = picks[index].url
        Task { [weak self] in
            guard let self else { return }
            do {
                // Decode off the UI thread; retain the original video's bytes.
                let encoded = try await Task.detached(priority: .utility) { try AttachmentEncoder.encode(fileAt: url) }.value
                guard let i = self.picks.firstIndex(where: { $0.id == id }) else { return }
                self.picks[i].encoded = encoded
                guard let api = self.api() else { throw URLError(.notConnectedToInternet) }
                let staged = try await api.stageAttachment(encoded, as: encoded.kind == .image ? .picture : .file) { [weak self] value in
                    Task { @MainActor in
                        guard let self, let i = self.picks.firstIndex(where: { $0.id == id }) else { return }
                        self.picks[i].progress = value
                    }
                }
                guard let i = self.picks.firstIndex(where: { $0.id == id }) else {
                    try? await api.removeStagedAttachment(id: staged.id); return
                }
                self.picks[i].serverID = staged.id; self.picks[i].progress = 1
            } catch {
                if let i = self.picks.firstIndex(where: { $0.id == id }) {
                    self.picks[i].failure = HistoryStore.message(error)
                }
            }
        }
    }

    struct Clip: Equatable {
        let id: String
        let audio: Data
        let duration: TimeInterval
        let levels: [Double]
    }

    var api: () -> ServerClient? = { nil }
    var recorder: VoiceMessageRecording = VoiceMessageRecorder()
    let now: MacNowModel
    /// Screenshots: nothing leaves the Mac; sending only adds the working card.
    var demo = false

    init(now: MacNowModel) { self.now = now }
    convenience init() { self.init(now: .shared) }

    var recording: Bool { recordingSince != nil || asking }

    /// A "Try asking" chip: the words go in the field; nothing is sent until Send.
    func fill(_ text: String) {
        draft = text
        notice = nil
        focusToken += 1
    }

    /// ↩ / Send: the words start a new chat + task, no call, no navigation.
    func send() {
        guard canSend else { return }
        let clean = TaskMessageRequest.clean(draft) ?? ""
        let clientID = (lastSentText == clean ? sendID : nil) ?? UUID().uuidString
        sendID = clientID; lastSentText = clean
        let attachments = picks.compactMap(\.serverID)
        let title = String((clean.isEmpty ? "Review attached media" : clean).prefix(80))
        notice = nil
        draft = ""
        if homeChat && !demo {
            sendToChat(clean, attachments: attachments, clientID: clientID)
            return
        }
        now.addPending(id: clientID, title: title)
        if demo { return }
        sending = true
        Task { [weak self] in
            guard let self else { return }
            defer { self.sending = false }
            do {
                guard let api = self.api() else { throw URLError(.notConnectedToInternet) }
                let started = try await api.startChatSession(text: clean, attachments: attachments, clientID: clientID)
                self.picks.removeAll(); self.sendID = nil
                self.now.pendingStarted(id: clientID, runID: started.runID, sessionID: started.sessionID)
                await self.now.refresh()
            } catch {
                self.now.dropPending(clientID)
                if self.draft.isEmpty { self.draft = clean }
                self.notice = HistoryStore.message(error)
            }
        }
    }

    // MARK: Voice message (stop = sent)

    /// Home's chat: the words show at once as your bubble; the coordinator's reply (and the card of
    /// any job it started) arrives with the next Home refresh.
    private func sendToChat(_ clean: String, attachments: [String], clientID: String) {
        sending = true
        Task { [weak self] in
            guard let self else { return }
            defer { self.sending = false }
            do {
                guard let api = self.api() else { throw URLError(.notConnectedToInternet) }
                _ = try await api.homeMessage(text: clean, attachments: attachments, clientID: clientID)
                self.picks.removeAll(); self.sendID = nil
                await self.now.refresh()
            } catch {
                if self.draft.isEmpty { self.draft = clean }
                self.notice = HistoryStore.message(error)
            }
        }
    }

    func tapMic() {
        guard voiceNotesSupported, !recording, !sending else { return }
        notice = nil
        asking = true
        recorder.authorizeMic { [weak self] granted in
            guard let self, self.asking else { return }
            self.asking = false
            guard granted else {
                self.notice = "Speakeasy can't use the microphone. Turn it on in System Settings › Privacy & Security › Microphone."
                return
            }
            guard self.recorder.startClip(level: { [weak self] in self?.level = $0 }) else {
                self.notice = "The microphone didn't start. Try again."
                return
            }
            self.recordingSince = Date()
        }
    }

    func cancelRecording() {
        asking = false
        guard recordingSince != nil else { return }
        recordingSince = nil
        level = 0
        recorder.cancel()
    }

    /// Stop = sent: the whole recording starts the new chat, like a Telegram voice note; Hermes writes
    /// it down and the task works from its words.
    func finishRecording() {
        guard recordingSince != nil else { return }
        recordingSince = nil
        level = 0
        guard let clip = recorder.finishClip(), clip.duration >= 0.4 else {
            notice = VoiceMessageFlow.nothingHeardText
            return
        }
        sendVoiceNote(Clip(id: UUID().uuidString, audio: clip.audio, duration: clip.duration, levels: clip.levels))
    }

    func sendVoiceNote(_ clip: Clip) {
        guard !sending else { return }
        failedClip = nil
        notice = nil
        VoiceClipStore.save(clip.id, audio: clip.audio,
                            meta: .init(duration: clip.duration, levels: VoiceClipStore.bars(clip.levels)))
        now.addPending(id: clip.id, title: "Voice message")
        if demo { return }
        sending = true
        Task { [weak self] in
            guard let self else { return }
            defer { self.sending = false }
            do {
                guard let api = self.api() else { throw URLError(.notConnectedToInternet) }
                let reply = try await api.sendVoiceNote(VoiceNoteRequest(
                    target: .newJob, audio: clip.audio, mimeType: VoiceMemoAudio.mimeType, clientID: clip.id,
                    duration: clip.duration))
                self.now.pendingStarted(id: clip.id, runID: reply.runID, sessionID: reply.sessionID)
                await self.now.refresh()
            } catch {
                self.now.dropPending(clip.id)
                self.failedClip = clip
                self.notice = "Your voice message didn't send. " + HistoryStore.message(error)
            }
        }
    }

    func retryVoiceNote() { if let clip = failedClip { sendVoiceNote(clip) } }
    func clearNotice() { notice = nil; failedClip = nil }

    #if DEBUG
    /// Screenshots: a voice message recording for `seconds`.
    func previewRecording(seconds: TimeInterval, level: Float) {
        recordingSince = Date().addingTimeInterval(-seconds); self.level = level
    }
    #endif
}

/// The bottom of Home: the composer, or the call's dock while a call is on.
struct MacHomeBottomBar: View {
    var voice: VoicePanelModel?
    @ObservedObject var composer: MacHomeComposerModel

    var body: some View {
        Group {
            if let voice {
                MacHomeBottomBarLive(voice: voice, composer: composer)
            } else {
                MacHomeComposerBar(model: composer, onCall: nil)
            }
        }
        .overlay(alignment: .top) { Rectangle().fill(Live.hairline).frame(height: 1) }
    }
}

private struct MacHomeBottomBarLive: View {
    @ObservedObject var voice: VoicePanelModel
    @ObservedObject var composer: MacHomeComposerModel
    var body: some View {
        if voice.state.connection.isOpen && !voice.state.workOnly {
            MacNowCallControls(voice: voice)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .padding(.horizontal, 12).padding(.vertical, 10)
                .accessibilityIdentifier("mac-home-call-dock")
        } else {
            MacHomeComposerBar(model: composer, onCall: voice.onStart)
                .onChange(of: voice.state.connection.isOpen) { _, open in if open { composer.cancelRecording() } }
        }
    }
}

/// Home's model menu on the Mac: the model jobs from Home run on (default: the Hermes default), and
/// below it, the chat's own model (Auto: a fast model you already have).
struct MacHomeModelMenu: View {
    let status: HomeModelStatus
    let work: HomeModelStatus.Work
    let choose: (_ work: Bool, _ id: String?) -> Void

    var body: some View {
        Menu {
            Section("Jobs from Home run on") {
                ForEach(work.options) { option in
                    let isDefault = option.id == work.defaultModel?.id
                    Button((work.model?.id == option.id ? "✓ " : "") + option.name + (isDefault ? " · Default" : "")) {
                        choose(true, isDefault ? nil : option.id)
                    }
                }
            }
            Menu("Chat replies: " + (status.chosen ? (status.model?.name ?? "Auto") : status.autoLabel)) {
                Button((status.chosen ? "" : "✓ ") + (status.autoLabel)) { choose(false, nil) }
                ForEach(status.options) { option in
                    Button((status.chosen && status.model?.id == option.id ? "✓ " : "") + option.name) { choose(false, option.id) }
                }
            }
        } label: {
            Label(work.model?.name ?? "Default model", systemImage: "sparkle").font(.system(size: 11.5, weight: .semibold))
        }
        .menuStyle(.borderlessButton).fixedSize()
        .accessibilityLabel("Model for jobs, \(work.model?.name ?? "default")")
        .accessibilityIdentifier("mac-home-model")
        .padding(.horizontal, 6)
    }
}

/// The session page's composer look (rounded field, round buttons), plus the brass call button.
struct MacHomeComposerBar: View {
    @ObservedObject var model: MacHomeComposerModel
    var onCall: (() -> Void)?
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let notice = model.notice {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(notice).font(.system(size: 11.5)).foregroundStyle(Color.primary.opacity(0.85))
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    if model.failedClip != nil {
                        Button("Retry") { model.retryVoiceNote() }
                            .buttonStyle(.plain).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Live.brassText)
                    }
                    Button { model.clearNotice() } label: { Image(systemName: "xmark").font(.system(size: 9.5, weight: .bold)) }
                        .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("Dismiss")
                }
                .padding(.horizontal, 6)
                .accessibilityIdentifier("mac-home-notice")
            }
            if !model.picks.isEmpty {
                ForEach(model.picks) { pick in
                    HStack(spacing: 8) {
                        if let data = pick.encoded?.videoPoster, let image = NSImage(data: data) {
                            Image(nsImage: image).resizable().scaledToFit().frame(width: 52, height: 36)
                        } else { Image(systemName: "doc").frame(width: 52, height: 36) }
                        VStack(alignment: .leading, spacing: 3) {
                            Text(pick.url.lastPathComponent).font(.caption).lineLimit(1)
                            if let duration = pick.encoded?.videoDuration {
                                Text(VoiceMessageFlow.clock(duration)).font(.caption2).foregroundStyle(.secondary)
                            }
                            if let failure = pick.failure {
                                Text(failure).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                            } else if pick.serverID == nil {
                                ProgressView(value: pick.progress).frame(maxWidth: 160)
                                    .accessibilityLabel("Uploading attachment")
                            }
                        }
                        Spacer(minLength: 0)
                        if pick.failure != nil { Button("Retry") { model.retryPick(pick.id) } }
                        Button { model.removePick(pick.id) } label: { Image(systemName: "xmark") }
                            .accessibilityLabel("Remove " + pick.url.lastPathComponent)
                    }
                    .disabled(model.sending)
                }
            }
            if let status = model.homeModel, let work = status.work {
                MacHomeModelMenu(status: status, work: work) { isWork, id in model.chooseHomeModel(work: isWork, id) }
            }
            HStack(alignment: .bottom, spacing: 8) {
                if model.recording { recording } else { typing }
                if let onCall { MacHomeCallButton(action: onCall) }
            }
        }
        .padding(.horizontal, 14).padding(.top, 10).padding(.bottom, 12)
        .task { await model.loadHomeModel() }
        .onChange(of: model.focusToken) { _, _ in focused = true }
        .dropDestination(for: URL.self) { urls, _ in model.addFiles(urls); return !urls.isEmpty }
    }

    private var typing: some View {
        let empty = TaskMessageRequest.clean(model.draft) == nil && model.picks.isEmpty
        return HStack(alignment: .bottom, spacing: 8) {
            circle("plus", help: "Attach a clip, picture or document", fill: Color.primary.opacity(0.08), ink: .primary) { model.chooseFiles() }
                .disabled(model.sending).accessibilityIdentifier("mac-home-attach")
            TextField(model.picks.isEmpty ? "Ask Hermes anything" : "Add a caption or ask about the clip", text: $model.draft, axis: .vertical)
                .textFieldStyle(.plain).font(.system(size: 13.5)).lineLimit(1...6)
                .focused($focused)
                .onSubmit { model.send() }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .frame(minHeight: 40)
                .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(HistoryLook.field))
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .strokeBorder(focused ? Live.brass.opacity(0.5) : Live.hairline, lineWidth: focused ? 1 : 0.5))
                .accessibilityIdentifier("mac-home-composer")
            if model.voiceNotesSupported && empty {
                circle("mic.fill", help: "Record a voice message", fill: Color.primary.opacity(0.08), ink: .primary) { model.tapMic() }
                    .accessibilityIdentifier("mac-home-mic")
            } else {
                circle("arrow.up", help: "Send (↩)", fill: empty ? Color.primary.opacity(0.06) : Live.brass,
                       ink: empty ? Color.secondary : JobLook.brassInk) { model.send() }
                    .disabled(!model.canSend)
                    .accessibilityIdentifier("mac-home-send")
            }
        }
    }

    private var recording: some View {
        HStack(spacing: 10) {
            HStack(spacing: 6) {
                if model.asking { LiveSpinner(size: 10) } else { LiveDot(color: Live.red, size: 7) }
                if let since = model.recordingSince {
                    TimelineView(.periodic(from: since, by: 1)) { ctx in
                        Text(VoiceMessageFlow.clock(ctx.date.timeIntervalSince(since)))
                            .font(.system(size: 11.5, weight: .semibold).monospacedDigit()).foregroundStyle(Live.red)
                    }
                } else {
                    Text("Waiting").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
                }
            }
            TimelineView(.periodic(from: .now, by: 0.1)) { ctx in
                LevelBars(level: model.level, active: model.recordingSince != nil, now: ctx.date)
            }
            .frame(minWidth: 0, maxWidth: .infinity, alignment: .trailing)
            .frame(height: 18)
            .clipped()
            .layoutPriority(-1)
            circle("xmark", help: "Discard (esc)", fill: Color.primary.opacity(0.08), ink: .secondary) { model.cancelRecording() }
                .keyboardShortcut(.cancelAction)
            circle("arrow.up", help: "Send voice message (↩)", fill: Live.brass, ink: JobLook.brassInk) { model.finishRecording() }
                .keyboardShortcut(.defaultAction)
                .disabled(model.recordingSince == nil)
                .accessibilityIdentifier("mac-home-stop")
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .frame(minHeight: 40)
        .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Live.red.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Live.red.opacity(0.35), lineWidth: 0.75))
        .accessibilityIdentifier("mac-home-recording")
    }

    private func circle(_ symbol: String, help: String, fill: Color, ink: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 13, weight: .bold)).foregroundStyle(ink)
                .frame(width: 34, height: 34).background(Circle().fill(fill))
        }
        .buttonStyle(.plain).help(help).accessibilityLabel(help)
    }
}

/// Starts a call: a brass circle with the call's waveform, the brightest control on the bar.
struct MacHomeCallButton: View {
    let action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Image(systemName: "waveform")
                .font(.system(size: 16, weight: .bold)).foregroundStyle(JobLook.brassInk)
                .frame(width: 40, height: 40)
                .background(Circle().fill(Live.brass.opacity(hover ? 1 : 0.94)))
                .shadow(color: Live.brass.opacity(0.4), radius: 8, y: 2)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Start a call")
        .accessibilityLabel("Start a call")
        .accessibilityIdentifier("mac-home-call")
    }
}

/// Empty Home: a small, quiet "All caught up." and a few things to try, which fill the composer.
struct MacNowEmpty: View {
    var onPick: (String) -> Void

    var body: some View {
        VStack(spacing: 12) {
            Text(NowHome.caughtUpLine)
                .font(.callout.weight(.medium)).foregroundStyle(.secondary)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("mac-now-caught-up")
            VStack(spacing: 7) {
                Text("Try asking").font(.caption).foregroundStyle(.tertiary)
                ForEach(HomeSuggestions.today(), id: \.self) { line in
                    MacSuggestionChip(text: line) { onPick(line) }
                }
            }
        }
        .frame(maxWidth: .infinity).padding(.vertical, 40)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("mac-now-empty")
    }
}

private struct MacSuggestionChip: View {
    let text: String
    let action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Text(text).font(.callout).foregroundStyle(Color.primary.opacity(0.85))
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(Capsule().fill(Color.primary.opacity(hover ? 0.09 : 0.05)))
                .overlay(Capsule().strokeBorder(Live.hairline))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Puts these words in the box below")
        .accessibilityIdentifier("mac-now-suggestion")
    }
}

/// "Try asking" lines for an empty Home: invented, everyday asks; three a day, the same all day.
enum HomeSuggestions {
    static let all = ["Find me a standing desk under $400", "Plan a Saturday in the city for two",
                      "Compare three tomato seeds for a shady garden", "Draft a thank-you note to my neighbor",
                      "What's on my calendar this week?", "Find a cabin two hours away for next weekend",
                      "Sort this week's photos into albums", "Check if my flight on Friday is on time"]
    static func today(_ date: Date = Date(), count: Int = 3) -> [String] {
        let day = Calendar.current.ordinality(of: .day, in: .era, for: date) ?? 0
        return (0..<count).map { all[(day * count + $0) % all.count] }
    }
}
