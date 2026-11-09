import AppKit
import SwiftUI

/// AppKit-backed vertical scroller for the panel's transcript and Work view.
///
/// SwiftUI's `ScrollView` inside the non-activating panel re-laid out on every
/// live transcript delta / work tick and the transcript forced itself back to
/// the bottom on each change, so the user could not read back. This wrapper owns a
/// real `NSScrollView` whose document is a SwiftUI hosting view:
/// - wheel/trackpad scrolling is plain AppKit, which works in a non-key panel
///   of an inactive (accessory) app;
/// - re-rendering the content never moves the reading position;
/// - `followsBottom` keeps live text in view only while the user is already at the
///   bottom (scroll up to read back; scroll to the end to resume following).
struct PanelScrollView<Content: View>: NSViewRepresentable {
    var maxHeight: CGFloat
    /// Height the user sized the panel to; short content keeps this room instead of collapsing.
    var minHeight: CGFloat = 0
    var followsBottom = false
    /// A rect in the content's coordinates (top-left origin) to bring into view when it changes,
    /// e.g. the keyboard-focused tile.
    var reveal: CGRect?
    var content: Content

    init(maxHeight: CGFloat, minHeight: CGFloat = 0, followsBottom: Bool = false, reveal: CGRect? = nil,
         @ViewBuilder content: () -> Content) {
        self.maxHeight = maxHeight
        self.minHeight = min(minHeight, maxHeight)
        self.followsBottom = followsBottom
        self.reveal = reveal
        self.content = content()
    }

    func makeNSView(context: Context) -> PanelNSScrollView {
        let view = PanelNSScrollView()
        view.followsBottom = followsBottom
        view.setContent(AnyView(content))
        return view
    }

    func updateNSView(_ view: PanelNSScrollView, context: Context) {
        view.followsBottom = followsBottom
        view.setContent(AnyView(content))
        view.reveal(reveal)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: PanelNSScrollView, context: Context) -> CGSize? {
        let width = proposal.width ?? Tokens.width
        return CGSize(width: width, height: min(max(nsView.contentHeight(for: width), minHeight), maxHeight))
    }
}

/// Plain flipped container so the scroll view's document is an ordinary
/// NSView: with an NSHostingView as the document directly, SwiftUI claims the
/// wheel events and the clip view never moves.
private final class FlippedDocument: NSView {
    override var isFlipped: Bool { true }
}

final class PanelNSScrollView: NSScrollView {
    var followsBottom = false
    private let hosting = NSHostingController(rootView: AnyView(EmptyView()))
    private let document = FlippedDocument()
    private var laidOut = false

    init() {
        super.init(frame: .zero)
        drawsBackground = false
        borderType = .noBorder
        hasVerticalScroller = true
        hasHorizontalScroller = false
        autohidesScrollers = true
        scrollerStyle = .overlay
        horizontalScrollElasticity = .none
        verticalScrollElasticity = .allowed
        hosting.sizingOptions = []
        hosting.view.translatesAutoresizingMaskIntoConstraints = true
        hosting.view.autoresizingMask = [.width, .height]
        document.addSubview(hosting.view)
        documentView = document
        contentView.drawsBackground = false
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    private var revealed: CGRect?

    /// Scroll just enough to show `rect` (content coordinates, flipped) with a little margin.
    func reveal(_ rect: CGRect?) {
        guard let rect, rect != revealed else { return }
        revealed = rect
        DispatchQueue.main.async { [weak self] in
            guard let self, let doc = self.documentView else { return }
            self.layoutSubtreeIfNeeded()
            let visible = self.contentView.bounds
            var y = visible.origin.y
            // Near the top, go all the way so the section label above the first tile shows too.
            if rect.minY - 8 < visible.minY { y = rect.minY < 60 ? 0 : rect.minY - 28 }
            else if rect.maxY + 8 > visible.maxY { y = rect.maxY + 8 - visible.height }
            y = min(max(0, y), max(0, doc.frame.height - visible.height))
            guard abs(y - visible.origin.y) > 0.5 else { return }
            self.contentView.scroll(to: NSPoint(x: 0, y: y))
            self.reflectScrolledClipView(self.contentView)
        }
    }

    /// Page Up/Down, Space (Shift+Space up), Home/End. Returns false when there's nothing to scroll.
    @discardableResult
    func page(_ key: PageKey) -> Bool {
        guard let doc = documentView else { return false }
        let visible = contentView.bounds
        let maxY = max(0, doc.frame.height - visible.height)
        guard maxY > 0.5 else { return false }
        let step = max(40, visible.height - 40)
        let y: CGFloat
        switch key {
        case .up: y = visible.origin.y - step
        case .down: y = visible.origin.y + step
        case .top: y = 0
        case .bottom: y = maxY
        }
        contentView.scroll(to: NSPoint(x: 0, y: min(max(0, y), maxY)))
        reflectScrolledClipView(contentView)
        return true
    }
    enum PageKey { case up, down, top, bottom }

    var scrollMaxY: CGFloat { max(0, (documentView?.frame.height ?? 0) - contentView.bounds.height) }

    /// Within 2pt of the end, or not yet scrollable.
    var isAtBottom: Bool {
        let doc = documentView?.frame.height ?? 0
        return contentView.bounds.maxY >= doc - 2
    }

    func setContent(_ view: AnyView) {
        let wasAtBottom = isAtBottom
        hosting.rootView = view
        relayout(wasAtBottom: wasAtBottom)
    }

    func contentHeight(for width: CGFloat) -> CGFloat {
        ceil(hosting.sizeThatFits(in: NSSize(width: width, height: .greatestFiniteMagnitude)).height)
    }

    override func layout() {
        let wasAtBottom = isAtBottom
        super.layout()
        relayout(wasAtBottom: wasAtBottom)
    }

    private func relayout(wasAtBottom: Bool) {
        let width = contentSize.width
        guard width > 0, let doc = documentView else { return }
        let height = contentHeight(for: width)
        let oldY = contentView.bounds.origin.y
        if doc.frame.size != NSSize(width: width, height: height) {
            doc.setFrameSize(NSSize(width: width, height: height))
        }
        hosting.view.frame = doc.bounds
        let maxY = max(0, height - contentView.bounds.height)
        let target = (followsBottom && (wasAtBottom || !laidOut)) ? maxY : min(max(0, oldY), maxY)
        laidOut = true
        if abs(contentView.bounds.origin.y - target) > 0.5 {
            contentView.scroll(to: NSPoint(x: 0, y: target))
            reflectScrolledClipView(contentView)
        }
    }
}
