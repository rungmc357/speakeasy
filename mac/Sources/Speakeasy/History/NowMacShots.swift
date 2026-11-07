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
                now.demo = MacDemoNow.home()
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
                    try await shot(MacNowPopover(now: now, voice: voice, open: { _ in }, more: {}),
                                   size: NSSize(width: 360, height: 560), path: "\(dir)/popover-\(appearance).png")
                    let idle = VoicePanelModel()
                    try await shot(MacNowPopover(now: now, voice: idle, open: { _ in }, more: {}),
                                   size: NSSize(width: 360, height: 560), path: "\(dir)/popover-offcall-\(appearance).png")
                    try await shot(MacNowCallControls(voice: voice, openHome: {}, floating: true)
                                    .clipShape(RoundedRectangle(cornerRadius: 14)),
                                   size: NSSize(width: 320, height: 56), path: "\(dir)/callbar-\(appearance).png")
                    try await shot(HistoryRootView(store: store, voice: voice),
                                   size: NSSize(width: 720, height: 640), path: "\(dir)/home-narrow-\(appearance).png")
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
                    now.demo = NowHome(rev: 2, asOf: Date(), sentence: "All quiet.", counts: NowHome.Counts(), items: [],
                                       backgroundRunning: 0)
                    await now.refresh()
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 1400, height: 700), path: "\(dir)/home-empty-\(appearance).png")
                    composer.fill(HomeSuggestions.today()[0])
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 720, height: 640), path: "\(dir)/home-typing-\(appearance).png")
                    composer.send()
                    precondition(now.visible.first?.kind == .working, "A sent message must show as a working card")
                    precondition(composer.draft.isEmpty, "Sending must empty the box")
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 720, height: 640), path: "\(dir)/home-sent-\(appearance).png")
                    #if DEBUG
                    composer.previewRecording(seconds: 7, level: 0.6)
                    try await shot(HistoryRootView(store: store, voice: idleVoice),
                                   size: NSSize(width: 720, height: 640), path: "\(dir)/home-memo-\(appearance).png")
                    composer.cancelRecording()
                    #endif
                    for p in now.pending { now.dropPending(p.id) }
                    now.demo = MacDemoNow.home()
                    await now.refresh()
                }
                store.stop(); now.stop(); panel.hide()
                print("NOW_MAC_SMOKE PASS: floating, Home-frontmost and call-ended visibility; Home composer send; 20 screenshots")
                NSApp.terminate(nil)
            } catch {
                print("NOW_MAC_SMOKE FAILED: \(error)")
                exit(1)
            }
        }
    }

    private static func shot<V: View>(_ root: V, size: NSSize, path: String) async throws {
        let view = NSHostingView(rootView: root)
        view.sizingOptions = []
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
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
    }
}
