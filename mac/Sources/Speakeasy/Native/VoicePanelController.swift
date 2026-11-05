import AppKit
import Combine
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// Borderless, non-activating floating panel that becomes key only when clicked,
/// so Escape works only when the user is interacting with the panel.
final class VoicePanel: NSPanel {
    var onEscape: (() -> Void)?
    /// Left/right arrow: returns true when it stepped through images (then the key is consumed).
    var onArrow: ((Int) -> Bool)?
    /// Board and job-view keys (key code, characters); true = handled.
    var onKey: ((UInt16, String) -> Bool)?
    /// False while the panel is folded into the orb: no edge resizing.
    var allowsResize: (() -> Bool)?
    /// True while arrow keys or ⌘V would do something (a review card's images; a call that takes
    /// pictures and files): a click then makes the panel key so they reach it. Otherwise the panel
    /// stays non-key on click and never takes typing away from other apps.
    var wantsKeysOnClick: (() -> Bool)?
    /// ⌘V (or Edit › Paste): true when the panel took the pasteboard's pictures or files.
    var onPaste: (() -> Bool)?
    /// Called once when a drag that moved the panel ends.
    var onMoved: (() -> Void)?
    /// Edge resize: live size delta (dx wider, dy taller) and end-of-drag commit.
    /// `fromTranscript` is true when the drag started on the transcript's grab bar.
    var onResizeDrag: ((CGFloat, CGFloat, Bool) -> Void)?
    var onResizeEnded: (() -> Void)?
    /// Width of the grab band along the right and bottom edges.
    static let resizeBand: CGFloat = 8
    /// Size of the bottom-right corner zone that resizes both ways. Larger than
    /// the edge band: the panel's rounded corner leaves the outermost pixels
    /// transparent (clicks there fall through to the app behind), so a tight
    /// corner square was nearly impossible to catch.
    static let cornerZone: CGFloat = 22
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onEscape?() }
    override func animationResizeTime(_ newFrame: NSRect) -> TimeInterval { 0.18 }

    /// ⌘V while the panel is key (a click during a call makes it key): pictures and files go with
    /// the next request. Other key equivalents, and arrow keys (`sendEvent`), are untouched.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.type == .keyDown, event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
           event.charactersIgnoringModifiers?.lowercased() == "v", !(firstResponder is NSText), onPaste?() == true {
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    /// Edit › Paste reaches the panel through the responder chain.
    @objc func paste(_ sender: Any?) {
        if onPaste?() != true { NSSound.beep() }
    }

    /// Drag-to-move, handled at the window level because SwiftUI claims every
    /// mouse-down in the hosting view (so `isMovableByWindowBackground` never
    /// fired). A press anywhere except inside the scrollable transcript/Work
    /// content moves the panel once it travels more than `dragSlop`; shorter
    /// presses fall through untouched, so buttons and taps keep working.
    private struct Press { let pointer: NSPoint; let origin: NSPoint; let eligible: Bool; let edges: ResizeEdges }
    struct ResizeEdges: OptionSet {
        let rawValue: Int
        static let right = Self(rawValue: 1)
        static let bottom = Self(rawValue: 2)
        /// The grab bar under the transcript: vertical resize that also opens the transcript.
        static let transcript = Self(rawValue: 4)
    }
    private var press: Press?
    private(set) var isDraggingPanel = false
    private(set) var isResizingPanel = false

    /// Which resize edges a window point sits on (right edge, bottom edge, or the corner).
    func resizeEdges(at point: NSPoint) -> ResizeEdges {
        let w = frame.width
        guard point.x >= 0, point.x <= w, point.y >= 0, point.y <= frame.height else { return [] }
        if point.x >= w - Self.cornerZone && point.y <= Self.cornerZone { return [.right, .bottom] }
        var edges: ResizeEdges = []
        if point.x >= w - Self.resizeBand { edges.insert(.right) }
        if point.y <= Self.resizeBand { edges.insert(.bottom) }
        return edges
    }
    static let dragSlop: CGFloat = 4

    override func sendEvent(_ event: NSEvent) {
        if event.type == .keyDown, !(firstResponder is NSText),
           event.modifierFlags.intersection([.command, .option, .control]).isEmpty,
           onKey?(event.keyCode, event.charactersIgnoringModifiers ?? "") == true {
            return
        }
        // Page Up/Down, Space (Shift+Space up), Home/End scroll the board or job view.
        if event.type == .keyDown, !(firstResponder is NSText),
           event.modifierFlags.intersection([.command, .option, .control]).isEmpty,
           let key = Self.pageKey(event.keyCode, shift: event.modifierFlags.contains(.shift)),
           let target = mainScrollView(), target.page(key) {
            return
        }
        if event.type == .keyDown, !(firstResponder is NSText),
           let delta = ImageReviewLayout.arrowStep(keyCode: event.keyCode,
                                                   commandOptionControl: !event.modifierFlags.intersection([.command, .option, .control]).isEmpty),
           onArrow?(delta) == true {
            return
        }
        switch event.type {
        case .leftMouseDown:
            if !isKeyWindow, wantsKeysOnClick?() == true { makeKey() }
            isDraggingPanel = false
            isResizingPanel = false
            var edges = allowsResize?() == false ? [] : resizeEdges(at: event.locationInWindow)
            if edges.isEmpty && startsOnTranscriptGrip(event.locationInWindow) { edges = [.bottom, .transcript] }
            press = Press(pointer: convertPoint(toScreen: event.locationInWindow), origin: frame.origin,
                          eligible: !startsInScrollableContent(event.locationInWindow), edges: edges)
            // Edge presses resize; never hand them to SwiftUI as a tap.
            if !edges.isEmpty { return }
        case .leftMouseDragged:
            if let press, !press.edges.isEmpty {
                let pointer = convertPoint(toScreen: event.locationInWindow)
                isResizingPanel = true
                onResizeDrag?(press.edges.contains(.right) ? pointer.x - press.pointer.x : 0,
                              press.edges.contains(.bottom) ? press.pointer.y - pointer.y : 0,
                              press.edges.contains(.transcript))
                return
            }
            if let press, press.eligible {
                // Location is relative to the window's *current* frame, which
                // stays correct while the window moves under the pointer.
                let pointer = convertPoint(toScreen: event.locationInWindow)
                let dx = pointer.x - press.pointer.x, dy = pointer.y - press.pointer.y
                if !isDraggingPanel, hypot(dx, dy) > Self.dragSlop { isDraggingPanel = true }
                if isDraggingPanel {
                    setFrameOrigin(NSPoint(x: round(press.origin.x + dx), y: round(press.origin.y + dy)))
                    return
                }
            }
        case .leftMouseUp:
            let wasEdge = !(press?.edges.isEmpty ?? true)
            press = nil
            if wasEdge {
                isResizingPanel = false
                onResizeEnded?()
                return
            }
            if isDraggingPanel {
                isDraggingPanel = false
                // Release the press SwiftUI saw on mouse-down far outside every
                // control, so no button or tap fires at the end of a move.
                if let cancel = NSEvent.mouseEvent(with: .leftMouseUp, location: NSPoint(x: -10_000, y: -10_000),
                                                   modifierFlags: event.modifierFlags, timestamp: event.timestamp,
                                                   windowNumber: windowNumber, context: nil, eventNumber: event.eventNumber,
                                                   clickCount: event.clickCount, pressure: 0) {
                    super.sendEvent(cancel)
                }
                onMoved?()
                return
            }
        default:
            break
        }
        super.sendEvent(event)
    }

    static func pageKey(_ code: UInt16, shift: Bool) -> PanelNSScrollView.PageKey? {
        switch code {
        case 116: return .up            // Page Up
        case 121: return .down          // Page Down
        case 115: return .top           // Home
        case 119: return .bottom        // End
        case 49: return shift ? .up : .down   // Space
        default: return nil
        }
    }

    /// The scroll area that holds the board or the open job: the tallest one that can scroll.
    func mainScrollView() -> PanelNSScrollView? {
        guard let root = contentView else { return nil }
        func walk(_ v: NSView) -> [PanelNSScrollView] { ((v as? PanelNSScrollView).map { [$0] } ?? []) + v.subviews.flatMap(walk) }
        return walk(root).filter { !$0.isHiddenOrHasHiddenAncestor && $0.scrollMaxY > 0.5 }
            .max { $0.frame.height < $1.frame.height }
    }

    private func hitView(_ point: NSPoint) -> NSView? {
        guard let root = contentView else { return nil }
        return root.hitTest(root.superview?.convert(point, from: nil) ?? point)
    }

    /// Presses inside the transcript/Work scroll areas select text and scroll
    /// instead of moving the panel.
    private func startsInScrollableContent(_ point: NSPoint) -> Bool {
        guard let root = contentView else { return false }
        var view = hitView(point)
        while let v = view, v !== root {
            if v is PanelNSScrollView { return true }
            view = v.superview
        }
        return false
    }

    private func startsOnTranscriptGrip(_ point: NSPoint) -> Bool {
        var view = hitView(point)
        while let v = view {
            if v is TranscriptGripNSView { return true }
            view = v.superview
        }
        return false
    }
}

