import Foundation
import XCTest
@testable import SpeakeasyCore

final class PairingTests: XCTestCase {
    func testParsesPairLink() throws {
        let url = URL(string: "speakeasy://pair?server=http%3A%2F%2F127.0.0.1%3A8795&code=123456")!
        let link = try PairingLink.parse(url).get()
        XCTAssertEqual(link.server.absoluteString, "http://127.0.0.1:8795")
        XCTAssertEqual(link.code, "123456")
    }

    func testParsesUnencodedServerAndTailnetHTTPS() throws {
        let url = URL(string: "speakeasy://pair?server=https://voice.example.ts.net&code=654321")!
        XCTAssertEqual(try PairingLink.parse(url).get().server.host, "voice.example.ts.net")
        let trailing = URL(string: "speakeasy://pair?server=http://localhost:8795/&code=000111")!
        XCTAssertEqual(try PairingLink.parse(trailing).get().server.absoluteString, "http://localhost:8795")
    }

    func testRejectsBadLinks() {
        func failure(_ s: String) -> PairingLink.ParseError? {
            if case .failure(let e) = PairingLink.parse(URL(string: s)!) { return e }
            return nil
        }
        XCTAssertEqual(failure("https://pair?server=http://127.0.0.1:8795&code=123456"), .notSpeakeasy)
        XCTAssertEqual(failure("speakeasy://open?server=http://127.0.0.1:8795&code=123456"), .notPairing)
        XCTAssertEqual(failure("speakeasy://pair?code=123456"), .missingServer)
        XCTAssertEqual(failure("speakeasy://pair?server=https://evil.example.com&code=123456"), .untrustedServer)
        XCTAssertEqual(failure("speakeasy://pair?server=http://voice.example.ts.net&code=123456"), .untrustedServer)
        XCTAssertEqual(failure("speakeasy://pair?server=http://127.0.0.1:8795&code=12345"), .invalidCode)
        XCTAssertEqual(failure("speakeasy://pair?server=http://127.0.0.1:8795&code=12a456"), .invalidCode)
        XCTAssertEqual(failure("speakeasy://pair?server=http://127.0.0.1:8795"), .invalidCode)
    }

    func testCodeNormalization() {
        XCTAssertEqual(normalizedPairingCode("123 456"), "123456")
        XCTAssertEqual(normalizedPairingCode(" 123-456 "), "123456")
        XCTAssertNil(normalizedPairingCode("1234567"))
        XCTAssertNil(normalizedPairingCode("١٢٣٤٥٦"), "non-ASCII digits are refused")
    }

