import AppKit
import Combine
import SwiftUI
import SpeakeasyClient
import SpeakeasyCore

/// The Mac's secure box: its own small window (the panel and History stay as they are), shown in
/// front whenever the plugin has a key or sudo password request open for this Mac, closed when it is
/// answered, skipped or expires. A regular titled window so the field takes the keyboard and Password
/// AutoFill can offer the saved password.
@MainActor
final class SecureInputWindowController: NSObject, NSWindowDelegate {
    let center: SecureInputCenter
    private var window: NSWindow?
    private var shownID: String?
    private var bag = Set<AnyCancellable>()

    init(client: @escaping () -> ServerClient?) {
        center = SecureInputCenter(client: client)
        super.init()
        center.$requests
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.sync() }
            .store(in: &bag)
    }

    func start() { center.start() }

    private func sync() {
        guard let request = center.current else { close(); return }
        if request.id == shownID, window?.isVisible == true { return }
        show(request)
    }

    private func show(_ request: SecureInputRequest) {
        let card = SecureInputCard(request: request,
                                   onSubmit: { [center] in await center.submit(request, value: $0) },
                                   onSubmitLogin: { [center] in await center.submitLogin(request, identifier: $0, password: $1, code: $2) },
                                   onSkip: { [center] in _ = await center.skip(request) })
        let host = NSHostingView(rootView: card.frame(width: 480).fixedSize(horizontal: false, vertical: true))
        let window = self.window ?? Self.makeWindow()
        window.title = ["sudo": "Allow a command", "login": "Add a login", "code": "One-time code"][request.kind.rawValue] ?? "Secure key"
        window.contentView = host
        window.setContentSize(host.fittingSize)
        window.delegate = self
        window.center()
        self.window = window
        shownID = request.id
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private func close() {
        shownID = nil
        window?.orderOut(nil)
        window?.contentView = nil  // drop the field (and anything typed) with the view
    }

    /// The window's close button means "not now", the same as Skip.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if let request = center.current { Task { _ = await center.skip(request) } }
        close()
        return false
    }

    static func makeWindow() -> NSWindow {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 360),
                              styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.sharingType = .none  // the box never shows up in screen sharing or screen recordings
        return window
    }
}

/// `--secure-input-shots DIR`: the key and sudo cards in light and dark, as PNGs (fixtures only).
@MainActor
enum SecureInputShots {
    static func run(dir: String) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let cases: [(String, SecureInputRequest, Bool, String)] = [
            ("mac-key", SecureInputFixtures.weatherKey, false, ""),
            ("mac-key-filled", SecureInputFixtures.weatherKey, false, "FAKE-owm-key-0000"),
            ("mac-sudo", SecureInputFixtures.sudo, false, ""),
            ("mac-login", SecureInputFixtures.login, false, "fake-password"),
            ("mac-openai-key", SecureInputRequest.openAIKey(), false, ""),
        ]
        for (name, request, revealed, typed) in cases {
            for (look, appearance) in [("light", NSAppearance.Name.aqua), ("dark", NSAppearance.Name.darkAqua)] {
                let card = SecureInputCard(request: request, startRevealed: revealed, previewValue: typed,
                                           previewIdentifier: request.kind == .login ? "fake.user@example.com" : "", onSubmit: { _ in nil }, onSkip: {})
                    .frame(width: 480).fixedSize(horizontal: false, vertical: true)
                    .background(Color(nsColor: .windowBackgroundColor))
                let host = NSHostingView(rootView: card)
                host.appearance = NSAppearance(named: appearance)
                let window = SecureInputWindowController.makeWindow()
                window.sharingType = .readOnly
                window.appearance = NSAppearance(named: appearance)
                window.contentView = host
                window.setContentSize(host.fittingSize)
                NSApp.setActivationPolicy(.accessory)
                NSApp.activate(ignoringOtherApps: true)
                window.makeKeyAndOrderFront(nil)
                host.layoutSubtreeIfNeeded()
                RunLoop.main.run(until: Date().addingTimeInterval(0.6))
                let bounds = host.bounds
                if let rep = host.bitmapImageRepForCachingDisplay(in: bounds) {
                    host.cacheDisplay(in: bounds, to: rep)
                    if let png = rep.representation(using: .png, properties: [:]) {
                        try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name)-\(look).png"))
                    }
                }
                window.orderOut(nil)
            }
        }
        exit(0)
    }
}
