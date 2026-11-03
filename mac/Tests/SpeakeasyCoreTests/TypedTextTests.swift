import XCTest
@testable import SpeakeasyCore

/// Typed turns on a call screen (`session.input_text`): the text rule against the table the plugin
/// runs too (Contract/typed_text.json), the status flag, the SSE event and the transcript.
final class TypedTextTests: XCTestCase {
    // MARK: The text rule (shared with plugin/tests/test_input_text.py)

    private struct Table {
        struct Vector { let name, input, normalized: String; let scalars: Int; let verdict: String }
        let maxScalars: Int
        let separators: [ClosedRange<UInt32>]
        let vectors: [Vector]
    }

    private func table() throws -> Table {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "typed_text", withExtension: "json", subdirectory: "Contract"))
        let object = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        func text(_ value: Any?) throws -> String {
            if let s = value as? String { return s }
            let o = try XCTUnwrap(value as? [String: Any])
            let unit = try XCTUnwrap(o["repeat"] as? String), times = try XCTUnwrap(o["times"] as? Int)
            return (o["before"] as? String ?? "") + String(repeating: unit, count: times) + (o["after"] as? String ?? "")
        }
        let ranges = try XCTUnwrap(object["separators"] as? [[Int]]).map { UInt32($0[0])...UInt32($0[1]) }
        let vectors = try XCTUnwrap(object["vectors"] as? [[String: Any]]).map { v in
            Table.Vector(name: try XCTUnwrap(v["name"] as? String), input: try text(v["input"]),
                         normalized: try text(v["normalized"]), scalars: try XCTUnwrap(v["scalars"] as? Int),
                         verdict: try XCTUnwrap(v["verdict"] as? String))
        }
        return Table(maxScalars: try XCTUnwrap(object["max_scalars"] as? Int), separators: ranges, vectors: vectors)
    }

    func testNormalizerMatchesTheSharedTable() throws {
        let t = try table()
        XCTAssertEqual(t.maxScalars, TypedText.maxScalars)
        XCTAssertGreaterThan(t.vectors.count, 20)
        for v in t.vectors {
            let out = TypedText.normalized(v.input)
            XCTAssertEqual(Array(out.unicodeScalars), Array(v.normalized.unicodeScalars), v.name)
            XCTAssertEqual(TypedText.scalarCount(out), v.scalars, v.name)
            switch (TypedText.check(v.input), v.verdict) {
            case (.ok(let text), "ok"): XCTAssertEqual(text, v.normalized, v.name)
            case (.empty, "empty_text"): break
            case (.tooLong(let n), "text_too_long"): XCTAssertEqual(n, v.scalars, v.name)
            case (let got, let want): XCTFail("\(v.name): \(got) vs \(want)")
            }
        }
    }

    func testSeparatorsAreExactlyTheSharedSet() throws {
        let ranges = try table().separators
        var mismatches: [String] = []
        for value in UInt32(0)...0x10FFFF {
            guard let scalar = Unicode.Scalar(value) else { continue }
            let shared = ranges.contains { $0.contains(value) }
            if TypedText.isSeparator(scalar) != shared { mismatches.append(String(value, radix: 16)) }
            if value < 0x3001 {   // through the last separator: the full rule, not only the predicate
                var probe = String.UnicodeScalarView()
                probe.append("a"); probe.append(scalar); probe.append("b")
                if (TypedText.normalized(String(probe)) == "a b") != shared { mismatches.append("normalized " + String(value, radix: 16)) }
            }
        }
        XCTAssertEqual(mismatches, [])
    }

    func testZWJAndBidiSurviveAndTheLimitCountsCodePoints() {
        let family = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}"
        XCTAssertEqual(TypedText.check(" \(family)\n"), .ok(family), "one Character, five code points, unchanged")
        XCTAssertEqual(TypedText.check("\u{200F}\u{05E9}\u{200E}"), .ok("\u{200F}\u{05E9}\u{200E}"))
        // 1000 Characters but 1001 code points: refused, not cut.
        let draft = String(repeating: "a", count: 999) + "\u{1F44D}\u{1F3FD}"
        XCTAssertEqual(draft.count, 1000)
        XCTAssertEqual(TypedText.check(draft), .tooLong(scalars: 1001))
        XCTAssertEqual(TypedText.check(String(repeating: "y", count: 1000)), .ok(String(repeating: "y", count: 1000)))
        XCTAssertEqual(TypedText.check(String(repeating: "y", count: 1001)), .tooLong(scalars: 1001))
    }

    func testRouteBodyAndReply() {
        XCTAssertEqual(TypedText.path(interactionID: "vi_1"), "/voice/interactions/vi_1/input-text")
        XCTAssertEqual(TypedText.path(interactionID: "vi/../x"), "/voice/interactions/vi%2F%2E%2E%2Fx/input-text")
        XCTAssertEqual(TypedText.body("hi") as? [String: String], ["type": "session.input_text", "text": "hi"])
        XCTAssertTrue(TypedText.taken(["interaction_id": "vi_1", "type": "session.input_text", "sent": true]))
        XCTAssertFalse(TypedText.taken(["interaction_id": "vi_1", "type": "session.input_text", "sent": false]))
        XCTAssertFalse(TypedText.taken([:]), "a 2xx without sent:true is not a delivery")
        XCTAssertFalse(TypedText.taken(["sent": true, "type": "session.close"]))
    }

    // MARK: The status flag

    func testStatusSaysWhetherTheServerTakesTypedTurns() throws {
        func status(_ json: String) throws -> ServerStatus { try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8)) }
        XCTAssertTrue(try status(#"{"version":"0.3.16","input_text":true}"#).takesTypedTurns)
        XCTAssertFalse(try status(#"{"version":"0.3.15"}"#).takesTypedTurns, "older plugins have no route: no composer")
        XCTAssertFalse(try status(#"{"input_text":false}"#).takesTypedTurns)
        let odd = try status(#"{"assistant_name":"Nova","input_text":"yes"}"#)
        XCTAssertFalse(odd.takesTypedTurns, "a non-boolean is not a yes")
        XCTAssertEqual(odd.assistantName, "Nova", "and it doesn't break the rest of the status")
    }

    // MARK: The SSE event and the transcript

    private func live(_ transcript: [TranscriptTurn] = []) -> VoiceState {
        var s = VoiceState()
        s.connection = .live
        s.interactionID = "vi_1"
        s.transcript = transcript
        return s
    }

    private let t0 = Date(timeIntervalSince1970: 1_000)

    func testTypedTranscriptEventParses() {
        let event = SSEEvent(name: "transcript", data: #"{"role":"user","text":"Order more coffee","source":"typed"}"#, id: "7")
        XCTAssertEqual(parseServerStreamEvent(event), [.typedTurn("Order more coffee", origin: .stream(id: "7"))])
        for data in [#"{"role":"assistant","text":"x","source":"typed"}"#, #"{"role":"user","text":"x","source":"spoken"}"#,
                     #"{"role":"user","text":"  ","source":"typed"}"#, #"not json"#] {
            XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "transcript", data: data)), [], data)
        }
    }

    /// The sender gets its own turn twice (the local echo after the 200 and the stream's copy), in
    /// either order: it shows once.
    func testServerStreamTypedTranscriptDoesNotDoubleThePhoneEcho() {
        for streamFirst in [false, true] {
            var s = live([TranscriptTurn(id: 0, speaker: .you, text: "Hi")])
            let local = VoiceEvent.typedTurn("Also milk", origin: .local)
            let stream = VoiceEvent.typedTurn("Also milk", origin: .stream(id: "4"))
            for event in streamFirst ? [stream, local] : [local, stream] { s = reduce(s, event, now: t0) }
            XCTAssertEqual(s.transcript.map(\.text), ["Hi", "Also milk"], "streamFirst=\(streamFirst)")
            XCTAssertTrue(s.transcript[1].typed)
            XCTAssertTrue(s.typedEchoes.isEmpty, "both copies matched")
        }
    }

    func testAnotherClientsTypedTurnShowsAndAReplayDoesNot() {
        var s = live()
        s = reduce(s, .typedTurn("From the Mac", origin: .stream(id: "9")), now: t0)
        s = reduce(s, .typedTurn("From the Mac", origin: .stream(id: "9")), now: t0)
        XCTAssertEqual(s.transcript.map(\.text), ["From the Mac"])
        // The same words typed again are a new turn.
        s = reduce(s, .typedTurn("From the Mac", origin: .stream(id: "12")), now: t0)
        XCTAssertEqual(s.transcript.count, 2)
    }

    func testTypingTheSameWordsTwiceShowsTwoTurns() {
        var s = live()
        for id in ["1", "2"] {
            s = reduce(s, .typedTurn("yes", origin: .local), now: t0)
            s = reduce(s, .typedTurn("yes", origin: .stream(id: id)), now: t0)
        }
        XCTAssertEqual(s.transcript.map(\.text), ["yes", "yes"])
    }

    /// A typed turn is its own complete turn: it doesn't join the user's unfinished spoken words,
    /// doesn't run into the assistant's reply, and what follows doesn't run into it.
    func testTypedTurnNeverBleedsIntoSpokenOrStreamingTurns() {
        var s = live()
        s = reduce(s, .inputDelta("Remind me"), now: t0)
        s = reduce(s, .outputDelta("Sure, what"), now: t0.addingTimeInterval(1))
        s = reduce(s, .typedTurn("to call Dana", origin: .local), now: t0.addingTimeInterval(1.5))
        XCTAssertEqual(s.transcript.map(\.text), ["Remind me", "Sure, what", "to call Dana"])
        XCTAssertEqual(s.speech, .speaking(lastDelta: t0.addingTimeInterval(1)), "typing doesn't stop the speaking label")
        // The assistant's next words and the user's next spoken words are new turns.
        s = reduce(s, .outputDelta("Calling Dana."), now: t0.addingTimeInterval(2))
        s = reduce(s, .inputDelta("thanks"), now: t0.addingTimeInterval(3))
        XCTAssertEqual(s.transcript.map(\.text), ["Remind me", "Sure, what", "to call Dana", "Calling Dana.", "thanks"])
        XCTAssertEqual(s.transcript.map(\.speaker), [.you, .assistant, .you, .assistant, .you])
        // Spoken words right after a typed turn (no reply yet) don't join it either.
        s = reduce(s, .typedTurn("and Sam", origin: .local), now: t0.addingTimeInterval(4))
        s = reduce(s, .inputDelta(" too"), now: t0.addingTimeInterval(4.5))
        XCTAssertEqual(s.transcript.suffix(2).map(\.text), ["and Sam", " too"])
    }

    func testTypedTurnKeepsBracketsAndIsDroppedOutsideACall() {
        var s = live()
        s = reduce(s, .typedTurn("[urgent] call mom", origin: .local), now: t0)
        XCTAssertEqual(s.transcript.last?.cleanedText, "[urgent] call mom", "typed text isn't run through the speech cleaner")
        var ended = s
        ended.connection = .ended(.complete)
        XCTAssertEqual(reduce(ended, .typedTurn("late", origin: .local), now: t0).transcript.count, 1)
        var idle = VoiceState()
        idle = reduce(idle, .typedTurn("late", origin: .stream(id: "1")), now: t0)
        XCTAssertTrue(idle.transcript.isEmpty)
    }

    func testAnUnmatchedEchoExpires() {
        var s = live()
        s = reduce(s, .typedTurn("ok", origin: .local), now: t0)   // its stream copy never came
        s = reduce(s, .typedTurn("ok", origin: .stream(id: "5")), now: t0.addingTimeInterval(VoiceState.typedEchoWindow + 1))
        XCTAssertEqual(s.transcript.map(\.text), ["ok", "ok"], "a minute later the same words are someone's new turn")
    }
}
