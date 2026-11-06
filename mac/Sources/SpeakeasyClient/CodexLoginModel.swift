import Foundation
import SpeakeasyCore

/// Drives the "Sign in to ChatGPT" sheet on the Mac and iPhone: start Codex's device-code login on
/// the Hermes computer, poll until it's signed in (or the code expires), cancel when the sheet
/// closes early. The server does the work; this only shows its code and link (CodexLogin.swift).
@MainActor
public final class CodexLoginModel: ObservableObject {
    public struct Backend: Sendable {
        public var start: @Sendable (_ switchFromAPIKey: Bool) async throws -> CodexLoginState
        public var poll: @Sendable () async throws -> CodexLoginState
        public var cancel: @Sendable () async throws -> Void
        public init(start: @escaping @Sendable (Bool) async throws -> CodexLoginState,
                    poll: @escaping @Sendable () async throws -> CodexLoginState,
                    cancel: @escaping @Sendable () async throws -> Void) {
            self.start = start; self.poll = poll; self.cancel = cancel
        }

        public static func server(_ client: ServerClient) -> Backend {
            Backend(start: { try await client.startCodexLogin(switchFromAPIKey: $0) },
                    poll: { try await client.codexLoginState() },
                    cancel: { try await client.cancelCodexLogin() })
        }

        /// Demo mode and UI tests: a code at once, signed in a few polls later. Never a network call.
        public static func demo(signInAfterPolls: Int = 3) -> Backend {
            let counter = Counter()
            let code = CodexLoginState(phase: .pending, verificationURL: URL(string: "https://auth.openai.com/codex/device"),
                                       userCode: "WXYZ-12345", expiresAt: Date().addingTimeInterval(14 * 60 + 45))
            return Backend(start: { _ in counter.reset(); return code },
                           poll: { counter.next() >= signInAfterPolls ? CodexLoginState(phase: .signedIn, signIn: .chatgpt) : code },
                           cancel: {})
        }
    }

    final class Counter: @unchecked Sendable {
        private var value = 0
        private let lock = NSLock()
        func next() -> Int { lock.lock(); defer { lock.unlock() }; value += 1; return value }
        func reset() { lock.lock(); value = 0; lock.unlock() }
    }

    @Published public private(set) var state: CodexLoginState = CodexLoginState(phase: .idle)
    @Published public private(set) var error: String?
    @Published public private(set) var copied = false
    public let pollInterval: TimeInterval
    private let backend: Backend
    private var pollTask: Task<Void, Never>?
    /// Called once when Codex is signed in (the app refreshes its status).
    public var onSignedIn: (() -> Void)?

    public init(backend: Backend, pollInterval: TimeInterval = 2) {
        self.backend = backend
        self.pollInterval = pollInterval
    }

    public func start(switchFromAPIKey: Bool = false) {
        error = nil
        state = CodexLoginState(phase: .idle)
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            guard let self else { return }
            do {
                self.apply(try await self.backend.start(switchFromAPIKey))
            } catch {
                self.error = Self.describe(error)
                self.state = CodexLoginState(phase: .failed, message: self.error ?? "")
                return
            }
            await self.pollLoop()
        }
    }

    private func pollLoop() async {
        while !Task.isCancelled, state.isWaiting {
            try? await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
            if Task.isCancelled { return }
            if let expiresAt = state.expiresAt, state.phase == .pending, Date() > expiresAt.addingTimeInterval(5) {
                apply(CodexLoginState(phase: .expired))
                return
            }
            if let next = try? await backend.poll() { apply(next) }
        }
    }

    private func apply(_ next: CodexLoginState) {
        let wasSignedIn = state.phase == .signedIn
        // A poll during the first install reports `idle` for a moment; keep the code on screen.
        if next.phase == .idle && state.isWaiting { return }
        state = next
        if next.phase == .signedIn && !wasSignedIn { onSignedIn?() }
    }

    /// The sheet closed: stop polling and drop a code still waiting.
    public func close() {
        pollTask?.cancel(); pollTask = nil
        if state.isWaiting {
            let backend = backend
            Task { try? await backend.cancel() }
            state = CodexLoginState(phase: .cancelled)
        }
    }

    public func markCopied() {
        copied = true
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            self?.copied = false
        }
    }

    static func describe(_ error: Error) -> String {
        if let http = error as? ServerClient.HTTPError {
            if http.reason == "api_key_signed_in" { return CodexLoginCopy.apiKeyNote }
            if http.status == 404 { return "Update the Speakeasy plugin on your Hermes to sign in from here." }
            return http.message
        }
        return "Couldn't reach your Hermes. Try again."
    }
}
