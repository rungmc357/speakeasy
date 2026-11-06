import SwiftUI
import SpeakeasyCore

// Steer or queue on the Mac (docs/API.md "Steer or queue"): nothing sits above the field while
// typing. ↩ / Send with nothing running is a plain send; while the open session's task runs it opens
// a small pop-up at the Send button with Steer now (↩) and Queue for after, and nothing is sent until
// one is picked (Esc or a click outside closes it, the words stay in the field). Held messages show
// at the end of the thread as pending bubbles with Steer now and Cancel.

/// The pop-up's two choices, anchored to Send.
struct HistorySendModeChoices: View {
    @ObservedObject var store: HistoryStore

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            row(.steer).keyboardShortcut(.defaultAction)
            row(.queue)
        }
        .padding(6)
        .frame(width: 260)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("history-send-choice")
    }

    private func row(_ mode: MessageMode) -> some View {
        Button { store.send(picked: mode) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: mode == .steer ? "arrow.turn.down.right" : "clock")
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(Live.brassText).frame(width: 16)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(mode.label).font(.system(size: 13, weight: .semibold)).foregroundStyle(.primary)
                        if mode == .steer { Text("↩").font(.system(size: 11)).foregroundStyle(.tertiary) }
                    }
                    Text(mode.hint).font(.system(size: 11.5)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(HistoryChoiceRowStyle())
        .help(mode.hint)
        .accessibilityIdentifier(mode == .steer ? "history-send-steer" : "history-send-queue")
    }
}

private struct HistoryChoiceRowStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(configuration.isPressed ? 0.1 : 0.0001)))
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
