import AppKit
import AVFoundation
import SpeakeasyCore
import SpeakeasyClient
import SwiftUI

/// The guided first run, one screen per step:
/// connect → microphone → voice sign-in → names → where finished work goes → task routing → home control
/// (only when Hermes has Home Assistant) → voice brief → where Speakeasy lives (menu bar and
/// Dock, or menu bar only) → hotkey + try it.
@MainActor
final class OnboardingFlow: ObservableObject {
    enum Step: Int, CaseIterable {
        case connect, microphone, voice, names, delivery, routing, home, brief, appearance, hotkey
        var title: String {
            switch self {
            case .connect: return "Connect to Hermes"
            case .microphone: return "Microphone"
            case .voice: return "Voice sign-in"
            case .names: return "Names"
            case .delivery: return "Finished work"
            case .routing: return "Task routing"
            case .home: return "Home control"
            case .brief: return "Voice brief"
            case .appearance: return "Menu bar and Dock"
            case .hotkey: return "Try it"
            }
        }
    }

    @Published var step: Step = .connect
    @Published var server = AppConfig.localServerURL.absoluteString
    @Published var code = ""
    @Published var pairing = false
    @Published var pairError: String?
    @Published var assistantName = ""
    @Published var userName = ""
    @Published var target: String?
    @Published var continuity = true
    @Published var saving = false
    @Published var saveError: String?
    /// Chosen on the "Where Speakeasy lives" step; applied when setup finishes, so the setup
    /// window can't slip behind other apps mid-setup the way a menu-bar-only app's can.
    @Published var showInDock = UserDefaults.standard.object(forKey: Prefs.showInDock) as? Bool ?? true

    let app: AppModel
    var onFinish: () -> Void = {}

    init(app: AppModel) {
        self.app = app
        if let saved = app.config.serverURL { server = saved.absoluteString }
        if app.isPaired { step = .microphone }
    }

    func pairFromLink(_ link: PairingLink) {
        server = link.server.absoluteString
        code = link.code
        step = .connect
        pair()
    }

    func pair() {
        guard let url = URL(string: server.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            pairError = "Enter a server address like http://127.0.0.1:8795"; return
        }
        pairing = true; pairError = nil
        Task {
            do {
                try await app.pair(server: url, code: code)
                pairing = false
                await app.refresh()
                loadNames()
                next()
            } catch {
                pairing = false
                pairError = app.describe(error)
            }
        }
    }

    func loadNames() {
        let s = app.settings
        if assistantName.isEmpty { assistantName = s.assistantName ?? app.status?.assistantName ?? "" }
        if userName.isEmpty { userName = s.userName ?? "" }
        if target == nil { target = s.deliveryTarget ?? app.suggestedDestination }
        continuity = s.continuity?.enabled ?? true
    }

    /// Steps `hermes voice setup` or the system already finished are skipped, so a normal setup
    /// shows: names, where results go, shortcut (plus microphone the first time).
    func isAlreadyDone(_ s: Step) -> Bool {
        switch s {
        case .connect: return app.isPaired
        case .microphone: return app.micAuthorization == .authorized
        case .voice: return app.status?.voiceReady == true
        case .brief: return true  // written in the background; shown and editable in Settings
        // Only offered when Hermes has Home Assistant and it answered; else nothing to ask.
        case .home: return app.home?.available != true
        default: return false
        }
    }

    func next() {
        var n = Step(rawValue: step.rawValue + 1)
        while let candidate = n, isAlreadyDone(candidate) { n = Step(rawValue: candidate.rawValue + 1) }
        if let n { step = n } else { finish() }
        if step == .names { loadNames() }
    }

    /// First step that still needs the user.
    func start() {
        step = .connect
        if isAlreadyDone(.connect) { next() }
    }

    func back() {
        var p = Step(rawValue: step.rawValue - 1)
        while let candidate = p, isAlreadyDone(candidate) { p = Step(rawValue: candidate.rawValue - 1) }
        if let p { step = p }
    }

