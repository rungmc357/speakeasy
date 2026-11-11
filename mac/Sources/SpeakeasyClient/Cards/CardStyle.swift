import SwiftUI

/// The look every card (the catalog, the artifacts, the confirm card, the question card) draws
/// with. Cards never name a colour, corner or surface themselves: they read `\.cardStyle`.
/// One place to change the look: `CardStyle.touchDefault` (iPhone, iPad, Vision Pro) and
/// `CardStyle.macDefault`; a host can also set `.environment(\.cardStyle, …)` for one screen.
///
/// - `classic`: the build-69 look (a grey wash, brass for highlights, small capital headers).
/// - `clean`: Home v7 "Clean": neutral glass on a calm background, plain sentence-case headers,
///   neutral type, and the accent only on the one primary action and on what needs the user.
public struct CardStyle: Sendable {
    public enum Surface: Sendable { case flat, glass }

    public var name: String
    public var surface: Surface
    /// Outer card corner and inner padding.
    public var radius: CGFloat
    public var padding: CGFloat
    /// The card's fill (flat) or tint over the material (glass), its edge and its shadow.
    public var fill: Color
    public var edge: Color
    public var shadow: Color
    /// A recessed area inside a card (a check list, a code block, a tile) and its corner.
    public var well: Color
    public var wellRadius: CGFloat
    public var hairline: Color
    /// Type ramp.
    public var ink: Color
    public var secondary: Color
    public var faint: Color
    /// Highlights that aren't actions: a recommended answer, a picked row, a chart's first series,
    /// an icon tile. Brass on classic, neutral ink on clean.
    public var emphasis: Color
    /// The one primary action (fill) and the text on it.
    public var action: Color
    public var onAction: Color
    /// A secondary button's fill and text.
    public var quietFill: Color
    public var quietInk: Color
    /// Something that needs the user (text and dots).
    public var needsYou: Color
    /// Status tones (also on clean: colour only for status).
    public var good: Color
    public var warn: Color
    public var bad: Color
    public var info: Color
    /// Headers: small capitals (classic) or a sentence-case title with an icon tile (clean).
    public var capsHeaders: Bool

    public func tone(_ tone: String?) -> Color {
        switch tone {
        case "good", "ok", "done", "passed": return good
        case "warn", "needs_you": return warn
        case "bad", "failed": return bad
        case "info": return info
        default: return secondary
        }
    }
}

public extension CardStyle {
    static let classic = CardStyle(
        name: "classic", surface: .flat, radius: 14, padding: 12,
        fill: Color.primary.opacity(0.06), edge: Color.primary.opacity(JobLook.touch ? 0 : 0.06), shadow: .clear,
        well: Color.primary.opacity(0.06), wellRadius: 12, hairline: Color.primary.opacity(0.08),
        ink: .primary, secondary: .secondary, faint: Color.secondary.opacity(0.75),
        emphasis: JobLook.brass, action: JobLook.brass, onAction: JobLook.brassInk,
        quietFill: JobStyle.blueInk.opacity(0.14), quietInk: JobStyle.blueInk,
        needsYou: JobStyle.amber,
        good: .green, warn: JobLook.brass, bad: .red, info: .blue,
        capsHeaders: true)

    /// Home v7 · Clean, in build 73's G1 treatment (B73-20): a SOLID neutral sheet, white in light and
    /// a raised graphite in dark, with a hairline edge and a faint shadow. Opaque on purpose: the old
    /// see-through glass picked up the page colour behind it (blue cards at the top, peach at the
    /// bottom). The glass surface's material sits under an opaque fill, so nothing shows through.
    static let clean = CardStyle(
        name: "clean", surface: .glass, radius: 22, padding: 16,
        fill: .adaptive(dark: Color(white: 0.115), light: Color.white),
        edge: .adaptive(dark: Color.white.opacity(0.06), light: Color.black.opacity(0.05)),
        shadow: .adaptive(dark: Color.clear, light: Color.black.opacity(0.04)),
        well: .adaptive(dark: Color.white.opacity(0.065), light: Color(hex: 0x1c1a17, alpha: 0.045)), wellRadius: 14,
        hairline: .adaptive(dark: Color.white.opacity(0.08), light: Color(hex: 0x1c1a17, alpha: 0.08)),
        ink: .adaptive(dark: Color(white: 0.96), light: Color(hex: 0x1c1a17)),
        secondary: .adaptive(dark: Color.white.opacity(0.66), light: Color(hex: 0x1c1a17, alpha: 0.64)),
        faint: .adaptive(dark: Color.white.opacity(0.46), light: Color(hex: 0x1c1a17, alpha: 0.50)),
        emphasis: .adaptive(dark: Color(white: 0.92), light: Color(hex: 0x2a2723)),
        action: BrandPalette.brass, onAction: BrandPalette.onBrass,
        quietFill: .adaptive(dark: Color.white.opacity(0.10), light: Color(hex: 0x1c1a17, alpha: 0.07)),
        quietInk: .adaptive(dark: Color(white: 0.96), light: Color(hex: 0x1c1a17)),
        needsYou: BrandPalette.brass,
        good: .adaptive(dark: Color(red: 0.45, green: 0.80, blue: 0.56), light: Color(red: 0.16, green: 0.52, blue: 0.30)),
        warn: .adaptive(dark: Color(red: 0.96, green: 0.70, blue: 0.32), light: Color(hex: 0x9a5b00)),
        // G1: the only red in the app is the call's End button; failed / destructive read in a
        // warm neutral (the words say it), not red.
        bad: .adaptive(dark: Color(hex: 0xe2cdb4), light: Color(hex: 0x5a4636)),
        info: .adaptive(dark: Color(red: 0.55, green: 0.70, blue: 0.95), light: Color(hex: 0x2f5fa8)),
        capsHeaders: false)

