import SwiftUI

/// Card body text follows the phone's text size (SPK-498). The card views were drawn at fixed point
/// sizes (`.system(size: 13)`), so at the largest text a widget's label, headline and buttons grew
/// while the rows inside the card (a status list's names and values, a comparison's prices) stayed
/// at 13 pt. `cardFont` keeps each view's designed size at the default text size and scales it on
/// the system body ramp (17 pt at Large, 28 pt at the first accessibility size), capped at
/// `CardType.cap` like the Home widget around it, so a card stays a card at the very largest sizes.
/// Symbols in fixed tiles keep their fixed sizes. The Mac panel reads Large, so nothing moves there.
public enum CardType {
    /// The largest text size card bodies grow to (the same cap as the Home widget and its buttons).
    public static let cap: DynamicTypeSize = .accessibility1

    /// How much a card's designed size grows at `size`: the body style's ramp relative to Large.
    public static func factor(_ size: DynamicTypeSize) -> CGFloat {
        let body: CGFloat
        switch min(size, cap) {
        case .xSmall: body = 14
        case .small: body = 15
        case .medium: body = 16
        case .large: body = 17
        case .xLarge: body = 19
        case .xxLarge: body = 21
        case .xxxLarge: body = 23
        default: body = 28
        }
        return body / 17
    }

    public static func size(_ base: CGFloat, at type: DynamicTypeSize) -> CGFloat {
        (base * factor(type)).rounded(.toNearestOrAwayFromZero)
    }
}

struct CardFontModifier: ViewModifier {
    @Environment(\.dynamicTypeSize) private var type
    let size: CGFloat
    let weight: Font.Weight?
    let design: Font.Design?
    let digits: Bool
    let italic: Bool

    func body(content: Content) -> some View {
        // Keep the designed fractional sizes (12.5, 11.5) exact at Large.
        let scaled = type == .large ? size : CardType.size(size, at: type)
        var font = Font.system(size: scaled, weight: weight, design: design)
        if digits { font = font.monospacedDigit() }
        if italic { font = font.italic() }
        return content.font(font)
    }
}

public extension View {
    /// A card body font at its designed size, grown with the user's text size up to `CardType.cap`.
    func cardFont(_ size: CGFloat, weight: Font.Weight? = nil, design: Font.Design? = nil,
                  digits: Bool = false, italic: Bool = false) -> some View {
        modifier(CardFontModifier(size: size, weight: weight, design: design, digits: digits, italic: italic))
    }
}
