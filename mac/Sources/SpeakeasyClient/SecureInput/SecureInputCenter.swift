import Foundation
import SwiftUI
import SpeakeasyCore

/// The open secure-input requests for this device (GET /voice/secrets, polled while something might
/// ask, plus `secure_inputs` from a call's event feed) and the one answer route. Holds no values.
@MainActor
public final class SecureInputCenter: ObservableObject {
    @Published public private(set) var requests: [SecureInputRequest] = []
    /// The plugin has no secure input (404 on the list route): stop asking.
    @Published public private(set) var unsupported = false
    @Published public var lastError: String?

    private var client: () -> ServerClient?
    private var poller: Task<Void, Never>?
    /// Ids answered or skipped here, so a stale list never re-opens the card.
    private var closed = Set<String>()
    public var interval: TimeInterval = 3

    public init(client: @escaping () -> ServerClient?) { self.client = client }

    public var current: SecureInputRequest? { requests.first { !closed.contains($0.id) && $0.secondsLeft() > 0 } }

    /// Start polling (idempotent). Hosts call this when the app is in the foreground.
    public func start() {
        guard poller == nil, !unsupported else { return }
        poller = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                let wait = self?.interval ?? 3
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
        }
    }

    public func stop() { poller?.cancel(); poller = nil }

    public func refresh() async {
        guard let server = client(), !unsupported else { return }
        do {
            let data = try await server.data("/voice/secrets", timeout: 10)
            apply(SecureInputRequest.list(data))
        } catch let error as ServerClient.HTTPError where error.status == 404 {
            unsupported = true
            stop()
        } catch {}
    }

    /// A call's `secure_inputs` event.
    public func ingest(event payload: Any?) { apply(SecureInputRequest.list(event: payload)) }

    /// For previews, demos and UI tests.
    public func show(_ list: [SecureInputRequest]) { apply(list) }

    private func apply(_ list: [SecureInputRequest]) {
        let live = list.filter { !closed.contains($0.id) }
        if live != requests { requests = live }
    }

    /// Send the value (``value`` is consumed: the body bytes are zeroed after the request).
    /// Returns nil on success, else the message to show.
    @discardableResult
    public func submit(_ request: SecureInputRequest, value: String) async -> String? {
        var bytes = SecureInputRequest.answerBody(value)
        defer { for i in bytes.indices { bytes[i] = 0 } }
        return await send(request, body: &bytes)
    }

    /// A login (Add a login card): username, password and an optional one-time code.
    @discardableResult
    public func submitLogin(_ request: SecureInputRequest, identifier: String, password: String, code: String) async -> String? {
        var bytes = SecureInputRequest.loginBody(identifier: identifier, password: password, code: code)
        defer { for i in bytes.indices { bytes[i] = 0 } }
        return await send(request, body: &bytes)
    }

    @discardableResult
    public func skip(_ request: SecureInputRequest) async -> String? {
        var bytes = SecureInputRequest.skipBody
        return await send(request, body: &bytes)
    }

    private func send(_ request: SecureInputRequest, body: inout [UInt8]) async -> String? {
        guard let server = client() else { return "Not connected to your Hermes." }
        var data = Data(body)
        defer { data.resetBytes(in: 0..<data.count) }
        do {
            _ = try await server.data("/voice/secrets/\(request.id)", method: "POST", body: data, timeout: 20)
            close(request.id)
            return nil
        } catch let error as ServerClient.HTTPError {
            if error.status == 404 || error.status == 403 { close(request.id) }
            lastError = error.message
            return error.message
        } catch {
            lastError = "Couldn't reach your Hermes. Try again."
            return lastError
        }
    }

    private func close(_ id: String) {
        closed.insert(id)
        requests.removeAll { $0.id == id }
    }
}
