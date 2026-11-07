import SwiftUI
import SpeakeasyCore
#if canImport(UIKit)
import UIKit
#elseif canImport(AppKit)
import AppKit
#endif

/// Puts an answer's code on the clipboard: exactly the block's contents, nothing around it.
public enum AnswerClipboard {
    @MainActor public static func copy(_ text: String) {
        #if os(iOS) || os(visionOS)
        UIPasteboard.general.string = text
        #elseif os(macOS)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #endif
    }
}

/// A fenced code block in an answer (SPK-187): monospaced, never wrapped (long lines scroll
/// sideways), with a visible one-tap Copy that copies exactly the block and says "Copied" (with a
/// haptic on iPhone). Answers only: tool-call steps don't get this.
public struct AnswerCodeBlock: View {
    @Environment(\.jobTextScale) private var s
    let code: String
    let language: String?
    @State private var copied = false

    public init(code: String, language: String? = nil) {
        self.code = code; self.language = language
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                if let language { Text(language).font(.system(size: 10.5 * s, weight: .semibold)).foregroundStyle(.secondary) }
                Spacer(minLength: 4)
                Button {
                    AnswerClipboard.copy(code)
                    copied = true
                    #if os(iOS)
                    UIAccessibility.post(notification: .announcement, argument: "Copied")
                    #endif
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { copied = false }
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 11.5 * s, weight: .semibold))
                        .foregroundStyle(JobStyle.blueInk)
                        .padding(.horizontal, 8).padding(.vertical, 4)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .sensoryFeedback(.success, trigger: copied) { _, now in now }
                .accessibilityLabel(copied ? "Copied" : "Copy code")
                .accessibilityIdentifier("code-copy")
            }
            .padding(.leading, 10).padding(.trailing, 2).padding(.vertical, 2)
            Divider().opacity(0.5)
            ScrollView(.horizontal, showsIndicators: true) {
                Text(code).font(.system(size: 12 * s, design: .monospaced)).foregroundStyle(.primary)
                    .textSelection(.enabled).fixedSize().padding(10)
            }
        }
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(Color.primary.opacity(0.1), lineWidth: 0.5))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("answer-code")
    }
}

/// An answer's text with its code blocks drawn as `AnswerCodeBlock`s and the prose between them
/// drawn by *prose* (each surface keeps its own text style).
public struct AnswerText<Prose: View>: View {
    let text: String
    let spacing: CGFloat
    let prose: (String) -> Prose

    public init(_ text: String, spacing: CGFloat = 8, @ViewBuilder prose: @escaping (String) -> Prose) {
        self.text = text; self.spacing = spacing; self.prose = prose
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(Array(MessageBlock.split(text).enumerated()), id: \.offset) { _, block in
                switch block {
                case .text(let t): prose(t)
                case .code(let code, let language): AnswerCodeBlock(code: code, language: language)
                }
            }
        }
    }
}

/// Only the code blocks of an answer (a folded report still shows what there is to copy).
public struct AnswerCodeBlocks: View {
    let text: String
    public init(_ text: String) { self.text = text }
    public var body: some View {
        let blocks = MessageBlock.split(text).compactMap { b -> (String, String?)? in
            if case .code(let c, let l) = b { return (c, l) } else { return nil }
        }
        if !blocks.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in AnswerCodeBlock(code: b.0, language: b.1) }
            }
        }
    }
}
