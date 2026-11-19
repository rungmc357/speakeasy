import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// SPK-540: an approval card a task sent, in the thread under the reply that sent it. The shared
/// card is the preview; under it sit the card's own buttons (Post, Send, Buy for $X, and Cancel)
/// while it waits. A click posts the exact-preview approval for its run, and the card then shows its
/// outcome in place (approved, done with its receipt, cancelled) with no buttons.
struct HistoryConfirmCard: View {
    let item: ThreadConfirms.Item
    @ObservedObject var store: HistoryStore

    var body: some View {
        let busy = store.cardBusy.contains(item.id)
        VStack(alignment: .leading, spacing: 8) {
            ViewCardView(item.card)
            if !item.buttons.isEmpty {
                HStack(spacing: 6) {
                    ForEach(item.buttons) { action in
                        HistoryPillButton(title: action.label, primary: action.style == .primary) {
                            store.runCardAction(action, on: item)
                        }
                        .accessibilityIdentifier("history-confirm-\(action.id)")
                    }
                    if busy { LiveSpinner(size: 12) }
                }
                .disabled(busy)
                .opacity(busy ? 0.6 : 1)
                // Only while it still waits: once the card settled (here or on another device), what
                // went wrong with an earlier click no longer describes it.
                if let error = store.cardErrors[item.id] {
                    Text(error).font(.system(size: 11.5)).foregroundStyle(Live.red).fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("history-confirm-error")
                }
            }
        }
        .frame(maxWidth: 520, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("history-confirm")
    }
}