    func testPairBodyAndResponse() throws {
        let body = try JSONSerialization.jsonObject(with: pairRequestBody(code: "123456", deviceName: "  ")) as? [String: String]
        XCTAssertEqual(body, ["code": "123456", "device_name": "Mac"])
        let response = try JSONDecoder().decode(PairResponse.self, from: Data(#"{"device_id":"ab12","token":"t0k","extra":1}"#.utf8))
        XCTAssertEqual(response, PairResponse(deviceID: "ab12", token: "t0k"))
    }
}

final class URLTrustTests: XCTestCase {
    func testTrustBoundary() {
        XCTAssertNotNil(trustedServerBaseURL("http://127.0.0.1:8795"))
        XCTAssertNotNil(trustedServerBaseURL("https://127.0.0.1:8795"))
        XCTAssertNotNil(trustedServerBaseURL("http://localhost:8795"))
        XCTAssertNotNil(trustedServerBaseURL("http://[::1]:8795"))
        XCTAssertNotNil(trustedServerBaseURL("https://voice.example.ts.net"))
        XCTAssertNotNil(trustedServerBaseURL("https://100.64.0.1:8795"))
        XCTAssertNotNil(trustedServerBaseURL("https://100.127.255.254"))
        XCTAssertNil(trustedServerBaseURL("https://100.128.0.1"), "outside 100.64/10")
        XCTAssertNil(trustedServerBaseURL("https://100.63.0.1"), "outside 100.64/10")
        XCTAssertNil(trustedServerBaseURL("https://100.64.0.999"))
        XCTAssertNil(trustedServerBaseURL("http://100.64.0.1"), "tailnet needs https")
        XCTAssertNil(trustedServerBaseURL("http://voice.example.ts.net"), "tailnet needs https")
        XCTAssertNil(trustedServerBaseURL("https://.ts.net"))
        XCTAssertNil(trustedServerBaseURL("https://ts.net.evil.com"))
        XCTAssertNil(trustedServerBaseURL("https://example.com"))
        XCTAssertNil(trustedServerBaseURL("http://192.168.1.2:8795"))
        XCTAssertNil(trustedServerBaseURL("ftp://127.0.0.1"))
        XCTAssertNil(trustedServerBaseURL("http://user:pass@127.0.0.1:8795"))
        XCTAssertNil(trustedServerBaseURL("http://127.0.0.1:8795?x=1"))
        XCTAssertNil(trustedServerBaseURL("http://127.0.0.1:8795/#f"))
        XCTAssertNil(trustedServerBaseURL("https://voice.example.ts.net/path"))
        XCTAssertNil(trustedServerBaseURL(""))
    }
}

final class ServerSettingsTests: XCTestCase {
    func testDecodesFullSettings() throws {
        let json = #"""
        {"assistant_name":"Juniper","user_name":"Sam","voice":{"provider":"openai","voice":"marin"},
         "idle_pause_minutes":10,"notify_target":null,"instructions_extra":"Answer briefly",
         "delivery":{"target":"telegram","new_thread_per_task":true},"continuity":{"enabled":false},
         "brief":{"auto_refresh":false,"include_recent_voice":true},"hermes_profile":"default","future":[1,2]}
        """#
        let s = try ServerSettings.decode(Data(json.utf8))
        XCTAssertEqual(s.resolvedAssistantName, "Juniper")
        XCTAssertEqual(s.userName, "Sam")
        XCTAssertEqual(s.resolvedProvider, .openai)
        XCTAssertEqual(s.voice?.voice, "marin")
        XCTAssertEqual(s.idleTimeout, 600)
        XCTAssertEqual(s.deliveryTarget, "telegram")
        XCTAssertEqual(s.delivery?.newThread, true, "the pre-channels flag is still read")
        XCTAssertEqual(s.continuity?.enabled, false)
        XCTAssertEqual(s.brief?.autoRefresh, false)
        XCTAssertEqual(s.brief?.includeRecentVoice, true)
        XCTAssertEqual(s.instructionsExtra, "Answer briefly")
    }

    func testDefaultsAndTolerance() throws {
        let s = try ServerSettings.decode(Data(#"{"settings":{"assistant_name":"  ","idle_pause_minutes":"five","voice":{"provider":"weird"}}}"#.utf8))
        XCTAssertEqual(s.resolvedAssistantName, "Hermes")
        XCTAssertEqual(s.idleTimeout, 900)
        XCTAssertEqual(s.resolvedProvider, .codex)
        XCTAssertNil(s.deliveryTarget)
        XCTAssertEqual(try ServerSettings.decode(Data("{}".utf8)), ServerSettings())
        XCTAssertEqual(ServerSettings(idlePauseMinutes: 0).idleTimeout, 0, "0 turns auto-pause off")
        XCTAssertEqual(ServerSettings(notifyTarget: "discord").deliveryTarget, "discord", "legacy field still read")
        XCTAssertNil(ServerSettings(delivery: .init(target: "none")).deliveryTarget)
    }

    func testPatchBodyRoundTrips() throws {
        let s = ServerSettings(assistantName: "Juniper", userName: "Sam", voice: .init(provider: "codex", voice: "cedar"),
                               idlePauseMinutes: 3, delivery: .init(target: " ", newThread: true),
                               continuity: .init(enabled: true), instructionsExtra: "x",
                               brief: .init(autoRefresh: true, includeRecentVoice: false))
        let object = try JSONSerialization.jsonObject(with: s.patchBody()) as? [String: Any]
        XCTAssertEqual((object?["delivery"] as? [String: Any])?["target"] as? String, "none", "blank target means off")
        XCTAssertNil(object?["notify_target"], "the server rejects unknown keys")
        let back = try ServerSettings.decode(s.patchBody())
        XCTAssertEqual(back.assistantName, "Juniper")
        XCTAssertEqual(back.voice, s.voice)
        XCTAssertEqual(back.brief, s.brief)
        XCTAssertEqual(back.continuity?.enabled, true)
        XCTAssertEqual(back.idleTimeout, 180)
        XCTAssertNil(object?["audio_duck"], "left alone unless edited")
    }

    func testAudioDuckSettingRoundTrips() throws {
        let read = try ServerSettings.decode(Data(#"{"settings":{"audio_duck":{"enabled":false,"level":30,"hold_s":4.0}}}"#.utf8))
        XCTAssertEqual(read.audioDuck?.enabled, false)
        var s = read
        s.audioDuck?.enabled = true
        let object = try JSONSerialization.jsonObject(with: s.patchBody()) as? [String: Any]
        XCTAssertEqual(object?["audio_duck"] as? [String: Bool], ["enabled": true], "only the switch: level and hold stay as set")
    }

    func testBriefDecoding() throws {
        let b = try JSONDecoder().decode(VoiceBrief.self, from: Data(#"{"text":"About you","state":"ready","updated_at":1700000000,"edited":true}"#.utf8))
        XCTAssertEqual(b.text, "About you"); XCTAssertEqual(b.state, "ready"); XCTAssertTrue(b.edited)
        XCTAssertEqual(b.updatedAt, Date(timeIntervalSince1970: 1_700_000_000))
        let iso = try JSONDecoder().decode(VoiceBrief.self, from: Data(#"{"text":"","updated_at":"2026-01-02T03:04:05Z"}"#.utf8))
        XCTAssertNotNil(iso.updatedAt)
        XCTAssertFalse(iso.edited)
        let empty = try JSONDecoder().decode(VoiceBrief.self, from: Data(#"{"state":"missing","updated_at":null}"#.utf8))
        XCTAssertEqual(empty.text, ""); XCTAssertNil(empty.updatedAt)
    }

    func testStatusChecks() throws {
        let s = try JSONDecoder().decode(ServerStatus.self, from: Data(#"""
        {"assistant_name":null,"provider":"codex","codex_signed_in":false,"api_key_set":true,
         "brief_state":"writing","hermes_api_ok":true,"version":"0.1.0","threads_supported":true}
        """#.utf8))
        XCTAssertEqual(s.resolvedAssistantName, "Hermes")
        XCTAssertEqual(s.threadsSupported, true)
        XCTAssertFalse(s.readyForCalls)
        XCTAssertEqual(s.checks.first { $0.id == "voice" }?.fix, "On the Hermes machine, run hermes voice setup (it signs you in to ChatGPT)")
        var openai = s; openai.provider = "openai"
        XCTAssertTrue(openai.readyForCalls, "API key set; the brief is optional")
        XCTAssertEqual(openai.checks.first { $0.id == "voice" }?.title, "OpenAI API key")
    }

    func testDestinationsAndOnboarding() throws {
        let d = Destination.list(Data(#"{"destinations":[{"target":"telegram","label":"Telegram","threads_supported":true},"discord",{"target":"none"},{"nope":1}]}"#.utf8))
        XCTAssertEqual(d.map(\.target), ["telegram", "discord"])
        XCTAssertEqual(d[1].label, "Discord")
        XCTAssertTrue(d[0].threadsSupported)
        XCTAssertEqual(Destination.list(Data("[]".utf8)), [])

        XCTAssertEqual(OnboardingStatus.parse(Data(#"{"steps":{"names":true,"delivery":false,"brief":{"done":true}}}"#.utf8)).done,
                       ["names", "brief"])
        XCTAssertTrue(OnboardingStatus.parse(Data(#"{"completed":true}"#.utf8)).isComplete)
        let body = try JSONSerialization.jsonObject(with: OnboardingStatus.postBody(
            assistantName: "", userName: " ", target: nil)) as? [String: Any]
        XCTAssertEqual(body?["assistant_name"] as? String, "Hermes")
        XCTAssertEqual(body?["delivery_target"] as? String, "none")
        XCTAssertEqual(Set(body?.keys ?? [:].keys), ["assistant_name", "user_name", "delivery_target", "write_brief"],
                       "only the keys POST /voice/onboarding accepts")
    }

    /// Payloads copied from the plugin's docs/API.md: the app must read what the server sends.
    func testServerOnboardingAndDestinationShapes() {
        let status = OnboardingStatus.parse(Data(#"""
        {"steps":{"paired":true,"codex_signed_in":true,"voice_ready":true,"names_set":true,"delivery_set":true,"brief_ready":false},
         "complete":true,"brief_state":"none","assistant_name":"Nova","user_name":"Sam","delivery_target":"none"}
        """#.utf8))
        XCTAssertTrue(status.isComplete)
        XCTAssertTrue(status.done.isSuperset(of: ["paired", "names_set", "delivery_set"]))
        let d = Destination.list(Data(#"""
        {"destinations":[{"platform":"telegram","connected":true,"state":"connected","target":"telegram",
          "home_channel":{"name":"Home","target":"telegram:123456"},
          "chats":[{"name":"Trip planning","type":"group","target":"telegram:-100222"}]},
          {"platform":"slack","connected":false,"target":"slack"}],
         "none":{"target":"none","name":"Don't post results anywhere"},"current":"none","threads_supported":true}
        """#.utf8))
        XCTAssertEqual(d.map(\.target), ["telegram:123456", "telegram:-100222"])
        XCTAssertEqual(d.map(\.label), ["Telegram · Home", "Telegram · Trip planning"])
        XCTAssertTrue(d.allSatisfy(\.threadsSupported))
    }

    func testAssistantNameAndIdleTimeoutFlowIntoState() {
        var s = VoiceState()
        s.assistantName = "Juniper"; s.idleTimeout = 120
        let t0 = Date(timeIntervalSince1970: 1000)
        s = reduce(s, .startRequested, now: t0)
        XCTAssertEqual(s.assistantName, "Juniper", "config survives a new call")
        s = reduce(s, .sessionAdmitted(interactionID: "vi"), now: t0)
        s = reduce(s, .sessionStarted, now: t0)
        XCTAssertFalse(s.idleExpired(at: t0 + 119))
        XCTAssertTrue(s.idleExpired(at: t0 + 120))
        s.idleTimeout = 0
        XCTAssertFalse(s.idleExpired(at: t0 + 10_000), "0 disables auto-pause")
        s = reduce(s, .outputDelta("Hi."), now: t0)
        s = reduce(s, .reset, now: t0)
        XCTAssertEqual(present(s).primary, "Juniper")
    }
}

final class PanelVisibilityTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1000)

    func testHotkeyNeverLeavesPanelStuck() {
        XCTAssertEqual(hotkeyAction(connection: .idle, panelVisible: false), .startCall)
        XCTAssertEqual(hotkeyAction(connection: .live, panelVisible: true), .endCall)
        XCTAssertEqual(hotkeyAction(connection: .connecting, panelVisible: true), .endCall)
        XCTAssertEqual(hotkeyAction(connection: .paused, panelVisible: true), .endCall)
        XCTAssertEqual(hotkeyAction(connection: .ending, panelVisible: true), .hidePanel)
        XCTAssertEqual(hotkeyAction(connection: .live, panelVisible: false), .showPanel, "x hid it mid-call")
        XCTAssertEqual(hotkeyAction(connection: .paused, panelVisible: false), .showPanel)
        // After End the panel is up showing Start: the hotkey hides it, then starts next time.
        XCTAssertEqual(hotkeyAction(connection: .ended(.complete), panelVisible: true), .hidePanel)
        XCTAssertEqual(hotkeyAction(connection: .ended(.complete), panelVisible: false), .startCall)
        XCTAssertEqual(hotkeyAction(connection: .failed("x"), panelVisible: true), .hidePanel)
        XCTAssertEqual(hotkeyAction(connection: .failed("x"), panelVisible: false), .startCall)
    }

    func testAutoHidesFourSecondsAfterEnd() {
        var hide = PanelAutoHide()
        XCTAssertFalse(hide.shouldHide(now: t0, hovering: false, needsAttention: false), "not armed")
        hide.arm(now: t0)
        XCTAssertFalse(hide.shouldHide(now: t0 + 3.9, hovering: false, needsAttention: false))
        XCTAssertTrue(hide.shouldHide(now: t0 + 4, hovering: false, needsAttention: false))
        XCTAssertFalse(hide.isArmed)
        XCTAssertFalse(hide.shouldHide(now: t0 + 10, hovering: false, needsAttention: false), "fires once")
    }

    func testHoverAndAttentionHoldThePanel() {
        var hide = PanelAutoHide()
        hide.arm(now: t0)
        XCTAssertFalse(hide.shouldHide(now: t0 + 5, hovering: true, needsAttention: false))
        XCTAssertFalse(hide.shouldHide(now: t0 + 8, hovering: false, needsAttention: false), "fresh grace after hover")
        XCTAssertTrue(hide.shouldHide(now: t0 + 9, hovering: false, needsAttention: false))
        hide.arm(now: t0)
        XCTAssertFalse(hide.shouldHide(now: t0 + 60, hovering: false, needsAttention: true))
        hide.cancel()
        XCTAssertFalse(hide.shouldHide(now: t0 + 100, hovering: false, needsAttention: false))
    }

    func testAttentionDetection() {
        var s = VoiceState()
        XCTAssertFalse(panelNeedsAttention(s))
        s.tasks = [TaskItem(id: "a", info: WorkInfo(runID: "r", status: "waiting_for_approval"))]
        XCTAssertTrue(panelNeedsAttention(s))
        s.tasks = [TaskItem(id: "a", info: WorkInfo(runID: "r", status: "completed"))]
        XCTAssertFalse(panelNeedsAttention(s))
        s.approval = ApprovalInfo(runID: "r", requestID: "q", description: "d")
        XCTAssertTrue(panelNeedsAttention(s))
    }
}

final class SlimSummaryTests: XCTestCase {
    func task(_ id: String, _ status: String, drafts: [EmailDraft] = []) -> TaskItem {
        var info = WorkInfo(runID: "run_\(id)", status: status)
        info.emailDrafts = drafts
        return TaskItem(id: id, info: info)
    }

    func testSummaries() {
        XCTAssertNil(slimTaskSummary([]))
        XCTAssertEqual(slimTaskSummary([task("a", "working")]), "1 task running")
        XCTAssertEqual(slimTaskSummary([task("a", "working"), task("b", "working")]), "2 tasks running")
        XCTAssertEqual(slimTaskSummary([task("a", "working"), task("b", "working"), task("c", "completed")]), "2 running · 1 done")
        XCTAssertEqual(slimTaskSummary([task("a", "completed")]), "1 task done")
        XCTAssertEqual(slimTaskSummary([task("a", "completed"), task("b", "failed")]), "2 tasks done")
    }

    func testWaitingTasksWin() {
        XCTAssertEqual(slimTaskSummary([task("a", "working"), task("b", "waiting_for_approval")]), "1 task needs you")
        let draft = EmailDraft(draftID: "d", sha256: "h", subject: "s", body: "b")
        XCTAssertEqual(slimTaskSummary([task("a", "working", drafts: [draft]), task("b", "waiting_for_approval")]), "2 tasks need you")
        var sent = draft; sent.status = .sent
        XCTAssertEqual(slimTaskSummary([task("a", "completed", drafts: [sent])]), "1 task done")
        XCTAssertEqual(slimTaskSummary([], approvalPending: true), "1 task needs you")
    }

    func testHoldShowsInSlimMode() {
        XCTAssertEqual(slimTaskSummary([], holdActive: true), "Waiting for your screen")
        XCTAssertEqual(slimTaskSummary([task("a", "working"), task("b", "completed")], holdActive: true),
                       "Waiting for your screen", "a held request only waits seconds, so it wins over the counts")
        XCTAssertEqual(slimTaskSummary([task("a", "waiting_for_approval")], approvalPending: true, holdActive: true),
                       "Waiting for your screen", "the approval keeps its own row in slim mode")
        XCTAssertEqual(slimTaskSummary([task("a", "working")], holdActive: false), "1 task running")
    }
}

final class DeliveryChannelsTests: XCTestCase {
    func testChannelsDecodeAndPatch() throws {
        let json = #"""
        {"settings":{"delivery":{"target":"telegram:1","new_thread":true,
          "channels":[{"target":"discord:2","label":"#work","topic":"software","new_thread":true},{"target":"discord:3"}]}}}
        """#
        let s = try ServerSettings.decode(Data(json.utf8))
        XCTAssertEqual(s.delivery?.newThread, true)
        XCTAssertEqual(s.delivery?.channels?.count, 2)
        XCTAssertEqual(s.delivery?.channels?.first?.label, "#work")
        XCTAssertEqual(s.delivery?.channels?.last?.label, "discord:3", "a missing label falls back to the target")
        let body = try JSONSerialization.jsonObject(with: s.patchBody()) as? [String: Any]
        let d = body?["delivery"] as? [String: Any]
        XCTAssertEqual(d?["new_thread"] as? Bool, true)
        XCTAssertNil(d?["new_thread_per_task"], "the old key is never sent")
        XCTAssertEqual((d?["channels"] as? [[String: Any]])?.first?["topic"] as? String, "software")
    }

    func testNewThreadIsOffWithoutADefaultTarget() throws {
        let s = ServerSettings(delivery: .init(target: "none", newThread: true))
        let d = (try JSONSerialization.jsonObject(with: s.patchBody()) as? [String: Any])?["delivery"] as? [String: Any]
        XCTAssertEqual(d?["new_thread"] as? Bool, false)
    }

    func testThreadToggleOnlyWhereSupported() throws {
        let json = #"{"threads_supported":true,"thread_platforms":["discord","telegram"],"tailscale_name":"box.tail1.ts.net","routing_model":"auto"}"#
        let status = try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
        XCTAssertTrue(status.canOpenThread(in: "discord:123"))
        XCTAssertFalse(status.canOpenThread(in: "signal:123"))
        XCTAssertFalse(status.canOpenThread(in: nil))
        XCTAssertEqual(status.reachability, "Connected over Tailscale: box.tail1.ts.net")
        XCTAssertEqual(status.routingModel, "auto")
        let off = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"threads_supported":false,"thread_platforms":["discord"]}"#.utf8))
        XCTAssertFalse(off.canOpenThread(in: "discord:123"))
        XCTAssertEqual(off.reachability, "Local only")
    }

    func testSpeechAndCallCapRoundTrip() throws {
        let raw = #"{"voice":{"provider":"codex","voice":"cove","max_call_minutes":45},"speech":{"progress":false}}"#
        var s = try ServerSettings.decode(Data(raw.utf8))
        XCTAssertEqual(s.maxCallMinutes, 45)
        XCTAssertEqual(s.speech?.progress, false)
        XCTAssertEqual(s.voice?.provider, "codex")
        s.maxCallMinutes = 60
        let body = try JSONSerialization.jsonObject(with: s.patchBody()) as! [String: Any]
        let voice = body["voice"] as! [String: Any]
        XCTAssertEqual(voice["max_call_minutes"] as? Int, 60)
        XCTAssertEqual(voice["provider"] as? String, "codex")
        XCTAssertEqual((body["speech"] as! [String: Any])["progress"] as? Bool, false)
    }

    func testSuggestionsParseAndMerge() {
        let raw = ##"{"suggestions":[{"target":"discord:2","label":"#work","topic":"code","new_thread":true},{"target":"discord:2","label":"#dup"},{"label":"no target"}]}"##
        let items = ChannelSuggestions.parse(Data(raw.utf8))
        XCTAssertEqual(items.map(\.label), ["#work"])
        let existing = [ServerSettings.Channel(target: "discord:9", label: "#Work")]
        XCTAssertEqual(ChannelSuggestions.merge(items, into: existing).count, 1, "same label is not added twice")
        XCTAssertEqual(ChannelSuggestions.merge(items, into: []).count, 1)
    }

    func testOnboardingBodyCarriesContinuity() throws {
        let body = try JSONSerialization.jsonObject(with: OnboardingStatus.postBody(
            assistantName: "Nova", userName: "", target: nil, continuity: false)) as? [String: Any]
        XCTAssertEqual(body?["continuity_enabled"] as? Bool, false)
        let without = try JSONSerialization.jsonObject(with: OnboardingStatus.postBody(
            assistantName: "Nova", userName: "", target: nil)) as? [String: Any]
        XCTAssertNil(without?["continuity_enabled"])
    }
}
