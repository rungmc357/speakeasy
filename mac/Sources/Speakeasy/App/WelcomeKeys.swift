import AppKit
import SwiftUI

/// Only intercept navigation while this view is in the event's own window. The
/// monitor is removed with the view; sheets and other app windows keep their keys.
struct WelcomeKeys: NSViewRepresentable {
    var handle: (UInt16) -> Bool
    func makeNSView(context: Context) -> KeyView { KeyView(handle: handle) }
    func updateNSView(_ view: KeyView, context: Context) { view.handle = handle }
    static func dismantleNSView(_ view: KeyView, coordinator: ()) { view.stop() }

    final class KeyView: NSView {
        var handle: (UInt16) -> Bool
        private var monitor: Any?
        init(handle: @escaping (UInt16) -> Bool) {
            self.handle = handle
            super.init(frame: .zero)
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard let self, let window = self.window, event.window === window,
                      window.attachedSheet == nil,
                      event.modifierFlags.intersection([.command, .control, .option, .shift]).isEmpty
                else { return event }
                return self.handle(event.keyCode) ? nil : event
            }
        }
        required init?(coder: NSCoder) { nil }
        func stop() {
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
        }
        deinit { stop() }
    }
}
