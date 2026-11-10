import SwiftUI
import SpeakeasyCore

/// A running job's one-line status ("Reading Yelp"), as the Home thread, the work strip and the
/// running card show it (SPK-231 R4, B73-05).
///
/// Always ONE line, cut with an ellipsis, never wrapped onto the row below, and always the SAME
/// size whatever the step says: the line's size comes from a hidden one-line "Ag" in its font and
/// the width it is offered, never from the status text. The text is drawn in an overlay, which
/// takes no part in its parent's layout, and a new step crossfades in there. So a status change
/// re-draws the line but cannot re-lay out the row or the thread it sits in (perf73 pass 3: the old
/// push transition inserted and removed lines inside the stack's layout; 40 ms/s of hitches with
/// 10 lines changing on a long thread).
///
/// Consequence: the line has no ideal width of its own. Don't put it under `fixedSize()`; give it
/// the width it may use. Digits are monospaced, so "3 of 12" → "4 of 12" doesn't jitter sideways.
/// With Reduce Motion the new step simply replaces the old one.
public struct LiveStatusLine: View {
    public let text: String
    public var font: Font
    public var color: Color
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// How long the old step takes to fade into the new one.
    public static let crossfade: Double = 0.22

    public init(_ text: String, font: Font = .footnote, color: Color = .secondary) {
        self.text = text
        self.font = font
        self.color = color
    }

    private var lineFont: Font { font.monospacedDigit() }

    public var body: some View {
        // The size: one line of this font, as wide as offered. Independent of the text.
        Text(verbatim: "Ag").font(lineFont).lineLimit(1).hidden()
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .leading) {
                ZStack(alignment: .leading) {
                    Text(AgentMarkdown.attributed(text))
                        .font(lineFont).foregroundStyle(color)
                        .lineLimit(1).truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .id(text)
                        .transition(reduceMotion ? .identity : .opacity)
                }
                .animation(reduceMotion ? nil : .easeInOut(duration: Self.crossfade), value: text)
            }
            .clipped()
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(Text(AgentMarkdown.attributed(text)))
    }
}

#if DEBUG
#Preview("LiveStatusLine") {
    VStack(alignment: .leading, spacing: 8) {
        LiveStatusLine("Reading 3 of 12 invented pages")
        LiveStatusLine("A much longer invented step that will not fit on one line and is cut with an ellipsis", font: .subheadline)
        LiveStatusLine("", font: .headline)
    }
    .padding()
    .frame(width: 320)
}
#endif
