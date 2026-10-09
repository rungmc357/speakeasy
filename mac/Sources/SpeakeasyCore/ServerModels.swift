import Foundation

// Typed, sanitized views of the api contract (docs/live-contract.md v2).
// Decoding is deliberately tolerant: unknown fields are ignored and missing
// optional fields decode as nil, so additive api changes never break the app.

public struct WorkEventItem: Equatable, Sendable {
    public var kind: String   // request | milestone | result
    public var text: String
    public var at: Date?
    public init(kind: String, text: String, at: Date?) { self.kind = kind; self.text = text; self.at = at }
}

public struct ProductCard: Equatable, Sendable, Identifiable {
    public let number: Int
    public let name: String
    public let url: URL
    public let imageURL: URL?
    public let price: String?
    public let store: String?
    public let rating: String?
    public let specs: [String]
    public var id: Int { number }

    public init?(json: Any?, number: Int) {
        guard let raw = json as? [String: Any], let name = raw["name"] as? String,
              let destination = raw["url"] as? String, let url = URL(string: destination),
              ["https", "http"].contains(url.scheme?.lowercased() ?? ""), url.host != nil else { return nil }
        self.number = number; self.name = name; self.url = url
        if let image = raw["image_url"] as? String, let candidate = URL(string: image),
           candidate.scheme?.lowercased() == "https", candidate.host != nil {
            imageURL = candidate
        } else { imageURL = nil }
        price = raw["price"] as? String; store = raw["store"] as? String
        rating = raw["rating"] as? String; specs = raw["specs"] as? [String] ?? []
    }
}

/// A picture a task produced (a Hermes render sent to Discord, or a web image). The app
/// only knows its number and name; bytes come from the authenticated api route
/// `/voice/card-image/<run>/<number>`, never from a path or URL the app opens itself.
public struct ImageCard: Equatable, Sendable, Identifiable {
    public let number: Int
    public let name: String
    public var id: Int { number }

    public init(number: Int, name: String) { self.number = number; self.name = name }

