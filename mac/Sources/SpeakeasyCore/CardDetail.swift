import Foundation

public struct CardChartPoint: Equatable, Sendable, Identifiable {
    public let id: String
    public let seriesIndex: Int
    public let series: String
    public let label: String
    public let value: Double
}

public extension ViewCard {
    /// A chart's points, each at its own label: a null (or non-finite) value is skipped without
    /// shifting the values after it onto the wrong label.
    var chartPoints: [CardChartPoint] {
        let labels = self["labels"].array
        return self["series"].array.enumerated().flatMap { seriesIndex, entry in
            entry["values"].array.enumerated().compactMap { index, raw -> CardChartPoint? in
                guard let value = raw.double, value.isFinite else { return nil }
                return CardChartPoint(id: "\(seriesIndex)/\(index)", seriesIndex: seriesIndex,
                                      series: entry["name"].string ?? "Series \(seriesIndex + 1)",
                                      label: index < labels.count ? (labels[index].string ?? "\(index + 1)") : "\(index + 1)",
                                      value: value)
            }
        }
    }
}

/// One supplied field of a card row. Only existing, nonblank fields are exposed: missing detail
/// is never invented.
public struct CardDetail: Equatable, Sendable, Identifiable {
    public let id: String
    public let text: String
    public var label: String { id.replacingOccurrences(of: "_", with: " ").capitalized }

    /// The row's own https link (`JSONValue.url` is https-only), if its host looks real.
    public static func destination(_ value: JSONValue) -> URL? {
        guard let url = value["url"].url, url.host?.contains(".") == true else { return nil }
        return url
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
