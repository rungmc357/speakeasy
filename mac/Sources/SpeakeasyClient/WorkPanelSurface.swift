import SwiftUI

/// The one look for a job's work panel (SPK-492): Home's live panel and the chat's work card draw
/// the same surface, so the "work in progress" block never has two looks. Home's accepted values:
/// 18 pt continuous corners, the foreground ink at 5% and a hairline edge (white 8% in dark, ink 10%
/// in light). It is a faint tint over the page's own backdrop, never a solid card under it; with
/// Reduce Transparency on it stays the same tint, since there is no blur or see-through material
/// to remove (the backdrop under it is the page's opaque canvas).
public enum WorkPanelStyle {
    public static let radius: CGFloat = 18
    public static let fill = Color.adaptive(dark: Color.white.opacity(0.05), light: Color(hex: 0x1e1912, alpha: 0.05))
    public static let edge = Color.adaptive(dark: Color.white.opacity(0.08), light: Color(hex: 0x1e1912, alpha: 0.10))
    public static let edgeWidth: CGFloat = 1
    public static var shape: RoundedRectangle { RoundedRectangle(cornerRadius: radius, style: .continuous) }
}

/// The work panel's surface as a background view: `.background(WorkPanelSurface())`.
public struct WorkPanelSurface: View {
    public init() {}
    public var body: some View {
        WorkPanelStyle.shape.fill(WorkPanelStyle.fill)
            .overlay(WorkPanelStyle.shape.strokeBorder(WorkPanelStyle.edge, lineWidth: WorkPanelStyle.edgeWidth))
    }
}
