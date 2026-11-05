import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// "Speakeasy plugin X is available" on the board, under the greeting (part of the scrolling
/// content, never over it): what's new in that version (the CHANGELOG entry's title; no line when
/// there is none) and **Update** (a normal job on the user's Hermes). Then "Updating…",
/// "Updated to X", or "Update didn't take" with the job one click away.
struct PluginUpdateCard: View {
    /// Actions only (dismiss). What the card shows is copied into `shown` when the card is made: on
    /// the board it lives in a scroll view's own SwiftUI host, which the panel's off-screen measurer
    /// doesn't re-render on the notice's own changes, so the panel was sized for the previous card
    /// (clipped, or a band of empty space). The panel model passes the notice's changes on, the board
    /// is rebuilt, and a new value of this view carries the new state.
    let notice: PluginUpdateNotice
    var onUpdate: () -> Void
    var onOpenJob: (String) -> Void
    var onCopy: () -> Void
    private let shown: Shown

    private struct Shown: Equatable {
        var phase: PluginUpdatePhase, error: String?, starting: Bool, canStartJob: Bool, inCall: Bool, running: String?
    }

    init(notice: PluginUpdateNotice, onUpdate: @escaping () -> Void, onOpenJob: @escaping (String) -> Void,
         onCopy: @escaping () -> Void) {
        self.notice = notice
        self.onUpdate = onUpdate; self.onOpenJob = onOpenJob; self.onCopy = onCopy
        shown = Shown(phase: notice.visiblePhase, error: notice.error, starting: notice.starting,
                      canStartJob: notice.canStartJob, inCall: notice.inCall, running: notice.running)
    }

    var body: some View {
        // No animated swaps: the panel's off-screen measurer never finishes an animation, so a card
        // fading out (or a line leaving) kept counting toward the panel's height and left a band.
        card.transaction { $0.animation = nil }
    }

    @ViewBuilder private var card: some View {
        let phase = shown.phase
        if phase != .none {
            HStack(alignment: .top, spacing: 10) {
                icon(phase).padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title(phase)).font(.system(size: 13, weight: .semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    if let line = detail(phase) {
                        Text(line).font(.system(size: 11.5)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                actions(phase)
            }
            // A board tile like the others (Running's look): same radius, card fill, brass glow.
            .padding(.horizontal, 12).padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
            .background(alignment: .topLeading) {
                RadialGradient(colors: [Live.brass.opacity(0.16), .clear], center: .topLeading, startRadius: 0, endRadius: 160)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Live.brass.opacity(0.35), lineWidth: 0.6))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("plugin-update-card")
            .padding(.bottom, 2)
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
        // No trailing "…": beside the spinner it read as a cut-off title.
        case .updating(let v, _): return "Updating to \(v)"
        case .updated(let v): return "Updated to \(v)"
        case .didntTake: return "Update didn't take"
        case .none: return ""
        }
    }

    private func detail(_ phase: PluginUpdatePhase) -> String? {
        switch phase {
        case .available(_, let note):
            if let error = shown.error { return error }
            return note
        case .updating: return shown.inCall ? "It switches over when the call ends." : "It switches over by itself. Nothing restarts."
        case .updated: return nil
        case .didntTake: return "Your Hermes still runs \(shown.running ?? "the old version")."
        case .none: return nil
        }
    }

    @ViewBuilder private func actions(_ phase: PluginUpdatePhase) -> some View {
        switch phase {
        case .available:
            HStack(spacing: 4) {
                if shown.canStartJob {
                    PillButton(title: shown.starting ? "Starting…" : "Update", prominent: true, action: onUpdate)
                        .fixedSize().disabled(shown.starting)
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
        Button { notice.dismiss() } label: {
            Image(systemName: "xmark").font(.system(size: 9.5, weight: .bold)).foregroundStyle(.secondary)
                .frame(width: 22, height: 22).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Not now")
        .accessibilityLabel("Dismiss")
    }
}
