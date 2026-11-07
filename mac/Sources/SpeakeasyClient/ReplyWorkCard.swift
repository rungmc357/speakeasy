import SwiftUI
import SpeakeasyCore

/// The same in-place work disclosure on phone, tablet and Mac. Raw evidence is selectable,
/// not a code-answer block: no Copy action, navigation or progress-by-step-count.
public struct ReplyWorkCard: View {
    public let work: ReplyWork
    public let images: [ImageCard]
    public let imageIdentity: String
    public let loadImage: (Int) async -> Data?
    @State private var expanded = false
    @State private var open: Set<String> = []
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    public init(work: ReplyWork, images: [ImageCard] = [], imageIdentity: String = "",
                loadImage: @escaping (Int) async -> Data? = { _ in nil }) {
        self.work = work; self.images = images; self.imageIdentity = imageIdentity; self.loadImage = loadImage
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: work.failed ? "exclamationmark.circle" : work.running ? "sparkles" : "checkmark.circle")
                        .foregroundStyle(work.failed ? Color.orange : Color.accentColor)
                    Text(AgentMarkdown.attributed(work.line)).font(.subheadline.weight(.medium))
                        .multilineTextAlignment(.leading).lineLimit(expanded ? nil : 2)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.down").font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(expanded ? 180 : 0)).foregroundStyle(.secondary)
                }.contentShape(Rectangle()).padding(14)
            }.buttonStyle(.plain).accessibilityIdentifier("reply-work-toggle")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                .accessibilityHint(expanded ? "Hides the work" : "Shows the work in this conversation")
            if expanded {
                Divider().padding(.horizontal, 14)
                VStack(alignment: .leading, spacing: 12) {
                    Text("How it got there").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                        .accessibilityIdentifier("reply-work-steps")
                    if !images.isEmpty {
                        PictureGallery(images: images, identity: imageIdentity, load: loadImage)
                    }
                    ForEach(work.sections.reversed()) { section in
                        if !section.heading.isEmpty {
                            Text(AgentMarkdown.attributed(section.heading)).font(.footnote.weight(.semibold))
                                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                        if let detail = section.detail, !detail.isEmpty {
                            Text(AgentMarkdown.attributed(detail)).font(.footnote).foregroundStyle(.secondary)
                        }
                        ForEach(section.calls.reversed()) { call in
                            VStack(alignment: .leading, spacing: 8) {
                                Button {
                                    withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.2)) {
                                        if open.contains(call.id) { open.remove(call.id) } else { open.insert(call.id) }
                                    }
                                } label: {
                                    HStack(alignment: .top, spacing: 8) {
                                        Image(systemName: call.status == .failed ? "xmark.circle" : call.symbol)
                                            .foregroundStyle(call.status == .failed ? Color.orange : Color.secondary)
                                        Text(AgentMarkdown.attributed(call.text)).font(.subheadline)
                                            .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                                        Spacer(minLength: 4)
                                        Image(systemName: open.contains(call.id) ? "chevron.up" : "chevron.down")
                                            .font(.caption2).foregroundStyle(.secondary)
                                    }.contentShape(Rectangle())
                                }.buttonStyle(.plain).accessibilityIdentifier("reply-work-step")
                                if open.contains(call.id) {
                                    evidence(call).accessibilityIdentifier("reply-work-evidence")
                                }
                            }
                        }
                    }
                }.padding(14)
            }
        }
        .background(RoundedRectangle(cornerRadius: 16).fill(Color.primary.opacity(0.045)))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5))
    }

    private func evidence(_ call: JobActivityItem) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let args = call.args, !args.isEmpty { raw("Command or input", args) }
            if let result = call.result, !result.isEmpty { raw("Output", result) }
            if call.args?.isEmpty != false && call.result?.isEmpty != false {
                Text("No recorded detail.").font(.footnote).foregroundStyle(.secondary)
            }
        }.padding(.leading, 24)
    }
    private func raw(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            ScrollView(.horizontal) {
                Text(text).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .fixedSize(horizontal: true, vertical: false).padding(8)
            }.background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
        }
    }
}
