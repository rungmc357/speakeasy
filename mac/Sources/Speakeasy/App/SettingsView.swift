import AppKit
import SpeakeasyCore
import SpeakeasyClient
import SwiftUI

/// Settings window. Server-side values load from and save to `/voice/settings`;
/// client values live in UserDefaults (`@AppStorage`).
struct SettingsView: View {
    @EnvironmentObject var app: AppModel

    var body: some View {
        TabView {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }
            ShortcutSettings().tabItem { Label("Shortcuts", systemImage: "keyboard") }
            VoiceSettings().tabItem { Label("Voice", systemImage: "waveform") }
            BriefSettings().tabItem { Label("Voice brief", systemImage: "text.quote") }
            BehaviorSettings().tabItem { Label("Behavior", systemImage: "slider.horizontal.3") }
            DeliverySettings().tabItem { Label("Delivery", systemImage: "paperplane") }
            HomeSettings().tabItem { Label("Home", systemImage: "house") }
            ConnectionSettings().tabItem { Label("Connection", systemImage: "network") }
            AboutSettings().tabItem { Label("About", systemImage: "info.circle") }
        }
        .frame(width: 560, height: 470)
        .task { await app.refresh() }
    }
}

/// A draft copy of the server settings with Save / Revert.
private struct ServerForm<Content: View>: View {
    @EnvironmentObject var app: AppModel
    @Binding var draft: ServerSettings
    @State private var saving = false
    @State private var saved = false
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(spacing: 0) {
            Form { content() }.formStyle(.grouped)
            HStack {
                if let error = app.lastError { Text(error).font(.caption).foregroundStyle(.orange).lineLimit(2) }
                else if saved { Text("Saved").font(.caption).foregroundStyle(.secondary) }
                Spacer()
                if app.routingNeedsModel && app.lastError == nil {
                    Text("Choose a routing model to save").font(.caption).foregroundStyle(.secondary)
                }
                Button("Revert") { draft = app.settings; app.routingDraft = nil }
                    .disabled((draft == app.settings && app.routingEdit == nil) || saving)
                Button("Save") {
                    saving = true
                    Task {
                        var ok = true
                        if draft != app.settings { ok = await app.save(draft) }
                        if ok { ok = await app.saveRoutingEdit() }
                        saved = ok
                        saving = false
                        if ok { draft = app.settings }
                    }
                }
                .keyboardShortcut("s", modifiers: .command)
                .disabled((draft == app.settings && app.routingEdit == nil) || app.routingNeedsModel
                          || saving || !app.isPaired)
            }
            .padding(.horizontal, 20).padding(.bottom, 14)
        }
        .onAppear { draft = app.settings }
        .onReceive(app.$settings) { new in if !saving { draft = new } }
        .disabled(!app.isPaired)
    }
}

// MARK: General

private struct GeneralSettings: View {
    @EnvironmentObject var app: AppModel
    @AppStorage(Prefs.startMuted) private var startMuted = false
    @AppStorage(Prefs.showPanelOnStart) private var showPanelOnStart = true
    @AppStorage(Prefs.followSystemAudio) private var followSystemAudio = true
    @AppStorage(Prefs.startSlim) private var startSlim = false
    @AppStorage(Prefs.listeningBeta) private var listeningBeta = false
    @AppStorage(Prefs.showCaptions) private var showCaptions = true
    @AppStorage(Prefs.notifyWhenDone) private var notifyWhenDone = true
    @AppStorage(Prefs.panelOnAllSpaces) private var panelOnAllSpaces = true
    @AppStorage(Prefs.tourPending) private var tourPending = false
    /// Why listening mode can't run here (nil when it can).
    @State private var listeningUnavailable: String?

    private static func listeningReason(app: AppModel) async -> String? {
        if let reason = await RoomListener.unavailability() { return reason.message }
        if app.status != nil && app.status?.roomListening != true { return RoomUnavailability.pluginTooOld.message }
        return nil
    }

