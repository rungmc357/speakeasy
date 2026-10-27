import XCTest
@testable import SpeakeasyCore

/// Grok Voice relay transport: answer decoding, PCM conversion/chunking, barge-in flush, and the
/// beta-gated provider picker.
final class RelayTransportTests: XCTestCase {
    // MARK: transport.type switch

    func testRelayAnswerDecodesWithoutSDP() throws {
        let json: [String: Any] = [
            "interaction_id": "vi_9", "voice_provider": "grok",
            "transport": ["type": "relay", "path": "/voice/interactions/vi_9/audio",
                          "format": ["encoding": "pcm16le", "rate": 24000, "channels": 1]],
        ]
        let admission = try XCTUnwrap(SessionAdmission(json: json))
        XCTAssertEqual(admission.voiceProvider, "grok")
        XCTAssertEqual(admission.answerSDP, "")
        XCTAssertEqual(admission.relay, RelayTransport(path: "/voice/interactions/vi_9/audio"))
        XCTAssertEqual(admission.transport, .relay(RelayTransport(path: "/voice/interactions/vi_9/audio")))
    }

    func testWebRTCAnswerStillDecodesUnchanged() throws {
        let admission = try XCTUnwrap(SessionAdmission(json: [
            "interaction_id": "vi_1", "transport": ["type": "webrtc", "sdp": "v=0\r\n"]]))
        XCTAssertEqual(admission.transport, .webrtc(sdp: "v=0\r\n"))
        XCTAssertEqual(admission.answerSDP, "v=0\r\n")
        XCTAssertNil(admission.relay)
        XCTAssertEqual(admission.voiceProvider, "openai")
        // A transport with no type and an sdp is WebRTC (older plugins).
        XCTAssertEqual(SessionAdmission(json: ["interaction_id": "vi_2", "transport": ["sdp": "x"]])?.transport, .webrtc(sdp: "x"))
    }

    func testRelayAnswerRefusesOtherHostsAndFormats() {
        func admit(_ transport: [String: Any]) -> SessionAdmission? {
            SessionAdmission(json: ["interaction_id": "vi", "transport": transport])
        }
        XCTAssertNil(admit(["type": "relay"]), "no path")
        XCTAssertNil(admit(["type": "relay", "path": "wss://evil.example/audio"]), "absolute URL")
        XCTAssertNil(admit(["type": "relay", "path": "//evil.example/audio"]), "scheme-relative")
        XCTAssertNil(admit(["type": "relay", "path": "/voice/../x"]))
        XCTAssertNil(admit(["type": "relay", "path": "/a", "format": ["encoding": "opus"]]))
        XCTAssertNil(admit(["type": "relay", "path": "/a", "format": ["rate": 16000]]))
        XCTAssertNotNil(admit(["type": "relay", "path": "/a"]), "format defaults to the contract's")
    }

    func testSocketURLFollowsTheServerScheme() {
        let relay = RelayTransport(path: "/voice/interactions/vi_9/audio")
        XCTAssertEqual(relay.socketURL(base: URL(string: "http://127.0.0.1:8787")!)?.absoluteString,
                       "ws://127.0.0.1:8787/voice/interactions/vi_9/audio")
        XCTAssertEqual(relay.socketURL(base: URL(string: "https://box.tail1.ts.net/")!)?.absoluteString,
                       "wss://box.tail1.ts.net/voice/interactions/vi_9/audio")
        XCTAssertEqual(relay.socketURL(base: URL(string: "https://host/prefix/")!)?.absoluteString,
                       "wss://host/prefix/voice/interactions/vi_9/audio")
        XCTAssertNil(relay.socketURL(base: URL(string: "ftp://host")!))
    }

    // MARK: control frames

