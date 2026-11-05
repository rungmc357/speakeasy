import CryptoKit
import Foundation

// Cloudflare mode and "away from home": every address a paired Hermes has (tailnet, Cloudflare),
// how the app picks one, how it learns a quick tunnel's new address (the encrypted address note on
// ntfy, and the "Update Speakeasy's address" link), and the proof that a new address is the same
// Hermes before the device token is ever sent there. Plugin side: plugin/speakeasy/tunnel.py,
// address_drop.py, devices.py (proof). Docs: docs/API.md › Endpoints.

/// `GET /voice/endpoints` (and the same fields in the pair response).
public struct ServerEndpoints: Codable, Equatable, Sendable {
    public var tailnet: URL?
    public var cloudflare: URL?
    /// A named tunnel on the user's own domain: the address never changes.
    public var cloudflarePermanent: Bool

    enum CodingKeys: String, CodingKey { case tailnet, cloudflare, cloudflarePermanent = "cloudflare_permanent" }

    public init(tailnet: URL? = nil, cloudflare: URL? = nil, cloudflarePermanent: Bool = false) {
        self.tailnet = tailnet; self.cloudflare = cloudflare; self.cloudflarePermanent = cloudflarePermanent
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        tailnet = (try? c.decodeIfPresent(String.self, forKey: .tailnet)).flatMap { $0 }.flatMap(trustedServerBaseURL)
        cloudflarePermanent = (try? c.decodeIfPresent(Bool.self, forKey: .cloudflarePermanent)) ?? false
        let raw = (try? c.decodeIfPresent(String.self, forKey: .cloudflare)).flatMap { $0 }
        // A permanent (own-domain) address arrives over an authenticated request: it may be trusted.
        cloudflare = raw.flatMap { cloudflarePermanent ? httpsBaseURL($0) : trustedServerBaseURL($0) }
    }

    public var isEmpty: Bool { tailnet == nil && cloudflare == nil }
}

/// Where the encrypted address note lives (from pairing; kept in the Keychain).
public struct AddressDrop: Codable, Equatable, Sendable {
    public var relay: URL
    public var topic: String
    public var key: String

    public init(relay: URL, topic: String, key: String) { self.relay = relay; self.topic = topic; self.key = key }
}

/// The endpoint fields of `POST /voice/pair` / `GET /voice/endpoints`.
public struct EndpointsResponse: Codable, Equatable, Sendable {
    public var endpoints: ServerEndpoints?
    public var addressDrop: AddressDrop?
    enum CodingKeys: String, CodingKey { case endpoints, addressDrop = "address_drop" }

    public init(endpoints: ServerEndpoints?, addressDrop: AddressDrop?) {
        self.endpoints = endpoints; self.addressDrop = addressDrop
    }

    public static func decode(_ data: Data) -> EndpointsResponse {
        (try? JSONDecoder().decode(EndpointsResponse.self, from: data)) ?? EndpointsResponse(endpoints: nil, addressDrop: nil)
    }
}

/// A Cloudflare quick-tunnel address: `https://<words>.trycloudflare.com`, no port or path.
public func isQuickTunnelURL(_ url: URL) -> Bool {
    guard url.scheme == "https", url.port == nil, url.path.isEmpty || url.path == "/",
          let host = url.host?.lowercased(), host.hasSuffix(".trycloudflare.com") else { return false }
    let label = host.dropLast(".trycloudflare.com".count)
    return !label.isEmpty && label.allSatisfy { $0.isLetter && $0.isASCII || $0.isNumber || $0 == "-" } && !label.contains(".")
}

/// Any https DNS-name base URL (no path, query, credentials). Only for addresses an authenticated
/// Hermes reported (a permanent Cloudflare hostname on the user's own domain).
public func httpsBaseURL(_ raw: String) -> URL? {
    var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    while text.hasSuffix("/") { text.removeLast() }
    guard let c = URLComponents(string: text), c.scheme?.lowercased() == "https", c.user == nil, c.password == nil,
          c.query == nil, c.fragment == nil, c.path.isEmpty,
          let host = c.host?.lowercased(), host.contains("."), host.count <= 253,
          host.allSatisfy({ ($0.isLetter && $0.isASCII) || $0.isNumber || $0 == "-" || $0 == "." }),
          host.contains(where: { $0.isLetter }) else { return nil }
    return c.url
}

