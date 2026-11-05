import Foundation

// "Look at this": one call's screen sharing, capture requests and pending pictures and files, as
// the panel shows them. Pure state; the call client performs the requests these values ask for.
// Plugin contract: docs/plans/2026-10-06-001-feat-look-at-this-screen-plan.md (U2, U4, U6).

/// What a call tells the plugin about the screen on `POST /voice/sessions` (`screen`). Sent only
/// when `/voice/status` advertises attachments, so an older plugin never sees the key.
public enum ScreenDeclaration: String, Equatable, Sendable {
    /// This Mac can capture its screen.
    case ready
    /// It could, but Screen Recording is off for Speakeasy.
    case noPermission = "no_permission"
}

/// The screen button.
public enum ScreenButton: Equatable, Sendable {
    /// This call can't share (an older plugin, a Hermes that can't read images, the iPhone).
    case unavailable
    case off
    case on
    /// Screen Recording is off: the button leads to Settings.
    case needsPermission
}

/// `{on, seq}`: a toggle sent to `POST /voice/interactions/{id}/screen`, or the plugin's state in force
/// (its reply, a `screen.state` event). Sequence numbers only grow within a call, so the plugin
/// ignores a late, older toggle.
public struct ScreenToggle: Equatable, Sendable {
    public var on: Bool
    public var seq: Int
    public init(on: Bool, seq: Int) { self.on = on; self.seq = seq }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let on = o["on"] as? Bool,
              let number = o["seq"] as? NSNumber, !isBool(number), number.intValue >= 0 else { return nil }
        self.init(on: on, seq: number.intValue)
    }

    /// Exactly `{"on": <bool>, "seq": <int>}` (the plugin refuses anything else).
    public func body() throws -> Data {
        try JSONSerialization.data(withJSONObject: ["on": on, "seq": max(0, seq)], options: [.sortedKeys])
    }
}

/// The interaction snapshot's `screen`: sharing in force and what the call declared.
public struct ScreenSnapshot: Equatable, Sendable {
    public var on: Bool
    public var seq: Int
    public var declared: ScreenDeclaration?
    public init(on: Bool, seq: Int, declared: ScreenDeclaration? = nil) { self.on = on; self.seq = seq; self.declared = declared }

    public init?(json: Any?) {
        guard let toggle = ScreenToggle(json: json), let o = json as? [String: Any] else { return nil }
        self.init(on: toggle.on, seq: toggle.seq, declared: (o["declared"] as? String).flatMap(ScreenDeclaration.init(rawValue:)))
    }
}

/// The app's report on one capture request (`POST /voice/interactions/{id}/captures/{capture_id}`).
public enum CaptureReport: Equatable, Sendable {
    /// About to capture. The plugin answers 410 once the request has closed: then nothing is captured.
    case capturing
    /// The capture is on its way; the plugin waits longer for it.
    case uploading
    /// No capture, and why (the spoken line comes from the reason).
    case failed(AttachmentFailure)

    /// `{"status": ...}`, plus `reason` for `failed` only.
    public func body() throws -> Data {
        var object: [String: Any]
        switch self {
        case .capturing: object = ["status": "capturing"]
        case .uploading: object = ["status": "uploading"]
        case .failed(let reason): object = ["status": "failed", "reason": reason.rawValue]
        }
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }
}

/// `X-Speakeasy-Kind` on an upload.
public enum AttachmentUploadKind: String, Equatable, Sendable {
    /// A capture answering a capture request (needs its capture id).
    case screen
    /// A dropped or pasted picture (JPEG or PNG).
    case picture
    /// Any other file, saved on the Hermes machine (needs its name).
    case file
}

/// The headers of `POST /voice/interactions/{id}/attachments` (the body is the raw bytes; URLSession
/// sets `Content-Length`). Names travel percent-encoded UTF-8. A capture id goes only with a screen
/// capture, the app name only with one; files always carry their name, pictures when they have one.
public func attachmentUploadHeaders(kind: AttachmentUploadKind, mimeType: String, filename: String? = nil,
                                    captureID: String? = nil, app: String? = nil) -> [String: String] {
    var headers = ["X-Speakeasy-Kind": kind.rawValue, "Content-Type": mimeType]
    switch kind {
    case .screen:
        if let captureID { headers["X-Speakeasy-Capture-Id"] = captureID }
        if let app = app?.trimmingCharacters(in: .whitespacesAndNewlines), !app.isEmpty {
            headers["X-Speakeasy-App"] = percentEncodedHeader(String(app.prefix(60)))
        }
    case .picture, .file:
        let name = filename?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        headers["X-Speakeasy-Filename"] = percentEncodedHeader(name.isEmpty ? (kind == .file ? "file" : "picture.jpg") : name)
    }
    return headers
}

