import Foundation

/// Which app opens music and places from an artifact card. Nothing is detected or guessed: the
/// first Play (or Maps) asks once, "Remember my choice" saves it here, and Settings changes it.
public enum MusicApp: String, CaseIterable, Sendable { case appleMusic = "apple_music", spotify
    public var label: String { self == .appleMusic ? "Apple Music" : "Spotify" }
}

public enum MapsApp: String, CaseIterable, Sendable { case apple, google
    public var label: String { self == .apple ? "Apple Maps" : "Google Maps" }
}

public enum OpenInPrefs {
    public static let musicKey = "artifact.open.music"
    public static let mapsKey = "artifact.open.maps"

    public static func music(_ d: UserDefaults = .standard) -> MusicApp? { d.string(forKey: musicKey).flatMap(MusicApp.init) }
    public static func maps(_ d: UserDefaults = .standard) -> MapsApp? { d.string(forKey: mapsKey).flatMap(MapsApp.init) }
    /// nil: ask again on the next tap.
    public static func setMusic(_ app: MusicApp?, _ d: UserDefaults = .standard) { d.set(app?.rawValue, forKey: musicKey) }
    public static func setMaps(_ app: MapsApp?, _ d: UserDefaults = .standard) { d.set(app?.rawValue, forKey: mapsKey) }
}

/// Deep links for one thing on an artifact card. The server sends ids and https URLs, never app
/// schemes; the app builds universal links (they open the app when it's installed, the web when
/// not), else a search link. Places never open on a bare name.
public enum ArtifactLinks {
    static func q(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed.subtracting(CharacterSet(charactersIn: "&=+?#"))) ?? "" }

    public static func isMusic(_ item: JSONValue, entity: String) -> Bool { entity == "media" && item["media_kind"].string.map { ["album", "song", "playlist", "artist"].contains($0) } ?? false }

    public static func music(_ item: JSONValue, app: MusicApp) -> URL? {
        guard let name = item["name"].string else { return nil }
        let kind = item["media_kind"].string ?? "album"
        let search = [name, item["creator"].string].compactMap { $0 }.joined(separator: " ")
        switch app {
        case .appleMusic:
            if let id = item["ids"]["apple_music"].string, ["album", "song", "artist"].contains(kind) {
                return URL(string: "https://music.apple.com/us/\(kind)/\(id)")
            }
            return URL(string: "https://music.apple.com/us/search?term=\(q(search))")
        case .spotify:
            if let id = item["ids"]["spotify"].string {
                let type = ["song": "track", "album": "album", "playlist": "playlist", "artist": "artist"][kind] ?? "album"
                return URL(string: "https://open.spotify.com/\(type)/\(id)")
            }
            return URL(string: "https://open.spotify.com/search/\(q(search).replacingOccurrences(of: "/", with: "%2F"))")
        }
    }

    /// Whether a place can be found: coordinates, an address, or a name with its city.
    public static func hasPlace(_ item: JSONValue) -> Bool {
        (item["lat"].double != nil && item["lon"].double != nil) || item["address"].string != nil
            || (item["name"].string != nil && item["city"].string != nil)
    }

    public static func maps(_ item: JSONValue, app: MapsApp) -> URL? {
        guard hasPlace(item) else { return nil }
        let name = item["name"].string ?? ""
        let text = item["address"].string.map { name.isEmpty || $0.localizedCaseInsensitiveContains(name) ? $0 : "\(name), \($0)" }
            ?? [name, item["city"].string ?? ""].filter { !$0.isEmpty }.joined(separator: ", ")
        switch app {
        case .apple:
            if let lat = item["lat"].double, let lon = item["lon"].double {
                return URL(string: "https://maps.apple.com/?ll=\(lat),\(lon)&q=\(q(name.isEmpty ? text : name))")
            }
            return URL(string: "https://maps.apple.com/?q=\(q(text))")
        case .google:
            if let lat = item["lat"].double, let lon = item["lon"].double, item["address"].string == nil {
                return URL(string: "https://www.google.com/maps/search/?api=1&query=\(lat),\(lon)")
            }
            return URL(string: "https://www.google.com/maps/search/?api=1&query=\(q(text))")
        }
    }

    /// `tel:` for a phone number written with digits (and + ( ) - spaces).
    public static func call(_ item: JSONValue) -> URL? {
        guard let phone = item["phone"].string else { return nil }
        let digits = phone.filter { $0.isNumber || $0 == "+" }
        return digits.filter(\.isNumber).count >= 5 ? URL(string: "tel:\(digits)") : nil
    }

    /// The SF Symbol for an entity's icon tile (shown when there's no photo or it didn't load).
    public static func symbol(_ entity: String, item: JSONValue = .null) -> String {
        switch entity {
        case "stay": return "bed.double.fill"
        case "reservation": return "fork.knife"
        case "flight": return "airplane"
        case "event": return "ticket.fill"
        case "product": return "bag.fill"
        case "media":
            switch item["media_kind"].string {
            case "podcast": return "mic.fill"
            case "show": return "tv.fill"
            case "book": return "book.closed.fill"
            default: return "music.note"
            }
        case "person": return "person.fill"
        case "article": return "doc.text.fill"
        case "pr": return "arrow.triangle.pull"
        case "listing": return "house.fill"
        case "generation": return "photo.fill"
        case "task": return "checklist"
        default: return "mappin.and.ellipse"
        }
    }
}
