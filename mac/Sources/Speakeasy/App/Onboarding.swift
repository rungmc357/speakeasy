import AppKit
import AVFoundation
import Combine
import SpeakeasyCore
import SpeakeasyClient
import SwiftUI

/// The first run, kept to what a first call needs (`OnboardingPlan` in SpeakeasyCore):
/// connect (only until paired) → voice sign-in (only while the voice isn't ready) → one
/// "You're ready" screen with the microphone, names, where finished work goes, home control
/// (only when Hermes has Home Assistant), the Dock choice and the call shortcut, each with a
/// default already picked, plus Start a call. Task routing and the voice brief keep their
/// defaults and live in Settings.
@MainActor
final class OnboardingFlow: ObservableObject {
    typealias Step = OnboardingStep

    @Published var welcome: WelcomeJourney
    @Published var step: Step = .connect
    /// Paired, and the first status check hasn't answered yet: show "Checking…" rather than
    /// flashing a screen that may turn out to be done.
    @Published var checking = false
    @Published var server = AppConfig.localServerURL.absoluteString
    @Published var code = ""
    @Published var pairing = false
    @Published var pairError: String?
    @Published var assistantName = ""
    @Published var userName = ""
    @Published var target: String?
    @Published var continuity = true
    /// Turn on home control at the end (offered only when Hermes has Home Assistant). The first
    /// setup starts it on, as the old Home screen's default button did; a re-run keeps what's set.
    @Published var homeOn = true
    @Published var saving = false
    @Published var saveError: String?
    /// Applied when setup finishes, so the setup window can't slip behind other apps mid-setup
    /// the way a menu-bar-only app's can.
    @Published var showInDock = UserDefaults.standard.object(forKey: Prefs.showInDock) as? Bool ?? true

    let app: AppModel
    var onFinish: () -> Void = {}
    private var namesLoaded = false
    private var homeLoaded = false
    private var watch: AnyCancellable?

    init(app: AppModel) {
        self.app = app
        welcome = WelcomeJourney(seen: UserDefaults.standard.bool(forKey: WelcomeJourney.seenKey), paired: app.isPaired)
        if let saved = app.config.serverURL { server = saved.absoluteString }
        step = OnboardingPlan.first(facts)
        // Signing in to the voice (here, or with `hermes voice setup`) moves on by itself.
        watch = app.$status.receive(on: RunLoop.main).sink { [weak self] status in
            guard let self, self.step == .voice, !self.checking, status?.voiceReady == true else { return }
            self.next()
        }
    }

    /// Screenshots only: stands in for a paired, signed-in setup without a server.
    var factsOverride: OnboardingFacts?
    var facts: OnboardingFacts { factsOverride ?? OnboardingFacts(paired: app.isPaired, voiceReady: app.status?.voiceReady) }
    var canGoBack: Bool { OnboardingPlan.previous(before: step, facts) != nil }
    /// Back, when there is a screen before this one the user can still change.
    var backAction: (() -> Void)? { canGoBack ? { [weak self] in self?.back() } : nil }

    func pairFromLink(_ link: PairingLink) {
        welcome.route = .setup
        UserDefaults.standard.set(true, forKey: WelcomeJourney.seenKey)
        server = link.server.absoluteString
        code = link.code
        step = .connect
        pair()
    }