    /// Names, delivery and continuity are saved together (`POST /voice/onboarding`): after the
    /// delivery step, and again from the last step when continuity is changed there.
    func saveProfile(then advance: Bool = true) {
        saving = true; saveError = nil
        Task {
            let ok = await app.completeOnboarding(assistantName: assistantName.trimmingCharacters(in: .whitespaces),
                                                  userName: userName.trimmingCharacters(in: .whitespaces),
                                                  target: target, continuity: continuity)
            saving = false
            if ok { if advance { next() } } else { saveError = app.lastError ?? "Couldn't save" }
        }
    }

    func finish() {
        UserDefaults.standard.set(showInDock, forKey: Prefs.showInDock)
        onFinish()
    }
}

@MainActor
final class OnboardingWindowController: NSObject, NSWindowDelegate {
    let flow: OnboardingFlow
    private var window: NSWindow?
    var onFinish: () -> Void = {}

    init(app: AppModel) {
        flow = OnboardingFlow(app: app)
        super.init()
        flow.onFinish = { [weak self] in
            self?.window?.close()
        }
    }

    func show() {
        if window == nil {
            Task { await flow.app.refresh(); flow.start() }
            let hosting = NSHostingController(rootView: OnboardingView(flow: flow).environmentObject(flow.app))
            let w = NSWindow(contentViewController: hosting)
            w.title = "Welcome to Speakeasy"
            w.styleMask = [.titled, .closable]
            w.setContentSize(NSSize(width: 520, height: 460))
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            window = w
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        // Closing early is fine: every step is also in Settings. Only a paired Mac counts as done.
        // Keep the Dock choice if the user got past that step, even without pressing Done.
        if flow.step.rawValue > OnboardingFlow.Step.appearance.rawValue {
            UserDefaults.standard.set(flow.showInDock, forKey: Prefs.showInDock)
        }
        if flow.app.isPaired { onFinish() }
        window = nil
    }
}

struct OnboardingView: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                ForEach(OnboardingFlow.Step.allCases, id: \.self) { step in
                    Capsule().fill(step.rawValue <= flow.step.rawValue ? Color.accentColor : Color.secondary.opacity(0.25))
                        .frame(height: 4)
                }
            }
            .accessibilityElement()
            .accessibilityLabel("Step \(flow.step.rawValue + 1) of \(OnboardingFlow.Step.allCases.count): \(flow.step.title)")
            .padding(.bottom, 18)

            Group {
                switch flow.step {
                case .connect: ConnectStep(flow: flow)
                case .microphone: MicrophoneStep(flow: flow)
                case .voice: VoiceSignInStep(flow: flow)
                case .names: NamesStep(flow: flow)
                case .delivery: DeliveryStep(flow: flow)
                case .routing: RoutingStep(flow: flow)
                case .home: HomeStep(flow: flow)
                case .brief: BriefStep(flow: flow)
                case .appearance: AppearanceStep(flow: flow)
                case .hotkey: HotkeyStep(flow: flow)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .padding(24)
        .frame(width: 520, height: 460)
    }
}

private struct StepHeader: View {
    var title: String
    var subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.title2.weight(.semibold))
            Text(subtitle).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 14)
    }
}

private struct StepButtons: View {
    var back: (() -> Void)?
    var skip: (() -> Void)?
    var primary: String
    var primaryDisabled = false
    var busy = false
    var action: () -> Void
    var body: some View {
        HStack {
            if let back { Button("Back", action: back) }
            Spacer()
            if busy { ProgressView().controlSize(.small) }
            if let skip { Button("Skip", action: skip) }
            Button(primary, action: action)
                .keyboardShortcut(.defaultAction)
                .disabled(primaryDisabled || busy)
        }
        .padding(.top, 12)
    }
}

// MARK: (a) Connect