    /// The look on iPhone, iPad and Vision Pro. Change this one line to restyle every card.
    static let touchDefault: CardStyle = .clean
    /// The Mac panel keeps its own look.
    static let macDefault: CardStyle = .classic
    static var platformDefault: CardStyle { JobLook.touch ? touchDefault : macDefault }
    /// The look in use. A theme switch sets it once at launch (before any card draws); a single
    /// screen can still override it with `.environment(\.cardStyle, …)`.
    nonisolated(unsafe) static var active: CardStyle = .platformDefault
}

public struct CardStyleKey: EnvironmentKey { public static var defaultValue: CardStyle { .active } }
/// The host draws the card's surface (the phone's card + button row share one glass): the card
/// itself only lays out its content.
public struct CardHostedKey: EnvironmentKey { public static let defaultValue = false }

public extension EnvironmentValues {
    var cardStyle: CardStyle {
        get { self[CardStyleKey.self] }
        set { self[CardStyleKey.self] = newValue }
    }
    var cardHosted: Bool {
        get { self[CardHostedKey.self] }
        set { self[CardHostedKey.self] = newValue }
    }
    /// Set by a Home widget around its card: the widget's own headline. The widget already says what
    /// the card is (its label row: the source's mark and name) and its title, so the card's body
    /// leaves out its source row and a title that echoes the headline (`TextEcho.nearDuplicate`): one headline per widget.
    var cardWidgetTitle: String? {
        get { self[CardWidgetTitleKey.self] }
        set { self[CardWidgetTitleKey.self] = newValue }
    }
}

public struct CardWidgetTitleKey: EnvironmentKey { public static let defaultValue: String? = nil }

extension EnvironmentValues {
    /// In a Home widget: its label row already names the source (mark + name).
    var inWidget: Bool { cardWidgetTitle != nil }
}

/// The card surface for a style: flat fill + hairline, or glass (material + tint + bright edge +
/// soft depth).
public struct CardSurface: View {
    var style: CardStyle
    var radius: CGFloat?
    public init(style: CardStyle, radius: CGFloat? = nil) { self.style = style; self.radius = radius }
    public var body: some View {
        let shape = RoundedRectangle(cornerRadius: radius ?? style.radius, style: .continuous)
        switch style.surface {
        case .flat:
            shape.fill(style.fill).overlay(shape.strokeBorder(style.edge))
        case .glass:
            ZStack {
                shape.fill(.ultraThinMaterial)
                shape.fill(style.fill)
                shape.strokeBorder(style.edge, lineWidth: 0.7)
            }
            #if canImport(UIKit)
            // A shadow cast from the card's rounded rectangle (`shadowPath`), not from its rendered
            // pixels: SwiftUI's `.shadow` re-rendered every card offscreen on every frame of a scroll
            // (build 73 perf pass 3). Same colour, blur and offset.
            .background(PathShadow(color: style.shadow, radius: 18, y: 8, corner: radius ?? style.radius))
            #else
            .shadow(color: style.shadow, radius: 18, y: 8)
            #endif
        }
    }
}

#if canImport(UIKit)
/// A soft drop shadow for a rounded rectangle, drawn by Core Animation from a path (no offscreen
/// pass), standing in for `.shadow(color:radius:y:)` on the glass surface (blur radius halved for
/// `CALayer.shadowRadius`; checked side by side against the SwiftUI shadow).
struct PathShadow: UIViewRepresentable {
    var color: Color
    var radius: CGFloat
    var y: CGFloat
    var corner: CGFloat