/// The order the app tries addresses in: tailnet first (most private), then Cloudflare, then the
/// one it was using, without repeats.
public enum EndpointChoice {
    public static func candidates(current: URL?, endpoints: ServerEndpoints?) -> [URL] {
        var out: [URL] = []
        for url in [endpoints?.tailnet, endpoints?.cloudflare, current].compactMap({ $0 }) {
            let key = url.absoluteString.lowercased()
            if !out.contains(where: { $0.absoluteString.lowercased() == key }) { out.append(url) }
        }
        return out
    }
}

/// The encrypted address note (`se1.<base64url(nonce ‖ AES-256-GCM ciphertext ‖ tag)>`, the topic
/// as additional data). Only a newer note (higher `seq`) than the last one accepted counts.
public struct AddressNote: Equatable, Sendable {
    public var url: URL
    public var ts: Int
    public var seq: Int

    public static func open(_ message: String, drop: AddressDrop) -> AddressNote? {
        guard message.hasPrefix("se1."), let key = base64URLDecode(drop.key), key.count == 32,
              let raw = base64URLDecode(String(message.dropFirst(4))), raw.count > 28,
              let box = try? AES.GCM.SealedBox(combined: raw),
              let plain = try? AES.GCM.open(box, using: SymmetricKey(data: key), authenticating: Data(drop.topic.utf8)),
              let object = (try? JSONSerialization.jsonObject(with: plain)) as? [String: Any],
              let rawURL = object["url"] as? String, let url = URL(string: rawURL), isQuickTunnelURL(url),
              let ts = object["ts"] as? Int, let seq = object["seq"] as? Int else { return nil }
        return AddressNote(url: url, ts: ts, seq: seq)
    }

    /// The newest valid note among ntfy's poll lines (`{"event":"message","message":…}` per line)
    /// that is newer than `after` (replay of an older address is ignored).
    public static func newest(inPoll body: Data, drop: AddressDrop, after lastSeq: Int) -> AddressNote? {
        let lines = String(decoding: body, as: UTF8.self).split(separator: "\n")
        let notes = lines.compactMap { line -> AddressNote? in
            guard let obj = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
                  (obj["event"] as? String ?? "message") == "message", let message = obj["message"] as? String else { return nil }
            return open(message, drop: drop)
        }
        return notes.filter { $0.seq > lastSeq }.max { $0.seq < $1.seq }
    }

    /// `GET <relay>/<topic>/json?poll=1&since=…` (ntfy keeps messages about 12 hours).
    public static func pollURL(_ drop: AddressDrop, since: String = "24h") -> URL? {
        var text = drop.relay.absoluteString
        while text.hasSuffix("/") { text.removeLast() }
        guard drop.topic.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }) else { return nil }
        return URL(string: "\(text)/\(drop.topic)/json?poll=1&since=\(since)")
    }
}

/// `POST /voice/prove`: the server's HMAC over a fresh nonce and the host the app called, keyed by
/// the SHA-256 of this device's token. Checked before the token is ever sent to a new address.
public enum ServerProof {
    public static func expected(token: String, nonce: String, host: String) -> String {
        let keyHex = SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()
        let mac = HMAC<SHA256>.authenticationCode(for: Data("speakeasy-prove-v1|\(nonce)|\(host.lowercased())".utf8),
                                                  using: SymmetricKey(data: Data(keyHex.utf8)))
        return Data(mac).map { String(format: "%02x", $0) }.joined()
    }

    public static func newNonce() -> String {
        var bytes = [UInt8](repeating: 0, count: 24)
        for i in bytes.indices { bytes[i] = UInt8.random(in: 0...255) }
        return base64URLEncode(Data(bytes))
    }

    /// Constant-time compare of two hex strings.
    public static func matches(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count, !x.isEmpty else { return false }
        return zip(x, y).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
    }
}

/// `https://speakeasyvoice.ai/pair#server=<url>&update=1` or `speakeasy://update?server=<url>`:
/// Hermes' "Update Speakeasy's address" message. Carries only the new address (no code).
public enum AddressUpdateLink {
    public static func parse(_ url: URL) -> URL? {
        var items: [URLQueryItem] = []
        if url.scheme?.lowercased() == "https", url.host?.lowercased() == "speakeasyvoice.ai",
           url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")).lowercased() == "pair" {
            var parts = URLComponents()
            parts.percentEncodedQuery = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedFragment ?? ""
            items = parts.queryItems ?? []
            guard items.contains(where: { $0.name == "update" && $0.value == "1" }) else { return nil }
        } else if url.scheme?.lowercased() == "speakeasy",
                  (url.host ?? url.path).trimmingCharacters(in: CharacterSet(charactersIn: "/")).lowercased() == "update" {
            items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        } else { return nil }
        guard !items.contains(where: { $0.name == "code" }),
              let raw = items.first(where: { $0.name == "server" })?.value,
              let server = trustedServerBaseURL(raw) else { return nil }
        return server
    }
}