private struct ConnectStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    @State private var manual = false
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            StepHeader(title: "Connect to your Hermes",
                       subtitle: "Your Hermes agent sets this up. Send it this message in any chat you use with it. It installs what's needed and sends you a link that connects this app.")
            HStack(alignment: .top, spacing: 12) {
                Text(setupPrompt)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button(copied ? "Copied" : "Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(setupPrompt, forType: .string)
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { copied = false }
                }
                .buttonStyle(.borderedProminent)
                .accessibilityHint("Copies the setup message for your Hermes agent")
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.06)))

            if flow.pairing {
                Label("Connecting…", systemImage: "link").foregroundStyle(.secondary)
            } else {
                Label("Waiting for the link from your agent. Click it and this moves on by itself.",
                      systemImage: "hourglass")
                    .foregroundStyle(.secondary).font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = flow.pairError {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let offer = app.tailnetOffer { TailnetOffer(url: offer) }

            DisclosureGroup("Have a code instead?", isExpanded: $manual) {
                Form {
                    TextField("Server address", text: $flow.server, prompt: Text("https://your-machine.your-tailnet.ts.net:8795"))
                        .accessibilityHint("The address your agent gave you: this Mac, or an https Tailscale address ending in .ts.net")
                    TextField("Pairing code", text: $flow.code, prompt: Text("6 digits"))
                        .font(.system(.body, design: .monospaced))
                        .onSubmit { if normalizedPairingCode(flow.code) != nil { flow.pair() } }
                }
                .formStyle(.grouped)
                .frame(height: 120)
            }
            Spacer()
            if manual {
                StepButtons(primary: "Connect", primaryDisabled: normalizedPairingCode(flow.code) == nil, busy: flow.pairing,
                            action: flow.pair)
            }
        }
    }

    private var setupPrompt: String { SetupPrompt.text }
}

// MARK: (b) Microphone

private struct MicrophoneStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Microphone",
                       subtitle: "Speakeasy listens only during a call you start. Audio goes to your voice provider through your own Hermes server.")
            if let status = app.status {
                Label(status.reachability, systemImage: status.tailscaleName?.isEmpty == false ? "lock.shield.fill" : "desktopcomputer")
                    .font(.headline)
            }
            switch app.micAuthorization {
            case .authorized:
                Label("Microphone access is on", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            case .denied, .restricted:
                Label("Microphone access is off", systemImage: "mic.slash.fill").foregroundStyle(.orange)
                Button("Open Privacy Settings") { AppModel.openMicrophonePrivacySettings() }
            default:
                Button("Allow microphone") { Task { _ = await app.requestMicrophone() } }
            }
            Spacer()
            StepButtons(back: nil, primary: "Continue", action: flow.next)
        }
    }
}

// MARK: (c) Voice sign-in

