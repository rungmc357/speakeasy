import Foundation
import SpeakeasyCore

// Files your agent made (servers with `files`; docs/API.md "Files").

extension ServerClient {
    public func agentFiles(kind: String? = nil, cursor: String? = nil, sessionID: String? = nil) async throws -> AgentFileList {
        AgentFileList(json: try await json(AgentFileList.path(kind: kind, cursor: cursor, sessionID: sessionID)))
    }

    /// A small JPEG of an image file, for the grid.
    public func agentFileThumbnail(_ id: String) async throws -> Data {
        let (data, response) = try await raw(AgentFileList.thumbPath(id), timeout: 30)
        guard response.statusCode == 200, response.mimeType?.hasPrefix("image/") == true, !data.isEmpty,
              data.count <= 8_000_000 else {
            throw HTTPError(status: response.statusCode == 200 ? 502 : response.statusCode, message: "No preview")
        }
        return data
    }

    /// The whole file, saved to a private temp copy with its real name (Quick Look, share, drag out).
    public func downloadAgentFile(_ file: AgentFile) async throws -> URL {
        let (data, response) = try await raw(AgentFileList.filePath(file.id), timeout: 300)
        guard response.statusCode == 200 else {
            let message = response.statusCode == 404 ? "That file isn't on your Hermes computer anymore."
                : response.statusCode == 413 ? "That file is too big to send to this device."
                : "Your Hermes couldn't send that file."
            throw HTTPError(status: response.statusCode, message: message)
        }
        return try AgentFileStore.save(data, for: file)
    }
}

@MainActor
public final class LiveAgentFilesBackend: AgentFilesBackend {
    let api: () -> ServerClient?
    public init(api: @escaping () -> ServerClient?) { self.api = api }
    private func client() throws -> ServerClient {
        guard let api = api() else { throw ServerClient.HTTPError(status: 0, message: "Pair with your Hermes first.") }
        return api
    }
    public func list(kind: String?, cursor: String?) async throws -> AgentFileList {
        try await client().agentFiles(kind: kind, cursor: cursor)
    }
    public func thumbnail(_ file: AgentFile) async throws -> Data { try await client().agentFileThumbnail(file.id) }
    public func download(_ file: AgentFile) async throws -> URL { try await client().downloadAgentFile(file) }
}
