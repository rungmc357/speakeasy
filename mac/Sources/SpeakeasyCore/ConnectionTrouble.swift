import Foundation
#if canImport(Darwin)
import Darwin
#endif

/// Plain-language reasons a call couldn't reach Hermes. Speakeasy is reachable only on the user's
/// tailnet (an https *.ts.net address), so "host not found" or "can't connect" to such an address
/// almost always means Tailscale is off on this device. Other errors keep their own wording.
public enum ConnectionTrouble {
    public static func isTailnet(_ url: URL?) -> Bool {
        guard let host = url?.host?.lowercased() else { return false }
        return host.hasSuffix(".ts.net")
    }

    /// The URL error codes that mean "this address isn't reachable from here".
    static let unreachable: Set<Int> = [
        NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed, NSURLErrorCannotConnectToHost,
        NSURLErrorNotConnectedToInternet, NSURLErrorTimedOut, NSURLErrorNetworkConnectionLost,
        NSURLErrorInternationalRoamingOff, NSURLErrorDataNotAllowed,
    ]

    public static func message(for error: Error, server: URL?) -> String {
        let ns = error as NSError
        guard ns.domain == NSURLErrorDomain, unreachable.contains(ns.code) else {
            return error.localizedDescription
        }
        if ns.code == NSURLErrorNotConnectedToInternet || ns.code == NSURLErrorDataNotAllowed {
            return "This device is offline. Connect to Wi-Fi or cellular, then try again."
        }
        if ns.code == NSURLErrorTimedOut {
            // It was reached and is slow (or the network is): never Tailscale's fault by itself.
            return "Your Hermes is taking too long to answer. Check that the computer it runs on is awake, then try again."
        }
        guard isTailnet(server), !TailnetInterface.isUp() else {
            return "Couldn't reach your Hermes. Check that the Hermes machine is on and awake, then try again."
        }
        #if os(visionOS)
        return "Couldn't reach your Hermes. Tailscale may be off on this Vision Pro: open Tailscale, turn it on, then try again."
        #elseif os(iOS)
        return "Couldn't reach your Hermes. Tailscale may be off on this phone: open Tailscale, turn it on, then try again."
        #else
        return "Couldn't reach your Hermes. Tailscale may be off on this Mac: open Tailscale, connect, then try again."
        #endif
    }

    /// True when the message is the Tailscale hint, so the app can offer an "Open Tailscale" button.
    public static func suggestsTailscale(_ message: String) -> Bool {
        message.contains("Tailscale may be off")
    }
}

/// Whether this device has a Tailscale tunnel up right now: an interface holding an address in
/// Tailscale's 100.64.0.0/10 range (getifaddrs; no entitlement needed). When it is, an unreachable
/// Hermes is never blamed on Tailscale being off.
public enum TailnetInterface {
    /// Tests and demos set this; nil reads the real interfaces.
    nonisolated(unsafe) public static var override: Bool?

    public static func isUp() -> Bool {
        if let override { return override }
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let first = list else { return false }
        defer { freeifaddrs(list) }
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let entry = cursor {
            defer { cursor = entry.pointee.ifa_next }
            let flags = Int32(entry.pointee.ifa_flags)
            guard flags & IFF_UP != 0, let addr = entry.pointee.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET) else { continue }
            let v4 = addr.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt32(bigEndian: $0.pointee.sin_addr.s_addr) }
            if isTailnetIPv4(v4) { return true }
        }
        return false
    }

    /// 100.64.0.0/10 (CGNAT space Tailscale hands out).
    public static func isTailnetIPv4(_ address: UInt32) -> Bool { address & 0xFFC0_0000 == 0x6440_0000 }
}

/// When to tell the user their Hermes can't be reached: only after a sustained real failure (several
/// failed checks over at least `minimumOutage`), never after one slow or failed request, and never
/// when the server answered at all (an HTTP error is a server problem, not a network one).
public struct ReachabilityGate: Equatable, Sendable {
    public static let failuresNeeded = 3
    public static let minimumOutage: TimeInterval = 20

    public private(set) var failures = 0
    public private(set) var firstFailure: Date?

    public init() {}

    public enum Check: Equatable, Sendable {
        /// It answered (any HTTP status): reachable.
        case answered
        /// No answer from any address (network error or timeout).
        case noAnswer
    }

    /// Record a check; returns true when the unreachable card should show now.
    public mutating func record(_ check: Check, at now: Date = Date()) -> Bool {
        switch check {
        case .answered:
            failures = 0; firstFailure = nil
            return false
        case .noAnswer:
            failures += 1
            if firstFailure == nil { firstFailure = now }
            return isDown(at: now)
        }
    }

    public func isDown(at now: Date = Date()) -> Bool {
        guard failures >= Self.failuresNeeded, let first = firstFailure else { return false }
        return now.timeIntervalSince(first) >= Self.minimumOutage
    }

    /// How soon to check again while a failure is being confirmed (nil: not confirming).
    public var retryAfter: TimeInterval? { failures > 0 && failures < 8 ? 5 : nil }
}