private struct VoiceSignInStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    @State private var showAPIKeyHelp = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Voice sign-in",
                       subtitle: "The voice is OpenAI's GPT-Live-1. By default it runs through Codex OAuth with your ChatGPT account on the Hermes machine, so no API key is needed. You can use an OpenAI API key instead.")
            if let status = app.status {
                ForEach(status.checks.filter { $0.id != "brief" }) { check in
                    HStack(alignment: .top) {
                        Image(systemName: check.ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                            .foregroundStyle(check.ok ? .green : .orange)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(check.title).fontWeight(.medium)
                            if let fix = check.fix {
                                Text(fix).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                            }
                        }
                    }
                    .accessibilityElement(children: .combine)
                }
                if status.offersCodexSignIn {
                    Text(status.codexOnAPIKey ? CodexLoginCopy.apiKeyNote
                         : "\(CodexLoginCopy.notSignedIn) Sign in here: you'll get a code to enter on any device.")
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                    CodexSignInButton(onAlternative: { showAPIKeyHelp = true }).controlSize(.large)
                } else if status.codexSignedIn != true && status.resolvedProvider == .codex {
                    // Older plugins: the checklist line above says to update; Update does it from here.
                    PluginUpdateForSignInButton().controlSize(.large)
                } else if status.resolvedProvider == .grok, status.grokVoiceReady == false {
                    GrokSignInRequestButton()
                }
            } else {
                Text(app.lastError ?? "Checking…").foregroundStyle(.secondary)
            }
            DisclosureGroup(app.status?.offersCodexSignIn == true ? CodexLoginCopy.alternative : "Use an API key instead",
                            isExpanded: $showAPIKeyHelp) {
                if app.status?.canAddOpenAIKey == true {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Add your OpenAI API key here. Calls then use GPT-Live-1 through the OpenAI API and bill that account per minute. The key is saved only on your Hermes computer, never on this Mac.")
                            .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        OpenAIKeyButton(useAfterSaving: true)
                    }
                } else {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Update Speakeasy on your Hermes to add your OpenAI API key here. Calls then use GPT-Live-1 through the OpenAI API and bill that account per minute. The key is saved only on your Hermes computer, never on this Mac.")
                            .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        PluginUpdateForSignInButton()
                    }
                }
            }
            Spacer()
            HStack {
                Button("Check again") { Task { await app.refresh() } }
                Spacer()
            }
            StepButtons(back: flow.back, primary: "Continue", busy: app.refreshing, action: flow.next)
        }
        .task { await app.refresh() }
    }
}

// MARK: (d) Names

private struct NamesStep: View {
    @ObservedObject var flow: OnboardingFlow
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Names", subtitle: "How you and your assistant talk to each other.")
            Form {
                TextField("What do you call your assistant?", text: $flow.assistantName, prompt: Text(VoiceState.defaultAssistantName))
                TextField("What should it call you?", text: $flow.userName, prompt: Text("Optional"))
            }
            .formStyle(.grouped)
            .frame(height: 120)
            Spacer()
            StepButtons(back: flow.back, primary: "Continue", action: flow.next)
        }
    }
}

// MARK: Task routing

/// Pick the model that decides where each spoken request goes. Uses the same picker as Settings
/// (providers Hermes is signed in to); a pending choice is saved on Continue.
private struct RoutingStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    @State private var saving = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Task routing",
                       subtitle: app.status?.routingExplainer ?? routingExplainerFallback)
            Form { RoutingModelPicker(app: app) }
                .formStyle(.grouped)
                .frame(height: 170)
            Text("A small, fast model is best; turning thinking off speeds it up. You can change this any time in Settings › Behavior.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let error = app.lastError, saving == false, app.routingEdit != nil { Text(error).foregroundStyle(.orange) }
            Spacer()
            StepButtons(back: { app.routingDraft = nil; flow.back() }, primary: "Continue",
                        primaryDisabled: app.routingNeedsModel, busy: saving) {
                saving = true
                Task {
                    let ok = await app.saveRoutingEdit()
                    if ok { await app.refreshHome() }  // decides whether the home step is offered
                    saving = false
                    if ok { flow.next() }
                }
            }
        }
        .task { await app.refresh() }
    }
}

// MARK: Home control (offered only when Hermes has Home Assistant)

