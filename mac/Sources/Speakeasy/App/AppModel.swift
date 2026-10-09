import AppKit
import AVFoundation
import Combine
import Foundation
import ServiceManagement
import SpeakeasyCore
import SpeakeasyClient

/// Client-side preferences (UserDefaults). Server-side settings live in `AppModel.settings`.
enum Prefs {
    static let serverURL = "serverURL"
    static let deviceName = "pairedDeviceName"
    static let deviceID = "pairedDeviceID"
    static let callShortcutKey = "callShortcut"
    static let muteShortcut = "muteShortcut"
    static let pauseShortcut = "pauseShortcut"
    static let startMuted = "startMuted"
    static let showPanelOnStart = "showPanelOnStart"
    static let followSystemAudio = "followSystemAudio"
    static let onboardingDone = "onboardingDone"
    static let startSlim = "startSlim"
    /// Beta: listening mode (off by default; Settings › Beta).
    static let listeningBeta = "betaListeningMode"
    static let showCaptions = "showCaptions"
    static let notifyWhenDone = "notifyWhenDone"
    static let panelOnAllSpaces = "panelOnAllSpaces"
    static let showInDock = "showInDock"
    static let checkForUpdates = "checkForUpdates"
    static let lastUpdateCheck = "lastUpdateCheck"
    /// The next new call starts with the first-call tour (set when onboarding finishes, or
    /// by Settings › General › Replay the tour; cleared once a call runs it).
    static let tourPending = "tourPending"
    /// The address the server last advertised for other devices (e.g. its tailnet URL).
    static let advertisedURL = "advertisedServerURL"
    /// Listening mode on/off shortcut (unset = none; empty = turned off). Stored like the others.
    static let listeningShortcut = "listeningShortcut"
    /// The one-time explainer was shown before listening mode was first turned on.
    static let listeningExplained = "listeningExplained"

    static func register() {
        UserDefaults.standard.register(defaults: [showPanelOnStart: true, followSystemAudio: true, startMuted: false, startSlim: false,
                                                 showCaptions: true, notifyWhenDone: true, panelOnAllSpaces: true,
                                                 showInDock: true])
    }

    /// The call shortcut (default ⌃⌥Space). Stored as e.g. `ctrl+opt+space`.
    static var callShortcut: KeyShortcut {
        guard let raw = UserDefaults.standard.string(forKey: callShortcutKey),
              case .success(let s) = KeyShortcut.parse(raw, reserved: nil) else { return .call }
        return s
    }
}

