import Foundation

/// The email card's facts, read from a `draft` view the server drew from a stored email draft
/// (`draft_id` set): every recipient with name and address, the thread it answers, names the job
/// couldn't find an address for, and the hash the card's buttons are bound to.
public struct EmailCardInfo: Equatable, Sendable {
    public struct Person: Equatable, Sendable, Hashable {
        public var name: String
        public var address: String
        public init(name: String, address: String) { self.name = name; self.address = address }

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
                return Person(name: p["name"].string ?? "", address: address)
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

    /// Names the job couldn't find an address for that the user hasn't added since.
    public func stillUnresolved(in fields: Fields) -> [String] {
        let names = (fields.to + fields.cc + fields.bcc).map { $0.name.lowercased() }.joined(separator: " ")
        return unresolved.filter { !names.contains($0.lowercased()) }
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