private struct HomeStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    @State private var selection: Set<String> = []
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Home control", subtitle: "Control your home by voice, instantly.")
            if let info = app.home, info.available {
                HomeStepContent(selection: $selection, info: info)
                    .frame(maxHeight: .infinity)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            if let error = app.homeError { Text(error).font(.caption).foregroundStyle(.orange) }
            HStack {
                Button("Back") { flow.back() }
                Spacer()
                Button("Not now") {
                    Task { saving = true; await app.setHome(enabled: false); saving = false; flow.next() }
                }
                .disabled(saving)
                Button("Turn on") {
                    Task {
                        saving = true
                        let ok = await app.setHome(enabled: true, entities: Array(selection))
                        saving = false
                        if ok { flow.next() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(saving || selection.isEmpty)
            }
            Text("You can change this any time in Settings › Home.").font(.caption).foregroundStyle(.secondary)
        }
        .task {
            if app.home == nil { await app.refreshHome() }
            selection = Set(app.home?.includedIDs ?? [])
        }
    }
}

// MARK: (e) Delivery

private struct DeliveryStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Where should finished work go?",
                       subtitle: "When a task finishes after you hang up, \(flow.assistantName.isEmpty ? app.assistantName : flow.assistantName) can send the result to one of your connected Hermes chats.")
            Form {
                DeliveryPicker(target: $flow.target, destinations: app.destinations)
            }
            .formStyle(.grouped)
            .frame(height: 80)
            Text("You can add more channels later in Settings › Delivery, each with a topic, so a task goes where it belongs. Suggest channels asks your Hermes to propose some.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            SuggestChannelsButton()
            if let error = flow.saveError { Text(error).foregroundStyle(.orange) }
            Spacer()
            StepButtons(back: flow.back, primary: "Continue", busy: flow.saving, action: { flow.saveProfile() })
        }
        .task { if app.destinations.isEmpty { await app.refresh() } }
    }
}

struct DeliveryPicker: View {
    @Binding var target: String?
    var destinations: [Destination]
    var body: some View {
        Picker("Send finished work to", selection: $target) {
            Text("Nowhere (notifications on this Mac only)").tag(String?.none)
            ForEach(destinations) { d in Text(d.label).tag(String?.some(d.target)) }
            if let target, !destinations.contains(where: { $0.target == target }) {
                Text(target).tag(String?.some(target))
            }
        }
    }
}

// MARK: (f) Brief

private struct BriefStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Your assistant is writing a short brief about you",
                       subtitle: "Your Hermes writes what the voice model should know — how to address you, what you're working on — from what it already knows. You can read and edit it in Settings › Voice brief.")
            HStack(spacing: 8) {
                if app.brief?.state == "writing" || app.brief?.state == "pending" { ProgressView().controlSize(.small) }
                Text(briefLine).foregroundStyle(.secondary)
            }
            if let text = app.brief?.text, !text.isEmpty {
                ScrollView {
                    Text(text).font(.callout).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 180)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.05)))
            }
            Spacer()
            StepButtons(back: flow.back, skip: flow.next, primary: "Continue", action: flow.next)
        }
        .task {
            // Poll gently while the brief is being written.
            for _ in 0..<30 {
                await app.refreshBrief()
                if let state = app.brief?.state, state != "writing" && state != "pending" { break }
                try? await Task.sleep(nanoseconds: 4_000_000_000)
            }
        }
    }

    private var briefLine: String {
        switch app.brief?.state {
        case "writing", "pending": return "Writing… calls work meanwhile."
        case "ready", "edited": return "Ready."
        case "failed": return "Couldn't write it this time. Calls still work; try Rewrite in Settings later."
        default: return "Not started yet. Calls still work."
        }
    }
}

// MARK: Dock icon choice

private struct AppearanceStep: View {
    @ObservedObject var flow: OnboardingFlow
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Where should Speakeasy live?",
                       subtitle: "The Speakeasy icon is always in the menu bar. You can also keep it in the Dock.")
            HStack(spacing: 12) {
                AppearanceOption(symbol: "dock.rectangle", title: "Menu bar and Dock",
                                 detail: "Easy to find: click the Dock icon to get back to Settings.",
                                 selected: flow.showInDock) { flow.showInDock = true }
                AppearanceOption(symbol: "menubar.rectangle", title: "Menu bar only",
                                 detail: "Out of the way. Everything is in the menu bar icon.",
                                 selected: !flow.showInDock) { flow.showInDock = false }
            }
            .fixedSize(horizontal: false, vertical: true)
            Text("You can change this any time in Settings › General › Show in Dock.")
                .font(.caption).foregroundStyle(.secondary)
            Spacer()
            StepButtons(back: flow.back, primary: "Continue", action: flow.next)
        }
    }
}

