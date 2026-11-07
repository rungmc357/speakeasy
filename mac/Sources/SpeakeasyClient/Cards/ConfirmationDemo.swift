#if DEBUG
import SwiftUI
import SpeakeasyCore

/// Invented, network-free fixtures exercising the production shared renderer and action bar.
public enum ConfirmationDemo {
    public static func raw(state: String = "pending", type: String = "checkout") -> [String: Any] {
        let digest = String(repeating: "a", count: 64)
        var raw: [String: Any] = ["kind": "confirm", "type": type, "title": "Studio headphones", "state": state, "sha256": digest,
            "details": ["merchant": "Example Audio", "address": "Home · Northside", "delivery": "Friday, November 13",
                "payment_last4": "4242", "subtotal": "80.00", "tax": "6.40", "shipping": "0.00", "total": "86.40", "currency": "USD"],
            "items": [["name": "Studio headphones", "description": "Over-ear · wireless", "quantity": 1, "size": "Standard", "colour": "Sand"]]]
        if type == "send" {
            raw["title"] = "Reply to support"
            raw["details"] = ["recipient": "Example support", "subject": "Delivery question", "channel": "email",
                "body": "Hello,\n\nCould you confirm the delivery window for my order?\n\nThank you."]
            raw["items"] = []
        }
        raw["actions"] = state == "pending" ? [
            ["id": "approve", "label": type == "checkout" ? "Buy for $86.40" : "Send", "style": "primary", "value": digest],
            ["id": "cancel", "label": "Cancel", "style": "secondary", "value": digest]] : []
        if state == "done" {
            raw["receipt"] = ["reference": "EXAMPLE-001", "summary": type == "checkout" ? "Your headphones are ordered." : "Your message was sent.",
                              "delivery": "Arriving Friday, November 13"]
        }
        if state == "unverified" { raw["error"] = "No confirmed receipt yet. Check the service before trying again." }
        return raw
    }
}

public struct ConfirmationDemoScreen: View {
    @StateObject private var model = VoicePanelModel()
    @State private var raw: [String: Any]
    public init(state: String = "pending", type: String = "checkout") {
        _raw = State(initialValue: ConfirmationDemo.raw(state: state, type: type))
    }
    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Review & confirm").font(.largeTitle.weight(.semibold))
                ActionableCard(taskID: "example-confirmation", card: ViewCard(kind: "confirm", data: raw), model: model)
                Text("Demo · no action will be taken").font(.caption).foregroundStyle(.secondary)
            }.padding(20).frame(maxWidth: 480)
                .frame(maxWidth: .infinity)
        }
        .onAppear {
            model.capabilities = ServerCapabilities(cardActions: true)
            model.onCardAction = { _, _, action, done in
                if action.id == "edit" {
                    var details = raw["details"] as? [String: Any] ?? [:]
                    details["body"] = action.value["body"].string
                    if let subject = action.value["subject"].string { details["subject"] = subject }
                    raw["details"] = details
                } else {
                    raw = ConfirmationDemo.raw(state: action.id == "approve" ? "done" : "cancelled", type: raw["type"] as? String ?? "checkout")
                }
                done(true, "Demo updated")
            }
        }
    }
}
#if os(macOS)
import AppKit

@MainActor public enum ConfirmationShots {
    private static var windows: [NSWindow] = []
    public static func run(dir: String) {
        Task { @MainActor in
            do {
                try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
                for appearance in ["light", "dark"] {
                    NSApp.appearance = NSAppearance(named: appearance == "light" ? .aqua : .darkAqua)
                    for state in ["pending", "done", "cancelled"] {
                        let root = ConfirmationDemoScreen(state: state)
                            .environment(\.colorScheme, appearance == "light" ? .light : .dark)
                            .frame(width: 520, height: 820)
                            .background(appearance == "light" ? Color.white : Color(red: 0.06, green: 0.06, blue: 0.07))
                        let view = NSHostingView(rootView: root)
                        let size = NSSize(width: 520, height: 820)
                        view.sizingOptions = []
                        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
                        window.isReleasedWhenClosed = false
                        window.contentView = view; windows.append(window)
                        window.center(); window.makeKeyAndOrderFront(nil)
                        try await Task.sleep(for: .milliseconds(700))
                        view.layoutSubtreeIfNeeded()
                        guard let image = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw CocoaError(.fileWriteUnknown) }
                        view.cacheDisplay(in: view.bounds, to: image)
                        guard let data = image.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
                        try data.write(to: URL(fileURLWithPath: "\(dir)/confirm-mac-\(state)-\(appearance).png"))
                        window.orderOut(nil)
                    }
                }
                print("CONFIRM_SHOTS PASS: 6 mock checkout screenshots")
                NSApp.terminate(nil)
            } catch { print("CONFIRM_SHOTS FAILED: \(error)"); exit(1) }
        }
    }
}
#endif
#endif