    public init?(json: Any?, number: Int) {
        guard let raw = json as? [String: Any], raw["kind"] as? String == "image", (1...8).contains(number) else { return nil }
        let name = (raw["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.number = number
        self.name = name.isEmpty ? "Image \(number)" : String(name.prefix(120))
    }
}

/// Why a task failed, in Speakeasy's own words (the api maps Hermes' error; the provider's raw
/// text never reaches the app). Present only on `failed` tasks.
public struct WorkFailure: Equatable, Sendable {
    /// `billing`, `auth`, `rate_limit`, `model_not_found`, `hermes_key`, `hermes_unreachable` or `unknown`.
    public var kind: String
    /// Short tag for the status line ("Out of credits"); nil when the reason isn't known.
    public var label: String?
    /// What happened and what to do, for the task detail.
    public var text: String

    public init(kind: String, label: String? = nil, text: String) {
        self.kind = kind; self.label = label; self.text = text
    }

    public init?(json: Any?) {
        guard let raw = json as? [String: Any], let text = nonEmpty(raw["text"]) else { return nil }
        kind = nonEmpty(raw["kind"]) ?? "unknown"
        label = nonEmpty(raw["label"]).map { String($0.prefix(60)) }
        self.text = String(text.prefix(400))
    }
}

public struct WorkResult: Equatable, Sendable {
    public var spoken: String?
    public var full: String?
    /// Short past-tense label for the one-line status ("Weather checked").
    public var label: String?
    public var products: [ProductCard]
    public var images: [ImageCard]
    /// Visual cards for the answer (a price chart, the weather, a game…), drawn above the text.
    public var views: [ViewCard]
    public init(spoken: String?, full: String?, label: String? = nil, products: [ProductCard] = [],
                images: [ImageCard] = [], views: [ViewCard] = []) {
        self.spoken = spoken; self.full = full; self.label = label; self.products = products; self.images = images
        self.views = views
    }
}

public struct WorkInfo: Equatable, Sendable {
    public var runID: String?
    public var status: String
    public var stale: Bool
    public var updated: Date?
    public var shortStatus: String?
    public var detail: String?
    public var updatedAt: Date?
    public var statusSource: String?
    public var events: [WorkEventItem]
    public var result: WorkResult?
    /// Short semantic name for the task list ("Weekend weather"); nil until named.
    public var title: String?
    /// One clean sentence of what the user asked for; nil until the api rewrites it.
    public var summary: String?
    /// Emails this task drafted; pending ones need the user's decision.
    public var emailDrafts: [EmailDraft] = []
    /// Image card numbers still waiting on the call panel's review card (finished tasks only).
    public var reviewImages: [Int] = []
    public var reviewSettledAt: Date?
    /// The latest image the task produced or is looking at (live view); bytes via `/voice/live-image/<run>`.
    public var liveImage: LiveImage?
    /// Why a `failed` task failed (out of credits, rejected key, …); nil for any other status.
    public var failure: WorkFailure?

    public init(runID: String?, status: String, stale: Bool = false, updated: Date? = nil,
                shortStatus: String? = nil, detail: String? = nil, updatedAt: Date? = nil,
                statusSource: String? = nil, events: [WorkEventItem] = [], result: WorkResult? = nil,
                title: String? = nil, summary: String? = nil, emailDrafts: [EmailDraft] = []) {
        self.emailDrafts = emailDrafts
        self.runID = runID; self.status = status; self.stale = stale; self.updated = updated
        self.shortStatus = shortStatus; self.detail = detail; self.updatedAt = updatedAt
        self.statusSource = statusSource; self.events = events; self.result = result
        self.title = title; self.summary = summary
    }

    public static let terminalStatuses: Set<String> = ["completed", "failed", "cancelled", "interrupted"]
    public var isTerminal: Bool { Self.terminalStatuses.contains(status) }

    /// Request text the user actually said, if the api recorded it.
    public var request: String? { events.first { $0.kind == "request" }?.text }

    /// What the user asked, as one clean sentence (rewritten by the api); the raw speech
    /// fragment is the fallback.
    public var askedFor: String? { summary ?? request }

    /// Numbering is scoped to this task's final answer; it survives call pause/reopen.
    public var cards: [ResultCard] { ResultCard.links(in: result?.full ?? result?.spoken ?? "") }
    public var products: [ProductCard] { result?.products ?? [] }
    public var images: [ImageCard] { result?.images ?? [] }
    public var views: [ViewCard] { result?.views ?? [] }

    /// Progress steps worth showing: everything except the raw request fragment.
    public var steps: [WorkEventItem] { events.filter { $0.kind != "request" } }

    /// Meaningful authored milestones only: receipts are not progress.
    public var milestones: [WorkEventItem] {
        events.filter { $0.kind == "milestone" && !Self.receiptTexts.contains($0.text) }
    }
    static let receiptTexts: Set<String> = ["Request received", "Started working"]

    public init?(json: Any?) {
        guard let object = json as? [String: Any] else { return nil }
        runID = (object["run_id"] as? String) ?? (object["source_run_id"] as? String)
        status = object["status"] as? String ?? "running"
        stale = object["stale"] as? Bool ?? false
        updated = decodeDate(object["updated"])
        shortStatus = nonEmpty(object["short_status"])
        detail = nonEmpty(object["detail"])
        updatedAt = decodeDate(object["updated_at"])
        statusSource = object["status_source"] as? String
        events = (object["events"] as? [Any] ?? []).compactMap { raw in
            guard let item = raw as? [String: Any], let kind = item["kind"] as? String else { return nil }
            let text = item["text"].map { "\($0)" } ?? ""
            return WorkEventItem(kind: kind, text: text, at: decodeDate(item["at"]))
        }
        title = nonEmpty(object["title"])
        summary = nonEmpty(object["summary"])
        emailDrafts = EmailDraft.list(json: object["email_drafts"])
        liveImage = LiveImage(json: object["live_image"])
        failure = status == "failed" ? WorkFailure(json: object["failure"]) : nil
        if let review = object["review"] as? [String: Any] {
            reviewImages = (review["images"] as? [Any] ?? []).compactMap { ($0 as? NSNumber)?.intValue }.filter { (1...8).contains($0) }
            reviewSettledAt = decodeDate(review["settled_at"])
        }
        if let raw = object["result"] as? [String: Any] {
            // Numbers are positions in the api's card list, so they address the image route.
            let cards = Array((raw["cards"] as? [Any] ?? []).prefix(8)).enumerated()
            let products = cards.compactMap { ($0.element as? [String: Any])?["kind"] as? String == "image"
                ? nil : ProductCard(json: $0.element, number: $0.offset + 1) }
            let images = cards.compactMap { ImageCard(json: $0.element, number: $0.offset + 1) }
            let views = ViewCard.list(json: raw["views"])
            let value = WorkResult(spoken: nonEmpty(raw["spoken"]), full: nonEmpty(raw["full"]),
                                   label: nonEmpty(raw["label"]), products: products, images: images, views: views)
            // Cards alone are still a result: never drop cards because the text is empty.
            result = (value.spoken == nil && value.full == nil && products.isEmpty && images.isEmpty && views.isEmpty)
                ? nil : value
        } else {
            result = nil
        }
    }
}

/// What a running task is looking at right now. Only a name and a sequence number reach the app;
/// the bytes come from the authenticated `/voice/live-image/<run>` route and `seq` says when to refetch.
public struct LiveImage: Equatable, Sendable {
    public var name: String
    public var source: String
    public var seq: Int
    public var at: Date?
    public init(name: String, source: String = "viewed", seq: Int, at: Date? = nil) {
        self.name = name; self.source = source; self.seq = seq; self.at = at
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let seq = (o["seq"] as? NSNumber)?.intValue, seq > 0 else { return nil }
        let name = (o["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.init(name: name.isEmpty ? "Image" : String(name.prefix(120)), source: o["source"] as? String ?? "viewed",
                  seq: seq, at: decodeDate(o["at"]))
    }
    /// Section title in the task detail.
    public var label: String { source == "generated" ? "Latest image" : "Looking at" }
}

/// One voice task of the call. Several can run in parallel; `id` is the
/// GPT-Live delegation id (stable across Pause/Resume).
public struct TaskItem: Equatable, Sendable, Identifiable {
    public var id: String
    public var info: WorkInfo
    public init(id: String, info: WorkInfo) { self.id = id; self.info = info }

    public init?(json: Any?) {
        guard let object = json as? [String: Any], let id = object["task_id"] as? String,
              let info = WorkInfo(json: object) else { return nil }
        self.init(id: id, info: info)
    }

    /// What the task list calls this task: its short title, else what the user said.
    public var name: String { info.title ?? info.request ?? "Earlier request" }

    /// Finished one way or another: can be cleared from the list.
    public var isSettled: Bool { info.isTerminal || info.status == "rejected" }

    /// Still in flight: counts toward "N tasks" and can be stopped.
    public var isActive: Bool { !info.isTerminal && info.status != "rejected" && info.status != "ambiguous" }

    /// Active, has a Hermes run to stop, and no stop is already in flight.
    public var canStop: Bool { isActive && info.runID != nil && info.status != "cancel_requested" }

    public static func list(json: Any?) -> [TaskItem]? {
        guard let array = json as? [Any] else { return nil }
        return array.compactMap(TaskItem.init(json:))
    }
}

public struct ApprovalInfo: Equatable, Sendable {
    public var runID: String
    public var requestID: String
    public var description: String
    public init(runID: String, requestID: String, description: String) {
        self.runID = runID; self.requestID = requestID; self.description = description
    }
    public init?(json: Any?) {
        guard let object = json as? [String: Any],
              let requestID = object["request_id"] as? String,
              let runID = object["run_id"] as? String else { return nil }
        self.init(runID: runID, requestID: requestID,
                  description: object["description"] as? String ?? "Approval needed to continue.")
    }
}

public struct InteractionSnapshot: Equatable, Sendable {
    public var interactionID: String?
    public var status: String?
    public var runID: String?
    public var approval: ApprovalInfo?
    public var finalization: String?
    public var error: String?
    public var paused: Bool

    public init(interactionID: String? = nil, status: String? = nil, runID: String? = nil,
                approval: ApprovalInfo? = nil, finalization: String? = nil, error: String? = nil,
                paused: Bool = false) {
        self.interactionID = interactionID; self.status = status; self.runID = runID
        self.approval = approval; self.finalization = finalization; self.error = error
        self.paused = paused
    }

    public init?(json: Any?) {
        guard let object = json as? [String: Any] else { return nil }
        interactionID = object["interaction_id"] as? String
        status = object["status"] as? String
        runID = (object["backend_run_id"] as? String) ?? (object["run_id"] as? String)
        approval = ApprovalInfo(json: object["approval"])
        finalization = object["finalization"] as? String
        error = nonEmpty(object["error"])
        paused = object["paused"] as? Bool ?? false
    }
}

/// Response of `POST /voice/sessions`. The live api returns
/// `{"interaction_id", "transport": {"sdp"}}`; contract v2 documents a top-level `sdp`.
public struct SessionAdmission: Equatable, Sendable {
    public var interactionID: String
    public var answerSDP: String
    public var voiceProvider: String
    public init?(json: Any?) {
        guard let object = json as? [String: Any],
              let interactionID = object["interaction_id"] as? String else { return nil }
        let transport = object["transport"] as? [String: Any]
        guard let sdp = (transport?["sdp"] as? String) ?? (object["sdp"] as? String), !sdp.isEmpty else { return nil }
        self.interactionID = interactionID
        self.answerSDP = sdp
        self.voiceProvider = object["voice_provider"] as? String ?? "openai"
    }
}

/// Encodes the offer body without touching the SDP: its final CRLF is significant.
/// `resumeFrom` names a paused call whose conversation the new session continues.
/// `tour` asks for the one-time first-call tour; values are the shortcut labels to mention
/// (`call`, `mute`, `pause`). Never sent with `resumeFrom`.
/// `room` is what listening mode heard (rendered transcript lines): with a new call, or with the
/// resume of a call that listening mode paused mid-conversation. Only to servers whose status says
/// `room_listening`, and it replaces the tour: a call opened from listening mode isn't a tour.
public func sessionRequestBody(sdp: String, resumeFrom: String? = nil, tour: [String: String]? = nil,
                               room: String? = nil) throws -> Data {
    var body: [String: Any] = ["sdp": sdp]
    if let room, !room.isEmpty { body["room"] = room }
    if let resumeFrom { body["resume_from"] = resumeFrom }
    else if body["room"] == nil, let tour { body["tour"] = tour }
    return try JSONSerialization.data(withJSONObject: body, options: [])
}

func decodeDate(_ value: Any?) -> Date? {
    if let number = value as? NSNumber { return Date(timeIntervalSince1970: number.doubleValue) }
    if let double = value as? Double { return Date(timeIntervalSince1970: double) }
    return nil
}

func nonEmpty(_ value: Any?) -> String? {
    guard let text = value as? String else { return nil }
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : text
}
