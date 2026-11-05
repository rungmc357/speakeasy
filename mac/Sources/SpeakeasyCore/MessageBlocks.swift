import Foundation

/// An answer split into prose and fenced code blocks (```lang … ```), so session pages draw code in
/// a monospaced, horizontally scrolling box instead of showing the fences as text. An unclosed fence
/// (a reply still streaming in) keeps its code as code to the end.
public enum MessageBlock: Equatable, Sendable {
    case text(String)
    case code(String, language: String?)

    public static func split(_ message: String) -> [MessageBlock] {
        guard message.contains("```") else { return message.isEmpty ? [] : [.text(message)] }
        var blocks: [MessageBlock] = []
        var prose: [Substring] = []
        var code: [Substring] = []
        var language: String?
        var inCode = false

        func flushProse() {
            let text = prose.joined(separator: "\n").trimmingCharacters(in: .newlines)
            if !text.trimmingCharacters(in: .whitespaces).isEmpty { blocks.append(.text(text)) }
            prose.removeAll()
        }
        func flushCode() {
            blocks.append(.code(code.joined(separator: "\n"), language: language))
            code.removeAll(); language = nil
        }

        for line in message.split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if inCode {
                    flushCode(); inCode = false
                } else {
                    flushProse(); inCode = true
                    let tag = trimmed.dropFirst(3).trimmingCharacters(in: .whitespaces)
                    language = tag.isEmpty ? nil : String(tag.prefix(20))
                }
                continue
            }
            if inCode { code.append(line) } else { prose.append(line) }
        }
        if inCode { flushCode() } else { flushProse() }
        return blocks
    }
}
