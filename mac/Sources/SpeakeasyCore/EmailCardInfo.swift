import Foundation

/// The email card's facts, read from a `draft` view the server drew from a stored email draft
/// (`draft_id` set): every recipient with name and address, the thread it answers, names the job
/// couldn't find an address for, and the hash the card's buttons are bound to.
public struct EmailCardInfo: Equatable, Sendable {
    public struct Person: Equatable, Sendable, Hashable {
        public var name: String
        public var address: String
        /// The firm the ask named beside this person, shown after the name ("Pat Doe · Lark & Finch");
        /// the server sets it only when the address domain is that firm's. Never sent.
        public var org: String?
        public init(name: String, address: String, org: String? = nil) {
            self.name = name; self.address = address; self.org = org.flatMap { $0.isEmpty ? nil : $0 }
        }

        /// "Pat Doe <pat@example.org>" or "pat@example.org"; nil when there's no address in it.
        public init?(typed: String) {
            let text = typed.trimmingCharacters(in: .whitespacesAndNewlines)
            if let open = text.lastIndex(of: "<"), text.hasSuffix(">") {
                let address = String(text[text.index(after: open)..<text.index(before: text.endIndex)])
                    .trimmingCharacters(in: .whitespaces)
                let name = String(text[..<open]).trimmingCharacters(in: CharacterSet(charactersIn: " \""))
                guard Self.isAddress(address) else { return nil }
                self.init(name: name, address: address)
            } else {
                guard Self.isAddress(text) else { return nil }
                self.init(name: "", address: text)
            }
        }

        /// What the server takes back: "Name <address>", or the bare address.
        public var wire: String { name.isEmpty ? address : "\(name) <\(address)>" }
        /// The card's short form: the name, else the address.
        public var label: String { name.isEmpty ? address : name }

        public static func isAddress(_ text: String) -> Bool {
            text.range(of: #"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$"#,
                       options: .regularExpression) != nil
        }
    }

    public struct Quoted: Equatable, Sendable {
        public var from: String?
        public var when: String?
        public var text: String
    }

    public enum Status: String, Sendable { case pending, approved, denied, revising, sent, failed, superseded }

    public var draftID: String
    public var sha256: String
    public var status: Status
    public var from: String?
    public var to: [Person]
    public var cc: [Person]
    public var bcc: [Person]
    public var subject: String
    public var body: String
    public var thread: String?
    public var threadCount: Int?
    public var threadLast: Quoted?
    public var unresolved: [String]
    public var notes: [String]
    public var attachments: [String]
    public var edited: Bool
    public var error: String?

    public init?(_ card: ViewCard) {
        guard card.kind == "draft", let id = card["draft_id"].string, !id.isEmpty,
              (card["channel"].string ?? "email").lowercased() == "email" else { return nil }
        draftID = id
        sha256 = card["sha256"].string ?? ""
        status = Status(rawValue: card["status"].string ?? "") ?? .pending
        from = card["send_as"].string
        func people(_ key: String) -> [Person] {
            card["recipients"][key].array.compactMap { p in
                guard let address = p["address"].string, !address.isEmpty else { return nil }
                return Person(name: p["name"].string ?? "", address: address, org: p["org"].string)
            }
        }
        to = people("to"); cc = people("cc"); bcc = people("bcc")
        subject = card["subject"].string ?? ""
        body = card["body"].string ?? ""
        thread = card["thread"].string.flatMap { $0.isEmpty ? nil : $0 }
        threadCount = card["thread_count"].double.map { Int($0) }
        let last = card["thread_last"]
        threadLast = last["text"].string.flatMap { $0.isEmpty ? nil : Quoted(from: last["from"].string, when: last["when"].string, text: $0) }
        unresolved = card["unresolved"].array.compactMap(\.string)
        notes = card["notes"].array.compactMap(\.string)
        attachments = card["attachments"].array.compactMap(\.string)
        edited = card["edited"].bool ?? false
        error = card["error"].string
    }

    /// "Reply in thread: Spring schedule · 4 messages"; nil for a new email.
    public var threadLine: String? {
        guard let thread else { return nil }
        let count = threadCount.map { $0 == 1 ? " · 1 message" : " · \($0) messages" } ?? ""
        return "Reply in thread: \(thread)\(count)"
    }

    /// What the user can change on the card.
    public struct Fields: Equatable, Sendable {
        public var to: [Person]
        public var cc: [Person]
        public var bcc: [Person]
        public var subject: String
        public var body: String
        public init(to: [Person], cc: [Person], bcc: [Person], subject: String, body: String) {
            self.to = to; self.cc = cc; self.bcc = bcc; self.subject = subject; self.body = body
        }
        /// The `edit` value: every field, so what goes out is exactly what the card shows.
        public func value(sha256: String, after: String?) -> JSONValue {
            var object: [String: Any] = ["sha256": sha256, "to": to.map(\.wire), "cc": cc.map(\.wire),
                                         "bcc": bcc.map(\.wire), "subject": subject, "body": body]
            if let after { object["after"] = after }
            return JSONValue(any: object)
        }
        public var sendable: Bool { !to.isEmpty && !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }

    public var fields: Fields { Fields(to: to, cc: cc, bcc: bcc, subject: subject, body: body) }