    func openSetup() {
        welcome.route = .setup
        UserDefaults.standard.set(true, forKey: WelcomeJourney.seenKey)
        Task { await start() }
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
                await app.refreshHome()
                next()
            } catch {
                pairing = false
                pairError = app.describe(error)
            }
        }
    }

    /// Fills the ready screen from what the server already has (names, delivery target or the
    /// server's suggested home channel, continuity, home control). Anything typed is kept.
    func loadDefaults() {
        let s = app.settings
        if !namesLoaded {
            if assistantName.isEmpty { assistantName = s.assistantName ?? app.status?.assistantName ?? "" }
            if userName.isEmpty { userName = s.userName ?? "" }
            if target == nil { target = s.deliveryTarget ?? app.suggestedDestination }
            continuity = s.continuity?.enabled ?? true
            namesLoaded = app.status != nil
        }
        if !homeLoaded, let home = app.home, home.available {
            homeOn = UserDefaults.standard.bool(forKey: Prefs.onboardingDone) ? home.enabled : true
            homeLoaded = true
        }
    }

    /// The window opened: load the status, then show the first screen that still needs the user.
    func start() async {
        checking = app.isPaired
        await app.refresh()
        if app.isPaired { await app.refreshHome() }
        checking = false
        step = OnboardingPlan.first(facts)
        if step == .ready { loadDefaults() }
    }

    func next() {
        step = OnboardingPlan.next(after: step, facts) ?? .ready
        if step == .ready { loadDefaults() }
    }

    func back() {
        if let p = OnboardingPlan.previous(before: step, facts) { step = p }
    }

    /// Saves everything on the ready screen in one go: names, delivery and continuity
    /// (`POST /voice/onboarding`), home control when offered, and the Dock choice. Finishes
    /// (and optionally starts the first call) only when the save worked.
    func save(thenCall: Bool) {
        saving = true; saveError = nil
        Task {
            var ok = await app.completeOnboarding(assistantName: assistantName.trimmingCharacters(in: .whitespaces),
                                                  userName: userName.trimmingCharacters(in: .whitespaces),
                                                  target: target, continuity: continuity)
            if !ok {
                saveError = app.lastError ?? "Couldn't save"
            } else if let home = app.home, home.available, homeOn != home.enabled {
                let entities = home.includedIDs
                ok = await app.setHome(enabled: homeOn, entities: homeOn && !entities.isEmpty ? entities : nil)
                if !ok { saveError = "Couldn't save home control: \(app.homeError ?? "try again")" }
            }
            saving = false
            guard ok else { return }
            finish()  // marks the tour pending before the call reads it
            if thenCall { app.startCall() }
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
            if flow.welcome.route == .setup { Task { await flow.start() } }
            let hosting = NSHostingController(rootView: OnboardingView(flow: flow).environmentObject(flow.app))
            let w = NSWindow(contentViewController: hosting)
            w.title = "Welcome to Speakeasy"
            w.styleMask = [.titled, .closable, .resizable]
            w.setContentSize(OnboardingView.size)
            w.contentMinSize = OnboardingView.size
            w.contentMaxSize = NSSize(width: 800, height: 720)
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            window = w
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        // Closing early is fine: everything here is also in Settings, with the defaults the
        // server already has. Only a paired Mac counts as done.
        if flow.app.isPaired { onFinish() }
        window = nil
    }
}

struct OnboardingView: View {
    static let size = NSSize(width: 540, height: 480)
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel

    var body: some View {
        if flow.welcome.route != .setup {
            MacWelcomeView(journey: $flow.welcome, onConnect: flow.openSetup)
        } else {
            setup
        }
    }

    private var setup: some View {
        Group {
            if flow.checking {
                VStack(spacing: 10) {
                    ProgressView()
                    Text("Checking your setup…").foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                switch flow.step {
                case .connect: ConnectStep(flow: flow)
                case .voice: VoiceSignInStep(flow: flow)
                case .ready: ReadyStep(flow: flow)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
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
    var primary: String
    var primaryDisabled = false
    var busy = false
    var action: () -> Void
    var body: some View {
        HStack {
            if let back { Button("Back", action: back) }
            Spacer()
            if busy { ProgressView().controlSize(.small) }
            Button(primary, action: action)
                .keyboardShortcut(.defaultAction)
                .disabled(primaryDisabled || busy)
        }
        .padding(.top, 12)
    }
}

// MARK: Connect (only until paired)

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

// MARK: Voice sign-in (only while the voice isn't ready)

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
            StepButtons(back: flow.backAction, primary: "Continue", busy: app.refreshing, action: flow.next)
        }
        .task { await app.refresh() }
    }
}

// MARK: Ready: everything else on one screen, then the first call

// MARK: Who hears what (the layout `--privacy-shots` renders)

/// The privacy step's layout (also rendered by `--privacy-shots`).
struct PrivacyStepLayout: View {
    var copy: WhoHearsWhat
    var back: () -> Void
    var next: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(WhoHearsWhat.title).font(.title2.weight(.semibold)).padding(.bottom, 4)
            ScrollView { WhoHearsWhatBody(copy: copy).frame(maxWidth: .infinity, alignment: .leading) }
            Text(WhoHearsWhat.footnote).font(.callout).foregroundStyle(.secondary)
            StepButtons(back: back, primary: WhoHearsWhat.button, action: next)
        }
    }
}


