import Foundation

// The user's Hermes approval rules, read-only (`GET /voice/approvals`, servers with
// `approval_rules`; docs/API.md). The server already words every row in plain English, so the apps
// show the sections as they come: a better wording ships with a plugin update, no app build.

/// One row: a title, and optionally a short trailing value, a sentence under it, or a pattern.
public struct ApprovalRuleRow: Equatable, Sendable, Identifiable, Hashable {
    public var id: String
    public var title: String
    /// Short, shown at the trailing edge ("5 minutes", "Blocked").
    public var value: String?
    /// A sentence under the title.
    public var detail: String?
    /// A pattern the user wrote (an "Always blocked" rule), shown fixed-width.
    public var code: String?

    public init(id: String, title: String, value: String? = nil, detail: String? = nil, code: String? = nil) {
        self.id = id; self.title = title; self.value = value; self.detail = detail; self.code = code
    }

    init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), let title = nonEmpty(o["title"]) else { return nil }
        self.init(id: id, title: title, value: nonEmpty(o["value"]), detail: nonEmpty(o["detail"]), code: nonEmpty(o["code"]))
    }
}

public struct ApprovalRuleSection: Equatable, Sendable, Identifiable, Hashable {
    public var id: String
    public var title: String
    public var rows: [ApprovalRuleRow]
    /// Shown instead of rows when there are none.
    public var empty: String?
    public var footer: String?

    public init(id: String, title: String, rows: [ApprovalRuleRow], empty: String? = nil, footer: String? = nil) {
        self.id = id; self.title = title; self.rows = rows; self.empty = empty; self.footer = footer
    }

    init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), let title = nonEmpty(o["title"]) else { return nil }
        let rows = (o["rows"] as? [Any] ?? []).compactMap(ApprovalRuleRow.init(json:))
        let empty = nonEmpty(o["empty"])
        // A section with nothing to show (no rows, no empty line) is left out.
        if rows.isEmpty && empty == nil && nonEmpty(o["footer"]) == nil { return nil }
        self.init(id: id, title: title, rows: rows, empty: empty, footer: nonEmpty(o["footer"]))
    }
}

public struct ApprovalRules: Equatable, Sendable {
    public static let path = "/voice/approvals"

    public enum Mode: String, Equatable, Sendable { case manual, smart, off }

    public var profile: String
    public var mode: Mode
    public var sections: [ApprovalRuleSection]
    /// How to change them today (read-only for now).
    public var note: String?

    public init(profile: String = "default", mode: Mode = .smart, sections: [ApprovalRuleSection], note: String? = nil) {
        self.profile = profile; self.mode = mode; self.sections = sections; self.note = note
    }

    /// `{"approvals": {...}}` or the inner object; nil when it isn't an approvals answer.
    public init?(json: Any?) {
        let outer = json as? [String: Any]
        guard let o = (outer?["approvals"] as? [String: Any]) ?? outer, o["sections"] is [Any] else { return nil }
        self.init(profile: nonEmpty(o["profile"]) ?? "default",
                  mode: Mode(rawValue: (o["mode"] as? String) ?? "") ?? .manual,
                  sections: (o["sections"] as? [Any] ?? []).compactMap(ApprovalRuleSection.init(json:)),
                  note: nonEmpty(o["note"]))
    }

    /// The SF Symbol for how risky commands are handled.
    public var modeSymbol: String {
        switch mode {
        case .manual: return "hand.raised"
        case .smart: return "checkmark.shield"
        case .off: return "bolt"
        }
    }

    /// The note for screens: a slash command never splits across lines ("/" + word joiner).
    public var displayNote: String {
        (note ?? "You can't change these from Speakeasy yet.").replacingOccurrences(of: " /", with: " /\u{2060}")
    }

    /// The headline row ("Quick safety check first"): the Settings row's trailing value.
    public var headline: String? { sections.first { $0.id == "mode" }?.rows.first { $0.id == "mode" }?.title }

    /// Invented rules for demos and screenshots (the shape `GET /voice/approvals` returns).
    public static let sample = ApprovalRules(profile: "default", mode: .smart, sections: [
        ApprovalRuleSection(id: "mode", title: "Risky commands", rows: [
            ApprovalRuleRow(id: "mode", title: "Quick safety check first",
                            detail: "A second model looks at each risky command. Clearly safe ones run, clearly harmful ones are blocked, and anything it isn't sure about comes to you."),
            ApprovalRuleRow(id: "timeout", title: "Waits for your answer", value: "5 minutes",
                            detail: "If nobody answers in time, the command is blocked."),
        ]),
        ApprovalRuleSection(id: "always_allowed", title: "Always allowed", rows: [
            ApprovalRuleRow(id: "allow_0", title: "Deleting folders and everything in them"),
            ApprovalRuleRow(id: "allow_1", title: "Managing containers"),
            ApprovalRuleRow(id: "allow_2", title: "Running scripts and code snippets"),
        ], empty: "Nothing yet. When you answer Always to a question, that kind of command shows up here.",
           footer: "These run without asking because you answered Always once. Speakeasy only ever allows a command once or for one task, never always."),
        ApprovalRuleSection(id: "always_blocked", title: "Always blocked", rows: [
            ApprovalRuleRow(id: "deny_0", title: "Commands matching", code: "git push --force*"),
        ], footer: "A few commands are always blocked, whatever the setting: wiping the computer or your home folder, erasing a disk, freezing the computer and shutting it down. Your own rules above apply even when Hermes never asks."),
        ApprovalRuleSection(id: "nobody_there", title: "When nobody can answer", rows: [
            ApprovalRuleRow(id: "cron_mode", title: "Scheduled jobs", value: "Blocked"),
            ApprovalRuleRow(id: "unattended_mode", title: "Webhooks and other automatic runs", value: "Blocked"),
        ], footer: "What happens to a risky command when no one is there to answer the question."),
        ApprovalRuleSection(id: "writes", title: "Memory and skills", rows: [
            ApprovalRuleRow(id: "memory", title: "Changes to memory", value: "Saved without asking"),
            ApprovalRuleRow(id: "skills", title: "Changes to skills", value: "Waits for your OK"),
        ]),
    ], note: "You can't change these from Speakeasy yet. In a Hermes chat, /approvals switches how risky commands are handled.")
}
