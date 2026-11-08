import Foundation
import SpeakeasyCore

// Hermes' projects (servers with `projects`, SPK-254; docs/API.md "Projects").

extension ServerClient {
    private static func safeID(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-_"))) ?? id
    }

    public func projects() async throws -> ProjectList {
        ProjectList(json: try await json("/voice/projects"))
    }

    public func projectPage(_ id: String) async throws -> ProjectPage {
        guard let page = ProjectPage(json: try await json("/voice/projects/\(Self.safeID(id))")) else {
            throw HTTPError(status: 502, message: "Your Hermes sent back something unexpected.")
        }
        return page
    }

    public func createProject(_ project: NewProject) async throws -> HermesProject {
        guard let body = project.body() else { throw HTTPError(status: 400, message: "A project needs a name.") }
        guard let made = HermesProject(json: try await post("/voice/projects", body)["project"]) else {
            throw HTTPError(status: 502, message: "Your Hermes sent back something unexpected.")
        }
        return made
    }

    /// File a chat (`sessionID`) or a job (`runID`) into a project; nil takes it out, "" declines a
    /// job card's suggestion.
    public func fileIntoProject(sessionID: String? = nil, runID: String? = nil, projectID: String?) async throws {
        var body: [String: Any] = ["project_id": projectID.map { $0 as Any } ?? NSNull()]
        if let sessionID { body["session_id"] = sessionID }
        if let runID { body["run_id"] = runID }
        _ = try await post("/voice/projects/file", body)
    }

    public func setProjectNotes(_ notes: String, projectID: String) async throws {
        _ = try await json("/voice/projects/\(Self.safeID(projectID))/notes", method: "PUT",
                           body: try JSONSerialization.data(withJSONObject: ["notes": notes]))
    }

    /// Where Home works: a project's id, or nil for Home (`PUT /voice/projects/selected`).
    public func selectProject(_ projectID: String?) async throws {
        _ = try await json("/voice/projects/selected", method: "PUT",
                           body: try JSONSerialization.data(withJSONObject: ["project_id": projectID.map { $0 as Any } ?? NSNull()]))
    }

    public func dismissProjectOffer(_ subject: String) async throws {
        _ = try await post("/voice/projects/offer/dismiss", ["subject": subject])
    }
}

@MainActor
public final class LiveProjectsBackend: ProjectsBackend {
    let api: () -> ServerClient?
    public init(api: @escaping () -> ServerClient?) { self.api = api }
    private func client() throws -> ServerClient {
        guard let api = api() else { throw ServerClient.HTTPError(status: 0, message: "Pair with your Hermes first.") }
        return api
    }
    public func projects() async throws -> ProjectList { try await client().projects() }
    public func page(_ id: String) async throws -> ProjectPage { try await client().projectPage(id) }
    public func create(_ project: NewProject) async throws -> HermesProject { try await client().createProject(project) }
    public func file(session: HistorySession, into projectID: String?) async throws {
        try await client().fileIntoProject(sessionID: session.id, projectID: projectID)
    }
    public func file(runID: String, into projectID: String?) async throws {
        try await client().fileIntoProject(runID: runID, projectID: projectID)
    }
    public func setNotes(_ notes: String, for projectID: String) async throws {
        try await client().setProjectNotes(notes, projectID: projectID)
    }
    public func dismissOffer(_ subject: String) async throws { try await client().dismissProjectOffer(subject) }
    public func select(_ projectID: String?) async throws { try await client().selectProject(projectID) }
}