private struct AppearanceOption: View {
    var symbol: String
    var title: String
    var detail: String
    var selected: Bool
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Image(systemName: symbol).font(.system(size: 28)).foregroundStyle(Color.accentColor)
                    Spacer()
                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                        .font(.title3).foregroundStyle(selected ? Color.accentColor : Color.secondary)
                }
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(14)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(selected ? 0.08 : 0.03)))
            .overlay(RoundedRectangle(cornerRadius: 10)
                .strokeBorder(selected ? Color.accentColor : Color.secondary.opacity(0.25), lineWidth: selected ? 2 : 1))
            .contentShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

// MARK: (g) Hotkey + try it

private struct HotkeyStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StepHeader(title: "Talk any time",
                       subtitle: "Press the shortcut to start a call; press it again to end. Change it here or later in Settings › General.")
            HStack {
                Text("Call shortcut")
                Spacer()
                ShortcutRecorder(shortcut: app.callShortcut) { app.setCallShortcut($0) }
            }
            if let problem = app.callShortcutProblem { Text(problem).foregroundStyle(.orange) }
            VStack(alignment: .leading, spacing: 4) {
                Toggle("Continue existing conversations", isOn: Binding(get: { flow.continuity },
                                                                         set: { flow.continuity = $0; flow.saveProfile(then: false) }))
                Text(continuityHelp).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 4)
            if let error = flow.saveError { Text(error).foregroundStyle(.orange) }
            Text("Your first call starts with a one-minute tour. Say \u{201C}skip\u{201D} or press Skip tour to jump straight in.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Button {
                flow.finish()  // marks the tour pending before the call reads it
                app.startCall()
            } label: {
                Label("Try it — start a call", systemImage: "waveform")
            }
            .controlSize(.large)
            .disabled(!app.isPaired)
            Spacer()
            StepButtons(back: flow.back, primary: "Done", action: flow.finish)
        }
    }
}

/// Onboarding's Suggest channels: asks Hermes, lets the user pick, and saves picked channels.
private struct SuggestChannelsButton: View {
    @EnvironmentObject var app: AppModel
    @State private var busy = false
    @State private var items: [ServerSettings.Channel]?
    @State private var error: String?
    @State private var added = 0

    var body: some View {
        HStack(spacing: 8) {
            Button { run() } label: { busy ? AnyView(ProgressView().controlSize(.small)) : AnyView(Text("Suggest channels")) }
                .disabled(busy || app.destinations.isEmpty)
            if let error { Text(error).font(.caption).foregroundStyle(.orange).lineLimit(2) }
            else if added > 0 { Text("Added \(added) channel\(added == 1 ? "" : "s").").font(.caption).foregroundStyle(.secondary) }
        }
        .sheet(item: Binding(get: { items.map { SuggestionPick(items: $0) } }, set: { items = $0?.items })) { pick in
            SuggestionsSheet(items: pick.items, existing: app.settings.delivery?.channels ?? [],
                             canThread: { app.status?.canOpenThread(in: $0) == true }) { picked in
                Task { await save(picked) }
            }
        }
    }

    private func run() {
        busy = true; error = nil
        Task {
            let result = await app.suggestChannels()
            busy = false
            if let result, !result.isEmpty { items = result } else { error = app.lastError ?? "Hermes didn't suggest any channels." }
        }
    }

    private func save(_ picked: [ServerSettings.Channel]) async {
        var s = app.settings
        var d = s.delivery ?? .init()
        let before = d.channels?.count ?? 0
        d.channels = ChannelSuggestions.merge(picked, into: d.channels ?? [])
        s.delivery = d
        if await app.save(s) { added = (d.channels?.count ?? 0) - before } else { error = app.lastError }
    }
}

private struct SuggestionPick: Identifiable {
    let items: [ServerSettings.Channel]
    var id: String { items.map(\.target).joined(separator: ",") }
}
