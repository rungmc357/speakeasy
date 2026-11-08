import SwiftUI
import SpeakeasyCore

/// The same in-place work disclosure on phone, tablet and Mac. Raw evidence is selectable,
/// not a code-answer block: no Copy action, navigation or progress-by-step-count.
public struct ReplyWorkCard: View {
    public let work: ReplyWork
    public let images: [ImageCard]
    public let imageIdentity: String
    public let loadImage: (Int) async -> Data?
    public let helpers: [JobSubagent]
    @State private var expanded = false
    @State private var open: Set<String> = []
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize
    public init(work: ReplyWork, images: [ImageCard] = [], imageIdentity: String = "", helpers: [JobSubagent] = [],
                loadImage: @escaping (Int) async -> Data? = { _ in nil }) {
        self.work = work; self.images = images; self.imageIdentity = imageIdentity; self.loadImage = loadImage; self.helpers = helpers
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: work.failed ? "exclamationmark.circle" : work.running ? "sparkles" : "checkmark.circle")
                        .font(.system(size: 18))
                        .foregroundStyle(work.failed ? Color.orange : Color.accentColor)
                    if work.running && !expanded && !typeSize.isAccessibilitySize {
                        // Its live line changes as it works: one line pushed in place, never two lines
                        // crossfading over each other or a wrap onto the next row (SPK-231 R4).
                        LiveStatusLine(work.line, font: .subheadline.weight(.medium), color: .primary)
                    } else {
                        Text(AgentMarkdown.attributed(work.line)).font(.subheadline.weight(.medium))
                            .multilineTextAlignment(.leading)
                            .lineLimit(expanded || typeSize.isAccessibilitySize ? nil : 2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.down").font(.system(size: 12, weight: .semibold))
                        .rotationEffect(.degrees(expanded ? 180 : 0)).foregroundStyle(.secondary)
                }
                .padding(14)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityIdentifier("reply-work-toggle")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                .accessibilityHint(expanded ? "Hides the work" : "Shows the work in this conversation")
            if expanded {
                Divider().padding(.horizontal, 14)
                VStack(alignment: .leading, spacing: 12) {
                    Text("How it got there").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                        .accessibilityIdentifier("reply-work-steps")
                    if work.calls.isEmpty {
                        Text(work.running ? "No recorded steps yet." : "No recorded steps.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    ForEach(helpers) { helper in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(AgentMarkdown.attributed(helper.title)).font(.footnote.weight(.medium))
                            if let line = helper.isRunning ? helper.now : (helper.summary ?? helper.note), !line.isEmpty {
                                Text(AgentMarkdown.attributed(line)).font(.caption).foregroundStyle(.secondary)
                            }
                        }.accessibilityIdentifier("helper-\(helper.id)")
                    }
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
                                    }
                                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                                    .contentShape(Rectangle())
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