    func makeUIView(context: Context) -> ShadowView {
        let view = ShadowView()
        view.isUserInteractionEnabled = false
        view.backgroundColor = .clear
        return view
    }

    func updateUIView(_ view: ShadowView, context: Context) {
        view.color = UIColor(color)
        view.corner = corner
        view.layer.shadowRadius = radius / 2
        view.layer.shadowOffset = CGSize(width: 0, height: y)
        view.layer.shadowOpacity = 1
        view.overrideUserInterfaceStyle = context.environment.colorScheme == .dark ? .dark : .light
        view.setNeedsLayout()
    }

    final class ShadowView: UIView {
        var color: UIColor = .clear
        var corner: CGFloat = 0
        override func layoutSubviews() {
            super.layoutSubviews()
            layer.shadowColor = color.resolvedColor(with: traitCollection).cgColor
            layer.shadowPath = UIBezierPath(roundedRect: bounds, cornerRadius: corner).cgPath
        }
    }
}
#endif

public extension View {
    /// The card's own surface, unless a host already draws one (`cardHosted`).
    func cardSurface(_ style: CardStyle, hosted: Bool = false, padding: CGFloat? = nil) -> some View {
        modifier(CardSurfaceModifier(style: style, hosted: hosted, padding: padding))
    }
    /// A recessed area inside a card.
    func cardWell(_ style: CardStyle, padding: CGFloat = 12) -> some View {
        self.padding(padding).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: style.wellRadius, style: .continuous).fill(style.well))
    }
}

struct CardSurfaceModifier: ViewModifier {
    var style: CardStyle
    var hosted: Bool
    var padding: CGFloat?
    func body(content: Content) -> some View {
        if hosted {
            content
        } else {
            content.padding(padding ?? style.padding)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(CardSurface(style: style))
        }
    }
}

/// A button inside a card: the primary one filled with the accent, the others quiet. Used by the
/// cards that carry their own buttons (and the phone's/Mac's button rows), so every button matches.
public struct CardPillButton: View {
    public enum Kind: Sendable { case primary, quiet, destructive }
    var title: String
    var icon: String?
    var kind: Kind
    var wide: Bool
    var compact: Bool
    var action: () -> Void
    @Environment(\.cardStyle) private var style
    public init(_ title: String, icon: String? = nil, kind: Kind = .quiet, wide: Bool = false, compact: Bool = false,
                action: @escaping () -> Void) {
        self.title = title; self.icon = icon; self.kind = kind; self.wide = wide; self.compact = compact; self.action = action
    }
    public var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if let icon { Image(systemName: icon).font(.system(size: compact ? 10.5 : 13, weight: .semibold)) }
                Text(title).font(.system(size: compact ? 11.5 : 15, weight: .semibold)).lineLimit(1)
            }
            .padding(.horizontal, compact ? 10 : 16)
            .frame(minHeight: compact ? 26 : 40)
            .frame(maxWidth: wide ? .infinity : nil)
            .foregroundStyle(fg)
            .background(Capsule().fill(bg))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
    }
    private var fg: Color {
        switch kind { case .primary: return style.onAction; case .quiet: return style.quietInk; case .destructive: return style.bad }
    }
    private var bg: Color {
        switch kind { case .primary: return style.action; case .quiet: return style.quietFill; case .destructive: return style.bad.opacity(0.16) }
    }
}

// MARK: - Layout grid (Clean)

/// The Clean layout grid: one spacing scale, one set of corners and glyph sizes for every Home
/// surface (thread, cards, composer, Work, the model sheet, the call card), so an edge fixed in one
/// place lines up everywhere. An 8-pt grid with 4-pt half steps; never an ad-hoc 38 or 18.
public enum CleanGrid {
    /// Spacing scale.
    public static let xxs: CGFloat = 2
    public static let xs: CGFloat = 4
    public static let s: CGFloat = 8
    public static let m: CGFloat = 12
    public static let l: CGFloat = 16
    public static let xl: CGFloat = 24
    public static let xxl: CGFloat = 32

    /// The page's side margin: thread, tracker pill, composer, Work and sheets all start here.
    public static let margin: CGFloat = 16
    /// A card's inner padding, all four sides (matches `CardStyle.clean.padding`).
    public static let cardPadding: CGFloat = 16

