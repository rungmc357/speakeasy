import Foundation

/// Local exploration only: never selects an answer or sends an action to the server.
public struct CardExpansion: Equatable, Sendable {
    private var paths: Set<String> = []
    public init() {}
    public func isExpanded(_ path: String) -> Bool { paths.contains(path) }
    public mutating func toggle(_ path: String) {
        if paths.contains(path) {
            paths = paths.filter { $0 != path && !$0.hasPrefix(path + "/") }
        } else {
            paths.insert(path)
        }
    }
}

public struct CardChartPoint: Equatable, Sendable, Identifiable {
    public let id: String
    public let series: String
    public let label: String
    public let value: Double
}

public extension ViewCard {
    /// Preserve original positions when a sparse/malformed series contains nulls.
    var chartPoints: [CardChartPoint] {
        let labels = self["labels"].array
        return self["series"].array.enumerated().flatMap { seriesIndex, entry in
            entry["values"].array.enumerated().compactMap { index, raw -> CardChartPoint? in
                guard let value = raw.double, value.isFinite else { return nil }
                return CardChartPoint(id: "\(seriesIndex)/\(index)",
                                      series: entry["name"].string ?? "Series \(seriesIndex + 1)",
                                      label: index < labels.count ? (labels[index].string ?? "\(index + 1)") : "\(index + 1)",
                                      value: value)
            }
        }
    }
}

/// Only existing, nonempty fields are exposed. Missing detail is not a request to invent it.
public struct CardDetail: Equatable, Sendable, Identifiable {
    public let id: String
    public let text: String
    public var label: String { id.replacingOccurrences(of: "_", with: " ").capitalized }

    public static func destination(_ value: JSONValue) -> URL? {
        guard let url = value["url"].url, url.host?.contains(".") == true else { return nil }
        return url
    }

    public static func rows(_ values: [JSONValue], keys: [String]) -> [JSONValue] {
        values.filter { !fields($0, keys: keys).isEmpty }
    }

    public static func fields(_ value: JSONValue, keys: [String]) -> [CardDetail] {
        keys.compactMap { key in
            guard key != "url" || destination(value) != nil,
                  let text = value[key].string,
                  !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            return CardDetail(id: key, text: text)
        }
    }
}
