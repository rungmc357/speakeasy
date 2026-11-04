import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// A finished task's images, popped into the call panel without opening the task (like an email
/// draft): a large preview, the task name, arrows between images, full size on click, Open task and
/// Dismiss. Bytes come only from the authenticated card-image route for this exact run and card.
struct ImageReviewCard: View {
    var review: ImageReview
    /// How many review cards are pinned: previews shrink so every card's buttons stay on screen.
    var shownCount: Int
    /// Only one review is open at a time; the others are compact rows (thumbnail, name, Show, Dismiss)
    /// so three waiting reviews never push the panel's controls off screen.
    var compact = false
    @ObservedObject var model: VoicePanelModel
    /// Shown inside its job's view: no "Open task" (that's where it is).
    var inJob = false
    /// Tallest the preview gets (job views keep pictures small; click for full size).
    var previewCap: CGFloat? = nil
    /// The card's heading: pictures that came along with an answer aren't "to review".
    var heading = "Ready to review"
    @State private var image: NSImage?
    @State private var failed = false
    @Environment(\.colorScheme) private var scheme

    private var index: Int { min(model.reviewIndex[review.runID] ?? 0, max(0, review.images.count - 1)) }
    private var card: ImageCard? { review.images.indices.contains(index) ? review.images[index] : nil }
    private var previewHeight: CGFloat {
        let h = ImageReviewLayout.previewHeight(visibleScreenHeight: NSScreen.main?.visibleFrame.height ?? 800, others: shownCount - 1)
        return previewCap.map { min(h, $0) } ?? h
    }

    var body: some View {
        if compact { row } else { full }
    }

    private var row: some View {
        HStack(spacing: 8) {
            ZStack {
                Color.primary.opacity(0.06)
                if let image { Image(nsImage: image).resizable().interpolation(.high).scaledToFill() }
                else { Image(systemName: failed ? "photo.badge.exclamationmark" : "photo").font(.system(size: 13)).foregroundStyle(.secondary) }
            }
            .frame(width: 52, height: 36).clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 1) {
                Text(review.taskName).font(.system(size: 11.5, weight: .semibold)).lineLimit(1).truncationMode(.tail)
                Text("Ready to review · \(review.countLabel)").font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            PillButton(title: "Show", action: { model.focusedReviewID = review.runID; model.objectWillChange.send() })
                .help("Show this design")
            PillButton(title: "Dismiss", action: { model.onDismissReview(review.runID) })
                .help("Hide this card; the images stay in the task")
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(scheme == .dark ? 0.07 : 0.05)))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.10), lineWidth: 0.5))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(review.countLabel) from \(review.taskName), ready to review")
        .task(id: "row/\(review.runID)/\(review.images.first?.number ?? 0)") { await load(review.images.first) }
    }

    private var full: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Image(systemName: "photo.on.rectangle.angled").font(.system(size: 11, weight: .semibold)).foregroundStyle(Tokens.blue)
                Text(heading).font(.system(size: 11.5, weight: .semibold))
                if !inJob {   // in its job's view the job title already says whose it is
                    Text("· \(review.taskName)").font(.system(size: 11.5)).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.tail)
                }
                Spacer(minLength: 4)
                Text(review.images.count > 1 ? "\(index + 1) of \(review.images.count)" : review.countLabel)
                    .font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary)
                    .accessibilityLabel(review.images.count > 1 ? "Image \(index + 1) of \(review.images.count)" : review.countLabel)
            }
            preview
            if let card {
                Text(card.name).font(.system(size: 11.5)).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            HStack(spacing: 8) {
                if review.images.count > 1 {
                    PillButton(title: "‹", action: { step(-1) }).help("Previous image").accessibilityLabel("Previous image")
                    PillButton(title: "›", action: { step(1) }).help("Next image").accessibilityLabel("Next image")
                }
                Spacer()
                if !inJob {
                    PillButton(title: "Open task", action: { model.onSelectTask(review.taskID) })
                        .help("Show this task's full answer")
                }
                PillButton(title: "Dismiss", action: { model.onDismissReview(review.runID) })
                    .help("Hide this card; the images stay in the task")
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(scheme == .dark ? 0.07 : 0.05)))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.10), lineWidth: 0.5))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(review.countLabel) from \(review.taskName), ready to review")
        .task(id: card.map { "\(review.runID)/\($0.number)" } ?? review.runID) { await load(card) }
    }

    private var preview: some View {
        Button {
            if let image { openFullSize(image) }
        } label: {
            ZStack(alignment: .bottomTrailing) {
                Color.primary.opacity(0.05)
                if let image {
                    Image(nsImage: image).resizable().interpolation(.high).scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)   // centred, not pushed to the corner
                } else {
                    Image(systemName: failed ? "photo.badge.exclamationmark" : "photo")
                        .font(.system(size: 26)).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                if image != nil {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.system(size: 10, weight: .semibold)).foregroundStyle(.white)
                        .padding(5).background(Circle().fill(Color.black.opacity(0.45))).padding(6)
                }
            }
            .frame(maxWidth: .infinity).frame(height: previewHeight)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(image == nil)
        .help(image == nil ? (failed ? "Image unavailable" : "Loading image") : "Open full size")
        .accessibilityLabel("\(card?.name ?? "Image"), open full size")
    }

    /// Full size, with arrow keys stepping through this review's images.
    private func openFullSize(_ image: NSImage) {
        let runID = review.runID, images = review.images, fallback = review.taskName
        let load = model.loadProductImage
        ImagePreviewWindow.shared.show(image, title: card?.name ?? fallback, index: index, count: images.count) { i in
            guard images.indices.contains(i), let data = await load(runID, images[i].number),
                  let loaded = NSImage(data: data) else { return nil }
            return (loaded, images[i].name)
        }
    }

    private func step(_ delta: Int) {
        model.reviewIndex[review.runID] = ImageReviewLayout.step(index, by: delta, count: review.images.count)
        model.objectWillChange.send()
    }

    private func load(_ card: ImageCard?) async {
        image = nil; failed = false
        guard let card else { failed = true; return }
        if let data = await model.loadProductImage(review.runID, card.number), let loaded = NSImage(data: data) {
            image = loaded
        } else {
            failed = true
        }
    }
}
