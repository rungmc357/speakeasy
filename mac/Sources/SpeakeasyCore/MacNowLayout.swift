import Foundation

/// Layout and admission rules shared by the Mac Home, menu bar and floating call surface.
public enum MacNowLayout {
    public static func showsSidebar(width: Double, nowSupported: Bool) -> Bool {
        !nowSupported || width >= 1000
    }

    public static func nowWidth(windowWidth: Double) -> Double {
        min(420, max(280, windowWidth * 0.44))
    }

    public static func floats(nowSupported: Bool, callOpen: Bool, homeFrontmost: Bool) -> Bool {
        nowSupported && callOpen && !homeFrontmost
    }

    /// Keep server order within each group. A compact menu must not become a second board.
    public static func menuItems(_ items: [NowItem]) -> [NowItem] {
        Array(items.filter { $0.kind == .question }.prefix(2))
            + Array(items.filter { $0.kind == .working || $0.kind == .waiting }.prefix(2))
    }
}