/// Plain ASCII for a header value: everything outside unreserved URL characters is percent-encoded
/// UTF-8, which the plugin decodes.
func percentEncodedHeader(_ text: String) -> String {
    let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    return text.addingPercentEncoding(withAllowedCharacters: unreserved) ?? ""
}

/// Pictures and files staged on a device for a typed message or new job, before it is sent
/// (servers with `attachments`): `POST /voice/attachments` takes the raw bytes with
/// `attachmentUploadHeaders(kind: .picture | .file, ...)`; the message names them by id.
public enum StagedAttachmentRoute {
    public static let stage = "/voice/attachments"
    public static func remove(_ id: String) -> String { "/voice/attachments/\(SharingRoute.pathID(id))" }
}

/// The plugin's answer to a staged upload.
public struct StagedAttachmentReply: Equatable, Sendable {
    public var id: String
    public var kind: String
    public var name: String?
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = o["id"] as? String, !id.isEmpty,
              let kind = o["kind"] as? String else { return nil }
        self.id = id; self.kind = kind; self.name = o["name"] as? String
    }
}

/// The sharing routes under one call. Ids are percent-encoded like every other api path.
public enum SharingRoute {
    public static func screen(_ interactionID: String) -> String { "/voice/interactions/\(pathID(interactionID))/screen" }
    public static func attachments(_ interactionID: String) -> String { "/voice/interactions/\(pathID(interactionID))/attachments" }
    public static func attachment(_ interactionID: String, _ attachmentID: String) -> String {
        attachments(interactionID) + "/\(pathID(attachmentID))"
    }
    public static func capture(_ interactionID: String, _ captureID: String) -> String {
        "/voice/interactions/\(pathID(interactionID))/captures/\(pathID(captureID))"
    }
    /// A picture a task carried (`n` counts the task's `shared` list from 1, like card images).
    public static func sharedImage(_ runID: String, _ number: Int) -> String { "/voice/shared-image/\(pathID(runID))/\(number)" }

    static func pathID(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn:
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")) ?? id
    }
}

/// A picture or file the plugin holds for this call (snapshot `attachments`): `pending` until a
/// request claims it, `sending` once one has. Sent ones leave the list.
public struct ServerAttachment: Equatable, Sendable {
    public var id: String
    public var kind: String
    public var name: String?
    public var app: String?
    public var sending: Bool
    public init(id: String, kind: String, name: String? = nil, app: String? = nil, sending: Bool = false) {
        self.id = id; self.kind = kind; self.name = name; self.app = app; self.sending = sending
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = o["id"] as? String, !id.isEmpty else { return nil }
        self.init(id: id, kind: o["kind"] as? String ?? "file", name: nonEmpty(o["name"]), app: nonEmpty(o["app"]),
                  sending: (o["state"] as? String) == "sending")
    }
}

/// A request held until screen sharing comes on ("Waiting for your screen"), then its outcome.
/// Decoded leniently: the plugin may send its own line, a state word, or both.
public struct ScreenHold: Equatable, Sendable {
    /// The plugin's word for where the hold is (`waiting` when it gives none).
    public var state: String
    /// The plugin's own line, when it sends one.
    public var text: String?
    public var taskID: String?
    public var expiresAt: Date?

    public init(state: String = "waiting", text: String? = nil, taskID: String? = nil, expiresAt: Date? = nil) {
        self.state = state; self.text = text; self.taskID = taskID; self.expiresAt = expiresAt
    }

    public init?(json: Any?) {
        if let line = nonEmpty(json) { self.init(text: String(line.prefix(120))); return }
        guard let o = json as? [String: Any] else { return nil }
        let state = (nonEmpty(o["state"]) ?? nonEmpty(o["status"]) ?? "waiting").lowercased()
        let text = (nonEmpty(o["text"]) ?? nonEmpty(o["line"]) ?? nonEmpty(o["label"])).map { String($0.prefix(120)) }
        self.init(state: state, text: text, taskID: nonEmpty(o["task_id"]) ?? nonEmpty(o["delegation_id"]),
                  expiresAt: decodeDate(o["expires_at"] ?? o["deadline"]))
    }

