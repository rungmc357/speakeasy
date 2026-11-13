import XCTest

/// The Clean spacing grid (`CleanGrid` in `Cards/CardStyle.swift`) is the one scale every iPhone
/// surface lays out on. Adding steps must never move an existing value, or every screen built on
/// the old ones shifts at once. CleanGrid lives in SpeakeasyClient, which this target doesn't link,
/// so this reads its source (the same way `CardStyleSourceTests` does).
final class CleanGridTokensTests: XCTestCase {
    private let file = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .appendingPathComponent("../../Sources/SpeakeasyClient/Cards/CardStyle.swift").standardized

    /// `name -> right-hand side` for every `static let` inside `enum CleanGrid { … }`.
    private func tokens() throws -> [String: String] {
        let text = try String(contentsOf: file, encoding: .utf8)
        guard let start = text.range(of: "public enum CleanGrid {") else {
            XCTFail("CleanGrid not found"); return [:]
        }
        var depth = 0, body = ""
        for ch in text[start.upperBound...] {
            if ch == "{" { depth += 1 }
            if ch == "}" { if depth == 0 { break }; depth -= 1 }
            body.append(ch)
        }
        var out: [String: String] = [:]
        let rx = try NSRegularExpression(pattern: #"public static let (\w+)\s*:\s*[\w\[\]]+\s*=\s*([^\n]+)"#)
        for line in body.components(separatedBy: "\n") {
            let code = line.components(separatedBy: "//").first ?? ""
            let ns = code as NSString
            guard let m = rx.firstMatch(in: code, range: NSRange(location: 0, length: ns.length)) else { continue }
            out[ns.substring(with: m.range(at: 1))] = ns.substring(with: m.range(at: 2)).trimmingCharacters(in: .whitespaces)
        }
        return out
    }

    func testEveryExistingValueIsUnchanged() throws {
        let t = try tokens()
        let shipped: [String: String] = [
            "xxs": "2", "xs": "4", "s": "8", "m": "12", "l": "16", "xl": "24", "xxl": "32",
            "margin": "16", "cardPadding": "16",
            "cardRadius": "22", "innerRadius": "14", "tileRadius": "9", "bubbleRadius": "20",
            "glyphTile": "28", "rowGlyph": "32", "heroGlyph": "44", "glyphGap": "12",
            "textInset": "glyphTile + glyphGap",
            "rowTextInset": "cardPadding + rowGlyph + glyphGap",
            "controlHeight": "36",
        ]
        for (name, value) in shipped {
            XCTAssertEqual(t[name], value, "CleanGrid.\(name) changed; existing tokens never move")
        }
    }

    func testTheNewStepsAndTheWideMarginExist() throws {
        let t = try tokens()
        XCTAssertEqual(t["lPlus"], "20")
        XCTAssertEqual(t["x3l"], "40")
        XCTAssertEqual(t["x4l"], "48")
        XCTAssertEqual(t["wideMargin"], "28")
    }

    func testTheScaleIsTheTenStepsInOrder() throws {
        let t = try tokens()
        XCTAssertEqual(t["scale"], "[xxs, xs, s, m, l, lPlus, xl, xxl, x3l, x4l]")
        let steps = ["xxs", "xs", "s", "m", "l", "lPlus", "xl", "xxl", "x3l", "x4l"].compactMap { t[$0].flatMap(Double.init) }
        XCTAssertEqual(steps, [2, 4, 8, 12, 16, 20, 24, 32, 40, 48])
    }

    func testNoTokenIsAddedOrRemovedSilently() throws {
        XCTAssertEqual(Set(try tokens().keys), [
            "xxs", "xs", "s", "m", "l", "lPlus", "xl", "xxl", "x3l", "x4l", "scale",
            "margin", "wideMargin", "cardPadding",
            "cardRadius", "innerRadius", "tileRadius", "bubbleRadius",
            "glyphTile", "rowGlyph", "heroGlyph", "glyphGap", "textInset", "rowTextInset",
            "controlHeight",
        ])
    }
}
