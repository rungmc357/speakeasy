import AppKit
import SpeakeasyClient
import SpeakeasyCore
import SwiftUI

/// Keychain-free rendering and real panel-visibility smoke checks with invented data.
@MainActor
enum NowMacShots {
    private static var kept: [NSWindow] = []
    static func run(dir: String) {
        FilmFrames.stillMotion = true
        Task { @MainActor in
            do {
                try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
                let now = MacNowModel.shared
                now.demo = snapshotHome()
                await now.refresh()
                let voice = VoicePanelModel()
                voice.state.connection = .live
                voice.state.mic = .live
                voice.shownStatus = "Looking at desk options."
                let store = HistoryStore()
                store.backend = DemoHistoryBackend()
                store.supported = true
                store.nowSupported = true
                store.recorder = SilentRecorder()
                store.start()
                store.open("s_flaky")
                let homeOnly = HistoryStore()
                homeOnly.backend = DemoHistoryBackend()
                homeOnly.supported = true
                homeOnly.nowSupported = true
                homeOnly.recorder = SilentRecorder()
                let panel = VoicePanelController(model: voice)
                MacNowPresentation.shared.enabled = true
                MacNowPresentation.shared.homeFrontmost = false
                panel.show()
                precondition(panel.isVisible, "An open call must float away from Home")
                MacNowPresentation.shared.homeFrontmost = true
                panel.show()
                precondition(!panel.isVisible, "The call bar must not cover Home")
                MacNowPresentation.shared.homeFrontmost = false
                voice.state.connection = .ended(.complete)
                panel.show()
                precondition(!panel.isVisible, "A finished call must not leave a floating bar")
                voice.state.connection = .live
                MacNowPresentation.shared.callStartedAt = Date().addingTimeInterval(-185)
                for appearance in ["light", "dark"] {
                    NSApp.appearance = NSAppearance(named: appearance == "light" ? .aqua : .darkAqua)
                    try await followCheck(now: now, store: store, shot: "\(dir)/home-follow-\(appearance).png")
                    try await shot(MacNowPopover(now: now, voice: voice, open: { _ in }, more: {}),
                                   size: NSSize(width: 360, height: 560), path: "\(dir)/popover-\(appearance).png")
                    let idle = VoicePanelModel()
                    try await shot(MacNowPopover(now: now, voice: idle, open: { _ in }, more: {}),
                                   size: NSSize(width: 360, height: 560), path: "\(dir)/popover-offcall-\(appearance).png")
                    // SPK-543: the real floating surface in a clear window, as the panel floats it.
                    try await callBarCheck(voice: voice, path: "\(dir)/callbar-\(appearance).png")
                    try await shot(HistoryRootView(store: store, voice: voice),
                                   size: NSSize(width: 760, height: 640), path: "\(dir)/home-narrow-\(appearance).png")
                    // SPK-535: nothing open on the right, so Home takes the whole narrow window.
                    try await shot(HistoryRootView(store: homeOnly, voice: voice),
                                   size: NSSize(width: 760, height: 560), path: "\(dir)/home-narrow-only-\(appearance).png")
                    try await shot(HistoryRootView(store: store, voice: voice),
                                   size: NSSize(width: 1400, height: 700), path: "\(dir)/home-wide-\(appearance).png")
                    // SPK-198: off a call, Home's composer with the round call button.
                    let idleVoice = VoicePanelModel()
                    let composer = MacHomeComposerModel.shared
                    composer.demo = true
                    composer.recorder = SilentRecorder()
                    composer.voiceNotesSupported = true
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 1400, height: 700), path: "\(dir)/home-composer-\(appearance).png")
                    try await shot(VStack(spacing: 0) {
                        MacNowWorkList(now: now, store: store, onClose: {})
                        MacHomeBottomBar(voice: idleVoice, composer: composer)
                    }, size: NSSize(width: 460, height: 600), path: "\(dir)/work-\(appearance).png")
                    try await shot(MacNowRecent(now: now, store: store),
                                   size: NSSize(width: 460, height: 500), path: "\(dir)/recent-\(appearance).png")
                    now.demo = NowHome(rev: 2, asOf: Date(), sentence: "All quiet.", counts: NowHome.Counts(), items: [],
                                       backgroundRunning: 0)
                    await now.refresh()
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 1400, height: 700), path: "\(dir)/home-empty-\(appearance).png")
                    composer.fill(HomeSuggestions.today()[0])
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 760, height: 640), path: "\(dir)/home-typing-\(appearance).png")
                    composer.send()
                    precondition(now.visible.first?.kind == .working, "A sent message must show as a working card")
                    precondition(composer.draft.isEmpty, "Sending must empty the box")
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 760, height: 640), path: "\(dir)/home-sent-\(appearance).png")
                    #if DEBUG
                    composer.previewRecording(seconds: 7, level: 0.6)
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 760, height: 640), path: "\(dir)/home-memo-\(appearance).png")
                    composer.cancelRecording()
                    #endif
                    for p in now.pending { now.dropPending(p.id) }
                    now.demo = snapshotHome()
                    await now.refresh()
                }
                store.stop(); now.stop(); panel.hide()
                print("NOW_MAC_SMOKE PASS: floating, Home-frontmost and call-ended visibility; Home composer send; thread follows a send (SPK-533); 24 screenshots; call bar filled edge to edge (SPK-543)")
                NSApp.terminate(nil)
            } catch {
                print("NOW_MAC_SMOKE FAILED: \(error)")
                exit(1)
            }
        }
    }

    /// SPK-533: Home's thread, longer than its window, follows a sent message and what arrives under
    /// it while you're at the bottom; scrolled up, it stays put until you send.
    private static func followCheck(now: MacNowModel, store: HistoryStore, shot path: String) async throws {
        let composer = MacHomeComposerModel.shared
        let wasDemo = composer.demo
        composer.demo = true
        defer { composer.demo = wasDemo }
        // Like a real launch: Home's list isn't fetched yet when the window first lays out.
        let full = now.demo
        now.demo = NowHome(rev: 1, asOf: Date(), sentence: "", counts: NowHome.Counts(), items: [], backgroundRunning: 0)
        await now.refresh()
        let view = NSHostingView(rootView: MacNowColumn(now: now, store: store).background(HistoryLook.window))
        view.sizingOptions = []
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 460, height: 380), styleMask: [.borderless],
                         backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        w.contentView = view
        kept.append(w)
        w.makeKeyAndOrderFront(nil)
        func settle() async throws {
            for _ in 0..<8 { view.layoutSubtreeIfNeeded(); try await Task.sleep(nanoseconds: 120_000_000) }
        }
        func thread() throws -> NSScrollView {
            var found: [NSScrollView] = []
            func walk(_ v: NSView) { if let s = v as? NSScrollView { found.append(s) }; v.subviews.forEach(walk) }
            walk(view)
            guard let s = found.max(by: { ($0.documentView?.frame.height ?? 0) < ($1.documentView?.frame.height ?? 0) })
            else { throw CocoaError(.featureUnsupported) }
            return s
        }
        /// Points of thread below the visible bottom.
        func below() -> CGFloat {
            guard let s = try? thread(), let doc = s.documentView else { return 0 }
            let clip = s.contentView.bounds
            return doc.isFlipped ? doc.frame.height - clip.maxY : clip.minY
        }
        /// A real scroll-wheel gesture, the way a person scrolls (lines; positive = up).
        func wheel(_ lines: Int32) throws {
            let s = try thread()
            for _ in 0..<12 {
                guard let cg = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: lines, wheel2: 0, wheel3: 0),
                      let e = NSEvent(cgEvent: cg) else { throw CocoaError(.featureUnsupported) }
                s.scrollWheel(with: e)
            }
        }
        try await settle()
        now.demo = full
        await now.refresh()
        try await settle()
        try wheel(-300)   // the person scrolls to the newest message
        try await settle()
        let s0 = try thread()
        precondition((s0.documentView?.frame.height ?? 0) > s0.contentView.bounds.height + 100,
                     "SPK-533 check needs a thread longer than the window")
        precondition(below() < 4, "Scrolled to the newest message")
        // 1. Send at the bottom: the sent message and its working card come into view.
        composer.fill("Find a quieter desk lamp under $60")
        composer.send()
        try await settle()
        precondition(below() < 4, "SPK-533: a sent message must be visible, \(below()) pt below")
        // 2. The next Home refresh carries your words and the reply (no send): still following.
        let long = String(repeating: "It should fit a narrow desk, take a warm bulb and not hum. ", count: 10)
        try await append([("user", long), ("assistant", "Three lamps under $60 fit that. The quietest uses a plain LED and has a dimmer.")], now: now)
        try await settle()
        precondition(below() < 4, "SPK-533: what arrives after a send must stay in view, \(below()) pt below")
        if let image = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: image)
            try image.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
            print("shot: \(path)")
        }
        // 3. Scrolled up to read: something new arrives, the thread stays where you put it.
        try wheel(30)
        try await settle()
        let up = below()
        precondition(up > 100, "Scrolling up must move off the bottom")
        try await append([("assistant", "A job finished while you read: the lamp comparison is ready.")], now: now)
        try await settle()
        precondition(below() > up, "SPK-533: scrolled up, new rows must not pull the thread down")
        // 4. Sending again brings the newest message back.
        composer.fill("And one for the bedroom")
        composer.send()
        try await settle()
        precondition(below() < 4, "SPK-533: sending from a scrolled-up thread must show the new message")
        // 5. Scrolled up, then back down by hand: a reply arriving is followed again.
        try wheel(30)
        try await settle()
        try wheel(-300)
        try await settle()
        precondition(below() < 4, "Scrolling back down must reach the bottom")
        try await append([("assistant", "The bedroom lamp is ordered for Friday.")], now: now)
        try await settle()
        precondition(below() < 4, "SPK-533: back at the bottom, a new reply must stay in view, \(below()) pt below")
        for p in now.pending { now.dropPending(p.id) }
        now.demo = snapshotHome()
        await now.refresh()
        w.orderOut(nil)
        print("NOW_MAC_FOLLOW PASS: sent message, arriving reply, scrolled-up hold, send re-follows, back-at-bottom follows")
    }

    /// A Home refresh that adds chat messages at the bottom of the thread.
    private static func append(_ messages: [(String, String)], now: MacNowModel) async throws {
        guard var home = now.demo, var stream = home.stream else { throw CocoaError(.featureUnsupported) }
        for (role, text) in messages {
            stream.entries.append(NowStream.Entry(id: "spk533-\(stream.entries.count)", kind: .message, at: Date(), text: text,
                                                  role: role == "user" ? .user : .assistant))
        }
        home.stream = stream
        now.demo = home
        await now.refresh()
    }

    private static func snapshotHome() -> NowHome {
        if let path = ProcessInfo.processInfo.environment["SPEAKEASY_NOW_FIXTURE"],
           let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            return NowHome(json: object)
        }
        var home = MacDemoNow.home()
        home.stream = NowStream(entries: home.items.reversed().flatMap { item in
            let at = item.startedAt ?? item.finishedAt ?? item.updatedAt ?? home.asOf
            return [NowStream.Entry(id: "a:" + item.id, kind: .ask, at: at, text: item.title, itemID: item.id),
                    NowStream.Entry(id: "c:" + item.id, kind: .card, at: at, itemID: item.id)]
        }, needsYou: home.items.filter { $0.kind == .question }.map(\.id))
        return home
    }

    /// SPK-543: the floating call bar is filled edge to edge. Shoots `MacNowVoiceSurface` (what the
    /// panel hosts) on a clear window and reads back its alpha: the top and bottom rows of the centre
    /// column must be filled (they were see-through when the fill stopped 4.5 pt short), and the
    /// corner pixel must stay clear (the rounded clip is on the bar's own outline).
    private static func callBarCheck(voice: VoicePanelModel, path: String) async throws {
        let size = NSSize(width: MacNowLayout.callBarSize.width, height: MacNowLayout.callBarSize.height)
        let rep = try await shot(MacNowVoiceSurface(voice: voice), size: size, path: path, clear: true)
        let mid = rep.pixelsWide / 2, last = rep.pixelsHigh - 1
        func alpha(_ x: Int, _ y: Int) -> CGFloat { rep.colorAt(x: x, y: y)?.alphaComponent ?? 0 }
        for y in [0, 1, last - 1, last] {
            precondition(alpha(mid, y) > 0.5, "SPK-543: the call bar must be filled at row \(y) of its centre column (alpha \(alpha(mid, y)))")
        }
        precondition(alpha(0, 0) < 0.1 && alpha(rep.pixelsWide - 1, last) < 0.1,
                     "SPK-543: the call bar's corners must be rounded off its own 320 × 56 outline")
        print("callbar fill: \(rep.pixelsWide)x\(rep.pixelsHigh) px, centre column filled top to bottom, corners clear")
    }

    @discardableResult
    private static func shot<V: View>(_ root: V, size: NSSize, path: String, clear: Bool = false) async throws -> NSBitmapImageRep {
        let view = NSHostingView(rootView: root)
        view.sizingOptions = []
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        if clear {
            w.isOpaque = false
            w.backgroundColor = .clear
            view.wantsLayer = true
            view.layer?.backgroundColor = .clear
        }
        w.contentView = view
        kept.append(w)
        w.center(); w.makeKeyAndOrderFront(nil)
        try await Task.sleep(nanoseconds: 650_000_000)
        view.layoutSubtreeIfNeeded()
        guard let image = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw CocoaError(.fileWriteUnknown) }
        view.cacheDisplay(in: view.bounds, to: image)
        guard let data = image.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try data.write(to: URL(fileURLWithPath: path))
        w.orderOut(nil)
        print("shot: \(path)")
        return image
    }
}
