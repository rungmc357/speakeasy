import Foundation

/// The plain lines a failure shows (SPK-118 copy table): never a status code, the server's own text
/// or the system's error description. Callers log the detail (`NSLog`) and show only these.
public enum PlainFailure {
    /// A settings save that didn't go through.
    public static let saveFailed = "Couldn't save that. Try again."
    /// Any other request that failed for a reason the person can't act on.
    public static let couldntDoThat = "Your Hermes couldn't do that. Try again."

    /// What a failed `POST /voice/pair` says, by status.
    public static func pairing(status: Int) -> String {
        switch status {
        case 400, 403: return "That code is wrong or has expired. Ask your Hermes for a new pairing link."
        case 429: return "Too many tries. Wait a minute, then try again."
        default: return "Couldn't pair with your Hermes. Try again, or ask it for a new link."
        }
    }
}