/// App-wide state shared by the menu bar, onboarding and Settings.
@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published private(set) var config: AppConfig
    @Published var status: ServerStatus?
    /// An unsaved routing-model change from Settings; nil when nothing is pending. Saved and
    /// reverted by the same Save/Revert bar as the other server settings.
    @Published var routingDraft: RoutingChoices.Current?

    /// The routing edit, when there is one that differs from what the server uses.
    var routingEdit: RoutingChoices.Current? {
        guard let draft = routingDraft, let current = status?.routingChoice?.current else { return nil }
        if draft.isDefault && current.isDefault { return nil }
        return draft == current ? nil : draft
    }
    /// A provider was picked but not yet a model: Save waits for one.
    var routingNeedsModel: Bool { routingEdit.map { !$0.isDefault && $0.model.isEmpty } ?? false }

    func saveRoutingEdit() async -> Bool {
        guard let edit = routingEdit else { return true }
        let ok = await chooseRouting(provider: edit.isDefault ? RoutingChoices.defaultID : edit.provider,
                                     model: edit.isDefault ? "" : edit.model,
                                     thinking: edit.isDefault ? nil : edit.thinking)
        if ok { routingDraft = nil }
        return ok
    }
    @Published var settings = ServerSettings()
    @Published var brief: VoiceBrief?
    @Published var tune: BriefTune?
    @Published var destinations: [Destination] = []
    /// The server's preselected delivery target for onboarding (a connected home channel).
    @Published var suggestedDestination: String?
    @Published var onboarding: OnboardingStatus?
    /// Instant home control (`/voice/home`); nil until loaded or on an older server.
    @Published var home: HomeControlInfo?
    @Published var homeError: String?
    @Published var lastError: String?
    @Published var refreshing = false
    @Published var callShortcut: KeyShortcut = Prefs.callShortcut
    @Published var callShortcutProblem: String?
    @Published var micAuthorization: AVAuthorizationStatus = AVCaptureDevice.authorizationStatus(for: .audio)

    /// The delegate re-wires the call client and hotkeys when these change.
    let configChanged = PassthroughSubject<AppConfig, Never>()
    let settingsChanged = PassthroughSubject<ServerSettings, Never>()
    let shortcutChanged = PassthroughSubject<KeyShortcut, Never>()
    /// Mute / pause shortcuts changed in Settings (stored in UserDefaults; empty string = off).
    let extraShortcutsChanged = PassthroughSubject<Void, Never>()
    /// Why the mute / pause shortcut isn't active (set by the delegate), shown in Settings.
    @Published var muteShortcutProblem: String?
    @Published var pauseShortcutProblem: String?
    @Published var listeningShortcutProblem: String?
    /// Onboarding "Try it" and Settings ask the delegate to start a call.
    var startCall: () -> Void = {}

    private init() {
        Prefs.register()
        config = AppConfig.resolve(savedServer: UserDefaults.standard.string(forKey: Prefs.serverURL),
                                   savedToken: Self.readSavedToken())
    }

    /// Automated runs (smoke tests, snapshots, previews) never touch the Keychain. Dev builds are
    /// ad-hoc signed, so every rebuild is a "new app" to macOS and "Always Allow" never sticks:
    /// the read blocks on a prompt no one is there to click.
    static let isAutomatedRun: Bool = {
        if ProcessInfo.processInfo.environment["SPEAKEASY_NO_KEYCHAIN"] == "1" { return true }
        let flags: Set<String> = ["--onboarding-snapshot", "--ui-preview", "--snapshot", "--film-frames"]
        return CommandLine.arguments.contains { $0.hasSuffix("-smoke") || flags.contains($0) }
    }()

    private static func readSavedToken() -> String? {
        isAutomatedRun ? nil : Keychain.readToken()
    }

    var api: ServerClient? { ServerClient(config: config) }
    var isPaired: Bool { config.isPaired }
    var deviceName: String? { UserDefaults.standard.string(forKey: Prefs.deviceName) }
    var assistantName: String { status?.assistantName.flatMap { $0.isEmpty ? nil : $0 } ?? settings.resolvedAssistantName }
    var appVersion: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev" }

    // MARK: Updates
    @Published var updateOutcome: UpdateCheck.Outcome?
    @Published private(set) var latestRelease: UpdateCheck.Release?
    @Published private(set) var latestPluginVersion: String?
    @Published var checkingForUpdates = false
    @Published var updateError: String?

    /// Ask GitHub for the latest release. `quiet` = the weekly background check: no error shown.
    func checkForUpdates(quiet: Bool = false) async {
        guard !checkingForUpdates else { return }
        checkingForUpdates = true; if !quiet { updateError = nil }
        defer { checkingForUpdates = false }
        var request = URLRequest(url: UpdateCheck.latestURL, timeoutInterval: 15)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("Speakeasy/\(appVersion)", forHTTPHeaderField: "User-Agent")
        if isPaired { await refresh() }
        var appCheckOK = false
        var pluginCheckOK = false
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 404 { latestRelease = nil; updateOutcome = .noReleases }
            else {
                guard status == 200 else { throw URLError(.badServerResponse) }
                guard let release = UpdateCheck.parse(data) else { throw URLError(.cannotParseResponse) }
                latestRelease = release
                updateOutcome = UpdateCheck.outcome(current: appVersion, latest: release)
            }
            appCheckOK = true
        } catch {
            if !quiet { updateError = "Couldn't check Mac app updates. Try again later." }
        }
        do {
            var pluginRequest = URLRequest(url: UpdateCheck.pluginManifestURL, timeoutInterval: 15)
            pluginRequest.setValue("Speakeasy/\(appVersion)", forHTTPHeaderField: "User-Agent")
            let (pluginData, pluginResponse) = try await URLSession.shared.data(for: pluginRequest)
            guard (pluginResponse as? HTTPURLResponse)?.statusCode == 200,
                  let version = UpdateCheck.parsePluginVersion(pluginData) else { throw URLError(.cannotParseResponse) }
            latestPluginVersion = version
            pluginCheckOK = true
        } catch {
            if !quiet { updateError = "Couldn't check plugin updates. Try again later." }
        }
        if appCheckOK && pluginCheckOK { UserDefaults.standard.set(Date(), forKey: Prefs.lastUpdateCheck) }
    }

    /// Weekly, only when the user leaves automatic checks on.
    func checkForUpdatesIfDue() {
        let defaults = UserDefaults.standard
        guard defaults.object(forKey: Prefs.checkForUpdates) as? Bool ?? true else { return }
        let last = defaults.object(forKey: Prefs.lastUpdateCheck) as? Date ?? .distantPast
        guard Date().timeIntervalSince(last) > 7 * 24 * 3600 else { return }
        Task { await checkForUpdates(quiet: true) }
    }

    var updateAvailable: UpdateCheck.Release? {
        if case .available(let release)? = updateOutcome { return release }
        return nil
    }

    /// Compare the running Hermes plugin independently of the Mac app version.
    var pluginUpdateAvailable: String? {
        guard isPaired else { return nil }
        return UpdateCheck.pluginUpdate(latestVersion: latestPluginVersion, runningVersion: status?.version)
    }

    func openUpdate(_ release: UpdateCheck.Release) {
        NSWorkspace.shared.open(release.downloadURL ?? release.pageURL)
    }

    func copyPluginUpdateRequest() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("Update the Speakeasy plugin on my Hermes installation. Check that it is enabled afterward, and tell me when I need to restart Hermes. Do not restart it yourself.", forType: .string)
    }

    // MARK: Pairing

    nonisolated static var defaultDeviceName: String { Host.current().localizedName ?? "Mac" }

    func pair(server: URL, code: String, deviceName: String? = nil) async throws {
        let deviceName = deviceName ?? AppModel.defaultDeviceName
        guard let base = trustedServerBaseURL(server.absoluteString) else {
            throw ServerClient.HTTPError(status: 0, message: "That server address isn't allowed. Use this Mac (http://127.0.0.1:8795) or an https Tailscale address (*.ts.net).")
        }
        guard let code = normalizedPairingCode(code) else {
            throw ServerClient.HTTPError(status: 0, message: "Enter the 6-digit code from hermes voice pair.")
        }
        let response = try await ServerClient.pair(server: base, code: code, deviceName: deviceName)
        guard Keychain.saveToken(response.token) else {
            throw ServerClient.HTTPError(status: 0, message: "Couldn't save the device token to the Keychain.")
        }
        UserDefaults.standard.set(base.absoluteString, forKey: Prefs.serverURL)
        UserDefaults.standard.set(deviceName, forKey: Prefs.deviceName)
        UserDefaults.standard.set(response.deviceID, forKey: Prefs.deviceID)
        setConfig(AppConfig.resolve(savedServer: base.absoluteString, savedToken: response.token))
        lastError = nil
        await refresh()
    }

    func unpair() {
        Keychain.deleteToken()
        UserDefaults.standard.removeObject(forKey: Prefs.deviceName)
        UserDefaults.standard.removeObject(forKey: Prefs.deviceID)
        setConfig(AppConfig(serverURL: config.serverURL, deviceToken: nil))
        status = nil; brief = nil; destinations = []; onboarding = nil
    }

    private func setConfig(_ new: AppConfig) {
        config = new
        configChanged.send(new)
    }

    // MARK: Server state

    func refresh() async {
        guard let api else { status = nil; return }
        refreshing = true
        defer { refreshing = false }
        do {
            let fresh = try await api.status()
            status = fresh
            lastError = nil
            tailnetOffer = nil
            if let url = fresh.advertisedURL, !url.isEmpty { UserDefaults.standard.set(url, forKey: Prefs.advertisedURL) }
        } catch {
            status = nil
            lastError = describe(error)
            if (error as NSError).domain == NSURLErrorDomain {
                checkTailnetOffer(advertised: UserDefaults.standard.string(forKey: Prefs.advertisedURL))
            }
        }
        if let s = try? await api.settings() { applySettings(s) }
        brief = try? await api.brief()
        if let (list, suggested) = try? await api.destinationsWithSuggestion() {
            destinations = list; suggestedDestination = suggested
        } else {
            destinations = []; suggestedDestination = nil
        }
        onboarding = try? await api.onboarding()
        micAuthorization = AVCaptureDevice.authorizationStatus(for: .audio)
    }

    /// Loads home control: whether Home Assistant is found, on/off, every device it could use.
    func refreshHome() async {
        guard let api else { return }
        do { home = try await api.homeControl(); homeError = nil }
        catch { homeError = describe(error) }
    }

    /// Turns home control on/off and/or saves exactly which devices it may use.
    @discardableResult
    func setHome(enabled: Bool? = nil, entities: [String]? = nil) async -> Bool {
        guard let api else { homeError = "Not connected"; return false }
        do {
            home = try await api.setHomeControl(enabled: enabled, entities: entities)
            homeError = nil
            if let s = try? await api.settings() { applySettings(s) }
            return true
        } catch {
            homeError = describe(error)
            return false
        }
    }

    func refreshBrief() async {
        guard let api else { return }
        if let b = try? await api.brief() { brief = b }
    }

    func save(_ new: ServerSettings) async -> Bool {
        guard let api else { lastError = "Not connected"; return false }
        do {
            applySettings(try await api.saveSettings(new))
            if let s = try? await api.status() { status = s }
            lastError = nil
            return true
        } catch {
            lastError = "Couldn't save: \(describe(error))"
            return false
        }
    }

    private func applySettings(_ s: ServerSettings) {
        settings = s
        settingsChanged.send(s)
    }

    func saveBrief(_ text: String) async -> Bool {
        guard let api else { return false }
        do {
            if let b = try await api.saveBrief(text) { brief = b } else { await refreshBrief() }
            return true
        } catch { lastError = "Couldn't save the brief: \(describe(error))"; return false }
    }

    func refreshTune() async {
        guard let api else { return }
        tune = try? await api.tune()
    }

    func startTune() async {
        guard let api else { return }
        do { tune = try await api.startTune() }
        catch let error as ServerClient.HTTPError where error.status == 409 {
            lastError = error.message.isEmpty ? "Can't tune yet" : error.message
        } catch { lastError = "Couldn't start tuning: \(describe(error))" }
    }

    func applyTune(_ accept: [String]) async {
        guard let api else { return }
        do {
            if let b = try await api.applyTune(accept: accept) { brief = b } else { await refreshBrief() }
            await refreshBrief()
            tune = try? await api.tune()
        } catch { lastError = "Couldn't apply the edits: \(describe(error))" }
    }

    func dismissTune() async {
        guard let api else { return }
        try? await api.dismissTune()
        tune = try? await api.tune()
    }

    func rewriteBrief() async {
        guard let api else { return }
        do {
            if let b = try await api.rewriteBrief() { brief = b } else { await refreshBrief() }
        } catch { lastError = "Couldn't start a rewrite: \(describe(error))" }
    }

    /// `POST /voice/onboarding`; falls back to `PATCH /voice/settings` on servers without it.
    func completeOnboarding(assistantName: String, userName: String, target: String?, continuity: Bool) async -> Bool {
        guard let api else { return false }
        do {
            try await api.completeOnboarding(assistantName: assistantName, userName: userName, target: target,
                                             continuity: continuity)
            if let s = try? await api.settings() { applySettings(s) }
            if let s = try? await api.status() { status = s }
            return true
        } catch let error as ServerClient.HTTPError where error.status == 404 || error.status == 405 || error.status == 400 {
            // Older servers: no onboarding route, or no continuity_enabled field. Save as settings.
            var s = settings
            s.assistantName = assistantName.isEmpty ? nil : assistantName
            s.userName = userName.isEmpty ? nil : userName
            var d = s.delivery ?? .init(); d.target = target; s.delivery = d
            s.continuity = .init(enabled: continuity)
            return await save(s)
        } catch {
            lastError = describe(error)
            return false
        }
    }

    /// Switch the model that routes voice requests. Takes effect on the next request.
    func chooseRouting(provider: String, model: String, thinking: Bool?) async -> Bool {
        guard let api else { lastError = "Not connected"; return false }
        do {
            let choices = try await api.chooseRouting(provider: provider, model: model, thinking: thinking)
            if var s = status {
                s.routingChoice = choices
                s.routingModel = choices.label
                status = s
            }
            if let s = try? await api.status() { status = s }
            lastError = nil
            return true
        } catch {
            lastError = "Couldn't switch the routing model: \(describe(error))"
            return false
        }
    }

    /// Every model Hermes lists for one provider (for the routing model picker).
    func routingModels(_ provider: String) async -> [String] {
        guard let api else { return [] }
        return (try? await api.routingModels(provider)) ?? []
    }

    /// Ask the user's Hermes to propose delivery channels. Returns suggestions, or sets `lastError`.
    func suggestChannels() async -> [ServerSettings.Channel]? {
        guard let api else { lastError = "Not connected"; return nil }
        do {
            let list = try await api.suggestChannels()
            lastError = nil
            return list
        } catch {
            lastError = describe(error)
            return nil
        }
    }

    /// The paired server is this Mac's loopback but doesn't answer, while setup advertised a tailnet
    /// address: the Mac is probably another computer. The app offers to switch.
    @Published var tailnetOffer: URL?

    func checkTailnetOffer(advertised: String?) {
        guard let current = config.serverURL, isLoopback(current),
              let text = advertised, let url = trustedServerBaseURL(text), !isLoopback(url) else { tailnetOffer = nil; return }
        tailnetOffer = url
    }

    /// Point the app at the advertised address (same device token: it's the same server).
    func switchServer(to url: URL) async {
        UserDefaults.standard.set(url.absoluteString, forKey: Prefs.serverURL)
        setConfig(AppConfig.resolve(savedServer: url.absoluteString, savedToken: Self.readSavedToken()))
        tailnetOffer = nil
        await refresh()
    }

    private func isLoopback(_ url: URL) -> Bool {
        ["127.0.0.1", "localhost", "::1"].contains((url.host ?? "").lowercased())
    }

    // MARK: Client preferences

    func setCallShortcut(_ shortcut: KeyShortcut) {
        UserDefaults.standard.set(shortcut.storage, forKey: Prefs.callShortcutKey)
        callShortcut = shortcut
        shortcutChanged.send(shortcut)
        extraShortcutsChanged.send()  // mute / pause must not collide with the new call shortcut
    }

    /// nil = the default; `.some(nil)` = turned off.
    static func storedShortcut(_ key: String, default fallback: KeyShortcut) -> KeyShortcut? {
        guard let raw = UserDefaults.standard.string(forKey: key) else { return fallback }
        if raw.trimmingCharacters(in: .whitespaces).isEmpty { return nil }
        if case .success(let s) = KeyShortcut.parse(raw, reserved: nil) { return s }
        return fallback
    }

    func setExtraShortcut(_ key: String, _ shortcut: KeyShortcut?) {
        UserDefaults.standard.set(shortcut?.storage ?? "", forKey: key)
        objectWillChange.send()
        extraShortcutsChanged.send()
    }

    func resetShortcuts() {
        for key in [Prefs.muteShortcut, Prefs.pauseShortcut, Prefs.listeningShortcut] { UserDefaults.standard.removeObject(forKey: key) }
        setCallShortcut(.call)
        objectWillChange.send()
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                lastError = nil
            } catch {
                lastError = "Launch at login: \(error.localizedDescription)"
            }
            objectWillChange.send()
        }
    }

    func requestMicrophone() async -> Bool {
        let granted = await AVCaptureDevice.requestAccess(for: .audio)
        micAuthorization = AVCaptureDevice.authorizationStatus(for: .audio)
        return granted
    }

    static func openMicrophonePrivacySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
    }

    func describe(_ error: Error) -> String {
        if let http = error as? ServerClient.HTTPError {
            if http.status == 401 || http.status == 403 { return "This Mac isn't paired anymore. Pair again in Settings › Connection." }
            if http.status == 404 {
                return "Your Hermes has an older Speakeasy plugin that can't do this yet. Ask your agent to update Speakeasy, then restart Hermes."
            }
            return http.message
        }
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            if let host = config.serverURL?.host, host.hasSuffix(".ts.net") {
                return "Can't reach your Hermes at \(host). It's on your Tailscale network: make sure Tailscale is installed and signed in on this Mac, with the same account as the computer running Hermes (tailscale.com/download)."
            }
            return "Can't reach the Speakeasy server at \(config.serverURL?.absoluteString ?? "?"). Is the Hermes gateway running?"
        }
        return error.localizedDescription
    }
}
