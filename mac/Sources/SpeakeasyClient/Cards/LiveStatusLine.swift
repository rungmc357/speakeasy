import SwiftUI
import SpeakeasyCore

/// A running job's one-line status ("Reading Yelp"), as the Home thread, the work strip and the
/// running card show it (SPK-231 R4). Always ONE line, cut with an ellipsis, never wrapped onto the
/// row below. A new line pushes the old one out of a clipped one-line frame instead of crossfading on
/// top of it: the crossfade briefly drew both lines over each other (and a wrapped line overlapped
/// the next row). With Reduce Motion the new line simply replaces the old one.
public struct LiveStatusLine: View {
    public let text: String
    public var font: Font
    public var color: Color
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    public init(_ text: String, font: Font = .footnote, color: Color = .secondary) {
        self.text = text
        self.font = font
        self.color = color
    }

    public var body: some View {
        ZStack(alignment: .leading) {
            Text(AgentMarkdown.attributed(text))
                .font(font).foregroundStyle(color)
                .lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .id(text)
                .transition(reduceMotion ? .identity : .push(from: .bottom))
        }
        .clipped()
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.28), value: text)
        .accessibilityElement(children: .combine)
    }
}