/// Invisible strip over one resize zone. Its only job is the cursor: a
/// topmost view with its own cursor-update tracking area wins over SwiftUI's
/// cursor handling underneath (setting the cursor from the hosting view's
/// mouseMoved was immediately reset, so the user never saw a resize cursor).
/// Presses are handled by `VoicePanel.sendEvent` before they reach any view.
final class ResizeHandleView: NSView {
    enum Kind { case right, bottom, corner }
    let kind: Kind
    init(kind: Kind) {
        self.kind = kind
        super.init(frame: .zero)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    static var cornerCursor: NSCursor {
        if #available(macOS 15.0, *) { return NSCursor.frameResize(position: .bottomRight, directions: .all) }
        return .crosshair
    }
    var cursor: NSCursor {
        switch kind {
        case .right: return .resizeLeftRight
        case .bottom: return .resizeUpDown
        case .corner: return Self.cornerCursor
        }
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.cursorUpdate, .mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }
    override func cursorUpdate(with event: NSEvent) { cursor.set() }
    override func mouseEntered(with event: NSEvent) { cursor.set() }
    override func mouseExited(with event: NSEvent) { NSCursor.arrow.set() }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// The panel's content: SwiftUI, with resize handles laid over the right edge,
/// bottom edge and bottom-right corner. Also the drop destination for pictures and files dragged
/// onto the panel, in a call or not (the SwiftUI content registers no drag types, so drags reach here).
final class PanelContainerView: NSView {
    let right = ResizeHandleView(kind: .right)
    let bottom = ResizeHandleView(kind: .bottom)
    let corner = ResizeHandleView(kind: .corner)
    /// The panel model drops go to (and whose `dropTargeted` lights the accent ring).
    weak var dropModel: VoicePanelModel?

    init(content: NSView) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = .clear
        content.translatesAutoresizingMaskIntoConstraints = true
        content.autoresizingMask = [.width, .height]
        addSubview(content)
        [bottom, right, corner].forEach(addSubview)   // corner last: topmost
        registerForDraggedTypes(PanelAttachments.draggedTypes)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    // MARK: Drops ("Look at this")

    /// `.copy` while the panel takes attachments and the drag carries something attachable; nothing
    /// otherwise. With no call open a drop never starts one: it waits for the next call.
    private func dropOperation(_ info: NSDraggingInfo) -> NSDragOperation {
        guard let model = dropModel, PanelAttachments.accepting(model),
              PanelAttachments.hasAttachable(info.draggingPasteboard) else {
            if dropModel?.dropTargeted == true { dropModel?.dropTargeted = false }
            return []
        }
        if !model.dropTargeted { model.dropTargeted = true }
        return .copy
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { dropOperation(sender) }
    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { dropOperation(sender) }
    override func draggingExited(_ sender: NSDraggingInfo?) { dropModel?.dropTargeted = false }
    override func draggingEnded(_ sender: NSDraggingInfo) { dropModel?.dropTargeted = false }
    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { dropOperation(sender) == .copy }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let model = dropModel, PanelAttachments.accepting(model) else { return false }
        model.dropTargeted = false
        PanelAttachments.take(from: sender.draggingPasteboard, into: model, imageName: "Dropped image")
        return true
    }

    override var isFlipped: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func layout() {
        super.layout()
        subviews.first?.frame = bounds
        let band = VoicePanel.resizeBand, zone = VoicePanel.cornerZone
        right.frame = NSRect(x: bounds.width - band, y: zone, width: band, height: max(0, bounds.height - zone - Tokens.radius))
        bottom.frame = NSRect(x: Tokens.radius, y: 0, width: max(0, bounds.width - zone - Tokens.radius), height: band)
        corner.frame = NSRect(x: bounds.width - zone, y: 0, width: zone, height: zone)
    }
}

/// the assistant's panel floats over other apps without activating the assistant. Accept the
/// first mouse-down so buttons and taps act on the first touch instead of it
/// being swallowed as a focus click.
final class PanelHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// The grab bar under the transcript. Dragging it resizes the transcript
/// (handled in `VoicePanel.sendEvent`); this view supplies the hit target and cursor.
final class TranscriptGripNSView: NSView {
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.cursorUpdate, .mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }
    override func cursorUpdate(with event: NSEvent) { NSCursor.resizeUpDown.set() }
    override func mouseEntered(with event: NSEvent) { NSCursor.resizeUpDown.set() }
    override func mouseExited(with event: NSEvent) { NSCursor.arrow.set() }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

struct TranscriptGrip: NSViewRepresentable {
    func makeNSView(context: Context) -> TranscriptGripNSView {
        let view = TranscriptGripNSView()
        view.setAccessibilityElement(true)
        view.setAccessibilityRole(.splitter)
        view.setAccessibilityLabel("Resize transcript")
        view.toolTip = "Drag to resize the transcript"
        return view
    }
    func updateNSView(_ nsView: TranscriptGripNSView, context: Context) {}
}

@MainActor
final class VoicePanelController {
    let model: VoicePanelModel
    private let panel: VoicePanel
    private let hosting: PanelHostingView<VoicePanelView>
    private var resizeScheduled = false
    /// Off-screen measurer: SwiftUI reports its ideal height for the fixed width.
    private lazy var measurer = NSHostingController(rootView: VoicePanelView(model: model))

    /// Top-left corner the user dragged the panel to (screen coordinates). `nil`
    /// means the default bottom-center spot. Growth/shrink keeps the top edge
    /// fixed once the user has placed it, so the panel no longer snaps back.
    private(set) var userTopLeft: NSPoint?
    private static let placementKey = "Speakeasy.panelTopLeft"
    private static let sizeKey = "Speakeasy.panelSize"   // "{width, extraHeight}"
    /// Size at the start of an edge drag.
    private var resizeStart: (width: CGFloat, extra: CGFloat)?

    var window: NSWindow { panel }
    var windowNumber: Int { panel.windowNumber }
    var isVisible: Bool { panel.isVisible }

    init(model: VoicePanelModel) {
        self.model = model
        panel = VoicePanel(contentRect: NSRect(x: 0, y: 0, width: Tokens.width, height: 64),
                           styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.hidesOnDeactivate = false
        // SwiftUI claims mouse-down inside the hosting view, so background
        // dragging never fired; VoicePanel.sendEvent moves the panel instead.
        panel.isMovableByWindowBackground = false
        panel.isMovable = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.worksWhenModal = true
        panel.title = "Speakeasy"
        panel.setAccessibilityLabel("Speakeasy voice panel")
        hosting = PanelHostingView(rootView: VoicePanelView(model: model))
        hosting.sizingOptions = []
        hosting.wantsLayer = true
        hosting.layer?.backgroundColor = .clear
        let container = PanelContainerView(content: hosting)
        container.dropModel = model
        panel.contentView = container
        if let saved = UserDefaults.standard.string(forKey: Self.placementKey) {
            let p = NSPointFromString(saved)
            if p != .zero { userTopLeft = p }
        }
        panel.onMoved = { [weak self] in self?.panelDidMove() }
        panel.onArrow = { [weak model] delta in model?.stepOpenReview(by: delta) ?? false }
        // Arrow keys for a review card's images and the board, and ⌘V whenever the panel takes pictures and files.
        panel.wantsKeysOnClick = { [weak model] in
            guard let model else { return false }
            return model.steppableReview != nil || model.showsBoard || model.selectedJobID != nil
                || PanelAttachments.accepting(model)
        }
        panel.onPaste = { [weak model] in
            guard let model, PanelAttachments.accepting(model) else { return false }
            PanelAttachments.take(from: .general, into: model, imageName: "Pasted image")
            return true
        }
        panel.onKey = { [weak model] code, chars in model?.handleKey(code: code, characters: chars) ?? false }
        panel.allowsResize = { [weak model] in !(model?.showsSlim ?? false) }
        if let saved = UserDefaults.standard.string(forKey: Self.sizeKey) {
            let s = NSSizeFromString(saved)
            model.panelWidth = Self.clampWidth(s.width == 0 ? Tokens.width : s.width)
            model.extraHeight = Self.clampExtra(s.height)
        }
        panel.onResizeDrag = { [weak self] dx, dy, transcript in self?.resizeDrag(dx: dx, dy: dy, transcript: transcript) }
        panel.onResizeEnded = { [weak self] in self?.resizeEnded() }
        // Any model change can change the height (e.g. tapping the caption
        // flips `captionExpanded` inside SwiftUI with no controller call). Re-
        // measure after every change, coalesced to one pass per runloop turn,
        // so the window never keeps a stale height that clips or offsets the
        // content (and its hit targets).
        modelChanges = model.objectWillChange.sink { [weak self] _ in
            MainActor.assumeIsolated { self?.setNeedsResize() }
        }
    }
    private var modelChanges: AnyCancellable?

    /// the user finished dragging: remember the spot (top-left) and clamp on screen.
    private func panelDidMove() {
        let topLeft = NSPoint(x: panel.frame.minX, y: panel.frame.maxY)
        guard topLeft != userTopLeft else { return }
        userTopLeft = topLeft
        UserDefaults.standard.set(NSStringFromPoint(topLeft), forKey: Self.placementKey)
        resize(animated: false)   // clamp so the panel cannot be lost off-screen
        if let clamped = Optional(NSPoint(x: panel.frame.minX, y: panel.frame.maxY)), clamped != topLeft {
            userTopLeft = clamped
            UserDefaults.standard.set(NSStringFromPoint(clamped), forKey: Self.placementKey)
        }
    }

    /// Forget the user's placement and size; return to bottom-center (menu: Reset Position).
    func resetPlacement() {
        userTopLeft = nil
        UserDefaults.standard.removeObject(forKey: Self.placementKey)
        UserDefaults.standard.removeObject(forKey: Self.sizeKey)
        model.panelWidth = Tokens.width
        model.extraHeight = 0
        resize(animated: false)
    }

    static func clampWidth(_ w: CGFloat) -> CGFloat { min(max(round(w), Tokens.minWidth), Tokens.maxWidth) }
    static func clampExtra(_ h: CGFloat) -> CGFloat { min(max(round(h), 0), Tokens.maxExtraHeight) }

    /// Live edge drag: the right edge sets width, the bottom edge gives the
    /// transcript/Work scroll areas more (or less) room. The top-left corner stays put.
    private func resizeDrag(dx: CGFloat, dy: CGFloat, transcript: Bool = false) {
        // The transcript's grab bar opens a collapsed transcript first, so the
        // drag has a scroll area to grow.
        if transcript && !model.captionExpanded { model.captionExpanded = true }
        if resizeStart == nil {
            resizeStart = (model.panelWidth, model.extraHeight)
            // Pin the current top-left so growth never recentres the panel.
            if userTopLeft == nil { userTopLeft = NSPoint(x: panel.frame.minX, y: panel.frame.maxY) }
        }
        guard let start = resizeStart else { return }
        let width = Self.clampWidth(start.width + dx), extra = Self.clampExtra(start.extra + dy)
        if width != model.panelWidth { model.panelWidth = width }
        if extra != model.extraHeight { model.extraHeight = extra }
        resize(animated: false)
    }

    private func resizeEnded() {
        resizeStart = nil
        UserDefaults.standard.set(NSStringFromSize(NSSize(width: model.panelWidth, height: model.extraHeight)), forKey: Self.sizeKey)
        if let userTopLeft { UserDefaults.standard.set(NSStringFromPoint(userTopLeft), forKey: Self.placementKey) }
        resize(animated: false)
    }


    /// Follow you across desktops (Spaces), or stay on the one where the call started.
    var onAllSpaces = true {
        didSet {
            panel.collectionBehavior = onAllSpaces ? [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
                                                   : [.moveToActiveSpace, .fullScreenAuxiliary, .ignoresCycle]
        }
    }

    func show() {
        resize(animated: false)
        if !panel.isVisible { panel.orderFrontRegardless() }
        setNeedsResize()   // SwiftUI may publish the new content next turn
    }

    func hide() { panel.orderOut(nil) }

    /// Render the panel's own view hierarchy to PNG (needs no Screen Recording
    /// permission). Behind-window blur cannot be sampled this way, so the
    /// snapshot composites over a neutral desktop-like backdrop.
    func snapshot(to url: URL) throws {
        resize(animated: false)
        guard let view = panel.contentView else { throw CocoaError(.fileWriteUnknown) }
        view.layoutSubtreeIfNeeded()
        let bounds = view.bounds
        guard let rep = view.bitmapImageRepForCachingDisplay(in: bounds) else { throw CocoaError(.fileWriteUnknown) }
        view.cacheDisplay(in: bounds, to: rep)
        let pad: CGFloat = 16
        let size = NSSize(width: bounds.width + pad * 2, height: bounds.height + pad * 2)
        let dark = panel.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        let image = NSImage(size: size)
        image.lockFocus()
        (dark ? NSColor(calibratedWhite: 0.18, alpha: 1) : NSColor(calibratedWhite: 0.86, alpha: 1)).setFill()
        NSRect(origin: .zero, size: size).fill()
        let panelRect = NSRect(x: pad, y: pad, width: bounds.width, height: bounds.height)
        let path = NSBezierPath(roundedRect: panelRect, xRadius: Tokens.radius, yRadius: Tokens.radius)
        (dark ? NSColor(calibratedRed: 0.11, green: 0.12, blue: 0.15, alpha: 0.92) : NSColor(calibratedWhite: 0.97, alpha: 0.92)).setFill()
        path.fill()
        path.addClip()
        rep.draw(in: panelRect)
        image.unlockFocus()
        // Website shots want retina pixels even on a machine whose screen is 1x (or locked):
        // SPEAKEASY_SNAPSHOT_SCALE=2 redraws the finished image at twice the size.
        let scale = CGFloat(Double(ProcessInfo.processInfo.environment["SPEAKEASY_SNAPSHOT_SCALE"] ?? "") ?? 1)
        if scale > 1, let big = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale),
                                                  pixelsHigh: Int(size.height * scale), bitsPerSample: 8,
                                                  samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                                  colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) {
            big.size = size
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: big)
            NSGraphicsContext.current?.imageInterpolation = .high
            // Draw the view itself again at the higher resolution, not an upscaled bitmap.
            let hi = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(bounds.width * scale),
                                      pixelsHigh: Int(bounds.height * scale), bitsPerSample: 8, samplesPerPixel: 4,
                                      hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                      bytesPerRow: 0, bitsPerPixel: 0)
            (dark ? NSColor(calibratedWhite: 0.18, alpha: 1) : NSColor(calibratedWhite: 0.86, alpha: 1)).setFill()
            NSRect(origin: .zero, size: size).fill()
            (dark ? NSColor(calibratedRed: 0.11, green: 0.12, blue: 0.15, alpha: 0.92) : NSColor(calibratedWhite: 0.97, alpha: 0.92)).setFill()
            path.fill()
            path.addClip()
            if let hi {
                hi.size = bounds.size
                view.cacheDisplay(in: bounds, to: hi)
                hi.draw(in: panelRect)
            } else {
                rep.draw(in: panelRect)
            }
            NSGraphicsContext.restoreGraphicsState()
            try big.representation(using: .png, properties: [:])?.write(to: url)
            return
        }
        guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff) else { throw CocoaError(.fileWriteUnknown) }
        try bitmap.representation(using: .png, properties: [:])?.write(to: url)
    }

    func focus() {
        show()
        panel.makeKey()
    }

    var onEscape: (() -> Void)? {
        get { panel.onEscape }
        set { panel.onEscape = newValue }
    }

    /// Coalesce layout changes into one height animation per runloop turn.
    func setNeedsResize() {
        guard !resizeScheduled else { return }
        resizeScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            resizeScheduled = false
            resize(animated: panel.isVisible && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion && !FilmFrames.filming)
        }
    }

    private func resize(animated: Bool) {
        let width = model.displayWidth
        // Lay the measurer out at the first answer, then ask again: content hosted inside the
        // board's scroll view (its own SwiftUI host, e.g. the plugin-update card) only catches up
        // in a layout pass. Measuring once returned its height from one change earlier, so the
        // panel was sized for the previous card (a band of empty space, or the board clipped).
        let first = measurer.sizeThatFits(in: NSSize(width: width, height: 2000))
        measurer.view.frame = NSRect(x: 0, y: 0, width: width, height: ceil(first.height))
        measurer.view.layoutSubtreeIfNeeded()
        let fitting = measurer.sizeThatFits(in: NSSize(width: width, height: 2000))
        let height = max(56, ceil(fitting.height))
        let frame = Self.placement(height: height, width: width, userTopLeft: userTopLeft,
                                   screens: NSScreen.screens.map(\.visibleFrame),
                                   fallback: (panel.screen ?? NSScreen.main)?.visibleFrame)
        guard let frame, frame != panel.frame, !panel.isDraggingPanel else { return }
        let animated = animated && !panel.isResizingPanel
        // `animator().setFrame` on this non-activating panel reported completion
        // without ever applying the frame, leaving the panel stuck at a stale
        // height (content clipped / trailing gap). NSWindow's own frame
        // animation applies reliably.
        panel.setFrame(frame, display: true, animate: animated)
        panel.invalidateShadow()
    }

    /// Where the panel goes for a content height. Default: bottom-center of the
    /// panel's screen. After the user drags it: keep his top-left corner (so growth
    /// extends downward and never snaps back), clamped fully onto the visible
    /// frame of whichever screen holds most of the panel.
    static func placement(height: CGFloat, width: CGFloat = Tokens.width, userTopLeft: NSPoint?,
                          screens: [NSRect], fallback: NSRect?) -> NSRect? {
        guard let userTopLeft else {
            guard let visible = fallback else { return nil }
            return NSRect(x: round(visible.midX - width / 2), y: visible.minY + 14,
                          width: width, height: min(height, visible.height - 40))
        }
        let wanted = NSRect(x: userTopLeft.x, y: userTopLeft.y - height, width: width, height: height)
        let visible = screens.max { $0.intersection(wanted).area < $1.intersection(wanted).area }
            .flatMap { $0.intersection(wanted).area > 0 ? $0 : nil }
            ?? screens.min { $0.distance(to: userTopLeft) < $1.distance(to: userTopLeft) }
            ?? fallback
        guard let visible else { return nil }
        let h = min(height, visible.height - 16)
        let w = min(width, visible.width - 8)
        let x = min(max(wanted.minX, visible.minX + 4), visible.maxX - w - 4)
        let top = min(max(userTopLeft.y, visible.minY + h + 4), visible.maxY - 4)
        return NSRect(x: round(x), y: round(top - h), width: w, height: h)
    }
}

