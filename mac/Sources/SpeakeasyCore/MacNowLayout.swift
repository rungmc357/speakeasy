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

    /// Menu-bar popover width (design §4.3: 360 wide, auto height up to 560).
    public static let popoverWidth: Double = 360
    /// The floating call bar (design §4.3).
    public static let callBarSize = (width: 320.0, height: 56.0)

    /// Keep server order within each group. A compact menu must not become a second board:
    /// up to 2 questions and 2 working rows, plus (off a call only) 2 new answers and one stopped
    /// row, so stopped work is never invisible here (no counting header: one card, its own line).
    public static func menuItems(_ items: [NowItem], onCall: Bool = false) -> [NowItem] {
        Array(items.filter { $0.kind == .question }.prefix(2))
            + Array(items.filter { $0.kind == .working || $0.kind == .waiting }.prefix(2))
            + (onCall ? [] : Array(items.filter { $0.kind == .newAnswer }.prefix(2))
                + Array(items.filter { $0.kind == .stopped }.prefix(1)))
    }

    /// The call bar's timer: "0:42", "12:05", "1:02:09".
    public static func callClock(seconds: Double) -> String {
        let t = max(0, Int(seconds))
        let h = t / 3600, m = (t % 3600) / 60, s = t % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }
}
