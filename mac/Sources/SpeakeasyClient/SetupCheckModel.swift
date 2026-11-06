import Foundation
import SpeakeasyCore

/// Drives the Setup check screen on the Mac and iPhone: load the rows (from the plugin, or built
/// from `/voice/status` on an older plugin), refresh while it's open so a fix done elsewhere (a
/// restart, a sign-in in the chat) turns its row green on its own, and run the plugin-side fixes.
@MainActor
public final class SetupCheckModel: ObservableObject {
    public struct Backend: Sendable {
        /// nil = the plugin has no Setup check route.
        public var load: @Sendable () async throws -> SetupCheck?
        public var status: @Sendable () async throws -> ServerStatus
        public var fix: @Sendable (_ rowID: String) async throws -> SetupCheck?
        public init(load: @escaping @Sendable () async throws -> SetupCheck?,
                    status: @escaping @Sendable () async throws -> ServerStatus,
                    fix: @escaping @Sendable (String) async throws -> SetupCheck?) {
            self.load = load; self.status = status; self.fix = fix
        }

        public static func server(_ client: ServerClient) -> Backend {
            Backend(load: { try await client.setupCheck() }, status: { try await client.status() },
                    fix: { try await client.fixSetupCheck($0) })
        }

        /// Demo mode and screenshots: invented rows, never a network call.
        public static func demo(_ check: SetupCheck = SetupCheckModel.demoCheck) -> Backend {
            Backend(load: { check }, status: { ServerStatus() }, fix: { _ in check })
        }
    }

    @Published public private(set) var check: SetupCheck?
    /// Couldn't reach the Hermes for the rows (shown as one row with the reason).
    @Published public private(set) var error: String?
    @Published public private(set) var working: String?
    @Published public private(set) var copiedRow: String?
    public let refreshInterval: TimeInterval
    private let backend: Backend
    private var refreshTask: Task<Void, Never>?
    /// Called once when every blocking row turns ready (the screen offers Start a call).
    public var onReady: (() -> Void)?

    public init(backend: Backend, initial: SetupCheck? = nil, refreshInterval: TimeInterval = 3) {
        self.backend = backend
        self.check = initial
        self.refreshInterval = refreshInterval
    }

    public var ready: Bool { check?.ready == true && error == nil }

    public func refresh() async {
        do {
            let next: SetupCheck
            if let rows = try await backend.load() { next = rows } else { next = SetupCheck.fallback(try await backend.status()) }
            let becameReady = next.ready && check?.ready != true
            check = next
            error = nil
            if becameReady { onReady?() }
        } catch {
            self.error = Self.describe(error)
        }
    }

    /// Refresh on open and every few seconds while the screen is up.
    public func start() {
        refreshTask?.cancel()
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                try? await Task.sleep(nanoseconds: UInt64(self.refreshInterval * 1_000_000_000))
            }
        }
    }

    public func stop() { refreshTask?.cancel(); refreshTask = nil }

    /// A row's `action` fix: the plugin does it, the rows come back fresh.
    public func runAction(_ row: SetupCheck.Row) async {
        working = row.id
        defer { working = nil }
        do {
            if let next = try await backend.fix(row.id) { check = next } else { await refresh() }
            error = nil
        } catch {
            self.error = Self.describe(error)
        }
    }

    /// The copy fixes: the app put `payload` on the clipboard; show "Copied" on that row for a bit.
    public func markCopied(_ row: SetupCheck.Row) {
        copiedRow = row.id
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            if self?.copiedRow == row.id { self?.copiedRow = nil }
        }
    }

    static func describe(_ error: Error) -> String {
        if let http = error as? ServerClient.HTTPError, !http.message.isEmpty { return http.message }
        if error is URLError { return "Couldn't reach your Hermes. Check that the computer it runs on is awake and online." }
        return "Couldn't check your Hermes right now. Try again in a moment."
    }

    /// Invented demo rows: Hermes needs a restart after an update, and ChatGPT isn't signed in.
    nonisolated public static let demoCheck = SetupCheck(ready: false, rows: [
        .init(id: "plugin", ok: true, title: "Speakeasy 0.3.37 is on your Hermes"),
        .init(id: "restart", ok: false, title: "Hermes needs a restart",
              detail: "Speakeasy was updated while Hermes was running. Send /restart in your chat with Hermes to finish the update; it takes a few seconds.",
              fix: .init(kind: .copy, label: "Copy /restart", payload: SetupCheck.restartCommand)),
        .init(id: "hermes_api", ok: true, title: "Speakeasy can reach Hermes"),
        .init(id: "voice", ok: false, title: "Voice: ChatGPT isn't signed in",
              detail: "Calls use your ChatGPT sign-in on the Hermes computer. Sign in from here with a short code.",
              fix: .init(kind: .codexSignIn, label: "Sign in with ChatGPT")),
        .init(id: "brief", ok: false, blocking: false, title: "Hermes is writing your voice brief",
              detail: "Optional: calls work without it. It helps the voice know your work and names."),
    ], version: "0.3.37")

    /// The same invented rows once everything a call needs is ready (the brief still optional).
    nonisolated public static let demoReadyCheck = SetupCheck(ready: true, rows: demoCheck.rows.map { row in
        switch row.id {
        case "restart": return .init(id: row.id, ok: true, title: "Hermes is running the current Speakeasy")
        case "voice": return .init(id: row.id, ok: true, title: "Voice: ChatGPT is signed in")
        default: return row
        }
    }, version: "0.3.37")

    /// Invented rows for a Hermes with no AI model yet (`-demoNoModel`): everything else is ready.
    nonisolated public static let demoNoModelCheck = SetupCheck(ready: false, rows: [
        .init(id: "plugin", ok: true, title: "Speakeasy 0.3.37 is on your Hermes"),
        .init(id: "restart", ok: true, title: "Hermes is running the current Speakeasy"),
        .init(id: "hermes_api", ok: true, title: "Speakeasy can reach Hermes"),
        .init(id: "agent_model", ok: false, title: AgentModelCopy.title, detail: AgentModelCopy.detail,
              fix: .init(kind: .copy, label: AgentModelCopy.copyButton, payload: AgentModelCopy.command)),
        .init(id: "voice", ok: true, title: "Voice: ChatGPT is signed in"),
        .init(id: "brief", ok: false, blocking: false, title: "No voice brief yet",
              detail: "Optional. Hermes writes it by itself once it has an AI model."),
    ], version: "0.3.37")
}