    var body: some View {
        Form {
            Section("Calls") {
                Toggle("Start calls muted", isOn: $startMuted)
                Toggle("Follow the system's audio devices (AirPods etc.)", isOn: $followSystemAudio)
            }
            Section("Panel") {
                Toggle("Show the panel when a call starts", isOn: $showPanelOnStart)
                Toggle("Start calls in slim mode", isOn: $startSlim)
                    .help("Just the controls and a one-line task summary; expand any time")
                Toggle("Show live captions", isOn: $showCaptions)
                    .help("What you and the assistant say, as text in the panel")
                Toggle("Keep the panel on every desktop", isOn: $panelOnAllSpaces)
                    .help("Off: the panel stays on the desktop (Space) where the call started")
            }
            Section("After a call") {
                Toggle("Notify me when work from a call finishes", isOn: $notifyWhenDone)
                    .help("A macOS notification when a task you started keeps running after you hang up and then finishes")
            }
            Section {
                Toggle("Listening mode", isOn: $listeningBeta)
            } header: {
                Text("Beta")
            } footer: {
                Text("Pause a call to let Speakeasy transcribe the room, then ask about the conversation without repeating it.")
            }
            if listeningBeta {
            Section {
                LabeledContent("Listening mode") {
                    if let reason = listeningUnavailable {
                        Text(reason).foregroundStyle(.secondary).multilineTextAlignment(.trailing)
                    } else {
                        Text("Ear button in the panel during a call, or the menu bar").foregroundStyle(.secondary)
                    }
                }
            } footer: {
                Text("During a call: pauses the call and transcribes the room on this Mac without answering, keeping the last 30 minutes as text. Turn it off and the call picks up again, knowing what was said. What it heard goes to the voice and to tasks from that call; Speakeasy keeps none of it, but Hermes keeps what its tasks receive in its own history. It stops when the call ends, after 2 hours, or when your Mac sleeps.")
            }
            .task { listeningUnavailable = await Self.listeningReason(app: app) }
            }
            Section("Tour") {
                LabeledContent {
                    if tourPending {
                        Button("Cancel") { tourPending = false }
                    } else {
                        Button("Replay the tour") { tourPending = true }
                    }
                } label: {
                    Text(tourPending ? "Your next call starts with the tour." :
                         "A one-minute spoken walkthrough at the start of your next call.")
                }
            }
            Section {
                Toggle("Launch at login", isOn: Binding(get: { app.launchAtLogin }, set: { app.launchAtLogin = $0 }))
                Toggle(isOn: Binding(get: { UserDefaults.standard.bool(forKey: Prefs.showInDock) },
                                     set: { UserDefaults.standard.set($0, forKey: Prefs.showInDock) })) {
                    Text("Show in Dock")
                    Text("Off: Speakeasy lives only in the menu bar.")
                }
            }
            Section {
                LabeledContent("Microphone") {
                    switch app.micAuthorization {
                    case .authorized: Text("Allowed").foregroundStyle(.secondary)
                    case .notDetermined: Button("Allow…") { Task { _ = await app.requestMicrophone() } }
                    default: Button("Open Privacy Settings") { AppModel.openMicrophonePrivacySettings() }
                    }
                }
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: Shortcuts

private struct ShortcutSettings: View {
    @AppStorage(Prefs.listeningBeta) private var listeningBeta = false
    @EnvironmentObject var app: AppModel

    private var mute: KeyShortcut? { AppModel.storedShortcut(Prefs.muteShortcut, default: .defaultMute) }
    private var pause: KeyShortcut? { AppModel.storedShortcut(Prefs.pauseShortcut, default: .defaultPause) }
    /// No default: nil until the user sets one.
    private var listening: KeyShortcut? {
        guard let raw = UserDefaults.standard.string(forKey: Prefs.listeningShortcut),
              case .success(let s) = KeyShortcut.parse(raw, reserved: nil) else { return nil }
        return s
    }

    var body: some View {
        Form {
            Section {
                LabeledContent("Start or end a call") {
                    ShortcutRecorder(shortcut: app.callShortcut, taken: [mute, pause].compactMap { $0 }) { app.setCallShortcut($0) }
                }
                if let problem = app.callShortcutProblem { Text(problem).foregroundStyle(.orange) }
            } footer: {
                Text("Works from any app. Press it again to show the panel, or to end the call when the panel is showing.")
            }
            Section {
                LabeledContent("Mute or unmute") {
                    ShortcutRecorder(shortcut: mute, name: "Mute", defaultShortcut: .defaultMute,
                                     taken: [app.callShortcut] + [pause].compactMap { $0 },
                                     onTurnOff: { app.setExtraShortcut(Prefs.muteShortcut, nil) }) {
                        app.setExtraShortcut(Prefs.muteShortcut, $0)
                    }
                }
                if let problem = app.muteShortcutProblem, mute != nil { Text(problem).foregroundStyle(.orange) }
            } footer: {
                Text("Tap to mute or unmute. Hold while muted to talk, hold while live to cough. Only active during a call.")
            }
            Section {
                LabeledContent("Pause or resume") {
                    ShortcutRecorder(shortcut: pause, name: "Pause", defaultShortcut: .defaultPause,
                                     taken: [app.callShortcut] + [mute].compactMap { $0 },
                                     onTurnOff: { app.setExtraShortcut(Prefs.pauseShortcut, nil) }) {
                        app.setExtraShortcut(Prefs.pauseShortcut, $0)
                    }
                }
                if let problem = app.pauseShortcutProblem, pause != nil { Text(problem).foregroundStyle(.orange) }
            } footer: {
                Text("A paused call stops listening and billing; tasks keep running.")
            }
            if RoomListener.isSupported && listeningBeta {
                Section {
                    LabeledContent("Listening mode on or off") {
                        ShortcutRecorder(shortcut: listening, name: "Listening", defaultShortcut: .suggestedListening,
                                         taken: [app.callShortcut] + [mute, pause].compactMap { $0 },
                                         onTurnOff: { app.setExtraShortcut(Prefs.listeningShortcut, nil) }) {
                            app.setExtraShortcut(Prefs.listeningShortcut, $0)
                        }
                    }
                    if let problem = app.listeningShortcutProblem, listening != nil { Text(problem).foregroundStyle(.orange) }
                } footer: {
                    Text("Off until you set one. Works during a call: turns listening mode on (pausing the call) and off again (resuming it, knowing what was said). The call and pause shortcuts also turn it off while it's on.")
                }
            }
            Section {
                Button("Restore default shortcuts") { app.resetShortcuts() }
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: Voice

private struct VoiceSettings: View {
    @EnvironmentObject var app: AppModel
    @State private var draft = ServerSettings()

    var body: some View {
        ServerForm(draft: $draft) {
            TextField("Assistant name", text: Binding(get: { draft.assistantName ?? "" },
                                                      set: { draft.assistantName = $0.isEmpty ? nil : $0 }),
                      prompt: Text(VoiceState.defaultAssistantName))
            TextField("What it calls you", text: Binding(get: { draft.userName ?? "" },
                                                         set: { draft.userName = $0.isEmpty ? nil : $0 }),
                      prompt: Text("Optional"))
            Picker("Voice provider", selection: Binding(get: { draft.resolvedProvider },
                                                         set: { p in
                                                             var v = draft.voice ?? .init(); v.provider = p.rawValue
                                                             v.voice = VoiceCatalog.compatible(v.voice, provider: p)
                                                             draft.voice = v })) {
                ForEach(VoiceProvider.allCases) { Text($0.label).tag($0) }
            }
            providerStatus
            VoiceChooser(provider: draft.resolvedProvider,
                         selection: Binding(get: { draft.voice?.voice ?? "" },
                                            set: { v in var voice = draft.voice ?? .init(); voice.voice = v.isEmpty ? nil : v; draft.voice = voice }))
        }
    }

    @ViewBuilder private var providerStatus: some View {
        switch draft.resolvedProvider {
        case .codex:
            LabeledContent("ChatGPT sign-in") {
                if let signed = app.status?.codexSignedIn {
                    Text(signed ? "Signed in" : "Not signed in — run codex login on the Hermes machine")
                        .foregroundStyle(signed ? Color.secondary : Color.orange)
                } else { Text("Unknown").foregroundStyle(.secondary) }
            }
        case .openai:
            LabeledContent("OpenAI API key") {
                if let set = app.status?.apiKeySet {
                    Text(set ? "Set on the server" : "Not set — add SPEAKEASY_OPENAI_API_KEY to the Hermes .env")
                        .foregroundStyle(set ? Color.secondary : Color.orange)
                } else { Text("Unknown").foregroundStyle(.secondary) }
            }
            Text("The key stays on the Hermes machine. Speakeasy never asks for it or stores it on this Mac.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

/// Voice list for the selected provider: pick by listening, not by label. Applies from the next call.
private struct VoiceChooser: View {
    let provider: VoiceProvider
    @Binding var selection: String
    @StateObject private var player = VoicePreviewPlayer()

    private var effective: String { selection.isEmpty ? VoiceCatalog.defaultVoice(for: provider) : selection }

    var body: some View {
        LabeledContent("Voice") {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(VoiceCatalog.voices(for: provider)) { option in row(option) }
                if !selection.isEmpty, VoiceCatalog.option(selection, provider: provider) == nil {
                    row(VoiceOption(id: selection, summary: "Not in this provider's list; the default is used.", hasPreview: false))
                }
                Text("Changes apply from your next call.").font(.caption).foregroundStyle(.secondary).padding(.top, 4)
            }
        }
        .onChange(of: provider) { _, _ in player.stop() }
        .onDisappear { player.stop() }
    }

    private func row(_ option: VoiceOption) -> some View {
        let chosen = option.id == effective
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Button {
                selection = option.id
            } label: {
                Image(systemName: chosen ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(chosen ? Color.accentColor : Color.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Use \(option.name)")
            .accessibilityAddTraits(chosen ? .isSelected : [])
            VStack(alignment: .leading, spacing: 0) {
                Text(option.name)
                if !option.summary.isEmpty { Text(option.summary).font(.caption).foregroundStyle(.secondary) }
            }
            .contentShape(Rectangle())
            .onTapGesture { selection = option.id }
            Spacer(minLength: 12)
            if option.hasPreview {
                let playing = player.playing == option.id
                Button { player.toggle(option.id) } label: {
                    Image(systemName: playing ? "stop.circle" : "play.circle").imageScale(.large)
                }
                .buttonStyle(.borderless)
                .help(playing ? "Stop" : "Hear \(option.name)")
                .accessibilityLabel(playing ? "Stop \(option.name) sample" : "Play \(option.name) sample")
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: Voice brief

private struct BriefSettings: View {
    @EnvironmentObject var app: AppModel
    @State private var text = ""
    @State private var dirty = false
    @State private var draft = ServerSettings()
    @State private var busy = false
    @State private var reviewing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(stateLine).font(.callout).foregroundStyle(.secondary)
                Spacer()
                Button(app.tune?.state == "working" ? "Tuning…" : "Tune from my calls") {
                    if app.tune?.state == "ready" { reviewing = true; return }
                    Task { await app.startTune() }
                }
                .help("Your Hermes reads your recent calls and suggests edits to this brief. You approve each one.")
                .disabled(!app.isPaired || app.tune?.state == "working" || (app.brief?.text ?? "").isEmpty)
                Button("Rewrite") {
                    busy = true
                    Task { await app.rewriteBrief(); busy = false; dirty = false }
                }
                .help("Ask your Hermes to write the brief again from what it knows now")
                .disabled(busy || !app.isPaired)
                Button("Save") {
                    busy = true
                    Task { if await app.saveBrief(text) { dirty = false }; busy = false }
                }
                .disabled(!dirty || busy)
            }
            TextEditor(text: $text)
                .font(.system(.body, design: .default))
                .frame(minHeight: 180)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
                .onChange(of: text) { _, new in dirty = new != (app.brief?.text ?? "") }
                .accessibilityLabel("Voice brief text")
            ServerForm(draft: $draft) {
                Toggle("Keep the brief fresh automatically", isOn: Binding(
                    get: { draft.brief?.autoRefresh ?? true },
                    set: { v in var b = draft.brief ?? .init(); b.autoRefresh = v; draft.brief = b }))
                Toggle("Include recent voice conversations", isOn: Binding(
                    get: { draft.brief?.includeRecentVoice ?? true },
                    set: { v in var b = draft.brief ?? .init(); b.includeRecentVoice = v; draft.brief = b }))
                TextField("Extra instructions", text: Binding(get: { draft.instructionsExtra ?? "" },
                                                              set: { draft.instructionsExtra = $0.isEmpty ? nil : $0 }),
                          prompt: Text("e.g. Keep answers under two sentences"), axis: .vertical)
                    .lineLimit(2...4)
            }
            .frame(height: 190)
        }
        .padding([.top, .horizontal], 16)
        .onAppear { text = app.brief?.text ?? "" }
        .onReceive(app.$brief) { b in if !dirty { text = b?.text ?? "" } }
        .task { await app.refreshTune() }
        .task(id: app.tune?.state) {
            // While Hermes reads the calls, check back every few seconds; open the review when it's ready.
            guard app.tune?.state == "working" else { return }
            while !Task.isCancelled, app.tune?.state == "working" {
                try? await Task.sleep(for: .seconds(4))
                await app.refreshTune()
            }
            if app.tune?.state == "ready" { reviewing = true }
        }
        .safeAreaInset(edge: .bottom) { tuneBanner }
        .sheet(isPresented: $reviewing) { TuneReview().environmentObject(app) }
    }

    @ViewBuilder private var tuneBanner: some View {
        if let tune = app.tune {
            switch tune.state {
            case "working":
                Label("Reading your recent calls. This takes a minute or two.", systemImage: "hourglass")
                    .font(.callout).foregroundStyle(.secondary).padding(.bottom, 8)
            case "ready":
                HStack {
                    Label("\(tune.edits.count) suggested edit\(tune.edits.count == 1 ? "" : "s") from your calls",
                          systemImage: "sparkles")
                    Spacer()
                    Button("Review") { reviewing = true }
                }
                .font(.callout).padding(.bottom, 8)
            case "failed":
                Label("Tuning didn't finish: \(tune.error ?? "unknown error")", systemImage: "exclamationmark.triangle")
                    .font(.callout).foregroundStyle(.secondary).padding(.bottom, 8)
            default:
                EmptyView()
            }
        }
    }

    private var stateLine: String {
        guard let brief = app.brief else { return app.isPaired ? "Loading…" : "Not connected" }
        var parts: [String] = []
        switch brief.state {
        case "writing", "pending": parts.append("Writing…")
        case "ready": parts.append("Ready")
        case "failed": parts.append("Last rewrite failed")
        case let s?: parts.append(s.capitalized)
        case nil: break
        }
        if brief.edited { parts.append("edited by you") }
        if let at = brief.updatedAt { parts.append("updated \(at.formatted(.relative(presentation: .named)))") }
        return parts.joined(separator: " · ")
    }
}

// MARK: Tune from my calls

/// Each suggested edit with the call moment behind it; nothing changes until the user applies it.
struct TuneReview: View {
    @EnvironmentObject var app: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var accepted: Set<String> = []
    @State private var busy = false

    var body: some View {
        let tune = app.tune ?? BriefTune()
        VStack(alignment: .leading, spacing: 12) {
            Text("Suggested edits to your voice brief").font(.headline)
            if !tune.summary.isEmpty {
                Text(tune.summary).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if tune.edits.isEmpty {
                        Text("Nothing to change: your calls didn't show anything the brief should fix.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(tune.edits) { edit in
                        TuneEditRow(edit: edit, on: Binding(
                            get: { accepted.contains(edit.id) },
                            set: { on in if on { accepted.insert(edit.id) } else { accepted.remove(edit.id) } }))
                    }
                    if !tune.productIssues.isEmpty {
                        Divider().padding(.vertical, 4)
                        Text("Not something your brief can fix").font(.subheadline.weight(.semibold))
                        ForEach(Array(tune.productIssues.enumerated()), id: \.offset) { _, issue in
                            VStack(alignment: .leading, spacing: 2) {
                                Text("• " + issue.what).fixedSize(horizontal: false, vertical: true)
                                if let e = issue.evidence, !e.isEmpty {
                                    Text(e).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("Your call transcripts went to the model your Hermes uses to make these suggestions.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Dismiss all") {
                    busy = true
                    Task { await app.dismissTune(); busy = false; dismiss() }
                }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button(accepted.isEmpty ? "Apply" : "Apply \(accepted.count)") {
                    busy = true
                    Task { await app.applyTune(Array(accepted)); busy = false; dismiss() }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(accepted.isEmpty || busy)
            }
        }
        .padding(20)
        .frame(width: 540, height: 520)
        .onAppear { accepted = Set((app.tune?.edits ?? []).map(\.id)) }
    }
}

private struct TuneEditRow: View {
    let edit: BriefTune.Edit
    @Binding var on: Bool

    var body: some View {
        Toggle(isOn: $on) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.callout.weight(.semibold))
                if let old = edit.old, !old.isEmpty {
                    Text(old).strikethrough().foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if let new = edit.new, !new.isEmpty {
                    Text(new).fixedSize(horizontal: false, vertical: true)
                }
                Text(edit.why + ((edit.evidence ?? "").isEmpty ? "" : " — \(edit.evidence!)"))
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .toggleStyle(.checkbox)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
    }

    private var title: String {
        switch edit.kind {
        case "add": return "Add to \(edit.section ?? "the brief")"
        case "remove": return "Remove"
        default: return "Change"
        }
    }
}

// MARK: Behavior

/// One-line explanation shared by Settings and onboarding.
let continuityHelp = "When you ask about something you were already discussing in a Hermes chat or thread, the task continues inside that conversation, with its history, and the reply posts there."

private struct BehaviorSettings: View {
    @EnvironmentObject var app: AppModel
    @State private var draft = ServerSettings()

    var body: some View {
        ServerForm(draft: $draft) {
            Section {
                Text("Several requests can run at once. Each becomes its own task in the panel; follow-ups go to the right one.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 4) {
                Toggle("Continue existing conversations", isOn: Binding(
                    get: { draft.continuity?.enabled ?? true },
                    set: { v in var c = draft.continuity ?? .init(); c.enabled = v; draft.continuity = c }))
                Text(continuityHelp).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Section("Spoken updates") {
                Toggle("Give a short update on long tasks", isOn: Binding(
                    get: { draft.speech?.progress ?? true },
                    set: { v in var x = draft.speech ?? .init(); x.progress = v; draft.speech = x }))
                Text("Where a task went (like a new thread) is always said.").font(.caption).foregroundStyle(.secondary)
            }
            Stepper(value: Binding(get: { Int(draft.idlePauseMinutes ?? 5) }, set: { draft.idlePauseMinutes = Double($0) }),
                    in: 0...60) {
                let minutes = Int(draft.idlePauseMinutes ?? 5)
                Text(minutes == 0 ? "Auto-pause a quiet call: off" : "Auto-pause a quiet call after \(minutes) min")
            }
            Stepper(value: Binding(get: { draft.maxCallMinutes ?? 30 }, set: { draft.maxCallMinutes = $0 }),
                    in: 5...240, step: 5) {
                Text("End a call after \(draft.maxCallMinutes ?? 30) min")
            }
            .help("A hard cap per call so a forgotten call can't run up your plan or bill")
            Section("Task routing") {
                Text(app.status?.routingExplainer ?? routingExplainerFallback)
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                RoutingModelPicker(app: app)
                Text(app.status?.routingHint ?? "Stored in your Hermes config under auxiliary → speakeasy_router.")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

// MARK: Delivery

private struct DeliverySettings: View {
    @EnvironmentObject var app: AppModel
    @State private var draft = ServerSettings()
    @State private var adding = false
    @State private var suggesting = false
    @State private var suggestions: [ServerSettings.Channel]?
    @State private var suggestError: String?

    private var channels: [ServerSettings.Channel] { draft.delivery?.channels ?? [] }

    private func setChannels(_ list: [ServerSettings.Channel]) {
        var d = draft.delivery ?? .init(); d.channels = list; draft.delivery = d
    }

    private var modeExplainer: String {
        switch draft.delivery?.mode ?? "home" {
        case "single": return "Everything goes to the default. Speakeasy never posts anywhere else or picks up a chat elsewhere."
        case "topic": return "New work goes to the channel whose description fits, else the default. Follow-ups continue where their task runs."
        default: return "New work goes to the default. A follow-up to something in an approved channel continues in that thread."
        }
    }

    var body: some View {
        ServerForm(draft: $draft) {
            Section {
                Text("Where finished work goes by default, and where tasks go that don't fit a channel below.")
                    .font(.callout).foregroundStyle(.secondary)
                DeliveryPicker(target: Binding(get: { draft.deliveryTarget }, set: { t in
                    var d = draft.delivery ?? .init(); d.target = t; if t == nil { d.newThread = false }
                    draft.delivery = d
                    draft.notifyTarget = t
                }), destinations: app.destinations)
                if app.status?.canOpenThread(in: draft.deliveryTarget) == true {
                    Toggle("Run each task in a new thread there", isOn: Binding(
                        get: { draft.delivery?.newThread ?? false },
                        set: { v in var d = draft.delivery ?? .init(); d.newThread = v; draft.delivery = d }))
                        .help("You can follow up in that thread, and the call still hears the result.")
                }
            }
            Section {
                Picker("Voice work goes", selection: Binding(
                    get: { draft.delivery?.mode ?? "home" },
                    set: { m in var d = draft.delivery ?? .init(); d.mode = m; draft.delivery = d })) {
                    Text("Only to the default").tag("single")
                    Text("Default, plus threads in channels below").tag("home")
                    Text("Sorted into channels below by topic").tag("topic")
                }
                Text(modeExplainer).font(.caption).foregroundStyle(.secondary)
            } header: {
                Text("Where work goes")
            }
            if (draft.delivery?.mode ?? "home") != "single" {
            Section {
                ForEach(channels) { channel in
                    ChannelRow(channel: Binding(
                        get: { channel },
                        set: { new in setChannels(channels.map { $0.target == channel.target ? new : $0 }) }),
                        destinationLabel: app.destinations.first(where: { $0.target == channel.target })?.label ?? channel.target,
                        canThread: app.status?.canOpenThread(in: channel.target) == true,
                        remove: { setChannels(channels.filter { $0.target != channel.target }) })
                }
                HStack {
                    Button("Add channel…") { adding = true }.disabled(channels.count >= 8 || app.destinations.isEmpty)
                    Button {
                        suggest()
                    } label: {
                        if suggesting { ProgressView().controlSize(.small) } else { Text("Suggest channels") }
                    }
                    .disabled(suggesting || app.destinations.isEmpty)
                    .help("Your Hermes proposes channels from what it knows about your work. Nothing is saved until you pick.")
                }
                if let suggestError { Text(suggestError).font(.caption).foregroundStyle(.orange) }
            } header: {
                Text("Approved channels")
            } footer: {
                Text("Speakeasy only posts in, or continues threads in, the default and these channels. Saying a channel's name (\"put this in #work\") sends a new task there.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            }
            if app.destinations.isEmpty {
                Text("No connected Hermes chats found. Connect one in Hermes (e.g. Telegram), then reopen Settings.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .sheet(isPresented: $adding) {
            AddChannelSheet(destinations: app.destinations.filter { d in !channels.contains { $0.target == d.target } },
                            canThread: { app.status?.canOpenThread(in: $0) == true }) { new in
                setChannels(ChannelSuggestions.merge([new], into: channels))
            }
        }
        .sheet(item: Binding(get: { suggestions.map { SuggestionList(items: $0) } }, set: { suggestions = $0?.items })) { list in
            SuggestionsSheet(items: list.items, existing: channels,
                             canThread: { app.status?.canOpenThread(in: $0) == true }) { picked in
                setChannels(ChannelSuggestions.merge(picked, into: channels))
            }
        }
    }

    private func suggest() {
        suggesting = true; suggestError = nil
        Task {
            let result = await app.suggestChannels()
            suggesting = false
            if let result, !result.isEmpty { suggestions = result }
            else { suggestError = app.lastError ?? "Hermes didn't suggest any channels." }
        }
    }
}

private struct SuggestionList: Identifiable {
    let items: [ServerSettings.Channel]
    var id: String { items.map(\.target).joined(separator: ",") }
}

private struct ChannelRow: View {
    @Binding var channel: ServerSettings.Channel
    var destinationLabel: String
    var canThread: Bool
    var remove: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                TextField("Name", text: $channel.label, prompt: Text("#work")).frame(maxWidth: 140)
                Text(destinationLabel).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    .help(destinationLabel)
                Spacer()
                Button(role: .destructive, action: remove) { Image(systemName: "minus.circle") }
                    .buttonStyle(.borderless).help("Remove this channel").accessibilityLabel("Remove \(channel.label)")
            }
            TextField("Topic", text: $channel.topic, prompt: Text("e.g. my job: meetings, email, projects"))
            if canThread {
                Toggle("Run each task in a new thread", isOn: $channel.newThread)
            }
        }
        .padding(.vertical, 2)
    }
}

private struct AddChannelSheet: View {
    var destinations: [Destination]
    var canThread: (String) -> Bool
    var add: (ServerSettings.Channel) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var target: String?
    @State private var label = ""
    @State private var topic = ""
    @State private var newThread = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Add a channel").font(.headline)
            Form {
                Picker("Chat", selection: $target) {
                    Text("Choose…").tag(String?.none)
                    ForEach(destinations) { d in Text(d.label).tag(String?.some(d.target)) }
                }
                TextField("Name", text: $label, prompt: Text("#work"))
                TextField("Topic", text: $topic, prompt: Text("my job: meetings, email, projects"))
                if let target, canThread(target) { Toggle("Run each task in a new thread", isOn: $newThread) }
            }
            .formStyle(.grouped)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Add") {
                    if let target {
                        add(.init(target: target, label: label.trimmingCharacters(in: .whitespaces), topic: topic,
                                  newThread: newThread && canThread(target)))
                    }
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(target == nil || label.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20).frame(width: 440)
        .onChange(of: target) { _, new in
            if label.isEmpty, let d = destinations.first(where: { $0.target == new }) { label = suggestedChannelLabel(d.label) }
        }
    }
}

/// The channels Hermes suggested, with checkboxes; only the picked ones are added (then Save).
struct SuggestionsSheet: View {
    var items: [ServerSettings.Channel]
    var existing: [ServerSettings.Channel]
    var canThread: (String) -> Bool
    var add: ([ServerSettings.Channel]) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var picked: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Suggested channels").font(.headline)
            Text("From what your Hermes knows about your work. Pick the ones you want; nothing is saved until you press Save.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            ForEach(items) { item in
                let already = existing.contains { $0.target == item.target }
                Toggle(isOn: Binding(get: { picked.contains(item.target) },
                                     set: { on in if on { picked.insert(item.target) } else { picked.remove(item.target) } })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.label + (item.newThread && canThread(item.target) ? " · new thread per task" : "")).fontWeight(.medium)
                        Text(item.topic).font(.caption).foregroundStyle(.secondary)
                        if already { Text("Already added").font(.caption2).foregroundStyle(.secondary) }
                    }
                }
                .toggleStyle(.checkbox)
                .disabled(already)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Add selected") {
                    add(items.filter { picked.contains($0.target) }.map { c in
                        var c = c; c.newThread = c.newThread && canThread(c.target); return c
                    })
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(picked.isEmpty)
            }
        }
        .padding(20).frame(width: 460)
        .onAppear { picked = Set(items.filter { i in !existing.contains { $0.target == i.target } }.map(\.target)) }
    }
}

// MARK: Connection

private struct ConnectionSettings: View {
    @EnvironmentObject var app: AppModel
    @State private var confirmUnpair = false

    var body: some View {
        Form {
            LabeledContent("Server", value: app.config.serverURL?.absoluteString ?? "—")
            if let status = app.status {
                Label(status.reachability, systemImage: status.tailscaleName?.isEmpty == false ? "lock.shield.fill" : "desktopcomputer")
                    .font(.headline)
            }
            if let offer = app.tailnetOffer {
                TailnetOffer(url: offer)
            }
            LabeledContent("This Mac", value: app.isPaired ? (app.deviceName ?? "Paired") : "Not paired")
            Section("Status") {
                if let status = app.status {
                    ForEach(status.checks) { check in
                        HStack(alignment: .top) {
                            Image(systemName: check.ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                                .foregroundStyle(check.ok ? .green : .orange)
                            VStack(alignment: .leading) {
                                Text(check.title)
                                if let fix = check.fix { Text(fix).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                    if let version = status.version { LabeledContent("Server version", value: version) }
                } else {
                    Text(app.isPaired ? (app.lastError ?? "Checking…") : "Pair this Mac to see status.")
                        .foregroundStyle(.secondary)
                }
                Button("Check again") { Task { await app.refresh() } }.disabled(!app.isPaired || app.refreshing)
            }
            Section {
                HStack {
                    Button(app.isPaired ? "Re-pair…" : "Pair…") {
                        (NSApp.delegate as? AppDelegate)?.showOnboarding()
                    }
                    if app.isPaired {
                        Button("Unpair", role: .destructive) { confirmUnpair = true }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .confirmationDialog("Unpair this Mac?", isPresented: $confirmUnpair) {
            Button("Unpair", role: .destructive) { app.unpair() }
        } message: {
            Text("The device token is removed from this Mac's Keychain. Revoke it on the server with hermes voice revoke.")
        }
    }
}

// MARK: About

private struct AboutSettings: View {
    @EnvironmentObject var app: AppModel
    @AppStorage(Prefs.checkForUpdates) private var autoCheck = true
    var body: some View {
        VStack(spacing: 10) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 64, height: 64)
            Text("Speakeasy").font(.title2.weight(.semibold))
            Text("Version \(app.appVersion)").foregroundStyle(.secondary)
            Link("What's new", destination: URL(string: "https://github.com/rungmc357/speakeasy/blob/main/CHANGELOG.md")!)
                .font(.callout)
            updateRow
            if let version = app.pluginUpdateAvailable {
                VStack(spacing: 4) {
                    Text("Hermes plugin update available: \(version)").font(.callout.weight(.semibold))
                    Text("Running plugin: \(app.status?.version ?? "unknown"). Ask your agent to update Speakeasy on Hermes; it loads without a restart. The Mac app is separate.")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("Copy update request") { app.copyPluginUpdateRequest() }
                }
                .multilineTextAlignment(.center)
            }
            Toggle("Check for updates automatically", isOn: $autoCheck)
                .toggleStyle(.checkbox).font(.callout)
                .help("Once a week, looks for a new version. Nothing is installed until you click Install Update.")
                .onChange(of: autoCheck) { _, on in AppUpdater.shared.automaticallyChecks = on }
            Text("Talk to your own Hermes agent by voice.").foregroundStyle(.secondary)
            Text("MIT License").font(.callout)
            Text("Uses WebRTC (BSD license).").font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder private var updateRow: some View {
        HStack(spacing: 8) {
            if let release = app.updateAvailable {
                Text("Version \(release.version) is available.").foregroundStyle(.primary)
                if AppUpdater.shared.isAvailable {
                    Button("Install Update…") { AppUpdater.shared.checkForUpdates() }
                } else {
                    Button("Download") { app.openUpdate(release) }
                }
            } else {
                Button(app.checkingForUpdates ? "Checking…" : "Check for Updates") {
                    Task { await app.checkForUpdates() }
                }
                .disabled(app.checkingForUpdates)
                switch app.updateOutcome {
                case .upToDate?: Text("You're up to date.").foregroundStyle(.secondary)
                case .noReleases?: Text("No releases published yet.").foregroundStyle(.secondary)
                default: EmptyView()
                }
            }
        }
        .font(.callout)
        if let error = app.updateError { Text(error).font(.caption).foregroundStyle(.orange) }
    }
}

/// Shown when this Mac is paired to the server's loopback address but can't reach it, while the
/// server advertised a tailnet address: offer to switch to it.
struct TailnetOffer: View {
    @EnvironmentObject var app: AppModel
    var url: URL
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Can't reach the server on this Mac, but it's available over Tailscale.", systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Button("Switch to \(url.host ?? url.absoluteString)") { Task { await app.switchServer(to: url) } }
        }
    }
}

/// "#work" from a destination label like "Discord · My Server / work".
func suggestedChannelLabel(_ destination: String) -> String {
    let last = destination.split(whereSeparator: { $0 == "·" || $0 == "/" }).last.map { $0.trimmingCharacters(in: .whitespaces) } ?? destination
    let name = last.hasPrefix("#") ? String(last.dropFirst()) : last
    return "#" + String(name.prefix(39))
}


/// Pick the model that routes voice requests. Falls back to a read-only label on older servers.
/// Shown when the server predates plugin 0.2.20 and doesn't send its own explanation.
let routingExplainerFallback = "When you ask for something on a call, a quick model first decides where it goes: a new task, an addition to a task that's already running, several separate tasks, or a Hermes chat or thread you were already working in. It also decides whether you want to see something on screen. It doesn't do the work; your Hermes agent does. Every request waits on this step, so pick a fast model; a wrong call can put a task in the wrong place."

struct RoutingModelPicker: View {
    @ObservedObject var app: AppModel
    /// Full model lists fetched per provider (the status list carries only Hermes' short list).
    @State private var fullModels: [String: [String]] = [:]

    var body: some View {
        if let routing = app.status?.routingChoice {
            let shown = app.routingDraft ?? routing.current
            let provider = shown.isDefault ? RoutingChoices.defaultID : shown.provider
            Picker("Provider", selection: Binding<String>(
                get: { provider },
                set: { picked in pickProvider(picked, routing: routing) })) {
                Text("Hermes default (your main model)").tag(RoutingChoices.defaultID)
                ForEach(routing.providers) { p in Text(p.name).tag(p.id) }
            }
            if provider != RoutingChoices.defaultID {
                let listed = modelList(provider, routing: routing)
                // The chosen model always shows, even before the full list has loaded.
                let models = shown.model.isEmpty || listed.contains(shown.model) ? listed : [shown.model] + listed
                Picker("Model", selection: Binding<String>(
                    get: { shown.model },
                    set: { picked in var d = shown; d.model = picked; app.routingDraft = d })) {
                    if shown.model.isEmpty { Text("Choose a model").tag("") }
                    ForEach(models, id: \.self) { Text($0).tag($0) }
                }
                .disabled(models.isEmpty)
                Toggle("Let the model think first", isOn: Binding(
                    get: { shown.thinking },
                    set: { on in var d = shown; d.thinking = on; app.routingDraft = d }))
                    .help("Off makes routing much faster; most models route fine without thinking.")
            }
            Text("From the providers Hermes is signed in to. Takes effect on the next request after you save.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            LabeledContent("Routing model", value: app.status?.routingModel ?? "Unknown")
        }
    }

    private func modelList(_ provider: String, routing: RoutingChoices) -> [String] {
        let short = routing.providers.first { $0.id == provider }?.models ?? []
        guard let full = fullModels[provider], !full.isEmpty else { return short }
        return short + full.filter { !short.contains($0) }
    }

    private func pickProvider(_ picked: String, routing: RoutingChoices) {
        let current = routing.current
        if picked == RoutingChoices.defaultID {
            app.routingDraft = RoutingChoices.Current()
        } else if !current.isDefault && picked == current.provider {
            app.routingDraft = nil  // back to what is saved
        } else {
            app.routingDraft = RoutingChoices.Current(provider: picked, model: "", thinking: false, isDefault: false)
        }
        if picked != RoutingChoices.defaultID && fullModels[picked] == nil {
            Task { fullModels[picked] = await app.routingModels(picked) }
        }
    }
}


// MARK: Settings smoke (development only)

/// `--settings-smoke <dir>`: the real Behavior tab against the paired server. Walks the routing
/// picker through the same state changes its dropdowns make, checks the Save/Revert bar at each
/// step, saves, and snapshots each state. Run on a development machine, never a user's.
@MainActor
enum SettingsSmoke {
    static func run(app: AppModel, dir: String, provider: String, model: String) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let host = NSHostingController(rootView: BehaviorSettings().environmentObject(app).frame(width: 560, height: 700))
        let window = NSWindow(contentViewController: host)
        window.title = "Behavior"
        window.setContentSize(NSSize(width: 560, height: 700))
        window.makeKeyAndOrderFront(nil)
        Task { @MainActor in
            func settle() async { try? await Task.sleep(nanoseconds: 1_500_000_000) }
            @MainActor func snap(_ name: String) {
                guard let view = window.contentView,
                      let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
                print("snapshot \(dir)/\(name).png")
            }
            @MainActor func bar(_ step: String) {
                let edit = app.routingEdit
                let needs = app.routingNeedsModel
                let saveOn = edit != nil && !needs
                print("\(step): save=\(saveOn ? "on" : "off") revert=\(edit != nil ? "on" : "off") "
                      + "needsModel=\(needs) server=\(app.status?.routingChoice?.label ?? "-")")
            }
            await app.refresh(); await settle()
            let saved = app.status?.routingChoice?.current
            bar("1 loaded"); snap("1-loaded")
            // Provider dropdown → a different provider: nothing chosen yet, Save waits for a model.
            app.routingDraft = RoutingChoices.Current(provider: provider, model: "", thinking: false, isDefault: false)
            await settle(); bar("2 provider picked"); snap("2-provider-picked")
            // Model dropdown.
            var d = app.routingDraft!; d.model = model; app.routingDraft = d
            await settle(); bar("3 model picked"); snap("3-model-picked")
            // Revert.
            app.routingDraft = nil
            await settle(); bar("4 reverted")
            // Pick again and Save.
            app.routingDraft = RoutingChoices.Current(provider: provider, model: model, thinking: false, isDefault: false)
            let ok = await app.saveRoutingEdit()
            await settle(); bar("5 saved ok=\(ok)"); snap("5-saved")
            // Put the original back through the same path.
            if let saved {
                app.routingDraft = saved.isDefault ? RoutingChoices.Current() : saved
                let back = await app.saveRoutingEdit()
                await settle(); bar("6 restored ok=\(back)")
            }
            exit(0)
        }
    }
}
