import SwiftUI
#if canImport(UIKit)
import UIKit
#elseif canImport(AppKit)
import AppKit
#endif

// One colour per look (dark / light), resolved by the system for whatever the view is drawn in,
// so a token written once works in both. Light values keep the brand (warm paper, ink, a deeper
// brass) with WCAG AA contrast for text on the light background.

extension Color {
    /// `dark` in Dark Mode, `light` in Light Mode.
    public static func adaptive(dark: Color, light: Color) -> Color {
        #if os(visionOS)
        return dark
        #elseif canImport(UIKit)
        return Color(UIColor { $0.userInterfaceStyle == .light ? UIColor(light) : UIColor(dark) })
        #elseif canImport(AppKit)
        return Color(NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .aqua ? NSColor(light) : NSColor(dark)
        })
        #else
        return dark
        #endif
    }

    /// An sRGB colour from a hex literal (`0xd4a24c`).
    public init(hex: UInt32, alpha: Double = 1) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xff) / 255, green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255, opacity: alpha)
    }
}

/// The brand's two looks, shared by the Mac panel and the touch apps.
public enum BrandPalette {
    /// Brass: on dark #d4a24c; on paper a deeper #8c5a12 (5.2:1 on #f8f3ea, AA for text).
    public static let brass = Color.adaptive(dark: Color(hex: 0xd4a24c), light: Color(hex: 0x8c5a12))
    /// Text and symbols on a brass fill: near-black ink on dark's brass, ivory on light's deep brass.
    public static let onBrass = Color.adaptive(dark: Color(hex: 0x17110a), light: Color(hex: 0xfff8ec))
    /// Amber (needs you): on paper a burnt amber that still reads as warm (5.4:1).
    public static let amber = Color.adaptive(dark: Color(red: 0.97, green: 0.745, blue: 0.44), light: Color(hex: 0x8f5300))
    public static let green = Color.adaptive(dark: Color(red: 0.45, green: 0.82, blue: 0.56), light: Color(hex: 0x2a7340))
    public static let red = Color.adaptive(dark: Color(red: 0.93, green: 0.36, blue: 0.36), light: Color(hex: 0xb3261e))
    /// Warm paper and ink.
    public static let paper = Color(hex: 0xf8f3ea)
    public static let ink = Color(hex: 0x1e1912)
}