    static let waitingStates: Set<String> = ["waiting", "holding", "held", "open", "pending"]
    static let sentStates: Set<String> = ["sent", "released", "running", "started", "done", "captured"]

    /// Still waiting for the screen.
    public var isWaiting: Bool { Self.waitingStates.contains(state) }

    /// The line the panel shows: the plugin's own, else "Waiting for your screen" while it waits and
    /// "Not sent: screen sharing was off" once it ended unsent. Nil once the request went ahead (its
    /// task shows instead).
    public var line: String? {
        if let text { return text }
        if isWaiting { return "Waiting for your screen" }
        if Self.sentStates.contains(state) { return nil }
        return "Not sent: screen sharing was off"
    }
}

/// A short line about sharing on the panel's status line ("Up to 3 at a time"). Lives a few seconds.
public struct ShareNotice: Equatable, Sendable {
    public enum Action: Equatable, Sendable {
        case none
        /// "Set up": System Settings › Privacy & Security › Screen Recording.
        case openScreenSettings
    }
    public var text: String
    public var action: Action
    public var at: Date
    public init(text: String, action: Action = .none, at: Date) { self.text = text; self.action = action; self.at = at }

    public static let lifetime: TimeInterval = 6
    public func isFresh(at now: Date) -> Bool { now.timeIntervalSince(at) < Self.lifetime }
}

/// The panel's line for a refusal reason: the plugin's (`too_many`, `too_large`, `unsupported`,
/// `ended`, `sent`, `permission`, `not_declared`), an `AttachmentFailure` from encoding a drop, or
/// the client's own (`old_plugin`, `no_images`, `no_vision`, `remove_failed`, `toggle_failed`). Nil when there's
/// nothing to say (the call ended; a capture request closed).
public func shareRefusalLine(_ reason: String?) -> String? {
    switch reason {
    case "ended", "closed": return nil
    case "too_many": return "Up to \(AttachmentPolicy.maxAttachments) at a time"
    case "too_large": return "That's too big to send"
    case "unsupported": return "Only pictures and files"
    case "sent": return "Already sent with your request"
    case "permission": return "Screen Recording is off · Set up"
    case "not_declared": return "Screen sharing isn't available on this call"
    case "old_plugin": return "Update Speakeasy on your Hermes to share"
    case "no_images": return "Your Hermes can't take pictures yet"
    case "no_vision": return "Your Hermes can't read pictures"
    case "remove_failed": return "Couldn't remove that. Try again."
    case "toggle_failed": return "Screen sharing didn't change. Try again."
    default: return "Couldn't attach that"
    }
}

/// A picture or file dropped or pasted on the panel, waiting to go with the next request. Only its
/// description lives here; the call client keeps the bytes (and re-uploads them after a resume).
public struct PendingAttachment: Equatable, Sendable, Identifiable {
    public enum Kind: String, Equatable, Sendable { case picture, file }
    public enum Phase: Equatable, Sendable {
        /// Kept on this device only: the call isn't taking uploads (connecting, paused).
        case local
        case uploading
        /// The plugin holds it for the next request.
        case pending
        /// A request has claimed it.
        case sending
    }
    /// The client's own id (stable across re-uploads).
    public var id: String
    public var kind: Kind
    public var name: String
    public var byteCount: Int
    public var phase: Phase = .local
    /// The plugin's id, while it holds it.
    public var serverID: String?
    /// The call it was (or is being) uploaded to.
    public var interactionID: String?
    /// The call that wouldn't take it (404, or 409 `ended`: it paused, or the plugin lost it). Never
    /// offered to that call again, which would only refuse it again; the next session (Resume) takes it.
    public var deferredFor: String?
    public var uploadedAt: Date?
    /// A snapshot has listed it: its absence later means it was sent.
    public var seen = false
    /// The ✕ was pressed; hidden while the plugin lets go of it.
    public var removing = false
    /// Dropped or pasted with no call open: kept past the ended call or the closed board, and sent
    /// with the next call.
    public var forNextCall = false

    public init(id: String, kind: Kind, name: String, byteCount: Int) {
        self.id = id; self.kind = kind; self.name = name; self.byteCount = byteCount
    }
}

