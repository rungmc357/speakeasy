import CoreGraphics

/// SPK-533: whether a chat thread keeps its newest message in view.
///
/// A thread that sits at its bottom follows what is added below (your sent message, the working
/// card, the reply as it arrives). Once the person scrolls up to read, it stays where they put it
/// until they scroll back down or send something. Sending always brings the newest message into view.
///
/// Feed it every layout report: the content's height, the visible height, and how far the content's
/// bottom is below the visible bottom (`distance`, zero or less when at the bottom).
public struct ThreadFollow: Equatable, Sendable {
    public enum Action: Equatable, Sendable { case none, scrollToEnd }

    /// Within this many points of the bottom counts as "at the bottom" (the Recent link and the
    /// padding under the last message are about 50 pt on Home).
    public static let slack: CGFloat = 80

    public private(set) var pinned = true
    private var content: CGFloat?
    private var viewport: CGFloat?

    public init() {}

    /// A layout report. `programmatic` is true while our own scroll animation is running, so its
    /// in-between positions are not read as the person scrolling up.
    public mutating func report(content: CGFloat, viewport: CGFloat, distance: CGFloat,
                                programmatic: Bool = false) -> Action {
        let grew = Self.changed(self.content, content) || Self.changed(self.viewport, viewport)
        self.content = content
        self.viewport = viewport
        // Content or window size changed: something was added (or the window resized), not a scroll.
        if grew { return pinned && distance > 0.5 ? .scrollToEnd : .none }
        if programmatic { return .none }
        // Same sizes, new position: the person scrolled.
        pinned = distance <= Self.slack
        return .none
    }

    /// The person sent something: follow again and show it.
    public mutating func sent() -> Action {
        pinned = true
        return .scrollToEnd
    }

    private static func changed(_ old: CGFloat?, _ new: CGFloat) -> Bool {
        guard let old else { return true }
        return abs(old - new) > 0.5
    }
}
