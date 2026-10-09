import AppKit
import AVFoundation
import Combine
import SpeakeasyCore
import SpeakeasyClient
import SwiftUI

@main
struct SpeakeasyApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Settings {
            SettingsView()
                .environmentObject(AppModel.shared)
        }
    }
}

/// Menu bar item, global hotkeys, the call client, pairing links and onboarding.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let app = AppModel.shared
    private var statusItem: NSStatusItem!
    private var updateItem: NSMenuItem?
    private var hotKey: GlobalHotKey?
    /// In-call mute shortcut: registered only while a call is open.
    private var muteHotKey: GlobalHotKey?
    private var muteGesture = MuteKeyGesture()
    private var muteShortcut: KeyShortcut?
    private var muteShortcutProblem: String?
    /// Pause/Resume shortcut: registered while a call is open or paused.
    private var pauseHotKey: GlobalHotKey?
    private var pauseShortcut: KeyShortcut?
    private var pauseShortcutProblem: String?
    private var callItem: NSMenuItem?
    private var muteItem: NSMenuItem?
    private var pauseItem: NSMenuItem?
    private var statusLine: NSMenuItem?
    private(set) var native: NativeVoiceClient!
    /// Listening mode: hears the room without answering; turning it off starts a call that knows.
    private var room: RoomListeningController!
    private var listenItem: NSMenuItem?
    private var discardListenItem: NSMenuItem?
    /// Listening mode on/off shortcut: registered whenever one is set (off until the user sets it).
    private var listenHotKey: GlobalHotKey?
    private var listenShortcut: KeyShortcut?
    /// Listening mode is a beta: hidden everywhere until it's turned on in Settings › Beta.
    private var listeningBeta: Bool { UserDefaults.standard.bool(forKey: Prefs.listeningBeta) }
    private var lastListeningBeta: Bool?
    private var badgeOn = false
    private var active = false
    private var quitPending = false
    private var idle: IdleContinuity?
    private var onboarding: OnboardingWindowController?
    private var bag = Set<AnyCancellable>()
    /// A pairing link that arrived before launch finished.
    private var pendingLink: URL?

    func applicationWillFinishLaunching(_ notification: Notification) {
        // speakeasy://pair links (Info.plist CFBundleURLTypes). An Apple Event handler works
        // whether or not SwiftUI has a scene that could claim the URL.
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(handleURLEvent(_:reply:)),
                                                     forEventClass: AEEventClass(kInternetEventClass),
                                                     andEventID: AEEventID(kAEGetURL))
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        applyDockPolicy()
        let args = ProcessInfo.processInfo.arguments
        if args.contains("--native-offer-smoke") { NativeSmoke.offer(); return }
        if args.contains("--native-mic-smoke") { NativeSmoke.mic(); return }
        if args.contains("--live-call-smoke") { LiveCallSmoke.run(); return }
        if let i = args.firstIndex(of: "--room-smoke"), i + 1 < args.count { RoomSmoke.run(path: args[i + 1]); return }
        if let i = args.firstIndex(of: "--onboarding-snapshot"), i + 1 < args.count {
            OnboardingSnapshot.render(to: args[i + 1], step: i + 2 < args.count && !args[i + 2].hasPrefix("--") ? args[i + 2] : nil); return
        }
        if args.contains("--panel-smoke") {
            let dir = args.firstIndex(of: "--snapshot-dir").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            PanelSmoke.run(config: app.config, snapshotDir: dir); return
        }
        if let i = args.firstIndex(of: "--film-frames"), i + 1 < args.count {
            FilmFrames.run(config: AppConfig(serverURL: nil, deviceToken: nil), dir: args[i + 1]); return
        }
        if let i = args.firstIndex(of: "--cards-smoke"), i + 2 < args.count {
            CardsSmoke.run(json: args[i + 1], dir: args[i + 2]); return
        }
        if let index = args.firstIndex(of: "--ui-preview") { runPreview(args, index: index); return }
        if let i = args.firstIndex(of: "--update-smoke"), i + 1 < args.count {
            AppUpdater.shared.runSmoke(feed: args[i + 1]); return
        }
        if let i = args.firstIndex(of: "--home-smoke"), i + 1 < args.count {
            HomeSmoke.run(app: app, dir: args[i + 1]); return
        }
        if let i = args.firstIndex(of: "--settings-smoke"), i + 3 < args.count {
            SettingsSmoke.run(app: app, dir: args[i + 1], provider: args[i + 2], model: args[i + 3]); return
        }

        native = NativeVoiceClient(config: app.config)
        room = RoomListeningController()
        applyClientPrefs()
        resolveMuteShortcut()
        resolvePauseShortcut()
        configureClient()
        configureListening()
        configureStatusItem()
        configureIdle()
        registerCallHotKey(app.callShortcut)
        observeModel()

        if let link = pendingLink { pendingLink = nil; handlePairURL(link) }
        else if !app.isPaired || !UserDefaults.standard.bool(forKey: Prefs.onboardingDone) { showOnboarding() }
        Task { await app.refresh() }
        AppUpdater.shared.start()
        app.checkForUpdatesIfDue()
        Timer.publish(every: 24 * 3600, on: .main, in: .common).autoconnect()
            .sink { [weak self] _ in self?.app.checkForUpdatesIfDue() }
            .store(in: &bag)
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(400)) { [weak self] in
            guard let self, !self.active else { return }
            self.native.hideIfIdle()
        }
    }

    /// Clicking the Dock icon (or opening the app again from Finder) brings back what's unfinished:
    /// setup if it isn't done, otherwise Settings. Nothing gets lost behind other windows.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if onboarding != nil || !app.isPaired || !UserDefaults.standard.bool(forKey: Prefs.onboardingDone) {
            showOnboarding()
        } else if !flag {
            openSettings()
        }
        return false
    }

    /// Dock icon on by default; Settings › General › Show in Dock turns Speakeasy into a
    /// menu-bar-only app.
    private func applyDockPolicy() {
        let policy: NSApplication.ActivationPolicy = UserDefaults.standard.bool(forKey: Prefs.showInDock) ? .regular : .accessory
        if NSApp.activationPolicy() != policy { NSApp.setActivationPolicy(policy) }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if active { native.end() }
        room?.discard()   // what listening mode heard lives only in memory; quitting drops it
    }

    // MARK: Model wiring

    private func observeModel() {
        app.$status.combineLatest(app.$latestPluginVersion)
            .receive(on: RunLoop.main)
            .sink { [weak self] _, _ in
                guard let self else { return }
                self.updateMenu()
                self.refreshListening()   // the plugin's room_listening support may have changed
                if let version = self.app.pluginUpdateAvailable {
                    self.idle?.notifyPluginUpdate(version: version)
                }
            }.store(in: &bag)
        app.configChanged.sink { [weak self] config in
            guard let self else { return }
            if self.active { self.native.end() }
            self.native.update(config: config)
            self.configureIdle()
            self.updateMenu()
        }.store(in: &bag)
        app.settingsChanged.sink { [weak self] settings in
            self?.native.apply(settings: settings)
        }.store(in: &bag)
        app.shortcutChanged.sink { [weak self] shortcut in
            self?.registerCallHotKey(shortcut)
        }.store(in: &bag)
        app.extraShortcutsChanged.sink { [weak self] in
            guard let self else { return }
            self.resolveMuteShortcut()
            self.resolvePauseShortcut()
            self.resolveListenShortcut()
            if self.active {  // swap live hotkeys without ending the call
                self.unregisterMuteHotKey(); self.pauseHotKey = nil
                if !self.native.isPaused { self.registerMuteHotKey() }
                self.registerPauseHotKey()
            }
            self.updateMenu()
        }.store(in: &bag)
        app.startCall = { [weak self] in
            guard let self, !self.active else { return }
            self.startConversation()
        }
        NotificationCenter.default.publisher(for: UserDefaults.didChangeNotification)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.applyClientPrefs(); self?.applyDockPolicy(); self?.applyListeningBeta() }
            .store(in: &bag)
    }

    private func applyClientPrefs() {
        let defaults = UserDefaults.standard
        native?.startMuted = defaults.bool(forKey: Prefs.startMuted)
        native?.showPanelOnStart = defaults.bool(forKey: Prefs.showPanelOnStart)
        native?.followSystemAudio = defaults.bool(forKey: Prefs.followSystemAudio)
        native?.startSlim = defaults.bool(forKey: Prefs.startSlim)
        native?.model.showCaptions = defaults.bool(forKey: Prefs.showCaptions)
        native?.panelOnAllSpaces = defaults.bool(forKey: Prefs.panelOnAllSpaces)
        idle?.notifyWhenDone = defaults.bool(forKey: Prefs.notifyWhenDone)
    }

    private func configureIdle() {
        let continuity = IdleContinuity(config: app.config)
        continuity.onOpenWork = { [weak self] in self?.showRecentWork() }
        continuity.onOpenSettings = { [weak self] in self?.openSettings() }
        continuity.onResume = { [weak self] in
            guard let self, self.native.isPaused else { self?.showRecentWork(); return }
            self.togglePause()
        }
        continuity.onBadge = { [weak self] on in self?.setBadge(on) }
        continuity.notifyWhenDone = UserDefaults.standard.bool(forKey: Prefs.notifyWhenDone)
        idle = continuity
    }

    private func setBadge(_ on: Bool) {
        badgeOn = on
        statusItem?.button?.image = BrandGlyph.menuBarImage(badge: on, listening: room?.isOn == true)
    }

    // MARK: Pairing links

    @objc private func handleURLEvent(_ event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let raw = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue, let url = URL(string: raw) else { return }
        if native == nil { pendingLink = url; return }
        handlePairURL(url)
    }

    private func handlePairURL(_ url: URL) {
        switch PairingLink.parse(url) {
        case .success(let link):
            showOnboarding()
            onboarding?.flow.pairFromLink(link)
        case .failure(let error):
            let alert = NSAlert()
            alert.messageText = "Couldn't use that pairing link"
            alert.informativeText = error.userMessage
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }

    // MARK: Onboarding & settings

    func showOnboarding() {
        if onboarding == nil {
            let controller = OnboardingWindowController(app: app)
            controller.onFinish = { [weak self] in
                if !UserDefaults.standard.bool(forKey: Prefs.onboardingDone) {
                    UserDefaults.standard.set(true, forKey: Prefs.tourPending)
                }
                UserDefaults.standard.set(true, forKey: Prefs.onboardingDone)
                self?.onboarding = nil
            }
            onboarding = controller
        }
        onboarding?.show()
    }

    @objc func openSettings() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
    }

    // MARK: Menu bar

    private func configureStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = BrandGlyph.menuBarImage()
        statusItem.button?.toolTip = "Speakeasy"
        let menu = NSMenu()
        let status = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status); statusLine = status
        menu.addItem(.separator())
        let call = NSMenuItem(title: "Start call", action: #selector(toggleConversation), keyEquivalent: "")
        call.target = self
        menu.addItem(call); callItem = call
        let listen = NSMenuItem(title: "Turn on listening mode", action: #selector(toggleListeningFromMenu), keyEquivalent: "")
        listen.target = self
        menu.addItem(listen); listenItem = listen
        let discardListen = NSMenuItem(title: "Discard what listening mode heard", action: #selector(discardListeningFromMenu),
                                       keyEquivalent: "")
        discardListen.target = self
        menu.addItem(discardListen); discardListenItem = discardListen
        let pause = NSMenuItem(title: "Pause call", action: #selector(togglePauseFromMenu), keyEquivalent: "")
        pause.target = self
        menu.addItem(pause); pauseItem = pause
        let mute = NSMenuItem(title: "Mute microphone", action: #selector(toggleMuteFromMenu), keyEquivalent: "")
        mute.target = self
        menu.addItem(mute); muteItem = mute
        let work = NSMenuItem(title: "Recent work", action: #selector(showRecentWork), keyEquivalent: "")
        work.target = self
        menu.addItem(work)
        let reset = NSMenuItem(title: "Reset panel position and size", action: #selector(resetPanelPosition), keyEquivalent: "")
        reset.target = self
        menu.addItem(reset)
        menu.addItem(.separator())
        let settings = NSMenuItem(title: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)
        let updates = NSMenuItem(title: "Check for Updates…", action: #selector(checkForUpdatesFromMenu), keyEquivalent: "")
        updates.target = self
        menu.addItem(updates); updateItem = updates
        let quit = NSMenuItem(title: "Quit Speakeasy", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu
        updateMenu()
    }

    func menuWillOpen(_ menu: NSMenu) { updateMenu() }

    /// Menu: check now and say the result in an alert (download offered when newer).
    @objc private func checkForUpdatesFromMenu() {
        Task { @MainActor in
            await app.checkForUpdates()
            let alert = NSAlert()
            alert.icon = NSApp.applicationIconImage
            if let version = app.pluginUpdateAvailable {
                alert.messageText = "Hermes plugin update available"
                alert.informativeText = "Speakeasy on Hermes is \(app.status?.version ?? "unknown"); \(version) is available. Ask your agent to update the plugin, then restart Hermes yourself when prompted. The Mac app updates separately."
                alert.addButton(withTitle: "Copy update request")
                alert.addButton(withTitle: "Later")
                NSApp.activate(ignoringOtherApps: true)
                if alert.runModal() == .alertFirstButtonReturn { app.copyPluginUpdateRequest() }
            } else if AppUpdater.shared.isAvailable {
                AppUpdater.shared.checkForUpdates()
            } else if let release = app.updateAvailable {
                alert.messageText = "Speakeasy \(release.version) is available"
                alert.informativeText = "You have \(app.appVersion). Download the new version, then drag it into Applications to replace this one."
                alert.addButton(withTitle: "Download")
                alert.addButton(withTitle: "Later")
                NSApp.activate(ignoringOtherApps: true)
                if alert.runModal() == .alertFirstButtonReturn { app.openUpdate(release) }
            } else {
                switch app.updateOutcome {
                case .upToDate?: alert.messageText = "You're up to date"
                    alert.informativeText = "Speakeasy \(app.appVersion) is the latest version."
                case .noReleases?: alert.messageText = "No releases yet"
                    alert.informativeText = "There's no published Speakeasy release to compare against yet."
                default: alert.messageText = "Couldn't check for updates"
                    alert.informativeText = app.updateError ?? "Try again later."
                }
                NSApp.activate(ignoringOtherApps: true)
                alert.runModal()
            }
            updateMenu()
        }
    }

    private func updateMenu() {
        if let version = app.pluginUpdateAvailable { updateItem?.title = "Hermes plugin update: \(version)…" }
        else if let release = app.updateAvailable { updateItem?.title = "Update available: \(release.version)…" }
        else { updateItem?.title = "Check for Updates…" }
        let name = app.assistantName
        if !app.isPaired {
            statusLine?.title = "Not connected — open Settings to pair"
        } else if let fix = app.status?.checks.first(where: { !$0.ok && $0.id != "brief" })?.fix {
            statusLine?.title = fix
        } else if app.status == nil {
            statusLine?.title = app.lastError ?? "Connecting to \(name)…"
        } else {
            statusLine?.title = "\(name) is ready"
        }
        callItem?.title = (active ? "End call" : "Start call") + " (\(app.callShortcut.display))"
        callItem?.isEnabled = app.isPaired
        if let room, let listenItem {
            let shortcut = listenShortcut.map { " (\($0.display))" } ?? ""
            if room.isOn {
                statusLine?.title = room.presentation().map { "\($0.title) · \($0.detail)" } ?? statusLine?.title ?? ""
                callItem?.title = "Turn off listening mode and ask (\(app.callShortcut.display))"
                listenItem.title = "Turn off listening mode and ask" + shortcut
            } else if room.canAsk {
                callItem?.title = "Ask about what listening mode heard (\(app.callShortcut.display))"
                listenItem.title = "Ask about what listening mode heard" + shortcut
            } else {
                listenItem.title = "Turn on listening mode" + shortcut
            }
            listenItem.isHidden = !listeningBeta || !room.isSupported || !(room.callActive() || room.canAsk)   // during a call only
            listenItem.isEnabled = app.isPaired && (room.canAsk || room.blocker == nil)
            listenItem.toolTip = room.canAsk ? nil : room.blocker
            discardListenItem?.isHidden = !room.canAsk
        }
        if let pauseItem {
            let shortcut = pauseShortcut.map { " (\($0.display))" } ?? ""
            pauseItem.title = (native.isPaused ? "Resume call" : "Pause call") + shortcut
            pauseItem.isEnabled = active && (native.isPaused || native.micControllable)
            pauseItem.toolTip = pauseShortcutProblem
        }
        if let muteItem {
            let shortcut = muteShortcut.map { " (\($0.display); hold to talk)" } ?? ""
            muteItem.title = (native.micMuted ? "Unmute microphone" : "Mute microphone") + shortcut
            muteItem.isEnabled = active && native.micControllable
            muteItem.toolTip = muteShortcutProblem
        }
    }

    @objc private func resetPanelPosition() { native.panel.resetPlacement() }

    @objc private func quit() {
        if active { quitPending = true; endConversation() } else { NSApp.terminate(nil) }
    }

    @objc private func showRecentWork() {
        setBadge(false)
        native.showWork(active: active)
    }

    // MARK: Call

    /// Menu Start/End: explicit, never hides-only.
    @objc func toggleConversation() {
        if active { endConversation() } else { startConversation() }
    }

    /// The global hotkey: start when idle and hidden, hide when idle and shown, end during a call.
    /// While listening mode is on it turns listening off into the call, panel shown or not.
    private func hotkeyPressed() {
        if room.canAsk { startConversation(); return }
        let connection = native.model.state.connection
        switch hotkeyAction(connection: active ? connection : (connection.isOpen ? connection : .idle),
                            panelVisible: native.panelVisible) {
        case .startCall: startConversation()
        case .endCall: endConversation()
        case .hidePanel: native.closePanel()
        case .showPanel: native.showPanel()
        }
    }

    /// Every way of starting a call (menu, panel Start, hotkey, Settings) comes through here, so
    /// while listening mode is on each of them turns it off into a call that knows what was said.
    private func startConversation() {
        guard app.isPaired else { showOnboarding(); return }
        if room.canAsk { room.takeOff(); return }   // continues in beginCall(room:takeoffAt:)
        beginCall(room: nil, takeoffAt: nil)
    }

    private func beginCall(room text: String?, takeoffAt: Date?) {
        if native.isPaused, let text, let takeoffAt {
            // Listening mode was turned on mid-conversation: back to that conversation, with what was heard.
            native.start(room: text, takeoffAt: takeoffAt)
            updateMenu()
            refreshListening()
            return
        }
        active = true
        idle?.callStarted()
        native.pendingTour = UserDefaults.standard.bool(forKey: Prefs.tourPending) ? tourShortcuts() : nil
        if let text, let takeoffAt { native.start(room: text, takeoffAt: takeoffAt) } else { native.start() }
        registerMuteHotKey()
        registerPauseHotKey()
        updateMenu()
        refreshListening()
    }

    // MARK: Listening mode

    private func configureListening() {
        // nil = no status yet (Hermes unreachable at launch): turning listening on asks again.
        // Listening mode always resumes the call it paused, so the plugin must take room text on a resume.
        room.pluginSupportsRoom = { [weak self] in
            self?.app.status.map { $0.roomListening == true && $0.roomOnResume == true }
        }
        room.refreshStatus = { [weak self] in await self?.app.refresh() }
        room.callBusy = { [weak self] in
            guard let self else { return false }
            let connection = self.native.model.state.connection
            return connection == .connecting || connection == .ending
        }
        room.callActive = { [weak self] in
            guard let self else { return false }
            let connection = self.native.model.state.connection
            return connection == .live || connection == .paused
        }
        // Mid-conversation, the call pauses first: the voice stops hearing and answering.
        room.prepareMic = { [weak self] in
            guard let self else { return false }
            guard self.native.model.state.connection == .live else { return true }
            let paused = await self.native.pauseForListening()
            self.updateMenu()
            return paused
        }
        room.onChange = { [weak self] in self?.refreshListening() }
        room.onShowPanel = { [weak self] in self?.native.showPanel() }
        room.onTakeoff = { [weak self] text, at in
            self?.beginCall(room: text, takeoffAt: at)
        }
        native.onRoomTakeoffOutcome = { [weak self] outcome in self?.room.callOutcome(outcome) }
        native.model.onToggleRoom = { [weak self] in
            guard let self else { return }
            if self.room.isOn { self.startConversation() } else { self.room.turnOn() }
        }
        native.model.onAskRoom = { [weak self] in self?.startConversation() }
        native.model.onDiscardRoom = { [weak self] in self?.room.discard() }
        native.model.onDismissRoomNotice = { [weak self] in self?.room.dismissNotice() }
        resolveListenShortcut()
        refreshListening()
    }

    /// Mirror listening mode into the panel, the menu and the menu bar glyph.
    private func refreshListening() {
        guard let room, let native else { return }
        let model = native.model
        let presentation = room.presentation()
        let notice = room.notice
        let offered = listeningBeta && room.isSupported && app.isPaired
        let blocked = room.isOn ? nil : room.blocker
        let changedShape = (model.room == nil) != (presentation == nil) || (model.roomNotice == nil) != (notice == nil)
            || model.room?.warnings != presentation?.warnings
        if model.room != presentation { model.room = presentation }
        if model.roomNotice != notice { model.roomNotice = notice }
        if model.roomOffered != offered { model.roomOffered = offered }
        if model.roomBlocked != blocked { model.roomBlocked = blocked }
        if model.roomCanAsk != room.canAsk { model.roomCanAsk = room.canAsk }
        if changedShape { native.surface.setNeedsResize() }
        setBadge(badgeOn)
        updateMenu()
    }

    private func applyListeningBeta() {
        let on = listeningBeta
        guard on != lastListeningBeta, room != nil, native != nil else { return }
        lastListeningBeta = on
        if !on && (room.isOn || room.canAsk) { room.discard() }
        resolveListenShortcut()
        refreshListening()
    }

    @objc private func toggleListeningFromMenu() {
        guard listeningBeta else { return }
        if room.canAsk { startConversation() } else { room.turnOn() }
    }

    @objc private func discardListeningFromMenu() { room.discard() }

    /// `defaults write <bundle id> listeningShortcut "ctrl+opt+l"`; unset or empty = no shortcut.
    private func resolveListenShortcut() {
        listenHotKey = nil
        listenShortcut = nil
        let raw = UserDefaults.standard.string(forKey: Prefs.listeningShortcut)?.trimmingCharacters(in: .whitespaces)
        var problem: String?
        if listeningBeta, let raw, !raw.isEmpty {
            switch KeyShortcut.parse(raw, reserved: app.callShortcut) {
            case .failure(let error):
                problem = "Listening shortcut '\(raw)' rejected: \(error)"
            case .success(let shortcut):
                if shortcut == muteShortcut || shortcut == pauseShortcut {
                    problem = "Listening shortcut \(shortcut.display) is already another Speakeasy shortcut"
                } else if GlobalHotKey.collidesWithSystemShortcut(keyCode: shortcut.keyCode, modifiers: shortcut.modifiers) {
                    problem = "Listening shortcut \(shortcut.display) is used by a macOS system shortcut"
                } else {
                    do {
                        listenHotKey = try GlobalHotKey(keyCode: shortcut.keyCode, modifiers: shortcut.modifiers,
                                                        display: shortcut.display) { [weak self] in
                            self?.toggleListeningFromMenu()
                        }
                        listenShortcut = shortcut
                    } catch {
                        problem = error.localizedDescription
                    }
                }
            }
        }
        app.listeningShortcutProblem = problem
        native.model.roomShortcutHint = listenShortcut.map { "\($0.display): turn listening mode on or off" } ?? ""
        updateMenu()
    }

    /// Shortcut labels the tour mentions (only the ones that are set).
    private func tourShortcuts() -> [String: String] {
        var out = ["call": app.callShortcut.display]
        if let muteShortcut { out["mute"] = muteShortcut.display }
        if let pauseShortcut, native.supportsPause { out["pause"] = pauseShortcut.display }
        return out
    }

    private func endConversation() {
        guard active else { return }
        native.end()
    }

    private func configureClient() {
        native.onPauseChanged = { [weak self] paused in
            guard let self else { return }
            if paused { self.unregisterMuteHotKey() } else { self.registerMuteHotKey() }
            self.updateMenu()
        }
        native.model.onStart = { [weak self] in self?.startConversation() }
        native.model.onTogglePause = { [weak self] in self?.togglePause() }
        native.model.onSkipTour = { [weak self] in self?.native.skipTour() }
        native.onTourStarted = { UserDefaults.standard.set(false, forKey: Prefs.tourPending) }
        native.onPausedTaskSettled = { [weak self] notice in
            guard let self else { return }
            if let idle = self.idle { idle.pausedNotice(notice) } else { self.setBadge(true) }
        }
        native.onClosed = { [weak self] in
            guard let self else { return }
            self.active = false
            self.unregisterMuteHotKey()
            self.pauseHotKey = nil
            self.updateMenu()
            let state = self.native.model.state
            // A delegated run whose first work update never arrived is still in flight.
            self.idle?.callEnded(lastWork: state.workInfo ?? state.runID.map { WorkInfo(runID: $0, status: "running") })
            self.room.callEnded()   // listening mode only exists during a call
            self.refreshListening()
            if self.quitPending { NSApp.terminate(nil) }
        }
    }

    // MARK: Hotkeys

    private func registerCallHotKey(_ shortcut: KeyShortcut) {
        hotKey = nil
        do {
            hotKey = try GlobalHotKey(keyCode: shortcut.keyCode, modifiers: shortcut.modifiers, display: shortcut.display) { [weak self] in
                self?.hotkeyPressed()
            }
            app.callShortcutProblem = nil
        } catch {
            app.callShortcutProblem = error.localizedDescription
        }
        native.setCallShortcutHint(shortcut.spoken)
        updateMenu()
    }

    /// `defaults write <bundle id> muteShortcut "ctrl+opt+m"` (empty disables).
    private func resolveMuteShortcut() {
        let raw = UserDefaults.standard.string(forKey: Prefs.muteShortcut)
        if raw?.trimmingCharacters(in: .whitespaces).isEmpty == true {
            muteShortcut = nil; muteShortcutProblem = "Mute shortcut disabled"
        } else {
            switch raw.map({ KeyShortcut.parse($0, reserved: app.callShortcut) }) ?? .success(.defaultMute) {
            case .failure(let error):
                muteShortcut = nil; muteShortcutProblem = "Mute shortcut '\(raw ?? "")' rejected: \(error)"
            case .success(let shortcut):
                if GlobalHotKey.collidesWithSystemShortcut(keyCode: shortcut.keyCode, modifiers: shortcut.modifiers) {
                    muteShortcut = nil; muteShortcutProblem = "Mute shortcut \(shortcut.display) is used by a macOS system shortcut"
                } else {
                    muteShortcut = shortcut; muteShortcutProblem = nil
                }
            }
        }
        app.muteShortcutProblem = muteShortcutProblem
        native.model.muteShortcutHint = muteShortcut.map { "\($0.display): tap to mute/unmute, hold to talk" } ?? ""
    }

    /// `defaults write <bundle id> pauseShortcut "ctrl+opt+p"` (empty disables).
    private func resolvePauseShortcut() {
        let raw = UserDefaults.standard.string(forKey: Prefs.pauseShortcut)
        if raw?.trimmingCharacters(in: .whitespaces).isEmpty == true {
            pauseShortcut = nil; pauseShortcutProblem = "Pause shortcut disabled"
        } else {
            switch raw.map({ KeyShortcut.parse($0, reserved: app.callShortcut) }) ?? .success(.defaultPause) {
            case .failure(let error):
                pauseShortcut = nil; pauseShortcutProblem = "Pause shortcut '\(raw ?? "")' rejected: \(error)"
            case .success(let shortcut):
                if shortcut == muteShortcut {
                    pauseShortcut = nil; pauseShortcutProblem = "Pause shortcut \(shortcut.display) is the mute shortcut"
                } else if GlobalHotKey.collidesWithSystemShortcut(keyCode: shortcut.keyCode, modifiers: shortcut.modifiers) {
                    pauseShortcut = nil; pauseShortcutProblem = "Pause shortcut \(shortcut.display) is used by a macOS system shortcut"
                } else {
                    pauseShortcut = shortcut; pauseShortcutProblem = nil
                }
            }
        }
        app.pauseShortcutProblem = pauseShortcutProblem
        native.model.pauseShortcutHint = pauseShortcut.map { "\($0.display): pause or resume the call" } ?? ""
    }

    private func registerPauseHotKey() {
        guard pauseHotKey == nil, native.supportsPause, let shortcut = pauseShortcut else { return }
        do {
            pauseHotKey = try GlobalHotKey(keyCode: shortcut.keyCode, modifiers: shortcut.modifiers,
                                           display: shortcut.display, exclusive: true,
                                           callback: { [weak self] in self?.togglePause() })
        } catch {
            pauseShortcutProblem = error.localizedDescription
        }
    }

    /// Pause / Resume (panel, menu, shortcut, the paused-task notice). While listening mode holds a
    /// paused call, Resume turns listening off into that conversation, with what was heard.
    private func togglePause() {
        guard active else { return }
        if native.isPaused && room.canAsk { startConversation(); return }
        native.togglePause()
        updateMenu()
    }

    @objc private func togglePauseFromMenu() { togglePause() }

    private func registerMuteHotKey() {
        guard muteHotKey == nil, let shortcut = muteShortcut else { return }
        muteGesture.cancel()
        do {
            muteHotKey = try GlobalHotKey(keyCode: shortcut.keyCode, modifiers: shortcut.modifiers,
                                          display: shortcut.display, exclusive: true,
                                          onRelease: { [weak self] in self?.muteKeyUp() },
                                          callback: { [weak self] in self?.muteKeyDown() })
        } catch {
            muteShortcutProblem = error.localizedDescription
        }
    }

    private func unregisterMuteHotKey() {
        muteHotKey = nil
        muteGesture.cancel()
    }

    private func muteKeyDown() {
        guard active, native.micControllable,
              let next = muteGesture.press(current: native.micMuted ? .muted : .live, now: Date()) else { return }
        native.setMicMuted(next == .muted)
        updateMenu()
    }

    private func muteKeyUp() {
        guard let restore = muteGesture.release(now: Date()) else { return }
        guard active, native.micControllable else { return }
        native.setMicMuted(restore == .muted)
        updateMenu()
    }

    @objc private func toggleMuteFromMenu() {
        guard active, native.micControllable else { return }
        native.setMicMuted(!native.micMuted)
        updateMenu()
    }

    // MARK: Previews (offline; no network)

    private func runPreview(_ args: [String], index: Int) {
        let name = index + 1 < args.count ? args[index + 1] : "listening"
        guard let fixture = PreviewFixtures.state(name) else {
            print("unknown --ui-preview state '\(name)'; expected one of: \(PreviewFixtures.names.joined(separator: ", "))")
            exit(2)
        }
        let preview = NativeVoiceClient(config: app.config)
        native = preview
        if name == "image" { preview.model.loadProductImage = { _, _ in PreviewFixtures.sampleImage() } }
        if name == "looking" { preview.model.loadLiveImage = { _ in PreviewFixtures.sampleDesign(variant: 1) } }
        if name == "design-review" { preview.model.loadProductImage = { _, n in PreviewFixtures.sampleDesign(variant: n - 1) } }
        preview.showPreview(fixture.0, workExpanded: fixture.workExpanded)
        print("ui-preview \(name) window=\(preview.panel.windowNumber)")
        if let appearance = args.firstIndex(of: "--appearance").flatMap({ $0 + 1 < args.count ? args[$0 + 1] : nil }) {
            NSApp.appearance = NSAppearance(named: appearance == "light" ? .aqua : .darkAqua)
        }
        if let snap = args.firstIndex(of: "--snapshot"), snap + 1 < args.count {
            let url = URL(fileURLWithPath: args[snap + 1])
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                do { try preview.panel.snapshot(to: url); print("snapshot \(url.path)"); exit(0) }
                catch { print("snapshot failed: \(error)"); exit(1) }
            }
        }
    }
}

extension PairingLink.ParseError {
    var userMessage: String {
        switch self {
        case .notSpeakeasy, .notPairing: return "This isn't a Speakeasy pairing link."
        case .missingServer: return "The link doesn't say which server to connect to."
        case .untrustedServer: return "The link points to a server Speakeasy won't trust. Use this Mac (127.0.0.1) or an https Tailscale address."
        case .invalidCode: return "The link's pairing code isn't a 6-digit code. Run hermes voice pair for a new one."
        }
    }
}

extension KeyShortcut {
    /// "Control–Option–Space" for the idle panel hint.
    var spoken: String {
        var parts: [String] = []
        if modifiers & Self.control != 0 { parts.append("Control") }
        if modifiers & Self.option != 0 { parts.append("Option") }
        if modifiers & Self.shift != 0 { parts.append("Shift") }
        if modifiers & Self.command != 0 { parts.append("Command") }
        return (parts + [keyName]).joined(separator: "–")
    }
}
