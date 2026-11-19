import XCTest

/// SPK-538: in light mode the job page's done/current step names were white on a light card, and
/// the Stop button's label was a fixed pale pink on a pale pink wash. The views live in the Mac app
/// and SpeakeasyClient, which this target doesn't link, so this reads their source and checks the
/// light-mode colours numerically (WCAG 4.5:1 for text).
final class JobLightModeContrastTests: XCTestCase {
    private let sources = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .appendingPathComponent("../../Sources").standardized

    private func source(_ path: String) throws -> String {
        try String(contentsOf: sources.appendingPathComponent(path), encoding: .utf8)
    }

    /// The light hex of a `Color.adaptive(dark: …, light: Color(hex: 0x……))` declaration.
    private func lightHex(_ text: String, declaring name: String) throws -> UInt32 {
        let line = try XCTUnwrap(text.components(separatedBy: "\n").first { $0.contains("static let \(name) = Color.adaptive(") },
                                 "no adaptive declaration for \(name)")
        let range = try XCTUnwrap(line.range(of: #"light: Color\(hex: 0x([0-9a-fA-F]{6})\)"#, options: .regularExpression))
        let hex = line[range].replacingOccurrences(of: "light: Color(hex: 0x", with: "").replacingOccurrences(of: ")", with: "")
        return try XCTUnwrap(UInt32(hex, radix: 16))
    }

    private typealias RGB = (Double, Double, Double)
    private func rgb(_ hex: UInt32) -> RGB {
        (Double((hex >> 16) & 0xff) / 255, Double((hex >> 8) & 0xff) / 255, Double(hex & 0xff) / 255)
    }
    private func over(_ top: RGB, _ alpha: Double, _ base: RGB) -> RGB {
        (top.0 * alpha + base.0 * (1 - alpha), top.1 * alpha + base.1 * (1 - alpha), top.2 * alpha + base.2 * (1 - alpha))
    }
    private func luminance(_ c: RGB) -> Double {
        func lin(_ v: Double) -> Double { v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
        return 0.2126 * lin(c.0) + 0.7152 * lin(c.1) + 0.0722 * lin(c.2)
    }
    private func contrast(_ a: RGB, _ b: RGB) -> Double {
        let (x, y) = (luminance(a), luminance(b))
        return (max(x, y) + 0.05) / (min(x, y) + 0.05)
    }

    func testStopLabelReadsOnItsWashInLight() throws {
        let jobs = try source("SpeakeasyClient/Jobs/JobViews.swift")
        let palette = try source("SpeakeasyClient/AdaptiveColor.swift")
        XCTAssertTrue(jobs.contains("public static let stopRed = BrandPalette.red"))
        let ink = rgb(try lightHex(jobs, declaring: "stopInk"))
        let red = rgb(try lightHex(palette, declaring: "red"))
        // The lightest backgrounds the wash sits on: the paper page and a white card.
        for base in [rgb(0xffffff), rgb(0xf8f3ea), rgb(0xececec)] {
            for alpha in [0.2, 0.28, 0.3] {   // the job card's Stop, a card's destructive action
                let fill = over(red, alpha, base)
                XCTAssertGreaterThanOrEqual(contrast(ink, fill), 4.5, "Stop label on a \(alpha) red wash")
            }
        }
    }

    func testStopLabelsUseTheAdaptiveInk() throws {
        let jobs = try source("SpeakeasyClient/Jobs/JobViews.swift")
        let actions = try source("SpeakeasyClient/Cards/CardActions.swift")
        // The old fixed pale pinks, unreadable on a light card.
        for (name, text) in [("JobViews.swift", jobs), ("CardActions.swift", actions)] {
            XCTAssertFalse(text.contains("Color(red: 1, green: 0.7, blue: 0.66))\n"), "\(name) draws a fixed pale pink label")
            XCTAssertFalse(text.contains("? Color(red: 1, green: 0.62, blue: 0.58)"), "\(name) draws a fixed pale pink label")
        }
        XCTAssertTrue(jobs.contains(".foregroundStyle(JobStyle.stopInk)"))
        XCTAssertTrue(actions.contains("look.capsHeaders ? JobStyle.stopInk"))
    }

    func testStepNamesNeverHardCodeWhite() throws {
        let live = try source("Speakeasy/Native/JobLiveViews.swift")
        let start = try XCTUnwrap(live.range(of: "struct StepPathPicture"))
        let end = try XCTUnwrap(live.range(of: "@ViewBuilder private func node", range: start.upperBound..<live.endIndex))
        let body = live[start.upperBound..<end.lowerBound]
        XCTAssertFalse(body.contains(": .white)"), "StepPathPicture draws step names white, unreadable on a light card")
        XCTAssertTrue(body.contains(": Color.primary)"))
    }
}