    /// Corners: cards and sheet cards; inner wells, option rows and tiles; glyph tiles; tags.
    public static let cardRadius: CGFloat = 22
    public static let innerRadius: CGFloat = 14
    public static let tileRadius: CGFloat = 9
    /// Bubbles (yours, a sent clip's caption) use the same continuous corner as inner wells plus
    /// the text's line: tall enough to read as a pill on one line, a soft card on several.
    public static let bubbleRadius: CGFloat = 20

    /// Glyph tiles beside a card's title and the circles in list rows.
    public static let glyphTile: CGFloat = 28
    public static let rowGlyph: CGFloat = 32
    public static let heroGlyph: CGFloat = 44
    /// The gap between a glyph tile and its text.
    public static let glyphGap: CGFloat = 12
    /// Where a card's text starts after its glyph tile: everything under the title (options,
    /// tiles, Reply · Later, a fix button) starts here too, so it lines up with the title.
    public static let textInset: CGFloat = glyphTile + glyphGap
    /// A list row's text after its glyph circle (row padding + circle + gap): separators start here.
    public static let rowTextInset: CGFloat = cardPadding + rowGlyph + glyphGap

    /// Minimum height of a pill button and of a quiet text action row.
    public static let controlHeight: CGFloat = 36
}

// MARK: - Shared parts (the Clean card language; classic draws them in its own sizes)

/// The rounded tile behind a card's title glyph: the well plus a hairline, so a pale light-mode
/// tile still reads as a tile (the well alone nearly vanished on a near-white card).
public struct GlyphTileBackground: View {
    @Environment(\.cardStyle) private var style
    public init() {}
    public var body: some View {
        RoundedRectangle(cornerRadius: CleanGrid.tileRadius, style: .continuous).fill(style.well)
            .overlay(RoundedRectangle(cornerRadius: CleanGrid.tileRadius, style: .continuous).strokeBorder(style.hairline, lineWidth: 0.75))
    }
}

/// Every card's first line on clean: a small glyph tile, a title, a quiet trailing note.
public struct CardTitleRow: View {
    var icon: String
    var title: String
    var note: String?
    var tint: Color?
    var lines: Int
    @Environment(\.cardStyle) private var style
    public init(icon: String, title: String, note: String? = nil, tint: Color? = nil, lines: Int = 1) {
        self.icon = icon; self.title = title; self.note = note; self.tint = tint; self.lines = lines
    }
    public var body: some View {
        HStack(spacing: CleanGrid.glyphGap) {
            Image(systemName: icon).font(.system(size: 13, weight: .semibold)).foregroundStyle(tint ?? style.secondary)
                .frame(width: CleanGrid.glyphTile, height: CleanGrid.glyphTile)
                .background(GlyphTileBackground())
            Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(style.ink)
                .lineLimit(lines).fixedSize(horizontal: false, vertical: lines > 1)
            Spacer(minLength: 8)
            if let note { Text(note).font(.footnote).foregroundStyle(style.faint).lineLimit(1) }
        }
    }
}

/// A small fact chip: a tinted glyph and neutral text on a well ("In review", "Conflicts").
public struct CardTag: View {
    var icon: String?
    var text: String
    var tint: Color?
    @Environment(\.cardStyle) private var style
    public init(icon: String? = nil, text: String, tint: Color? = nil) { self.icon = icon; self.text = text; self.tint = tint }
    public var body: some View {
        HStack(spacing: 5) {
            if let icon { Image(systemName: icon).font(.caption2.weight(.bold)).foregroundStyle(tint ?? style.secondary) }
            Text(text).font(.footnote.weight(.medium)).foregroundStyle(style.ink).lineLimit(1)
        }
        .padding(.horizontal, 9).padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(style.well))
    }
}

/// One quiet fact with a glyph ("Mara K. · High priority").
public struct CardFactLine: View {
    var icon: String
    var text: String
    @Environment(\.cardStyle) private var style
    public init(icon: String, text: String) { self.icon = icon; self.text = text }
    public var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon).font(.caption).foregroundStyle(style.faint).frame(width: 16)
            Text(text).font(.footnote).foregroundStyle(style.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// The quiet mark beside a running status line: a still dot, never a ring, spinner or percent.
public struct CardLiveDot: View {
    var color: Color?
    @Environment(\.cardStyle) private var style
    public init(color: Color? = nil) { self.color = color }
    public var body: some View {
        let c = color ?? style.good
        Circle().fill(c).frame(width: 6, height: 6)
            .background(Circle().fill(c.opacity(0.25)).frame(width: 12, height: 12))
            .frame(width: 12, height: 12)
            .accessibilityHidden(true)
    }
}