    func testControlFrames() {
        XCTAssertEqual(RelayControl(text: #"{"type":"speech_started"}"#), .speechStarted)
        XCTAssertEqual(RelayControl(text: #"{"type":"response_done"}"#), .responseDone)
        XCTAssertEqual(RelayControl(text: #"{"type":"closed","reason":"provider_error"}"#), .closed(reason: "provider_error"))
        XCTAssertEqual(RelayControl(text: #"{"type":"session.output_transcript.delta","delta":"hi"}"#),
                       .other("session.output_transcript.delta"))
        XCTAssertNil(RelayControl(text: "not json"))
        XCTAssertEqual(try json(RelayControl.mute(true)), ["type": "mute", "muted": true] as NSDictionary)
        XCTAssertEqual(try json(RelayControl.mute(false)), ["type": "mute", "muted": false] as NSDictionary)
        XCTAssertEqual(try json(RelayControl.end), ["type": "end"] as NSDictionary)
    }

    private func json(_ text: String) throws -> NSDictionary {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(text.utf8)) as? NSDictionary)
    }

    // MARK: PCM

    func testPCM16IsLittleEndianAndClips() {
        let samples: [Float] = [0, 1, -1, 2, -2, 0.5]
        let data = samples.withUnsafeBufferPointer { RelayPCM.pcm16($0) }
        XCTAssertEqual(data.count, 12)
        XCTAssertEqual(Array(data.prefix(4)), [0x00, 0x00, 0xFF, 0x7F])   // 0, 32767 little-endian
        let back = RelayPCM.floats(data)
        XCTAssertEqual(back.count, 6)
        XCTAssertEqual(back[1], 32767 / 32768, accuracy: 1e-6)
        XCTAssertEqual(back[2], -32767 / 32768, accuracy: 1e-6)
        XCTAssertEqual(back[3], back[1], "clipped")
        XCTAssertEqual(back[5], 0.5, accuracy: 1e-3)
        XCTAssertEqual(RelayPCM.floats(Data([0x01])).count, 0, "odd trailing byte ignored")
    }

    func testLevelIsRMS() {
        let silent = Data(count: 1920)
        XCTAssertEqual(RelayPCM.level(silent), 0)
        let full = [Float](repeating: 1, count: 960).withUnsafeBufferPointer { RelayPCM.pcm16($0) }
        XCTAssertEqual(RelayPCM.level(full), 1, accuracy: 1e-3)
    }

    func testChunkerEmitsFortyMillisecondFrames() {
        XCTAssertEqual(RelayPCM.chunkBytes, 24_000 / 25 * 2, "40 ms of 24 kHz mono 16-bit")
        var chunker = PCMChunker()
        // Hardware hands over odd sizes (e.g. 4800 bytes for 100 ms at 24 kHz, then 700).
        var out = chunker.append(Data(repeating: 1, count: 4_800))
        XCTAssertEqual(out.map(\.count), [1920, 1920])
        XCTAssertEqual(chunker.pendingBytes, 960)
        out = chunker.append(Data(repeating: 2, count: 700))
        XCTAssertTrue(out.isEmpty)
        out = chunker.append(Data(repeating: 3, count: 260))
        XCTAssertEqual(out.count, 1)
        XCTAssertEqual(out[0].count, 1920)
        XCTAssertEqual(out[0].first, 1); XCTAssertEqual(out[0].last, 3, "order kept across appends")
        XCTAssertEqual(chunker.pendingBytes, 0)
        _ = chunker.append(Data(count: 100))
        chunker.reset()
        XCTAssertEqual(chunker.pendingBytes, 0, "mute drops a partial frame")
        // Chunks are standalone Data values (index 0 based), safe to send.
        let chunk = chunker.append(Data((0..<1920).map { UInt8($0 % 256) }))[0]
        XCTAssertEqual(chunk.startIndex, 0)
    }

    // MARK: barge-in

    func testSpeechStartedFlushDropsQueuedAudio() {
        var queue = RelayPlaybackQueue()
        XCTAssertFalse(queue.flush(), "nothing playing: nothing to stop")
        let a = queue.enqueue(samples: 960)
        let b = queue.enqueue(samples: 960)
        _ = queue.enqueue(samples: 480)
        queue.finished(samples: 960, generation: a)
        XCTAssertTrue(queue.isPlaying)
        XCTAssertEqual(queue.queuedSamples, 1_440)
        XCTAssertTrue(queue.flush(), "speech_started while audio is queued stops the player")
        XCTAssertFalse(queue.isPlaying)
        XCTAssertEqual(queue.droppedSamples, 1_440)
        XCTAssertEqual(queue.flushes, 2)
        // Completions of flushed buffers (the player fires them on stop) don't touch the new queue.
        let c = queue.enqueue(samples: 960)
        queue.finished(samples: 960, generation: b)
        XCTAssertEqual(queue.queuedBuffers, 1)
        XCTAssertNotEqual(b, c)
        queue.finished(samples: 960, generation: c)
        XCTAssertFalse(queue.isPlaying)
    }

    // MARK: provider picker

    func testGrokOfferedOnlyWithBetaAndAvailability() throws {
        func status(_ json: String) throws -> ServerStatus {
            try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
        }
        let on = try status(#"{"provider":"grok","beta":true,"grok_available":true,"grok_signed_in":false}"#)
        XCTAssertEqual(on.beta, true)
        XCTAssertEqual(on.grokAvailable, true)
        XCTAssertEqual(on.grokSignedIn, false)
        XCTAssertEqual(VoiceProvider.offered(status: on), [.codex, .openai, .grok])
        XCTAssertEqual(on.resolvedProvider, .grok)
        XCTAssertEqual(on.checks.first { $0.id == "voice" }?.ok, false)
        XCTAssertEqual(VoiceProvider.offered(status: try status(#"{"beta":false,"grok_available":true}"#)), [.codex, .openai])
        XCTAssertEqual(VoiceProvider.offered(status: try status(#"{"beta":true}"#)), [.codex, .openai])
        XCTAssertEqual(VoiceProvider.offered(status: nil), [.codex, .openai])
        XCTAssertEqual(VoiceProvider.offered(status: nil, current: .grok), [.codex, .openai, .grok], "a saved choice stays visible")
        XCTAssertEqual(VoiceProvider.grok.label, "Grok Voice via your X/SuperGrok sign-in")
    }

    func testGrokVoicesAndSavedChoice() throws {
        XCTAssertEqual(VoiceCatalog.voices(for: .grok).map(\.id), ["Ara", "Rex", "Sal", "Eve", "Leo"])
        XCTAssertEqual(VoiceCatalog.defaultVoice(for: .grok), "Ara")
        XCTAssertNil(VoiceCatalog.compatible("cove", provider: .grok))
        XCTAssertEqual(VoiceCatalog.compatible("Rex", provider: .grok), "Rex")
        // The explicit provider id is what gets saved.
        var settings = ServerSettings()
        settings.voice = .init(provider: VoiceProvider.grok.rawValue, voice: "Eve")
        let data = try JSONEncoder().encode(settings)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual((object["voice"] as? [String: Any])?["provider"] as? String, "grok")
        XCTAssertEqual(try JSONDecoder().decode(ServerSettings.self, from: data).resolvedProvider, .grok)
    }
}
