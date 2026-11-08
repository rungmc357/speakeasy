import SwiftUI
import SpeakeasyCore

/// Shared neutral event presentation on phone, tablet and Mac. No user/assistant bubble styling.
public struct AutomaticEventCard: View {
    public let event: HistoryAutomaticEvent
    public init(event: HistoryAutomaticEvent) { self.event = event }
    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(event.label, systemImage: "tray.and.arrow.down")
                .font(.caption).foregroundStyle(.secondary)
            Text(AgentMarkdown.attributed(event.title)).font(.headline)
                .fixedSize(horizontal: false, vertical: true)
            Text(AgentMarkdown.attributed(event.summary)).font(.subheadline).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let url = event.url {
                Link("View task", destination: url).font(.subheadline)
            }
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 16))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("automatic-event-card")
    }
}
