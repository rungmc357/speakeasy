import AppKit
import SpeakeasyCore
import SwiftUI

/// `Speakeasy --onboarding-snapshot <file.png> [connect|voice|ready]`: renders a setup screen
/// offscreen (no window shown) so the setup copy can be reviewed as an image.
/// `SPEAKEASY_APPEARANCE=light|dark` picks the look (dark by default). `SPEAKEASY_ONBOARDING_DEMO=1`
/// fills the ready screen with invented sample data and contacts no server.
enum OnboardingSnapshot {
    @MainActor
    static func render(to path: String, step: String? = nil) {
        let env = ProcessInfo.processInfo.environment
        let demo = env["SPEAKEASY_ONBOARDING_DEMO"] == "1"
        let app = AppModel.shared
        let flow = OnboardingFlow(app: app)
        flow.welcome.route = .setup
        if let step, let s = OnboardingFlow.Step.allCases.first(where: { $0.rawValue == step }) {
            if demo {
                app.destinations = [Destination(target: "discord:1001", label: "Discord · #updates"),
                                    Destination(target: "telegram:2002", label: "Telegram · Notes")]
                app.suggestedDestination = "discord:1001"
                app.home = HomeSmoke.sample
                app.micAuthorization = .authorized
                flow.userName = "Sam"
                flow.assistantName = "Juniper"
                flow.factsOverride = OnboardingFacts(paired: true, voiceReady: s != .voice)
            } else {
                Task { await app.refresh() }
                RunLoop.main.run(until: Date().addingTimeInterval(4))
            }
            flow.step = s
            if s == .ready { flow.loadDefaults() }
        }
        let size = OnboardingView.size
        let view = OnboardingView(flow: flow).environmentObject(app)
            .background(Color(nsColor: .windowBackgroundColor))
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: host.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: env["SPEAKEASY_APPEARANCE"] == "light" ? .aqua : .darkAqua)
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.6))
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
        try? png.write(to: URL(fileURLWithPath: path))
        print("snapshot \(path)")
        exit(0)
    }
}
