import SwiftUI
import SpeakeasyCore

struct ExpandedCardText: PreferenceKey {
    static var defaultValue: [String: String] = [:]
    static func reduce(value: inout [String: String], nextValue: () -> [String: String]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

/// Keeps the original label's geometry. The affordance overlays the existing leading gutter;
/// only expanded content participates in layout. Links and disclosure are separate, never nested.
struct CardDisclosure<Label: View, Detail: View>: View {
    var url: URL? = nil
    var count: Int
    var spoken: String
    @ViewBuilder var label: Label
    @ViewBuilder var detail: Detail
    @State private var expansion = CardExpansion()
    @State private var identity = UUID().uuidString
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var expanded: Bool { expansion.isExpanded("detail") }
    var body: some View {
        VStack(alignment: .leading, spacing: expanded ? 8 : 0) {
            if let destination = webURL(url) {
                Link(destination: destination) { label.contentShape(Rectangle()) }
                    .buttonStyle(.plain).help(destination.absoluteString)
            } else if count > 0 {
                Button {
                    withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.18)) { expansion.toggle("detail") }
                } label: {
                    label.contentShape(Rectangle())
                        .overlay(alignment: .leading) {
                            HStack(spacing: 1) {
                                Text("\(count)")
                                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                            }
                            .font(.system(size: 7)).foregroundStyle(.secondary)
                            .offset(x: -12)
                            .accessibilityHidden(true).allowsHitTesting(false)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(.isButton)
                .accessibilityHint("shows details")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            } else {
                label
            }
            if expanded {
                detail.padding(.leading, 12)
                    .transition(.opacity)
                    .preference(key: ExpandedCardText.self, value: [identity: spoken])
            }
        }
    }
}

/// Row adapter used inside the existing CardBox. Body is one more optional level; metadata
/// remains readable first. A real https URL always takes precedence over expanding a row.
struct CardDetailRow<Label: View>: View {
    let value: JSONValue
    let keys: [String]
    var showsLinkArrow = false
    @ViewBuilder var label: Label
    var body: some View {
        let fields = CardDetail.fields(value, keys: keys)
        CardDisclosure(url: CardDetail.destination(value), count: fields.count,
                       spoken: fields.filter { $0.id != "body" }.map { "\($0.label): \($0.text)" }.joined(separator: ", ")) {
            if showsLinkArrow, CardDetail.destination(value) != nil {
                HStack(alignment: .top, spacing: 6) {
                    label
                    Image(systemName: "arrow.up.right").font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(.secondary).padding(.top, 2)
                }
            } else {
                label
            }
        } detail: {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(fields) { field in
                    if field.id == "body" {
                        CardDisclosure(count: 1, spoken: field.text) {
                            Text("Body").font(.system(size: 11, weight: .medium))
                        } detail: {
                            Text(field.text).font(.system(size: 11.5)).textSelection(.enabled)
                        }
                    } else if field.id == "url", let url = CardDetail.destination(value) {
                        Link(field.text, destination: url).font(.system(size: 11))
                    } else {
                        Text("\(field.label): \(field.text)").font(.system(size: 11))
                            .foregroundStyle(.secondary).textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }
}
