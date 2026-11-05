import SwiftUI
import SpeakeasyCore

/// Every board card's own actions, the same on the Mac and on touch: a context menu (right-click,
/// long press) with Open, Dismiss and, for a running job, Stop (asks first), and a VoiceOver
/// action "Dismiss". Dismiss goes through `VoicePanelModel.dismissFromBoard` (set aside, mark
/// read, hide a running job without stopping it, or clear a finished one).
///
/// `hoverDismiss`: the Mac's ✕ in the corner while the pointer is over the card.
public struct BoardCardActions: ViewModifier {
    let job: Job
    @ObservedObject var model: VoicePanelModel
    var hoverDismiss: Bool
    @State private var confirmStop = false
    @State private var hover = false

    public init(job: Job, model: VoicePanelModel, hoverDismiss: Bool = false) {
        self.job = job; self.model = model; self.hoverDismiss = hoverDismiss
    }

    public func body(content: Content) -> some View {
        let canDismiss = model.canDismissFromBoard(job)
        let stop = model.stoppableRun(job)
        content
            .onHover { hover = $0 }
            .overlay(alignment: .topTrailing) {
                if hoverDismiss && hover && canDismiss {
                    DismissAnswerButton(name: job.name) { model.dismissFromBoardWithUndo(job) }
                        .padding(4)
                        .transition(.opacity)
                }
            }
            .contextMenu {
                Button("Open") { model.onOpenJob(job.id) }
                if canDismiss {
                    Button("Dismiss") { model.dismissFromBoardWithUndo(job) }
                }
                if stop != nil {
                    Divider()
                    Button("Stop…", role: .destructive) { confirmStop = true }
                }
            }
            .accessibilityAction(named: "Dismiss") { if canDismiss { model.dismissFromBoardWithUndo(job) } }
            .confirmationDialog("Stop \(job.name)?", isPresented: $confirmStop, titleVisibility: .visible) {
                Button("Stop", role: .destructive) { if let run = model.stoppableRun(job) { model.onStopTask(run) } }
            } message: {
                Text("Your other jobs keep going.")
            }
    }
}