    /// People the job couldn't find an address for that the user hasn't added since. An
    /// organisation named in the ask is never a missing recipient ("email Ben and Wren Mallory",
    /// Wren Mallory being Ben's firm): a name that reads as a firm, or is already shown beside a
    /// recipient, or matches a recipient's address domain, never warns (older servers sent them).
    public func stillUnresolved(in fields: Fields) -> [String] {
        let everyone = fields.to + fields.cc + fields.bcc
        let names = everyone.map { $0.name.lowercased() }.joined(separator: " ")
        let orgs = Set(everyone.compactMap { $0.org?.lowercased() })
        return unresolved.filter { entry in
            !names.contains(entry.lowercased()) && !orgs.contains(entry.lowercased())
                && !Self.looksLikeOrg(entry) && !everyone.contains { Self.domain($0.address, matches: entry) }
        }
    }

    /// "Lark & Finch", "Ostrow Holdings LLP": a firm, not a person.
    public static func looksLikeOrg(_ name: String) -> Bool {
        name.range(of: #"(&|\+|\b(llp|llc|lp|inc|incorporated|corp|corporation|co|company|ltd|limited|plc|gmbh|ag|bv|pllc|group|partners|partnership|associates|capital|ventures|holdings|advisors|advisers|consulting|legal|foundation|labs|agency|realty|properties|cpas?|accountants|solicitors|attorneys)\b\.?)"#,
                   options: [.regularExpression, .caseInsensitive]) != nil
    }

    /// Whether an address's domain spells the firm's name ("Wren Mallory" ~ wrenmallory.example).
    /// Personal mail hosts never match.
    static func domain(_ address: String, matches org: String) -> Bool {
        let host = address.split(separator: "@").last.map { $0.lowercased() } ?? ""
        let parts = host.split(separator: ".").map(String.init)
        guard parts.count >= 2 else { return false }
        let secondLevel = parts.count >= 3 && ["co", "com", "org", "net", "ac", "gov"].contains(parts[parts.count - 2])
            && parts[parts.count - 1].count == 2
        let label = secondLevel ? parts[parts.count - 3] : parts[parts.count - 2]
        let personal: Set<String> = ["gmail", "googlemail", "outlook", "hotmail", "live", "msn", "yahoo", "ymail", "icloud",
                                     "me", "mac", "aol", "proton", "protonmail", "pm", "fastmail", "gmx", "zoho", "hey",
                                     "mail", "example"]
        let core = org.lowercased().components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty && !looksLikeOrg($0) }.joined()
        let name = core.isEmpty ? org.lowercased().filter { $0.isLetter || $0.isNumber } : core
        guard label.count >= 3, name.count >= 3, !personal.contains(label) else { return false }
        // Most of the letters, not a fragment: "Pat" never matches patterson.example.
        let (short, long) = label.count <= name.count ? (label, name) : (name, label)
        return long.contains(short) && Double(short.count) / Double(long.count) >= 0.6
    }
}

/// What this device did to one draft since its card was drawn. Hermes's status is the truth once
/// it moves: an ask only stands while the draft is still pending.
public enum EmailCardPhase: Equatable, Sendable {
    case waiting(String, until: Date)   // "send" | "discard", with Undo until then
    case asked(String)                  // posted to Hermes: "send" | "save" | "discard"

    /// What stays once Hermes answers the POST. A refused or unreached ask gives the buttons back; a
    /// saved draft is still a draft; a send or discard Hermes took waits for the status to move.
    public static func answered(_ verb: String, ok: Bool) -> EmailCardPhase? {
        ok && verb != "save" ? .asked(verb) : nil
    }

    public func shown(_ status: EmailCardInfo.Status) -> EmailCardPhase? {
        if case .asked = self, status != .pending { return nil }
        return self
    }
}

public extension EmailCardInfo {
    /// The word in the card's corner: what this device asked while Hermes still says pending,
    /// then Hermes's own status ("Sent", "Didn't send").
    func stateWord(_ phase: EmailCardPhase?, editedHere: Bool) -> String {
        switch phase?.shown(status) {
        case .asked("send")?: return "Sending…"
        case .asked("save")?: return editedHere ? "Saving…" : "Saving to Drafts…"
        case .asked("discard")?: return "Discarded"
        default: break
        }
        switch status {
        case .pending: return editedHere || edited ? "Draft · edited" : "Draft"
        case .approved: return "Sending…"
        case .sent: return "Sent"
        case .denied: return "Discarded"
        case .revising: return "Revising…"
        case .failed: return "Didn't send"
        case .superseded: return "Replaced"
        }
    }
}

public extension WorkInfo {
    /// Drafts the job's own email card already shows (the server adds one per job): surfaces
    /// that list `emailDrafts` beside `views` leave these out, so an email is never drawn twice.
    var draftsWithoutCard: [EmailDraft] {
        let carded = Set(views.compactMap { EmailCardInfo($0)?.draftID })
        guard !carded.isEmpty else { return emailDrafts }
        // The card shows the job's newest draft; older ones in the same job are its history.
        return []
    }
}

public extension ViewCard {
    /// The email card the server drew from a stored draft.
    var isEmailCard: Bool { EmailCardInfo(self) != nil }
}

public enum ResultCardPick {
    /// The card Home shows for a finished job: an email it drafted always wins (what was asked
    /// for is the email itself); else the first usable card, a thing before a count summary.
    public static func pick(_ views: [ViewCard]) -> ViewCard? {
        let usable = views.filter { !["question", "image"].contains($0.kind) }
        if let email = usable.first(where: \.isEmailCard) { return email }
        let summary: Set<String> = ["stats", "fact"]
        let things: Set<String> = ["rows", "jobs", "list", "issue", "pr", "calendar_day", "agenda", "explore", "places",
                                   "compare", "status", "timeline", "site", "mockups", "draft", "confirm", "preview"]
        if let first = usable.first, summary.contains(first.kind), let thing = usable.first(where: { things.contains($0.kind) }) {
            return thing
        }
        return usable.first
    }
}
