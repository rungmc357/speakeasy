import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// "Speakeasy plugin X is available" in the panel, before any call: one line of what's new and
/// **Update** (a normal job on the user's Hermes). Then "Updating…", "Updated to X", or
/// "Update didn't take" with the job one click away. Mac idiom: a quiet bordered row, brass button.
struct PluginUpdateCard: View {
    @ObservedObject var notice: PluginUpdateNotice
    var onUpdate: () -> Void
    var onOpenJob: (String) -> Void
    var onCopy: () -> Void

    var body: some View {
        let phase = notice.visiblePhase
        if phase != .none {
            HStack(alignment: .center, spacing: 10) {
                icon(phase)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title(phase)).font(.system(size: 12.5, weight: .semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    if let line = detail(phase) {
                        Text(line).font(.system(size: 11.5)).foregroundStyle(.secondary)
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 4)
                actions(phase)
            }
            .padding(.leading, 11).padding(.trailing, 8).padding(.vertical, 9)
            .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Tokens.brass.opacity(0.08)))
            .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(Tokens.brass.opacity(0.32), lineWidth: 0.75))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("plugin-update-card")
            .transition(.opacity)
        }
    }

    @ViewBuilder private func icon(_ phase: PluginUpdatePhase) -> some View {
        switch phase {
        case .updating:
            ProgressView().controlSize(.small).frame(width: 16)
        case .updated:
            Image(systemName: "checkmark.circle.fill").font(.system(size: 15)).foregroundStyle(Tokens.green)
        case .didntTake:
            Image(systemName: "exclamationmark.circle.fill").font(.system(size: 15)).foregroundStyle(Tokens.amber)
        default:
            Image(systemName: "arrow.down.circle.fill").font(.system(size: 15)).foregroundStyle(Tokens.brass)
        }
    }

    private func title(_ phase: PluginUpdatePhase) -> String {
        switch phase {
        case .available(let v, _): return "Speakeasy plugin \(v) is available"
        case .updating(let v, _): return "Updating to \(v)…"
        case .updated(let v): return "Updated to \(v)"
        case .didntTake: return "Update didn't take"
        case .none: return ""
        }
    }

    private func detail(_ phase: PluginUpdatePhase) -> String? {
        switch phase {
        case .available(_, let note):
            if let error = notice.error { return error }
            return note
        case .updating: return notice.inCall ? "It switches over when the call ends." : "It switches over by itself; nothing restarts."
        case .updated: return nil
        case .didntTake(let v, _): return "The job finished, but Hermes still runs \(notice.running ?? "the old version"), not \(v)."
        case .none: return nil
        }
    }

    @ViewBuilder private func actions(_ phase: PluginUpdatePhase) -> some View {
        switch phase {
        case .available:
            HStack(spacing: 4) {
                if notice.canStartJob {
                    PillButton(title: notice.starting ? "Starting…" : "Update", prominent: true, action: onUpdate)
                        .fixedSize().disabled(notice.starting)
                } else {
                    PillButton(title: "Copy request", prominent: true, action: onCopy).fixedSize()
                }
                close
            }
        case .updating(_, let run?):
            PillButton(title: "Open job", action: { onOpenJob(run) }).fixedSize()
        case .didntTake(_, let run):
            HStack(spacing: 4) {
                if let run { PillButton(title: "Open job", action: { onOpenJob(run) }).fixedSize() }
                close
            }
        default:
            EmptyView()
        }
    }

    private var close: some View {
        Button { withAnimation(.easeOut(duration: 0.2)) { notice.dismiss() } } label: {
            Image(systemName: "xmark").font(.system(size: 9.5, weight: .bold)).foregroundStyle(.secondary)
                .frame(width: 22, height: 22).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Not now")
        .accessibilityLabel("Dismiss")
    }
}