/// What the app shows when it can't reach its Hermes.
public enum ReachabilityCard: Equatable, Sendable {
    case none
    /// The quick tunnel moved; this verified address fixes it with one tap.
    case updateAddress(URL)
    /// Nothing answers: explain, offer Tailscale (recommended) and the setup request for Hermes.
    case unreachable(hasTailnet: Bool, hasCloudflare: Bool)

    public static func decide(paired: Bool, reachable: Bool, verifiedUpdate: URL?, endpoints: ServerEndpoints?,
                              current: URL?) -> ReachabilityCard {
        guard paired, !reachable else { return .none }
        if let verifiedUpdate { return .updateAddress(verifiedUpdate) }
        let hasTailnet = endpoints?.tailnet != nil || (current?.host?.hasSuffix(".ts.net") ?? false)
        let hasCloudflare = endpoints?.cloudflare != nil || current.map(isQuickTunnelURL) == true
        return .unreachable(hasTailnet: hasTailnet, hasCloudflare: hasCloudflare)
    }
}

/// "Copy a fix-it message for your Hermes": what the user pastes to their agent (in Telegram, Discord
/// or wherever they chat with it) when the phone can't reach it. One message per way the phone was
/// connected, so each asks for exactly one thing.
public enum AwaySetupRequest {
    public static let tailscaleAppStore = URL(string: "https://apps.apple.com/app/tailscale/id1470499037")!
    public static let cloudflareScript = "https://github.com/rungmc357/speakeasy/blob/main/scripts/speakeasy-cloudflare.py"

    /// The phone reached Hermes over Tailscale: check Tailscale on the computer, nothing else.
    public static func tailscaleCheck(device: String = "iPhone") -> String {
        """
        My Speakeasy app on my \(device) can't reach you over Tailscale. On this computer, please:
        1. Check Tailscale with `tailscale status`. If it's stopped, start it (`tailscale up`). If it needs me \
        to sign in or to turn on HTTPS certificates, send me the one link (certificates: https://login.tailscale.com/admin/dns).
        2. Re-run `hermes voice setup` so Speakeasy publishes its current address.
        3. Reply with what you found, and anything I need to do on my \(device) (Tailscale on, signed in to the \
        same account as this computer).
        """
    }

    /// The phone reached Hermes only through a Cloudflare tunnel: restart it and send the update link.
    public static func cloudflareRestart(device: String = "iPhone") -> String {
        """
        My Speakeasy app on my \(device) can't reach you through its Cloudflare connection. On this computer, please:
        1. Check Speakeasy's Cloudflare tunnel: `python3 speakeasy-cloudflare.py status` (the script from \
        Speakeasy's Cloudflare mode: \(cloudflareScript)). If it stopped, run `python3 speakeasy-cloudflare.py restart`.
        2. Re-run `hermes voice setup` so Speakeasy publishes its current address.
        3. Send me the "Update Speakeasy's address" link.
        """
    }

    /// Move to Tailscale (the most private option).
    public static func switchToTailscale(device: String = "iPhone") -> String {
        """
        Please set up Tailscale for my Speakeasy app so my \(device) reaches you privately from anywhere:
        1. Install and start Tailscale on this computer if it isn't already (never Tailscale Funnel). If it needs me \
        to sign in or to turn on HTTPS certificates, send me the one link (certificates: https://login.tailscale.com/admin/dns).
        2. Re-run `hermes voice setup` so Speakeasy publishes its Tailscale address.
        3. Reply with step-by-step \(device) instructions: install the Tailscale app, sign in with the same account \
        as this computer, then open Speakeasy.
        """
    }
}

// MARK: base64url

public func base64URLDecode(_ text: String) -> Data? {
    var s = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    while s.count % 4 != 0 { s += "=" }
    return Data(base64Encoded: s)
}

public func base64URLEncode(_ data: Data) -> String {
    data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}