/// What happens to a call's sharing (VoiceEvent `.sharing`).
public enum SharingEvent: Equatable, Sendable {
    /// What this call declares, or declared, on `POST /voice/sessions` (nil: nothing, no sharing).
    case declared(ScreenDeclaration?)
    /// The screen button or shortcut.
    case toggle
    /// The plugin's sharing state in force: the `/screen` reply, a `screen.state` event.
    case state(ScreenToggle)
    /// `/screen` failed for the toggle sent with `seq`; `reason` is the plugin's, nil for no answer.
    case toggleFailed(seq: Int, reason: String?)
    /// The plugin asked for a capture (a `capture` event, or the snapshot's open requests).
    case captureRequested(String)
    /// The plugin wants the button noticed (`screen.hint`, e.g. a request about the screen while it's off).
    case hint(String)
    /// Dropped or pasted.
    case added(PendingAttachment)
    /// Its upload to this call started.
    case uploading(String, interactionID: String)
    case uploaded(String, serverID: String, interactionID: String)
    /// The call wasn't taking uploads (it paused, or the plugin lost it): kept for the next session,
    /// never retried on this one.
    case uploadDeferred(String, interactionID: String)
    /// Refused or gone; the notice says why (`shareRefusalLine`).
    case refused(String, reason: String?)
    /// The ✕: hidden now, removed once the plugin let go of it.
    case removing(String)
    case removed(String)
    case removeFailed(String)
    /// A line for the status line, by reason (`shareRefusalLine`).
    case notice(String)
}

/// One call's sharing. Off at the start of every call and every resume; the button shows the
/// plugin-confirmed state, or the user's newer choice while it's on its way.
public struct CallSharing: Equatable, Sendable {
    public var declared: ScreenDeclaration?
    /// The plugin's state in force, and the seq it came with (older reports are stale).
    public private(set) var confirmedOn = false
    public private(set) var confirmedSeq = 0
    /// The user's choice not confirmed yet (optimistic; rolled back if `/screen` fails).
    public private(set) var requestedOn: Bool?
    /// Pressed before the call was admitted: sent once it is, before anything else.
    public private(set) var queuedOn: Bool?
    /// Highest sequence number sent or seen this call.
    public private(set) var seq = 0
    /// The toggle in flight (its seq), if any.
    public private(set) var sentSeq: Int?
    /// The newest toggle to send. The client sends each new value once.
    public private(set) var outgoing: ScreenToggle?
    /// Capture requests accepted for capturing, oldest first (deduplicated; the client serves new ones).
    public private(set) var captures: [String] = []
    /// Grows on every `screen.hint` (the button pulses once per change).
    public private(set) var hintPulse = 0
    public private(set) var hint: String?
    /// "Waiting for your screen", or how that ended.
    public var hold: ScreenHold?
    /// Dropped pictures and files, in drop order.
    public private(set) var attachments: [PendingAttachment] = []
    public private(set) var notice: ShareNotice?

    public init() {}

    static let capturesKept = 32
    /// The status line after a drop with no call open.
    public static let nextCallLine = "Goes with your next call"
    /// A plugin listing that doesn't name an upload this long after it landed means it was sent
    /// (covers polling, where the "pending" moment can fall between two polls).
    public static let unseenGrace: TimeInterval = 3

    /// Sharing as the user chose it (the latest choice, confirmed or on its way).
    public var isOn: Bool { declared == .ready && (requestedOn ?? confirmedOn) }

    public var button: ScreenButton {
        switch declared {
        case nil: return .unavailable
        case .noPermission?: return .needsPermission
        case .ready?: return isOn ? .on : .off
        }
    }

    /// Attachments the panel shows (removed ones disappear at once).
    public var visibleAttachments: [PendingAttachment] { attachments.filter { !$0.removing } }

    /// Waiting on this device for the call to take them.
    public var hasLocalAttachments: Bool { attachments.contains { $0.phase == .local && !$0.removing } }

    /// What the client uploads to `interactionID` now: waiting on this device, not being removed, and
    /// not already turned away by that same call (it would only refuse again, in a loop while the call
    /// still looks live). A new session's id takes them all.
    public func uploadable(to interactionID: String) -> [PendingAttachment] {
        attachments.filter { $0.phase == .local && !$0.removing && $0.deferredFor != interactionID }
    }

    // MARK: Events

