import XCTest
@testable import SpeakeasyCore

/// Cloudflare mode: address trust, endpoint order, the encrypted address note (vectors sealed by
/// the plugin's address_drop.py), the same-Hermes proof (devices.proof), update links, the away card.
final class CloudflareModeTests: XCTestCase {
    let drop = AddressDrop(relay: URL(string: "https://ntfy.sh")!, topic: "se-a0b1c2d3e4f5a6b7c8d9eaebecedeeef",
                           key: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
    let newNote = "se1.N7st0ftglzT_EwB1jxTv-fJX3zJkFqM-1JRLruhSspxDZBITQPi8JBPxdGdwaBM-cNz-7lVDasFvPIFwvdjGr4tAHLhNDv-_vLmgDpptYxpeKs2PUdQwpMIulPYnJFlNzFyBMyBVMgX53DgINg"
    let oldNote = "se1.SlBA3thOFTK59WMJzDN8oOnlSTZXAzZ8vHKw3Qqya0ZN4teIeQ5r05QS4JvUikveFROwN5bDDVfignr_OI4sWG0loi5FFGCFempiYM7f5xYkHw_LaxU7tafGIQbL2EoalzDfhhStqGqgKg"

    func testQuickTunnelsAreTrustedButOtherHostsAreNot() {
        XCTAssertNotNil(trustedServerBaseURL("https://calm-river-sound-test.trycloudflare.com"))
        XCTAssertNil(trustedServerBaseURL("http://calm-river-sound-test.trycloudflare.com"), "https only")
        XCTAssertNil(trustedServerBaseURL("https://a.b.trycloudflare.com"), "one label only")
        XCTAssertNil(trustedServerBaseURL("https://trycloudflare.com"))
        XCTAssertNil(trustedServerBaseURL("https://x.trycloudflare.com.evil.com"))
        XCTAssertNil(trustedServerBaseURL("https://speakeasy.example.com"))
        TrustedHosts.set(["speakeasy.example.com"])
        defer { TrustedHosts.set([]) }
        XCTAssertNotNil(trustedServerBaseURL("https://speakeasy.example.com"), "a permanent hostname Hermes reported")
        XCTAssertNil(trustedServerBaseURL("http://speakeasy.example.com"))
    }

    func testEndpointsDecodeAndOrder() throws {
        let raw = #"{"endpoints":{"tailnet":"https://mini.tail1.ts.net","cloudflare":"https://calm-river-sound-test.trycloudflare.com","cloudflare_permanent":false},"address_drop":{"relay":"https://ntfy.sh","topic":"se-1","key":"k"}}"#
        let decoded = EndpointsResponse.decode(Data(raw.utf8))
        XCTAssertEqual(decoded.endpoints?.tailnet?.host, "mini.tail1.ts.net")
        XCTAssertEqual(decoded.addressDrop?.topic, "se-1")
        let current = URL(string: "https://old-words-here-now.trycloudflare.com")!
        let order = EndpointChoice.candidates(current: current, endpoints: decoded.endpoints)
        XCTAssertEqual(order.map { $0.host! }, ["mini.tail1.ts.net", "calm-river-sound-test.trycloudflare.com",
                                                "old-words-here-now.trycloudflare.com"], "tailnet first")
        let evil = EndpointsResponse.decode(Data(#"{"endpoints":{"cloudflare":"https://evil.example.com"}}"#.utf8))
        XCTAssertNil(evil.endpoints?.cloudflare, "a non-permanent address must be a quick tunnel")
        let named = EndpointsResponse.decode(Data(#"{"endpoints":{"cloudflare":"https://speakeasy.example.com","cloudflare_permanent":true}}"#.utf8))
        XCTAssertEqual(named.endpoints?.cloudflare?.host, "speakeasy.example.com")
        XCTAssertNil(EndpointsResponse.decode(Data("{}".utf8)).endpoints, "older servers: nothing")
    }

    func testPairResponseStillDecodesOldServers() throws {
        let old = try JSONDecoder().decode(PairResponse.self, from: Data(#"{"device_id":"ab12","token":"t"}"#.utf8))
        XCTAssertNil(old.endpoints); XCTAssertNil(old.addressDrop)
    }

    func testAddressNoteOpensPluginCiphertext() {
        let note = AddressNote.open(newNote, drop: drop)
        XCTAssertEqual(note?.url.absoluteString, "https://calm-river-sound-test.trycloudflare.com")
        XCTAssertEqual(note?.seq, 7)
        XCTAssertEqual(note?.ts, 1_700_000_000)
    }

    func testAddressNoteRejectsTamperWrongKeyAndWrongTopic() {
        var bad = Array(newNote)
        bad[20] = bad[20] == "A" ? "B" : "A"
        XCTAssertNil(AddressNote.open(String(bad), drop: drop))
        var otherKey = drop; otherKey.key = "AQECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
        XCTAssertNil(AddressNote.open(newNote, drop: otherKey))
        var otherTopic = drop; otherTopic.topic = "se-ffff"
        XCTAssertNil(AddressNote.open(newNote, drop: otherTopic), "bound to its topic")
        XCTAssertNil(AddressNote.open("hello", drop: drop))
    }

    func testPollPicksTheNewestAndIgnoresReplays() {
        let poll = [#"{"event":"open"}"#,
                    #"{"event":"message","message":"\#(newNote)"}"#,
                    #"{"event":"message","message":"\#(oldNote)"}"#,
                    #"{"event":"message","message":"junk"}"#].joined(separator: "\n")
        XCTAssertEqual(AddressNote.newest(inPoll: Data(poll.utf8), drop: drop, after: 0)?.seq, 7)
        XCTAssertNil(AddressNote.newest(inPoll: Data(poll.utf8), drop: drop, after: 7), "already accepted")
        let onlyOld = #"{"event":"message","message":"\#(oldNote)"}"#
        XCTAssertNil(AddressNote.newest(inPoll: Data(onlyOld.utf8), drop: drop, after: 5), "an old address replayed")
        XCTAssertEqual(AddressNote.pollURL(drop)?.absoluteString,
                       "https://ntfy.sh/se-a0b1c2d3e4f5a6b7c8d9eaebecedeeef/json?poll=1&since=24h")
    }

    func testProofMatchesThePlugin() {
        let want = "2550f11bc3d45d821f2fca904332cc45285da6c90f9a008a03c93e6edad0325d"
        let got = ServerProof.expected(token: "test-token-not-real", nonce: "nonce-nonce-nonce-nonce-1",
                                       host: "calm-river-sound-test.trycloudflare.com")
        XCTAssertEqual(got, want)
        XCTAssertTrue(ServerProof.matches(got, want))
        XCTAssertFalse(ServerProof.matches(got, ServerProof.expected(token: "test-token-not-real", nonce: "nonce-nonce-nonce-nonce-1",
                                                                      host: "evil-relay-host.trycloudflare.com")), "bound to the host")
        XCTAssertFalse(ServerProof.matches("", ""))
        XCTAssertNotEqual(ServerProof.newNonce(), ServerProof.newNonce())
        XCTAssertGreaterThanOrEqual(ServerProof.newNonce().count, 22)
    }

    func testAddressUpdateLinks() {
        let web = URL(string: "https://speakeasyvoice.ai/pair#server=https%3A%2F%2Fcalm-river-sound-test.trycloudflare.com&update=1")!
        XCTAssertEqual(AddressUpdateLink.parse(web)?.host, "calm-river-sound-test.trycloudflare.com")
        XCTAssertEqual(AddressUpdateLink.parse(URL(string: "speakeasy://update?server=https://calm-river-sound-test.trycloudflare.com")!)?.host,
                       "calm-river-sound-test.trycloudflare.com")
        XCTAssertNil(AddressUpdateLink.parse(URL(string: "https://speakeasyvoice.ai/pair#server=https%3A%2F%2Fevil.example.com&update=1")!))
        XCTAssertNil(AddressUpdateLink.parse(URL(string: "https://speakeasyvoice.ai/pair#server=https%3A%2F%2Fa.trycloudflare.com&code=123456")!),
                     "a pairing link isn't an update")
        if case .success = PairingLink.parse(URL(string: "https://speakeasyvoice.ai/pair#server=https%3A%2F%2Fa.trycloudflare.com&code=123456")!) {} else {
            XCTFail("pairing links to a quick tunnel work")
        }
    }

    func testReachabilityCard() {
        let quick = URL(string: "https://calm-river-sound-test.trycloudflare.com")!
        XCTAssertEqual(ReachabilityCard.decide(paired: true, reachable: true, verifiedUpdate: nil, endpoints: nil, current: quick), .none)
        XCTAssertEqual(ReachabilityCard.decide(paired: false, reachable: false, verifiedUpdate: nil, endpoints: nil, current: nil), .none)
        XCTAssertEqual(ReachabilityCard.decide(paired: true, reachable: false, verifiedUpdate: quick, endpoints: nil, current: quick),
                       .updateAddress(quick))
        XCTAssertEqual(ReachabilityCard.decide(paired: true, reachable: false, verifiedUpdate: nil, endpoints: nil, current: quick),
                       .unreachable(hasTailnet: false, hasCloudflare: true))
        XCTAssertEqual(ReachabilityCard.decide(paired: true, reachable: false, verifiedUpdate: nil,
                                               endpoints: ServerEndpoints(tailnet: URL(string: "https://m.t.ts.net")!), current: nil),
                       .unreachable(hasTailnet: true, hasCloudflare: false))
    }

    func testFixItMessagesAskForOneThingEach() {
        let tailscale = AwaySetupRequest.tailscaleCheck()
        XCTAssertTrue(tailscale.contains("tailscale status") && tailscale.contains("hermes voice setup")
                      && tailscale.contains("Reply with what you found") && tailscale.contains("same account"))
        XCTAssertFalse(tailscale.lowercased().contains("cloudflare"))
        let cloudflare = AwaySetupRequest.cloudflareRestart()
        XCTAssertTrue(cloudflare.contains("speakeasy-cloudflare.py status") && cloudflare.contains("speakeasy-cloudflare.py restart")
                      && cloudflare.contains("hermes voice setup") && cloudflare.contains("\"Update Speakeasy's address\" link"))
        XCTAssertFalse(cloudflare.lowercased().contains("tailscale"))
        let move = AwaySetupRequest.switchToTailscale(device: "iPad")
        XCTAssertTrue(move.contains("Install and start Tailscale") && move.contains("hermes voice setup")
                      && move.contains("step-by-step iPad instructions") && move.contains("same account"))
        XCTAssertFalse(move.lowercased().contains("cloudflare"))
        for text in [tailscale, cloudflare] { XCTAssertFalse(text.lowercased().contains("funnel")) }
        XCTAssertTrue(move.contains("never Tailscale Funnel"))
    }
}
