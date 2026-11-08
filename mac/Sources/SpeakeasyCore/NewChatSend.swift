import Foundation

/// A New chat's first message (where Home's typed words land) is plain chat or a job, and an app
/// may only show a job ("Starting a new chat…", a task card) when the server made one. Plugins with
/// `chat_replies` answer a greeting or small talk on `POST /voice/work` themselves when the body asks
/// (`delivered_as: "chat"` + `reply`, no run). Older ones start a job for anything sent there, so
/// a greeting to one of those is held back with an honest update line instead (docs/API.md
/// "POST /voice/work"). Build 70 was withdrawn for showing "hi" as a started task.
public enum NewChatSend {
    public enum Route: Equatable, Sendable {
        /// Send it; the server starts a job ("Starting a new chat…" while it does).
        case job
        /// Send it; the server answers it in the chat with no job (no "Starting…").
        case chat
        /// Not sent: this server would make a job of small talk.
        case updateHermes
    }

    /// What the server made of it.
    public enum Outcome: Equatable, Sendable {
        case job(runID: String?, sessionID: String?)
        case chat(reply: String)
    }

    public static let updateHermes = "Your Hermes would start a task for that. Update Speakeasy on your Hermes to just chat, or ask for something to do."

    public static func route(_ text: String, hasAttachments: Bool, serverChats: Bool) -> Route {
        guard !hasAttachments, isSmallTalk(text) else { return .job }
        return serverChats ? .chat : .updateHermes
    }

    /// The `POST /voice/work` body (nil: nothing to send). Another agent runs on its own model: no model fields.
    /// `serverChats`: the server has `chat_replies`. Only then does the body ask for a plain reply to
    /// small talk; an older server refuses the unknown key.
    public static func body(text: String, attachments: [String], clientID: String, choice: ModelChoice?,
                            agent: String?, serverChats: Bool = false) -> [String: Any]? {
        let clean = TaskMessageRequest.clean(text)
        guard clean != nil || !attachments.isEmpty else { return nil }
        var body: [String: Any] = ["text": clean ?? "", "source": "typed", "client_id": clientID]
        if serverChats { body["chat_replies"] = true }
        if !attachments.isEmpty { body["attachments"] = attachments }
        if let agent {
            body["agent"] = agent
        } else if let choice, !choice.isEmpty { body.merge(choice.body()) { _, new in new } }
        return body
    }

    /// The `POST /voice/work` reply. A job only when the server says it made one (`new_run`, or a
    /// plugin from before `delivered_as` naming a run); nil when nothing started.
    public static func outcome(_ reply: [String: Any]) -> Outcome? {
        guard reply["ok"] as? Bool == true else { return nil }
        let delivered = reply["delivered_as"] as? String
        if delivered == "chat" {
            guard let text = reply["reply"] as? String, !text.isEmpty else { return nil }
            return .chat(reply: text)
        }
        guard delivered == nil || delivered == "new_run" else { return nil }
        let run = reply["run_id"] as? String
        let session = (reply["session_id"] as? String) ?? ((reply["task"] as? [String: Any])?["session_id"] as? String)
        guard run != nil || session != nil || reply["task_id"] is String else { return nil }
        return .job(runID: run, sessionID: session)
    }

    /// The plugin's rule (calls.py GREETING_RE): a greeting, a filler sound or how-are-you small talk on
    /// its own. "hi can you check the parcel" is a real ask.
    public static func isSmallTalk(_ text: String) -> Bool {
        let s = text.replacingOccurrences(of: "\u{2019}", with: "'")
        return smallTalk.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    private static let hello = #"(?:hey|hi|hello|yo|sup|hiya|howdy|good\s+(?:morning|afternoon|evening)|morning|evening"#
        + #"|um+|uh+|hmm+|so|okay so|alright so)(?:\s+(?:there|again|man|buddy|dude|you|yo|all|everyone))?"#
    private static let how = #"(?:what'?s up|whats up|wassup|what'?s new|how'?s it going|how is it going"#
        + #"|how(?:'re| are) (?:you|ya|things)(?:\s+doing)?(?:\s+today)?|how have you been"#
        + #"|how'?s (?:your|the) (?:day|morning|evening|week)(?:\s+going)?|you there|are you there|you around)"#
    private static let smallTalk = try! NSRegularExpression(
        pattern: #"^\s*(?:"# + hello + #"(?:[\s,.!]+"# + how + #")?|"# + how + #")\s*[.!?,]*\s*$"#,
        options: [.caseInsensitive])
}
