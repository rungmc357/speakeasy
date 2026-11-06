import Foundation
import SpeakeasyCore

// Hermes Kanban boards, read-only (servers with `boards`; docs/API.md "Boards").

extension ServerClient {
    public func kanbanBoards() async throws -> KanbanBoardList {
        KanbanBoardList(json: try await json("/voice/boards"))
    }

    public func kanbanBoard(_ slug: String) async throws -> KanbanBoardPage {
        let safe = slug.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-_"))) ?? slug
        guard let page = KanbanBoardPage(json: try await json("/voice/boards/\(safe)")) else {
            throw HTTPError(status: 502, message: "Your Hermes sent back something unexpected.")
        }
        return page
    }
}

@MainActor
public final class LiveBoardBackend: BoardBackend {
    let api: () -> ServerClient?
    public init(api: @escaping () -> ServerClient?) { self.api = api }
    private func client() throws -> ServerClient {
        guard let api = api() else { throw ServerClient.HTTPError(status: 0, message: "Pair with your Hermes first.") }
        return api
    }
    public func boards() async throws -> KanbanBoardList { try await client().kanbanBoards() }
    public func board(_ slug: String) async throws -> KanbanBoardPage { try await client().kanbanBoard(slug) }
}
