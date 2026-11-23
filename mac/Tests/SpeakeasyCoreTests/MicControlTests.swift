import Foundation
import XCTest
@testable import SpeakeasyCore

final class MicControlTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    // MARK: Shortcut parsing / conflict rules

    func testDefaultMuteShortcutIsControlOptionM() {
        XCTAssertEqual(KeyShortcut.defaultMute.display, "⌃⌥M")
        XCTAssertEqual(KeyShortcut.defaultMute.keyCode, 0x2E)
        XCTAssertEqual(KeyShortcut.defaultMute.modifiers, KeyShortcut.control | KeyShortcut.option)
        XCTAssertNotEqual(KeyShortcut.defaultMute, KeyShortcut.call)
    }

    func testParsesWordsAndGlyphs() throws {
        let words = try KeyShortcut.parse("ctrl+opt+m").get()
        let glyphs = try KeyShortcut.parse("⌃⌥M").get()
        XCTAssertEqual(words, glyphs)
        let spaced = try KeyShortcut.parse("Control Option Command F5").get()
        XCTAssertEqual(spaced.display, "⌃⌥⌘F5")
    }

    func testRejectsCollisionProneOrInvalidBindings() {
        XCTAssertEqual(KeyShortcut.parse(""), .failure(.empty))
        XCTAssertEqual(KeyShortcut.parse("ctrl+opt"), .failure(.missingKey), "modifier-only is refused")
        XCTAssertEqual(KeyShortcut.parse("m"), .failure(.tooFewModifiers))
        XCTAssertEqual(KeyShortcut.parse("cmd+m"), .failure(.tooFewModifiers), "⌘M minimizes windows")
        XCTAssertEqual(KeyShortcut.parse("shift+m"), .failure(.tooFewModifiers), "typing a capital M")
        XCTAssertEqual(KeyShortcut.parse("ctrl+opt+space"), .failure(.reservedForCall))
        XCTAssertEqual(KeyShortcut.parse("ctrl+opt+m+k"), .failure(.multipleKeys))
        XCTAssertEqual(KeyShortcut.parse("hyper+m"), .failure(.unknownToken("hyper")))
        // A single primary modifier is allowed only on function keys.
        XCTAssertNoThrow(try KeyShortcut.parse("ctrl+f5").get())
    }

    // MARK: Tap / hold gesture

    func testTapTogglesAndStays() {
        var gesture = MuteKeyGesture()
        XCTAssertEqual(gesture.press(current: .live, now: t0), .muted)
        XCTAssertNil(gesture.release(now: t0 + 0.1), "tap leaves the mic muted")
        XCTAssertEqual(gesture.press(current: .muted, now: t0 + 1), .live)
        XCTAssertNil(gesture.release(now: t0 + 1.1), "tap unmutes and stays live")
    }

    func testHoldWhileMutedIsPushToTalk() {
        var gesture = MuteKeyGesture()
        XCTAssertEqual(gesture.press(current: .muted, now: t0), .live, "mic opens on key-down; no first word lost")
        XCTAssertNil(gesture.press(current: .live, now: t0 + 0.5), "auto-repeat is ignored")
        XCTAssertEqual(gesture.release(now: t0 + 2), .muted, "releasing a hold re-mutes")
        XCTAssertFalse(gesture.isHeld)
    }

    func testHoldWhileLiveIsCoughButton() {
        var gesture = MuteKeyGesture()
        XCTAssertEqual(gesture.press(current: .live, now: t0), .muted)
        XCTAssertEqual(gesture.release(now: t0 + 1), .live)
    }

    func testCancelForgetsInFlightPress() {
        var gesture = MuteKeyGesture()
        _ = gesture.press(current: .muted, now: t0)
        gesture.cancel()
        XCTAssertNil(gesture.release(now: t0 + 5), "a stale release after the call ended does nothing")
    }

    // MARK: Mic state in the reducer

    func testSetMicOnlyAppliesDuringACall() {
        let idle = reduce(VoiceState(), .setMic(.muted), now: t0)
        XCTAssertEqual(idle.mic, .live, "no phantom mute outside a call")
        var s = reduce(VoiceState(), .startRequested, now: t0)
        s = reduce(s, .setMic(.muted), now: t0)
        XCTAssertEqual(s.mic, .muted, "start-muted applies while connecting")
        XCTAssertFalse(s.localAudioEnabled)
        s = reduce(s, .sessionStarted, now: t0)
        XCTAssertEqual(s.mic, .muted, "mute survives connection")
    }

    func testMuteNeverEndsCallOrTouchesWork() {
        var s = reduce(VoiceState(), .startRequested, now: t0)
        s = reduce(s, .sessionStarted, now: t0)
        let info = WorkInfo(runID: "run_1", status: "working")
        s = reduce(s, .work(info), now: t0)
        let before = s
        s = reduce(s, .setMic(.muted), now: t0 + 1)
        s = reduce(s, .setMic(.live), now: t0 + 2)
        XCTAssertEqual(s.connection, before.connection)
        XCTAssertEqual(s.runID, "run_1")
        XCTAssertEqual(s.workInfo, before.workInfo)
        if case .waiting = s.work {} else { XCTFail("work state changed kind: \(s.work)") }
    }

    // MARK: Transcript cleanup

    func testCleanTranscriptHidesTagsKeepsWords() {
        XCTAssertEqual(cleanTranscript("[clear throat] Okay, [cough] so the plan [laughs] works."),
                       "Okay, so the plan works.")
        XCTAssertEqual(cleanTranscript("No tags here"), "No tags here")
        XCTAssertEqual(cleanTranscript("[cough]"), "")
        XCTAssertEqual(cleanTranscript("Check the build [clear"), "Check the build", "unclosed streaming tag hidden")
        XCTAssertEqual(cleanTranscript("Use arr[0] now"), "Use arr[0] now", "only word-like tags are hidden")
        let prose = "Brackets [are not closed because this text is far too long to be a tag]"
        XCTAssertEqual(cleanTranscript(prose), prose, "long bracketed prose is real speech and is kept")
        // SPK-561: a tag whose "[" never arrived, and sounds written as stage directions.
        XCTAssertEqual(cleanTranscript("cough] Thanks, glad it helps."), "Thanks, glad it helps.")
        XCTAssertEqual(cleanTranscript("Sure. clears throat] The plan works."), "Sure. The plan works.")
        XCTAssertEqual(cleanTranscript("(laughs) Fair point. *sighs* Okay."), "Fair point. Okay.")
        XCTAssertEqual(cleanTranscript("The demo (the second one) went well."), "The demo (the second one) went well.")
        XCTAssertEqual(cleanTranscript("Use arr[0] and item] here"), "Use arr[0] and item] here",
                       "a ] after a [ is not a tag's tail")
    }

    func testReducerHidesTagsAndTagOnlyInputKeepsExchange() {
        var s = reduce(VoiceState(), .startRequested, now: t0)
        s = reduce(s, .sessionStarted, now: t0)
        s = reduce(s, .inputDelta("[clear throat] What's "), now: t0)
        s = reduce(s, .inputDelta("[cough] next?"), now: t0)
        XCTAssertEqual(s.exchange.cleaned.you, "What's next?")
        s = reduce(s, .outputDelta("The build."), now: t0)
        s = reduce(s, .inputDelta("[cough]"), now: t0)
        XCTAssertEqual(s.exchange.cleaned.assistant, "The build.", "a cough after the reply is not a new turn")
        XCTAssertEqual(s.exchange.cleaned.you, "What's next?")
        s = reduce(s, .inputDelta(" And then?"), now: t0)
        XCTAssertEqual(s.exchange.cleaned.you, "And then?")
        XCTAssertEqual(s.exchange.cleaned.assistant, "")
    }

    func testTagOnlyInputDoesNotUnquietAssistant() {
        var s = reduce(VoiceState(), .startRequested, now: t0)
        s = reduce(s, .sessionStarted, now: t0)
        s = reduce(s, .inputDelta("Hi"), now: t0)
        s = reduce(s, .outputDelta("Hello there"), now: t0)
        s = reduce(s, .quietAssistant, now: t0)
        XCTAssertEqual(s.speech, .quieted)
        s = reduce(s, .inputDelta("[laughs]"), now: t0)
        XCTAssertEqual(s.speech, .quieted, "non-speech must not re-open the assistant's audio")
        s = reduce(s, .inputDelta(" wait"), now: t0)
        XCTAssertEqual(s.speech, .idle, "real words do")
    }
}
