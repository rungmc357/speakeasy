import Foundation

/// A `speakeasy://pair?server=<url>&code=<code>` link opened by `hermes voice setup`
/// (same Mac) or sent through a Hermes chat (another Mac).
public struct PairingLink: Equatable, Sendable {
    public var server: URL
    public var code: String

    public init(server: URL, code: String) { self.server = server; self.code = code }

    public enum ParseError: Error, Equatable {
        case notSpeakeasy, notPairing, missingServer, untrustedServer, invalidCode
    }

    public static func parse(_ url: URL) -> Result<PairingLink, ParseError> {
        if let web = webPairLink(url) { return web }
        guard url.scheme?.lowercased() == "speakeasy" else { return .failure(.notSpeakeasy) }
        // `speakeasy://pair?...` puts "pair" in the host; `speakeasy:pair?...` in the path.
        let target = (url.host ?? url.path).trimmingCharacters(in: CharacterSet(charactersIn: "/")).lowercased()
        guard target == "pair" else { return .failure(.notPairing) }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        guard let rawServer = items.first(where: { $0.name == "server" })?.value, !rawServer.isEmpty else {
            return .failure(.missingServer)
        }
        guard let server = trustedServerBaseURL(rawServer) else { return .failure(.untrustedServer) }
        guard let code = normalizedPairingCode(items.first(where: { $0.name == "code" })?.value ?? "") else {
            return .failure(.invalidCode)
        }
        return .success(PairingLink(server: server, code: code))
    }
}

/// `https://speakeasyvoice.ai/pair#server=…&code=…` (what agents send in chat). The details sit in
/// the fragment, which browsers never send to the website. nil when the URL isn't that page.
private func webPairLink(_ url: URL) -> Result<PairingLink, PairingLink.ParseError>? {
    guard url.scheme?.lowercased() == "https", url.host?.lowercased() == "speakeasyvoice.ai",
          url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")).lowercased() == "pair" else { return nil }
    // Read the fragment still percent-encoded, then decode it as a query string exactly once.
    var parts = URLComponents()
    parts.percentEncodedQuery = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedFragment ?? ""
    let items = parts.queryItems ?? []
    guard let rawServer = items.first(where: { $0.name == "server" })?.value, !rawServer.isEmpty else {
        return .failure(.missingServer)
    }
    guard let server = trustedServerBaseURL(rawServer) else { return .failure(.untrustedServer) }
    guard let code = normalizedPairingCode(items.first(where: { $0.name == "code" })?.value ?? "") else {
        return .failure(.invalidCode)
    }
    return .success(PairingLink(server: server, code: code))
}

/// The message a user sends their Hermes agent to set Speakeasy up (same text as the website).
public enum SetupPrompt {
    public static let text = "Set up Speakeasy for me: https://speakeasyvoice.ai/setup.md"
}

/// Six digits; spaces and dashes a user types ("123 456", "123-456") are ignored.
public func normalizedPairingCode(_ raw: String) -> String? {
    let digits = raw.filter { !$0.isWhitespace && $0 != "-" }
    guard digits.count == 6, digits.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
    return digits
}

/// `POST /voice/pair` response.
public struct PairResponse: Codable, Equatable, Sendable {
    public var deviceID: String
    public var token: String
    /// Every address of this Hermes and the address-note details (servers with `endpoints`).
    public var endpoints: ServerEndpoints?
    public var addressDrop: AddressDrop?
    enum CodingKeys: String, CodingKey { case deviceID = "device_id", token, endpoints, addressDrop = "address_drop" }
    public init(deviceID: String, token: String, endpoints: ServerEndpoints? = nil, addressDrop: AddressDrop? = nil) {
        self.deviceID = deviceID; self.token = token; self.endpoints = endpoints; self.addressDrop = addressDrop
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        deviceID = try c.decode(String.self, forKey: .deviceID)
        token = try c.decode(String.self, forKey: .token)
        endpoints = try? c.decodeIfPresent(ServerEndpoints.self, forKey: .endpoints)
        addressDrop = try? c.decodeIfPresent(AddressDrop.self, forKey: .addressDrop)
    }
}

public func pairRequestBody(code: String, deviceName: String) throws -> Data {
    let name = deviceName.trimmingCharacters(in: .whitespacesAndNewlines)
    return try JSONSerialization.data(withJSONObject: ["code": code, "device_name": name.isEmpty ? "Mac" : String(name.prefix(64))],
                                      options: [.sortedKeys])
}
