import SwiftUI
import SpeakeasyCore

/// The session header's usage line ("About $1.84 · estimate"): click it for what the chat used —
/// tokens, model calls, a per-model split and the estimate caveat — in a small card under the header.
struct HistoryUsageLine: View {
    let usage: ChatUsage
    let sessionID: String
    @ObservedObject var store: HistoryStore

    private var key: String { "\(sessionID)|usage" }

    var body: some View {
        let open = store.openSections.contains(key)
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.18)) {
                    if open { store.openSections.remove(key) } else { store.openSections.insert(key) }
                }
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: usage.symbol).font(.system(size: 9.5, weight: .semibold))
                    Text(usage.summary).fixedSize(horizontal: false, vertical: true)
                    Image(systemName: "chevron.right").font(.system(size: 7.5, weight: .bold))
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
                .font(.system(size: 11)).foregroundStyle(Color.primary.opacity(0.5))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("What this chat used, estimated from the records on your Hermes")
            .accessibilityLabel(usage.accessibilityText)
            .accessibilityIdentifier("history-usage")
            if open { HistoryUsageDetails(usage: usage).transition(.opacity) }
        }
    }
}

/// "Claude Opus 5.5 · about 22% of context used · 3 min": which model ran it and how full its context
/// got, quiet like a footer. Hover for the token count, the peak and the estimate note.
struct ContextUseLine: View {
    let context: ContextUse
    let text: String
    var size: CGFloat = 11

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: context.symbol).font(.system(size: size - 1.5, weight: .semibold))
            Text(text).lineLimit(2).fixedSize(horizontal: false, vertical: true)
        }
        .font(.system(size: size))
        .foregroundStyle(context.isNearlyFull ? AnyShapeStyle(Live.brass) : AnyShapeStyle(Color.primary.opacity(0.45)))
        .help(context.detail)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(context.accessibilityText)
        .accessibilityIdentifier("context-use")
    }
}

struct HistoryUsageDetails: View {
    let usage: ChatUsage

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("USAGE · ESTIMATE").font(.system(size: 9.5, weight: .bold)).tracking(0.9).foregroundStyle(.secondary)
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 16, verticalSpacing: 5) {
                ForEach(usage.lines) { line in
                    GridRow {
                        Text(line.label).foregroundStyle(.secondary)
                        Text(line.value).foregroundStyle(.primary).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .font(.system(size: 12))
            if usage.models.count > 1 {
                Divider().overlay(Live.hairline)
                Text("BY MODEL").font(.system(size: 9.5, weight: .bold)).tracking(0.9).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(usage.models) { m in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(m.name).font(.system(size: 12, weight: .medium))
                            Spacer(minLength: 8)
                            Text(m.value).font(.system(size: 11.5)).foregroundStyle(.secondary)
                        }
                    }
                }
            } else if let m = usage.models.first {
                Text("Model: \(m.name)").font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            Text(usage.note).font(.system(size: 10.5)).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: 420, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.045)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Live.hairline))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("history-usage-details")
    }
}
