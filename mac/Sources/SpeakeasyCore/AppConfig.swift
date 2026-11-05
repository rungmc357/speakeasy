import Foundation

/// Where the app's Speakeasy server lives and the device token it pairs with.
/// The server—not this client—owns the voice provider credentials and Hermes access.
public struct AppConfig: Sendable, Equatable {
    public var serverURL: URL?
    public var deviceToken: String?

    public init(serverURL: URL?, deviceToken: String?) {
        self.serverURL = serverURL
        self.deviceToken = deviceToken
    }

    /// Paired: a trusted server address and a device token.
    public var isPaired: Bool {
        serverURL.flatMap { trustedServerBaseURL($0.absoluteString) } != nil && deviceToken?.isEmpty == false
    }

    /// The zero-config server address when Hermes runs on this Mac.
    public static let localServerURL = URL(string: "http://127.0.0.1:8795")!

    /// Development overrides (`SPEAKEASY_SERVER_URL`, `SPEAKEASY_DEVICE_TOKEN`) win over the saved values.
    /// Untrusted addresses are ignored, never used.
    public static func resolve(savedServer: String?, savedToken: String?,
                               environment: [String: String] = ProcessInfo.processInfo.environment) -> AppConfig {
        let server = environment["SPEAKEASY_SERVER_URL"].flatMap(trustedServerBaseURL)
            ?? savedServer.flatMap(trustedServerBaseURL)
        let token = (environment["SPEAKEASY_DEVICE_TOKEN"] ?? savedToken)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return AppConfig(serverURL: server, deviceToken: token?.isEmpty == false ? token : nil)
    }
}

/// Permanent Cloudflare hostnames (a named tunnel on the user's own domain) this app learned from
/// its paired Hermes over an authenticated request. Only these extra https hosts are trusted.
public enum TrustedHosts {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var extra: Set<String> = []

    public static func set(_ hosts: [String]) {
        lock.lock(); extra = Set(hosts.map { $0.lowercased() }); lock.unlock()
    }

    public static func contains(_ host: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return extra.contains(host.lowercased())
    }
}

/// The app may reach only a loopback server (http or https), an HTTPS Tailscale address
/// (`*.ts.net` or a 100.64.0.0/10 tailnet IP), an HTTPS Cloudflare quick tunnel
/// (`*.trycloudflare.com`), or a permanent Cloudflare hostname its Hermes reported (`TrustedHosts`).
/// No path, query, fragment or credentials.
public func trustedServerBaseURL(_ raw: String) -> URL? {
    var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    while text.hasSuffix("/") { text.removeLast() }
    guard let components = URLComponents(string: text),
          components.user == nil, components.password == nil,
          components.query == nil, components.fragment == nil,
          let scheme = components.scheme?.lowercased(),
          let host = components.host?.lowercased(), !host.isEmpty,
          components.path.isEmpty else { return nil }
    let loopback = host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "[::1]"
    let tailnetName = host.hasSuffix(".ts.net") && host.count > ".ts.net".count
    let parts = host.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
    let octets = parts.compactMap { $0 }
    let tailnetIPv4 = parts.count == 4 && octets.count == 4 && octets.allSatisfy { (0...255).contains($0) }
        && octets[0] == 100 && (64...127).contains(octets[1])
    let label = host.hasSuffix(".trycloudflare.com") ? host.dropLast(".trycloudflare.com".count) : ""
    let quickTunnel = !label.isEmpty && !label.contains(".") && components.port == nil
    guard (loopback && (scheme == "http" || scheme == "https")) ||
          ((tailnetName || tailnetIPv4 || quickTunnel || TrustedHosts.contains(host)) && scheme == "https") else { return nil }
    return components.url
}

public func jsonScriptLiteral(_ value: String) -> String {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed])
    return String(decoding: data, as: UTF8.self)
}