    /// `admitted`: the interaction id when the plugin has admitted the call; `live`: the call is
    /// connecting or live and not pausing (uploads and captures are welcome).
    public mutating func apply(_ event: SharingEvent, now: Date, admitted: String?, live: Bool) {
        switch event {
        case .declared(let declaration):
            declared = declaration
            if declaration != .ready { requestedOn = nil; queuedOn = nil }

        case .toggle:
            guard live else { return }
            switch declared {
            case nil: return
            case .noPermission?:
                notice = ShareNotice(text: shareRefusalLine("permission") ?? "", action: .openScreenSettings, at: now)
            case .ready?:
                let target = !isOn
                requestedOn = target
                if admitted == nil {
                    queuedOn = target
                } else {
                    send(target)
                }
            }

        case .state(let toggle):
            adopt(toggle)

        case .toggleFailed(let failedSeq, let reason):
            guard failedSeq == sentSeq else { return }
            requestedOn = nil
            sentSeq = nil
            switch reason {
            case "ended": break
            case "permission":
                declared = .noPermission
                notice = ShareNotice(text: shareRefusalLine("permission") ?? "", action: .openScreenSettings, at: now)
            case "not_declared":
                declared = nil
                notice = ShareNotice(text: shareRefusalLine("not_declared") ?? "", at: now)
            default:
                notice = ShareNotice(text: shareRefusalLine("toggle_failed") ?? "", at: now)
            }

        case .captureRequested(let id):
            // Only while sharing is on, never twice for one request.
            guard live, admitted != nil, isOn, !id.isEmpty, !captures.contains(id) else { return }
            captures.append(id)
            if captures.count > Self.capturesKept { captures.removeFirst(captures.count - Self.capturesKept) }

        case .hint(let reason):
            hint = reason
            hintPulse += 1

        case .added(var item):
            guard !attachments.contains(where: { $0.id == item.id }) else { return }
            guard visibleAttachments.count < AttachmentPolicy.maxAttachments else {
                notice = ShareNotice(text: shareRefusalLine("too_many") ?? "", at: now)
                return
            }
            item.phase = .local
            item.serverID = nil
            item.interactionID = nil
            item.deferredFor = nil
            attachments.append(item)
            // With no call open, say where it went (a refusal from the same drop keeps the line).
            if item.forNextCall, notice?.isFresh(at: now) != true { notice = ShareNotice(text: Self.nextCallLine, at: now) }

        case .uploading(let id, let interactionID):
            update(id) { item in
                item.phase = .uploading
                item.interactionID = interactionID
                item.serverID = nil
                item.seen = false
                item.uploadedAt = nil
                item.forNextCall = false   // this call's now: it goes with it, or not at all
            }

        case .uploaded(let id, let serverID, let interactionID):
            update(id) { item in
                // A late answer from an earlier session's upload changes nothing.
                guard item.phase == .uploading, item.interactionID == interactionID else { return }
                if live && admitted == interactionID {
                    item.phase = .pending
                    item.serverID = serverID
                    item.uploadedAt = now
                } else {
                    // The call paused meanwhile: the plugin dropped it; it goes again after Resume.
                    item.phase = .local
                    item.serverID = nil
                }
            }

        case .uploadDeferred(let id, let interactionID):
            guard let item = attachments.first(where: { $0.id == id }), item.interactionID == interactionID,
                  item.phase == .uploading else { return }
            if item.removing {
                drop(id)
            } else {
                update(id) { $0.phase = .local; $0.serverID = nil; $0.deferredFor = interactionID }
            }

        case .refused(let id, let reason):
            guard let item = attachments.first(where: { $0.id == id }) else { return }
            drop(id)
            if !item.removing, let line = shareRefusalLine(reason) { notice = ShareNotice(text: line, at: now) }

        case .removing(let id):
            update(id) { $0.removing = true }

        case .removed(let id):
            drop(id)

        case .removeFailed(let id):
            guard attachments.contains(where: { $0.id == id }) else { return }
            update(id) { $0.removing = false }
            notice = ShareNotice(text: shareRefusalLine("remove_failed") ?? "", at: now)

        case .notice(let reason):
            if let line = shareRefusalLine(reason) {
                notice = ShareNotice(text: line, action: reason == "permission" ? .openScreenSettings : .none, at: now)
            }
        }
    }

    /// The call was admitted: a toggle pressed while connecting goes now (turning it off before
    /// admission needs nothing sent: a call starts with sharing off).
    public mutating func admitted() {
        guard let queued = queuedOn else { return }
        queuedOn = nil
        if queued && declared == .ready { send(true) } else { requestedOn = nil }
    }