private extension NSRect {
    var area: CGFloat { isNull || isEmpty ? 0 : width * height }
    func distance(to p: NSPoint) -> CGFloat {
        let dx = max(minX - p.x, 0, p.x - maxX), dy = max(minY - p.y, 0, p.y - maxY)
        return (dx * dx + dy * dy).squareRoot()
    }
}

/// The surface the shared call client drives: the floating panel on the Mac.
extension VoicePanelController: VoiceSurface {}

extension NativeVoiceClient {
    /// The Mac client: drives the floating panel, and can share the screen (ScreenCaptureKit stays
    /// in the app; the shared client only gets these closures).
    convenience init(config: AppConfig) {
        self.init(config: config, makeSurface: { VoicePanelController(model: $0) })
        screenPermitted = { ScreenCapture.permitted }
        captureScreen = {
            let shot = try await ScreenCapture.capture()
            return CapturedScreen(attachment: shot.attachment, appName: shot.appName)
        }
        // "Set up" leads to Speakeasy's own Settings › General › Screen (Allow, System Settings,
        // Relaunch): a grant needs a relaunch, so the panel never jumps straight to System Settings.
        openScreenSettings = { AppModel.shared.showSettings(.general) }
    }
    // swiftlint:disable:next force_cast
    var panel: VoicePanelController { surface as! VoicePanelController }
}
