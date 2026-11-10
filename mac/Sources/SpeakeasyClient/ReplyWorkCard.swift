import SwiftUI
import SpeakeasyCore

/// The same in-place work disclosure on phone, tablet and Mac. Raw evidence is selectable,
/// not a code-answer block: no Copy action, navigation or progress-by-step-count.
/// Done, it is one closed "How I did it" row (SPK-309 D2): what it did, the task's times, the
/// thread's notes ("A helper reported back") and the steps all live inside, so the page reads as
/// your ask, that row, then the answer. Running, the row is the plain live line.
public struct ReplyWorkCard: View {
    public let work: ReplyWork
    public let images: [ImageCard]
    public let imageIdentity: String
    public let loadImage: (Int) async -> Data?
    public let helpers: [JobSubagent]
    /// The task's own clock ("Task started 9:40 AM · Done 9:50 AM · Took 10 min"), shown inside.
    public var times: String? = nil
    /// Notes the thread folded in here ("A helper reported back", "Earlier messages were summarized").
    public var notes: [String] = []
    public static let title = "How I did it"
    @State private var expanded = false
    @State private var open: Set<String> = []
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize
    public init(work: ReplyWork, images: [ImageCard] = [], imageIdentity: String = "", helpers: [JobSubagent] = [],
                times: String? = nil, notes: [String] = [],
                loadImage: @escaping (Int) async -> Data? = { _ in nil }) {
        self.work = work; self.images = images; self.imageIdentity = imageIdentity; self.loadImage = loadImage; self.helpers = helpers
        self.times = times; self.notes = notes
    }
    /// The row's words: the live line while it works; "How I did it" once done (a stop says what went wrong).
    private var heading: String { work.running || work.failed ? work.line : Self.title }

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
                        Text(AgentMarkdown.attributed(heading)).font(.subheadline.weight(.medium))
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
                    // What it did in plain words (the row itself only says "How I did it"), then its clock.
                    Text(AgentMarkdown.attributed(work.running || work.failed ? "Steps" : work.line))
                        .font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("reply-work-steps")
                    if let times, !times.isEmpty {
                        Label(times, systemImage: "clock").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            .accessibilityIdentifier("session-task-times")
                    }
                    ForEach(Array(notes.enumerated()), id: \.offset) { _, note in
                        Text(AgentMarkdown.attributed(note)).font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("reply-work-note")
                    }
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
