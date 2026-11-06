import AppKit
import SwiftUI
import SpeakeasyCore

/// Offline screenshots and real-window navigation checks. No Keychain, mic or server.
@MainActor
enum WelcomeShots {
    static func run(dir: String) {
        do { try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true) }
        catch { print(error); exit(1) }
        keyboardCheck(dir: dir)
        var count = 0
        for dark in [false, true] {
            for large in [false, true] {
                let size = NSSize(width: large ? 800 : 540, height: large ? 720 : 480)
                let name = "\(dark ? "dark" : "light")-\(large ? "large" : "small")"
                for page in 0..<4 {
                    let flow = OnboardingFlow(app: AppModel.shared)
                    flow.welcome = WelcomeJourney()
                    flow.welcome.page = page
                    capture(OnboardingView(flow: flow).environmentObject(flow.app), size: size,
                            dark: dark, path: "\(dir)/\(name)-page\(page + 1).png")
                    count += 1
                }
                let flow = OnboardingFlow(app: AppModel.shared)
                flow.welcome.route = .newcomer
                capture(OnboardingView(flow: flow).environmentObject(flow.app), size: size,
                        dark: dark, path: "\(dir)/\(name)-new.png")
                count += 1
            }
            capture(HowItWorksDemo(onTryIt: {}), size: NSSize(width: 520, height: 600), dark: dark,
                    path: "\(dir)/\(dark ? "dark" : "light")-demo.png")
            count += 1
        }
        // Exercise the existing link path and first-run/paired entry without making a network call.
        let flow = OnboardingFlow(app: AppModel.shared)
        flow.welcome = WelcomeJourney()
        precondition(flow.welcome.route == .intro)
        for _ in 0..<3 { flow.welcome.forward() }
        precondition(flow.welcome.page == 3 && flow.welcome.route == .intro)
        flow.welcome.forward()
        precondition(flow.welcome.route == .setup)
        flow.welcome.back()
        precondition(flow.welcome.page == 3 && flow.welcome.route == .intro)
        precondition(WelcomeJourney(paired: true).route == .setup)
        print("PASS: first launch, next, setup, back, paired entry; \(count) screenshots")
        exit(0)
    }

    struct Harness: View {
        @ObservedObject var flow: OnboardingFlow
        var body: some View {
            MacWelcomeView(journey: $flow.welcome, onConnect: { flow.welcome.route = .setup })
        }
    }
    static func keyboardCheck(dir: String) {
        PanelSmoke.setEnhancedAccessibility(true)
        let flow = OnboardingFlow(app: AppModel.shared)
        flow.welcome = WelcomeJourney()
        let host = NSHostingView(rootView: Harness(flow: flow))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 480), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        func key(_ chars: String, _ code: UInt16) {
            RunLoop.main.run(until: Date().addingTimeInterval(0.35))
            let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber, context: nil, characters: chars, charactersIgnoringModifiers: chars, isARepeat: false, keyCode: code)!
            NSApp.postEvent(event, atStart: true)
            if let queued = NSApp.nextEvent(matching: .keyDown, until: Date().addingTimeInterval(1), inMode: .default, dequeue: true) {
                NSApp.sendEvent(queued)
            }
            RunLoop.main.run(until: Date().addingTimeInterval(0.35))
        }
        key(String(UnicodeScalar(NSRightArrowFunctionKey)!), 124)
        guard flow.welcome.page == 1 else { print("FAIL: right arrow"); exit(1) }
        key(String(UnicodeScalar(NSLeftArrowFunctionKey)!), 123)
        guard flow.welcome.page == 0 else { print("FAIL: left arrow"); exit(1) }
        key("\r", 36)
        guard flow.welcome.page == 1 else { print("FAIL: Return"); exit(1) }
        func press(_ label: String) {
            RunLoop.main.run(until: Date().addingTimeInterval(0.4))
            let root = window.attachedSheet?.contentView ?? window.contentView!
            var found: AnyObject?
            func walk(_ object: Any, _ depth: Int) {
                guard depth < 60, let element = object as? NSAccessibilityElementProtocol & NSObject else { return }
                let ax = element as AnyObject
                if ax.accessibilityLabel?() == label, ax.accessibilityRole?() == .button { found = ax }
                for child in (ax.accessibilityChildren?() ?? nil) ?? [] { walk(child, depth + 1) }
            }
            walk(root, 0)
            guard let found, found.accessibilityPerformPress?() == true else { print("FAIL: button \(label)"); exit(1) }
            RunLoop.main.run(until: Date().addingTimeInterval(0.4))
            print("PASS: press \(label)")
        }
        press("Continue")
        guard flow.welcome.page == 2 else { exit(1) }
        press("Who hears what")
        guard let privacy = window.attachedSheet?.contentView else { exit(1) }
        for dark in [false, true] {
            window.attachedSheet?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
            privacy.layoutSubtreeIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.2))
            guard let rep = privacy.bitmapImageRepForCachingDisplay(in: privacy.bounds) else { exit(1) }
            privacy.cacheDisplay(in: privacy.bounds, to: rep)
            try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "\(dir)/\(dark ? "dark" : "light")-privacy.png"))
        }
        press("Got it")
        press("Skip")
        guard flow.welcome.page == 3 else { exit(1) }
        press("I'm new to Hermes")
        guard flow.welcome.route == .newcomer else { exit(1) }
        press("Copy install command")
        guard NSPasteboard.general.string(forType: .string) == WelcomeJourney.installCommand else { exit(1) }
        press("Back")
        guard flow.welcome.route == .intro && flow.welcome.page == 3 else { exit(1) }
        press("See how it works")
        guard window.attachedSheet != nil else { exit(1) }
        press("Connect your agent")
        guard flow.welcome.route == .setup else { exit(1) }
        flow.welcome = WelcomeJourney(seen: true)
        RunLoop.main.run(until: Date().addingTimeInterval(0.4))
        press("I have Hermes")
        guard flow.welcome.route == .setup else { exit(1) }
        window.orderOut(nil)
        print("PASS: actual window arrows, Return, all welcome destinations, clipboard and sheets")
    }

    static func capture<V: View>(_ view: V, size: NSSize, dark: Bool, path: String) {
        let host = NSHostingView(rootView: view
            .environment(\.colorScheme, dark ? .dark : .light))
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: host.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        window.contentView = host
        window.orderFrontRegardless()
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
        do { try png.write(to: URL(fileURLWithPath: path)) } catch { print(error); exit(1) }
        window.orderOut(nil)
        print("welcome shot \(path)")
    }
}
