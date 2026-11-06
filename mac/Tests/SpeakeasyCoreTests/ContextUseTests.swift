import XCTest
@testable import SpeakeasyCore

/// A task's or session page's `context` (plugin 0.3.37+): which model ran it and how full its context
/// window got, decoded for the quiet footer line; absent on older plugins.
final class ContextUseTests: XCTestCase {
    private func json(_ s: String) -> Any { try! JSONSerialization.jsonObject(with: Data(s.utf8)) }

    func testTaskCarriesContext() throws {
        let work = try XCTUnwrap(WorkInfo(json: json(#"""
        {"run_id": "run_1", "status": "completed", "title": "Fix the flaky checkout test",
         "context": {"estimate": true, "line": "Claude Opus 5.5 · about 22% of context used · 3 min",
                     "use": "about 22% of context used", "took": "3 min",
                     "detail": "About 44K of 200K tokens in the latest model call (peak about 31%). An estimate.",
                     "model": "Claude Opus 5.5", "percent": 22, "peak_percent": 31, "tokens": 44000, "window": 200000}}
        """#)))
        let ctx = try XCTUnwrap(work.context)
        XCTAssertEqual(ctx.line, "Claude Opus 5.5 · about 22% of context used · 3 min")
        XCTAssertEqual(ctx.percent, 22)
        XCTAssertEqual(ctx.peakPercent, 31)
        XCTAssertEqual(ctx.window, 200_000)
        XCTAssertEqual(ctx.took, "3 min")
        XCTAssertEqual(ctx.useSentence, "About 22% of context used")
        XCTAssertEqual(ctx.modelAndUse, "Claude Opus 5.5 · about 22% of context used")
        XCTAssertTrue(ctx.accessibilityText.contains("Estimate"))
        XCTAssertFalse(ctx.isNearlyFull)
    }

    func testPageCarriesContextAndOlderPluginsDont() {
        let page = HistoryConversation(json: json(#"""
        {"session": {"id": "d1", "title": "Garden plan", "source": "discord", "source_label": "Discord",
          "context": {"line": "GPT-6.1 · about 12K tokens of context", "use": "about 12K tokens of context",
                      "tokens": 12000, "window": null, "percent": null}}}
        """#))
        let ctx = page?.context
        XCTAssertEqual(ctx?.percent, nil)
        XCTAssertEqual(ctx?.window, nil)
        XCTAssertEqual(ctx?.symbol, "gauge.with.dots.needle.0percent")
        XCTAssertEqual(ctx?.modelAndUse, "About 12K tokens of context")   // no model sent: no model shown
        let old = HistoryConversation(json: json(#"{"session": {"id": "d2", "title": "Old", "source": "cli", "source_label": "Terminal"}}"#))
        XCTAssertNil(old?.context)
        XCTAssertNil(ContextUse(json: ["line": ""]))
        XCTAssertNil(WorkInfo(json: ["status": "running"])?.context)
    }

    func testNearlyFullContextIsFlagged() {
        XCTAssertTrue(ContextUse(json: ["line": "x", "percent": 85, "tokens": 170_000, "window": 200_000])!.isNearlyFull)
        XCTAssertEqual(ContextUse(json: ["line": "x", "percent": 85, "tokens": 1])!.symbol, "gauge.with.dots.needle.67percent")
    }
}
