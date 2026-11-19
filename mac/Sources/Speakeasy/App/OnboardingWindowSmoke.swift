import AppKit
import SpeakeasyCore
import SwiftUI

/// `Speakeasy --onboarding-window-smoke <dir>`: opens the real setup window the way an
/// already-paired first launch does (onboarding not done, so it lands on "You're ready to
/// talk"), with invented sample data and no server, then checks that the window fits the
/// screen and that Done and Start a call sit inside it. Saves light and dark captures of the
/// actual window. Exit 0 = PASS. Also checks the size clamp on a short screen (SPK-534).
@MainActor
enum OnboardingWindowSmoke {
    static func run(dir: String) {
        do { try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true) }
        catch { print(error); exit(1) }
        var failures: [String] = []
        func check(_ ok: Bool, _ what: String) {
            print("\(ok ? "PASS" : "FAIL"): \(what)")
            if !ok { failures.append(what) }
        }

        // The clamp itself: a 700 pt tall screen never gets a taller window, a big one keeps the design size.
        let short = OnboardingWindowController.fittedContentSizes(for: NSSize(width: 1280, height: 400))
        check(short.initial.height <= 400 && short.min.height <= 400 && short.max.height <= 400,
              "short screen clamps initial/min/max height to 400 (got \(short.initial.height)/\(short.min.height)/\(short.max.height))")
        let big = OnboardingWindowController.fittedContentSizes(for: NSSize(width: 2560, height: 1400))
        check(big.initial == OnboardingView.size && big.max == OnboardingView.maxSize, "large screen keeps the design sizes")

        PanelSmoke.setEnhancedAccessibility(true)
        let app = AppModel.shared
        app.destinations = [Destination(target: "discord:1001", label: "Discord · #updates"),
                            Destination(target: "telegram:2002", label: "Telegram · Notes")]
        app.suggestedDestination = "discord:1001"
        app.home = HomeSmoke.sample
        app.micAuthorization = .authorized

        for dark in [false, true] {
            let flow = OnboardingFlow(app: app)
            flow.factsOverride = OnboardingFacts(paired: true, voiceReady: true)
            flow.welcome = WelcomeJourney(paired: true)
            check(flow.welcome.route == .setup, "already-paired first launch goes straight to setup")
            flow.step = OnboardingPlan.first(flow.facts)
            check(flow.step == .ready, "already-paired, voice ready: first screen is You're ready to talk")
            flow.userName = "Sam"
            flow.assistantName = "Juniper"
            flow.loadDefaults()

            // The content itself must not ask for a screen-tall window either: any host that sizes
            // from the SwiftUI view (the welcome shots, a future hosting change) would grow with it.
            // Measured after the form has loaded its rows, in a window sized the default way.
            let probe = NSHostingView(rootView: OnboardingView(flow: flow).environmentObject(app))
            probe.frame = NSRect(origin: .zero, size: OnboardingView.size)
            let probeWindow = NSWindow(contentRect: probe.frame, styleMask: [.titled], backing: .buffered, defer: false)
            probeWindow.contentView = probe
            probeWindow.orderFrontRegardless()
            for _ in 0..<6 { probe.layoutSubtreeIfNeeded(); RunLoop.main.run(until: Date().addingTimeInterval(0.2)) }
            let grown = probe.frame.height
            print("probe: fitting \(Int(probe.fittingSize.height)), intrinsic \(Int(probe.intrinsicContentSize.height)), hosted \(Int(grown))")
            check(grown <= OnboardingView.maxSize.height,
                  "\(dark ? "dark" : "light"): the ready step's own height \(Int(grown)) is within the design maximum \(Int(OnboardingView.maxSize.height))")
            probeWindow.orderOut(nil)

            let window = OnboardingWindowController.makeWindow(flow: flow)
            window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            // Let SwiftUI lay out and any content-driven resize happen (the bug grew the window here).
            for _ in 0..<8 {
                window.contentView?.layoutSubtreeIfNeeded()
                RunLoop.main.run(until: Date().addingTimeInterval(0.25))
            }
            let look = dark ? "dark" : "light"
            let frame = window.frame
            let visible = (window.screen ?? NSScreen.main)?.visibleFrame ?? .zero
            print("\(look): window \(Int(frame.width)) x \(Int(frame.height)) at y \(Int(frame.minY)); screen visible \(Int(visible.width)) x \(Int(visible.height))")
            check(frame.height <= visible.height + 0.5, "\(look): window height \(Int(frame.height)) fits the screen (\(Int(visible.height)))")
            check(frame.height <= OnboardingView.maxSize.height + 60, "\(look): window height \(Int(frame.height)) within the design maximum")
            check(visible.contains(frame.insetBy(dx: 1, dy: 1)), "\(look): window sits on the screen")

            for label in ["Done", "Start a call"] {
                guard let element = findButton(label, in: window.contentView!) else { check(false, "\(look): \(label) button exists"); continue }
                let r = element.accessibilityFrame() // screen coordinates
                check(r.width > 0 && frame.insetBy(dx: -0.5, dy: -0.5).contains(r),
                      "\(look): \(label) is inside the window (button \(Int(r.minX)),\(Int(r.minY)) \(Int(r.width))x\(Int(r.height)))")
                check(visible.contains(r), "\(look): \(label) is on the screen")
            }

            if let view = window.contentView, let content = view.bitmapImageRepForCachingDisplay(in: view.bounds),
               let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: content)
                // The cached content has no window background; paint it underneath so dark reads as dark.
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
                window.effectiveAppearance.performAsCurrentDrawingAppearance {
                    NSColor.windowBackgroundColor.setFill()
                    NSRect(origin: .zero, size: view.bounds.size).fill()
                }
                content.draw(in: NSRect(origin: .zero, size: view.bounds.size), from: .zero, operation: .sourceOver,
                             fraction: 1, respectFlipped: true, hints: nil)
                NSGraphicsContext.restoreGraphicsState()
                let path = "\(dir)/ready-window-\(look).png"
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
                print("window shot \(path)")
            }
            window.orderOut(nil)
        }
        if failures.isEmpty { print("PASS: onboarding window fits the screen (\(OnboardingView.size))"); exit(0) }
        print("FAIL: \(failures.count) check(s)"); exit(1)
    }

    private static func findButton(_ label: String, in root: NSView) -> NSAccessibilityElementProtocol? {
        var found: NSAccessibilityElementProtocol?
        func walk(_ object: Any, _ depth: Int) {
            guard found == nil, depth < 60, let element = object as? NSAccessibilityElementProtocol & NSObject else { return }
            let ax = element as AnyObject
            if ax.accessibilityLabel?() == label, ax.accessibilityRole?() == .button { found = element; return }
            for child in (ax.accessibilityChildren?() ?? nil) ?? [] { walk(child, depth + 1) }
        }
        walk(root, 0)
        return found
    }
}