private struct ReadyStep: View {
    @ObservedObject var flow: OnboardingFlow
    @EnvironmentObject var app: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            StepHeader(title: "You\u{2019}re ready to talk", subtitle: subtitle)
            Form {
                LabeledContent("Microphone") { MicrophoneRow() }
                TextField("Your name", text: $flow.userName, prompt: Text("Optional"))
                TextField("Assistant\u{2019}s name", text: $flow.assistantName, prompt: Text(VoiceState.defaultAssistantName))
                DeliveryPicker(target: $flow.target, destinations: app.destinations)
                if let home = app.home, home.available {
                    Toggle(isOn: $flow.homeOn) {
                        Text("Control your home by voice")
                        Text(homeDetail(home))
                    }
                }
                Toggle("Show in the Dock", isOn: $flow.showInDock)
                LabeledContent("Call shortcut") {
                    ShortcutRecorder(shortcut: app.callShortcut) { app.setCallShortcut($0) }
                }
            }
            .formStyle(.grouped)
            .scrollDisabled(true)
            .scrollContentBackground(.hidden)
            .padding(.horizontal, -20)
            if let problem = app.callShortcutProblem { Text(problem).font(.callout).foregroundStyle(.orange) }
            if let error = flow.saveError {
                Text(error).font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            Text("You can change any of this later in Settings. Your first call starts with a one-minute tour; say \u{201C}skip\u{201D} to jump straight in.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                if flow.canGoBack { Button("Back", action: flow.back) }
                Spacer()
                if flow.saving { ProgressView().controlSize(.small) }
                Button("Done") { flow.save(thenCall: false) }
                    .disabled(flow.saving)
                Button { flow.save(thenCall: true) } label: { Label("Start a call", systemImage: "waveform") }
                    .keyboardShortcut(.defaultAction)
                    .disabled(flow.saving)
            }
            .padding(.top, 10)
        }
        .onAppear { flow.loadDefaults() }
        .onChange(of: app.home) { _, _ in flow.loadDefaults() }
    }

    private var subtitle: String {
        let link = app.status.map { $0.reachability + ". " } ?? ""
        return link + "Here\u{2019}s how your calls are set up. Press the shortcut any time to talk."
    }

    private func homeDetail(_ home: HomeControlInfo) -> String {
        let n = home.includedIDs.count
        let devices = n == 0 ? "No devices picked yet" : "\(n) device\(n == 1 ? "" : "s")"
        return "\(devices) \u{00B7} choose which in Settings \u{203A} Home"
    }
}

/// The microphone line: on, or the one button that fixes it.
private struct MicrophoneRow: View {
    @EnvironmentObject var app: AppModel
    var body: some View {
        switch app.micAuthorization {
        case .authorized:
            Label("On", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
        case .denied, .restricted:
            Button("Open Privacy Settings") { AppModel.openMicrophonePrivacySettings() }
        default:
            Button("Allow") { Task { _ = await app.requestMicrophone() } }
        }
    }
}

// MARK: Delivery picker (also used in Settings)

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

