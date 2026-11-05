import Foundation
import SpeakeasyCore

// Hermes scheduled jobs (servers with `schedules`; docs/API.md "Schedules").

extension ServerClient {
    public func schedules() async throws -> ScheduleList {
        ScheduleList(json: try await json("/voice/schedules"))
    }

    public func schedule(_ id: String) async throws -> ScheduleDetail {
        try Self.detail(try await json(ScheduleDetail.path(id)))
    }

    /// Reads a sentence ("every weekday at 8 summarize my inbox") into a preview. Saves nothing.
    public func previewSchedule(text: String) async throws -> SchedulePreview {
        guard let body = SchedulePreview.requestBody(text: text) else {
            throw HTTPError(status: 400, message: "Say when it should run and what it should do.")
        }
        return try Self.preview(try await post("/voice/schedules/preview", body))
    }

    /// A separate when ("weekdays at 9", "0 9 * * 1-5") and what. Saves nothing.
    public func previewSchedule(schedule: String, prompt: String) async throws -> SchedulePreview {
        guard let body = SchedulePreview.requestBody(schedule: schedule, prompt: prompt) else {
            throw HTTPError(status: 400, message: "Say when it should run and what it should do.")
        }
        return try Self.preview(try await post("/voice/schedules/preview", body))
    }

    /// Saves a confirmed preview.
    public func createSchedule(_ preview: SchedulePreview, name: String? = nil, deliver: String? = nil) async throws -> ScheduleDetail {
        try Self.detail(try await post("/voice/schedules", preview.createBody(name: name, deliver: deliver)))
    }

    public func pauseSchedule(_ id: String) async throws -> ScheduleDetail {
        try Self.detail(try await post(ScheduleDetail.path(id) + "/pause", [:]))
    }

    public func resumeSchedule(_ id: String) async throws -> ScheduleDetail {
        try Self.detail(try await post(ScheduleDetail.path(id) + "/resume", [:]))
    }

    /// Queues one run now (within a minute). A paused job resumes (`resumed` says so).
    public func runScheduleNow(_ id: String) async throws -> ScheduleDetail {
        try Self.detail(try await post(ScheduleDetail.path(id) + "/run", [:]))
    }

    /// Any of the schedule (words or Hermes' forms), the prompt, the name.
    public func updateSchedule(_ id: String, schedule: String? = nil, prompt: String? = nil, name: String? = nil) async throws -> ScheduleDetail {
        var body: [String: Any] = [:]
        if let schedule { body["schedule"] = schedule }
        if let prompt { body["prompt"] = prompt }
        if let name { body["name"] = name }
        guard !body.isEmpty else { throw HTTPError(status: 400, message: "Nothing to change.") }
        return try Self.detail(try await json(ScheduleDetail.path(id), method: "PATCH",
                                              body: try JSONSerialization.data(withJSONObject: body)))
    }

    public func deleteSchedule(_ id: String) async throws {
        _ = try await json(ScheduleDetail.path(id), method: "DELETE")
    }

    private static func detail(_ object: [String: Any]) throws -> ScheduleDetail {
        guard let detail = ScheduleDetail(json: object) else {
            throw HTTPError(status: 502, message: "Your Hermes sent back something unexpected.")
        }
        return detail
    }

    private static func preview(_ object: [String: Any]) throws -> SchedulePreview {
        guard let preview = SchedulePreview(json: object) else {
            throw HTTPError(status: 502, message: "Your Hermes sent back something unexpected.")
        }
        return preview
    }
}

extension ServerClient.HTTPError: StatusCarrying {}

@MainActor
public final class LiveScheduleBackend: ScheduleBackend {
    let api: () -> ServerClient?
    public init(api: @escaping () -> ServerClient?) { self.api = api }
    public convenience init(api: ServerClient) { self.init(api: { api }) }
    private func client() throws -> ServerClient {
        guard let api = api() else { throw ServerClient.HTTPError(status: 0, message: "Pair with your Hermes first.") }
        return api
    }
    public func list() async throws -> ScheduleList { try await client().schedules() }
    public func detail(_ id: String) async throws -> ScheduleDetail { try await client().schedule(id) }
    public func preview(text: String) async throws -> SchedulePreview { try await client().previewSchedule(text: text) }
    public func preview(schedule: String, prompt: String) async throws -> SchedulePreview {
        try await client().previewSchedule(schedule: schedule, prompt: prompt)
    }
    public func create(_ preview: SchedulePreview, name: String?, deliver: String?) async throws -> ScheduleDetail {
        try await client().createSchedule(preview, name: name, deliver: deliver)
    }
    public func pause(_ id: String) async throws -> ScheduleDetail { try await client().pauseSchedule(id) }
    public func resume(_ id: String) async throws -> ScheduleDetail { try await client().resumeSchedule(id) }
    public func runNow(_ id: String) async throws -> ScheduleDetail { try await client().runScheduleNow(id) }
    public func update(_ id: String, schedule: String?, prompt: String?, name: String?) async throws -> ScheduleDetail {
        try await client().updateSchedule(id, schedule: schedule, prompt: prompt, name: name)
    }
    public func delete(_ id: String) async throws { try await client().deleteSchedule(id) }
}

