import Foundation

/// The URL path piece for one Home item id (`s:session`, `q:run`, `t:run`, `c:call`).
/// The plugin's routes match the one-letter prefix and its colon literally and decode the rest,
/// so only the part after the prefix is percent-encoded (`s:a b` -> `s:a%20b`).
public enum NowItemPath {
    private static let unreserved = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_.~"))

    public static func segment(_ itemID: String) -> String {
        let escape = { (s: String) in s.addingPercentEncoding(withAllowedCharacters: unreserved) ?? s }
        if let colon = itemID.firstIndex(of: ":"), itemID.distance(from: itemID.startIndex, to: colon) == 1,
           "qstc".contains(itemID[itemID.startIndex]) {
            return String(itemID[...colon]) + escape(String(itemID[itemID.index(after: colon)...]))
        }
        return escape(itemID)
    }
}
