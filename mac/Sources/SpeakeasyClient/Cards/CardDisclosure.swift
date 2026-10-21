import SwiftUI
import SpeakeasyCore

/// What the open disclosures in a card show, keyed per disclosure: `ViewCardView` appends it to
/// the card's accessibility label so VoiceOver hears what the user opened.
struct ExpandedCardText: PreferenceKey {
    static var defaultValue: [String: String] = [:]
    static func reduce(value: inout [String: String], nextValue: () -> [String: String]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

/// In-place exploration of one row: a real https `url` opens with Link; otherwise a click shows
/// the row in full (the label gets `expanded` to drop its line limits) plus any `detail` the row
/// doesn't draw collapsed. Local only: never selects, answers or sends anything to the server.
/// The chevron sits in the card's leading padding so the collapsed row keeps its geometry.
struct CardDisclosure<Label: View, Detail: View>: View {
    var url: URL? = nil
    var expandable: Bool
    var spoken: String
    /// Told the new state after each toggle (inside the same animation), for a card whose other
    /// cells follow this row, like a comparison's values.
    var onToggle: ((_ expanded: Bool) -> Void)? = nil
    @ViewBuilder var label: (_ expanded: Bool) -> Label
    @ViewBuilder var detail: Detail
    @State private var expansion = CardExpansion()
    @State private var identity = UUID().uuidString
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var expanded: Bool { expansion.isExpanded("row") }
    var body: some View {
        VStack(alignment: .leading, spacing: expanded ? 6 : 0) {
            if let destination = webURL(url) {
                Link(destination: destination) { label(false).contentShape(Rectangle()) }
                    .buttonStyle(.plain).help(destination.absoluteString)
            } else if expandable {
                Button {
                    withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.18)) {
                        expansion.toggle("row")
                        onToggle?(expanded)
                    }
                } label: {
                    label(expanded).contentShape(Rectangle())
                        .overlay(alignment: .topLeading) {
                            Image(systemName: expanded ? "chevron.down" : "chevron.right")
                                .font(.system(size: 8, weight: .semibold)).foregroundStyle(.tertiary)
                                .frame(width: 10).offset(x: -11, y: 3)
                                .accessibilityHidden(true).allowsHitTesting(false)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityHint(expanded ? "Hides details" : "Shows details")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            } else {
                label(false)
            }
            if expanded {
                detail
                    .transition(.opacity)
            }
        }
        // Adds to (not replaces) what disclosures nested inside report.
        .transformPreference(ExpandedCardText.self) { if expanded, !spoken.isEmpty { $0[identity] = spoken } }
    }
}

extension CardDisclosure where Detail == EmptyView {
    /// Expanding only lifts the label's line limits.
    init(url: URL? = nil, expandable: Bool, spoken: String, onToggle: ((_ expanded: Bool) -> Void)? = nil,
         @ViewBuilder label: @escaping (_ expanded: Bool) -> Label) {
        self.init(url: url, expandable: expandable, spoken: spoken, onToggle: onToggle, label: label, detail: { EmptyView() })
    }
}

/// A row of a card's data (`value`): `shows` are the fields the row draws (possibly truncated),
/// `reveals` the supplied fields it only draws open. Blank or missing fields never appear, so a
/// row with nothing to say isn't expandable. A message `body` is one more disclosure inside.
struct CardDetailRow<Label: View>: View {
    let value: JSONValue
    var shows: [String]
    var reveals: [String] = []
    /// False when the row keeps its own trailing link (places keep Maps), so the row itself expands.
    var linksRow = true
    var showsLinkArrow = false
    @ViewBuilder var label: (_ expanded: Bool) -> Label

    var body: some View {
        let shown = CardDetail.fields(value, keys: shows)
        let revealed = CardDetail.fields(value, keys: reveals)
        let url = linksRow ? CardDetail.destination(value) : nil
        CardDisclosure(url: url, expandable: !(shown + revealed).isEmpty,
                       spoken: (shown + revealed).filter { $0.id != "body" }.map(\.text).joined(separator: ", ")) { expanded in
            if showsLinkArrow, url != nil {
                HStack(alignment: .top, spacing: 6) {
                    label(false)
                    Image(systemName: "arrow.up.right").font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(.secondary).padding(.top, 2)
                }
            } else {
                label(expanded)
            }
        } detail: {
            if !revealed.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(revealed) { field in
                        if field.id == "body" {
                            CardDisclosure(expandable: true, spoken: field.text) { _ in
                                Text("Message").font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary)
                            } detail: {
                                Text(field.text).font(.system(size: 12)).textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        } else {
                            Text("\(field.label): \(field.text)").font(.system(size: 11.5))
                                .foregroundStyle(.secondary).textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
        }
    }
}
