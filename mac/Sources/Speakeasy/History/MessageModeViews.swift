import SwiftUI
import SpeakeasyCore

// Steer or queue on the Mac (docs/API.md "Steer or queue"): while the open session works, a small
// choice above the field (Steer now / When it's done); ↩ sends with it, ⌘↩ with the other. Held
// messages show at the end of the thread as pending bubbles with Steer now and Cancel.

/// The composer's choice, only while a run is active.
struct HistoryMessageModeBar: View {
    @ObservedObject var store: HistoryStore

    var body: some View {
        HStack(spacing: 8) {
            Text("WHILE IT WORKS").font(.system(size: 9.5, weight: .bold)).tracking(0.7).foregroundStyle(.tertiary)
            Picker("Send", selection: $store.messageMode.selected) {
                ForEach(MessageMode.allCases, id: \.self) { mode in
                    Text(mode.label).tag(mode).help(mode.hint)
                }
            }
            .pickerStyle(.segmented).labelsHidden().controlSize(.small).fixedSize()
            .accessibilityIdentifier("history-message-mode")
            Text("⌘↩ \(store.messageMode.selected.other.label.lowercased())")
                .font(.system(size: 10.5)).foregroundStyle(.tertiary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 6)
        // ⌘↩: the other choice for this one message.
        .background(Button("") { store.send(alternate: true) }
            .keyboardShortcut(.return, modifiers: .command).opacity(0).allowsHitTesting(false))
    }
}

/// One held message at the end of the thread.
struct HistoryQueuedBubble: View {
    let message: QueuedMessage
    @ObservedObject var store: HistoryStore

    var body: some View {
        let busy = store.queuedBusy.contains(message.clientID)
        VStack(alignment: .trailing, spacing: 5) {
            Text(message.text).font(.system(size: 13.5)).foregroundStyle(Color.primary.opacity(0.8))
                .lineSpacing(2).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 13).padding(.vertical, 9)
                .background(UnevenRoundedRectangle(topLeadingRadius: 14, bottomLeadingRadius: 14, bottomTrailingRadius: 5,
                                                   topTrailingRadius: 14, style: .continuous)
                    .strokeBorder(Live.brass.opacity(0.55), style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
            HStack(spacing: 8) {
                Image(systemName: "clock").font(.system(size: 9, weight: .bold))
                Text(QueuedMessage.label)
                if let error = message.error { Text("· \(error)").foregroundStyle(Live.red).lineLimit(1) }
                if busy { LiveSpinner(size: 9) }
                Button(MessageModeCopy.steerNow) { store.steerQueued(message.clientID) }
                    .buttonStyle(.plain).foregroundStyle(Live.brassText).fontWeight(.semibold).disabled(busy)
                    .accessibilityIdentifier("history-queued-steer")
                Button(MessageModeCopy.cancel) { store.cancelQueued(message.clientID) }
                    .buttonStyle(.plain).foregroundStyle(.secondary).fontWeight(.semibold).disabled(busy)
                    .accessibilityIdentifier("history-queued-cancel")
            }
            .font(.system(size: 10.5)).foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 60)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("history-queued-bubble")
    }
}
