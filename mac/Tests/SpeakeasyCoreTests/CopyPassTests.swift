import Foundation
import XCTest
@testable import SpeakeasyCore

/// SPK-158 (the SPK-118 copy table): one label for unfinished work, plain pairing and save
/// failures, no codes or system error text, and "your Hermes" instead of "Hermes's".
final class CopyPassTests: XCTestCase {
    func testPairingFailuresArePlainLines() {
        XCTAssertEqual(PlainFailure.pairing(status: 403), "That code is wrong or has expired. Ask your Hermes for a new pairing link.")
        XCTAssertEqual(PlainFailure.pairing(status: 400), PlainFailure.pairing(status: 403))
        XCTAssertEqual(PlainFailure.pairing(status: 429), "Too many tries. Wait a minute, then try again.")
        for status in [0, 404, 500, 502, 503] {
            let line = PlainFailure.pairing(status: status)
            XCTAssertEqual(line, "Couldn't pair with your Hermes. Try again, or ask it for a new link.")
            XCTAssertFalse(line.contains("HTTP") || line.contains(String(status == 0 ? 999 : status)), line)
        }
        for status in [400, 403, 429, 500] {
            XCTAssertFalse(PlainFailure.pairing(status: status).contains("hermes voice pair"))
        }
    }

    func testSaveFailureCarriesNoSystemText() {
        XCTAssertEqual(PlainFailure.saveFailed, "Couldn't save that. Try again.")
        XCTAssertEqual(PlainFailure.couldntDoThat, "Your Hermes couldn't do that. Try again.")
        for line in [PlainFailure.saveFailed, PlainFailure.couldntDoThat] {
            XCTAssertFalse(line.contains("(HTTP") || line.contains("-25300") || line.contains("NSError")
                           || line.contains("Keychain error") || line.contains("The operation couldn"), line)
        }
    }

    func testUnfinishedWorkHasOneLabel() {
        XCTAssertEqual(OrbState.failed(needs: 0).label, "A task didn't finish")
        XCTAssertEqual(OrbState.failed(needs: 2).label, "A task didn't finish")
        let info = WorkInfo(runID: "run_a", status: "failed", updated: Date(timeIntervalSince1970: 1_800_000_000))
        XCTAssertEqual(taskStatusLine(info, now: Date(timeIntervalSince1970: 1_800_000_010)).0, "Didn't finish")
    }

    /// Every string literal the Mac app and the shared core show: none of the retired lines survive.
    func testNoRetiredLinesInMacSource() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()   // mac/
        let sources = root.appendingPathComponent("Sources")
        // Agent-facing prompt text that tells the agent *not* to restart Hermes (D6).
        let exempt: Set<String> = ["PluginUpdateFlow.swift"]
        let retired = ["Work failed", "Stopped with an error", "A task failed", "Pairing failed", "(HTTP \\(",
                       "HTTP 500", "Keychain error", "-25300", "Run hermes voice pair", "from hermes voice pair",
                       "Hermes's", "restart Hermes", "Restart Hermes", "Couldn't save: ", "Couldn't pause: ",
                       "Couldn't clear tasks: ", "Couldn't dismiss: ", "Couldn't start tuning: ",
                       "Couldn't switch the routing model: ", "That didn't work", "No recent work",
                       "Not connected to Hermes", "Not connected — open Settings"]
        let literal = try NSRegularExpression(pattern: #""(?:[^"\\]|\\.)*""#)
        var hits: [String] = []
        var scanned = 0
        let files = FileManager.default.enumerator(at: sources, includingPropertiesForKeys: nil)!
        for case let url as URL in files where url.pathExtension == "swift" && !exempt.contains(url.lastPathComponent) {
            let text = try String(contentsOf: url, encoding: .utf8)
            scanned += 1
            for (n, line) in text.components(separatedBy: "\n").enumerated() {
                let code = line.trimmingCharacters(in: .whitespaces)
                if code.hasPrefix("//") { continue }
                let ns = code as NSString
                for m in literal.matches(in: code, range: NSRange(location: 0, length: ns.length)) {
                    let s = ns.substring(with: m.range)
                    for old in retired where s.contains(old) { hits.append("\(url.lastPathComponent):\(n + 1): \(old)") }
                }
                // A user-visible failure line never interpolates the system's description.
                if code.contains("\"Couldn't"), code.contains("\\(error.localizedDescription)"),
                   !code.contains("log(") && !code.contains("NSLog") && !code.contains("print(") {
                    hits.append("\(url.lastPathComponent):\(n + 1): localizedDescription in \(code)")
                }
            }
        }
        // Out of this pass (follow-up list): approval rules, agents, launch at login, relaunch, transcription.
        let followUp = ["approval rules", "your agents", "Launch at login", "relaunch Speakeasy", "transcribe that"]
        hits.removeAll { hit in followUp.contains { hit.contains($0) } }
        XCTAssertGreaterThan(scanned, 100, "the scan found the Mac sources")
        XCTAssertEqual(hits, [], hits.joined(separator: "\n"))
    }
}