    /// The plugin's view of this call (an interaction snapshot for the current call).
    public mutating func reconcile(_ snapshot: InteractionSnapshot, now: Date) {
        if let screen = snapshot.screen { adopt(ScreenToggle(on: screen.on, seq: screen.seq)) }
        if snapshot.hasHold { hold = snapshot.hold }
        guard let listed = snapshot.attachments, let interactionID = snapshot.interactionID else { return }
        var gone: [String] = []
        for index in attachments.indices {
            let item = attachments[index]
            guard item.interactionID == interactionID, let serverID = item.serverID,
                  item.phase == .pending || item.phase == .sending else { continue }
            if let match = listed.first(where: { $0.id == serverID }) {
                attachments[index].seen = true
                attachments[index].phase = match.sending ? .sending : .pending
            } else if item.seen || item.uploadedAt.map({ now.timeIntervalSince($0) >= Self.unseenGrace }) == true {
                gone.append(item.id)   // sent with a request: its task shows it now
            }
        }
        attachments.removeAll { gone.contains($0.id) }
    }

    /// A new session continues the call (Resume): sharing starts off again, the new call's own
    /// requests and hold come from its snapshots. Pending pictures and files stay to be re-sent,
    /// including ones the old session turned away.
    public mutating func resumed() {
        resetScreen()
        notice = nil
        for index in attachments.indices { attachments[index].deferredFor = nil }
    }

    /// The call paused (`paused`) or ended, or no call is open. Paused keeps the pictures and files
    /// not sent yet (the plugin dropped its copies; they go again after Resume); ones already claimed
    /// by a request went with it. Ended keeps nothing of the call's, only what was dropped with no
    /// call open (`forNextCall`). `closing`: the call closed just now, so its line goes too.
    public mutating func callClosed(paused: Bool, closing: Bool = true) {
        resetScreen()
        if paused {
            attachments.removeAll { $0.phase == .sending || ($0.removing && $0.phase != .uploading) }
            for index in attachments.indices where attachments[index].phase == .pending {
                attachments[index].phase = .local
                attachments[index].serverID = nil
                attachments[index].seen = false
            }
        } else {
            attachments.removeAll { !$0.forNextCall || $0.removing }
            if closing { notice = nil }
            declared = nil
        }
    }

    /// A new call (or the board): nothing of the last one, except what was dropped for the next call.
    public func carriedToNextCall() -> CallSharing {
        var next = CallSharing()
        next.attachments = attachments.filter { $0.forNextCall && !$0.removing }
        return next
    }

    /// Old notices fade.
    public mutating func tick(now: Date) {
        if let notice, !notice.isFresh(at: now) { self.notice = nil }
    }

    // MARK: Helpers

    private mutating func resetScreen() {
        confirmedOn = false
        confirmedSeq = 0
        requestedOn = nil
        queuedOn = nil
        seq = 0
        sentSeq = nil
        outgoing = nil
        captures = []
        hold = nil
        hint = nil
    }

    private mutating func send(_ on: Bool) {
        seq += 1
        sentSeq = seq
        outgoing = ScreenToggle(on: on, seq: seq)
    }

    /// The plugin's state in force. A report older than one already applied (a late snapshot) is
    /// ignored. One older than the toggle still on its way only updates what's confirmed: the user's
    /// newer choice keeps showing until its own answer arrives.
    private mutating func adopt(_ toggle: ScreenToggle) {
        guard toggle.seq >= confirmedSeq else { return }
        seq = max(seq, toggle.seq)
        confirmedOn = toggle.on
        confirmedSeq = toggle.seq
        if let sent = sentSeq, toggle.seq < sent { return }
        sentSeq = nil
        if queuedOn == nil { requestedOn = nil }
    }

    private mutating func update(_ id: String, _ change: (inout PendingAttachment) -> Void) {
        guard let index = attachments.firstIndex(where: { $0.id == id }) else { return }
        change(&attachments[index])
    }

    private mutating func drop(_ id: String) { attachments.removeAll { $0.id == id } }
}

/// The reducer events one interaction snapshot carries: the snapshot itself, plus one
/// `captureRequested` per open capture request, so a polling client serves captures exactly like
/// one that gets `capture` events.
public func interactionEvents(_ snapshot: InteractionSnapshot) -> [VoiceEvent] {
    [.interaction(snapshot)] + snapshot.captures.map { .sharing(.captureRequested($0)) }
}

private func isBool(_ number: NSNumber) -> Bool { CFGetTypeID(number) == CFBooleanGetTypeID() }
