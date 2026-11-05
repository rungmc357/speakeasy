import XCTest
@testable import SpeakeasyCore

/// "Look at this" on the client side: what the plugin sends (events, status, snapshots, tasks), and
/// one call's sharing state (the button, capture requests, pending pictures and files).
final class AttachmentEventTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    private func run(_ events: [VoiceEvent], from state: VoiceState = VoiceState(), at start: Date? = nil) -> VoiceState {
        var s = state
        var now = start ?? t0
        for event in events { s = reduce(s, event, now: now); now += 0.1 }
        return s
    }

    /// A live call that declared `ready` (sharing off).
    private func ready(_ id: String = "vi_1") -> VoiceState {
        run([.startRequested, .sharing(.declared(.ready)), .sessionAdmitted(interactionID: id), .sessionStarted])
    }

    private func sharingOn(_ id: String = "vi_1") -> VoiceState {
        run([.sharing(.toggle), .sharing(.state(ScreenToggle(on: true, seq: 1)))], from: ready(id))
    }

    private func picture(_ id: String, bytes: Int = 300_000) -> PendingAttachment {
        PendingAttachment(id: id, kind: .picture, name: "\(id).jpg", byteCount: bytes)
    }

    private func snapshot(_ json: [String: Any], id: String = "vi_1") -> InteractionSnapshot {
        var object: [String: Any] = ["interaction_id": id, "status": "live"]
        object.merge(json) { _, new in new }
        return InteractionSnapshot(json: object)!
    }

    // MARK: Events from the plugin

    func testCaptureEventParsesIntoARequestWithItsID() {
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "capture", data: #"{"capture_id":"cap_1a2b"}"#)),
                       [.sharing(.captureRequested("cap_1a2b"))])
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "capture", data: #"{"capture_id":""}"#)), [])
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "capture", data: "{}")), [])
    }

    func testScreenHintAndStateEvents() {
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "screen.hint", data: #"{"reason":"off"}"#)), [.sharing(.hint("off"))])
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "screen.hint", data: "{}")), [.sharing(.hint("screen"))])
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "screen.state", data: #"{"on":false,"seq":4}"#)),
                       [.sharing(.state(ScreenToggle(on: false, seq: 4)))])
        for bad in [#"{"on":"no","seq":1}"#, #"{"on":true}"#, #"{"on":true,"seq":true}"#, #"{"on":true,"seq":-1}"#] {
            XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "screen.state", data: bad)), [], bad)
        }
    }

    func testUnknownEventKindsStayIgnored() {
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "screen.future", data: #"{"x":1}"#)), [])
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "attachments", data: "[]")), [])
    }

    func testSnapshotCarriedCaptureRequestsProduceTheSameEventsAsSSE() throws {
        let fromSSE = parseServerStreamEvent(SSEEvent(name: "capture", data: #"{"capture_id":"cap_1"}"#))
        let interaction: [String: Any] = ["interaction_id": "vi_1", "captures": [["capture_id": "cap_1"]],
                                          "screen": ["on": true, "seq": 1, "declared": "ready"]]
        let streamed = parseServerStreamEvent(SSEEvent(name: "interaction",
            data: String(decoding: try JSONSerialization.data(withJSONObject: interaction), as: UTF8.self)))
        let polled = interactionEvents(try XCTUnwrap(InteractionSnapshot(json: interaction)))
        XCTAssertEqual(streamed, polled, "a polling client gets exactly what the stream gives")
        XCTAssertEqual(streamed.filter { if case .sharing = $0 { return true }; return false }, fromSSE)
        let initial = parseServerStreamEvent(SSEEvent(name: "snapshot",
            data: String(decoding: try JSONSerialization.data(withJSONObject: ["interaction": interaction]), as: UTF8.self)))
        XCTAssertTrue(initial.contains(.sharing(.captureRequested("cap_1"))), "a reconnect's snapshot carries open requests too")

        // Either way the call serves the request once.
        let viaSSE = run(fromSSE + fromSSE, from: sharingOn())
        let viaPolling = run(polled + polled, from: sharingOn())
        XCTAssertEqual(viaSSE.sharing.captures, ["cap_1"])
        XCTAssertEqual(viaPolling.sharing.captures, viaSSE.sharing.captures)
    }

    // MARK: Status, tasks and snapshots

    func testStatusAttachmentsDecideWhetherSharingIsOffered() throws {
        func status(_ attachments: String?) throws -> ServerStatus {
            let block = attachments.map { #","attachments":"# + $0 } ?? ""
            return try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"assistant_name":"Nova"\#(block)}"#.utf8))
        }
        let native = try status(#"{"images":true,"vision":"native","max_attachments":3,"max_image_bytes":2500000,"max_request_image_bytes":6500000,"max_file_bytes":10000000}"#)
        XCTAssertTrue(native.attachmentsSupported)
        XCTAssertEqual(native.attachments, AttachmentSupport(images: true, vision: "native", maxAttachments: 3, maxImageBytes: 2_500_000,
                                                             maxRequestImageBytes: 6_500_000, maxFileBytes: 10_000_000))
        XCTAssertTrue(try status(#"{"images":true,"vision":"described"}"#).attachmentsSupported)
        XCTAssertTrue(try status(#"{"images":true,"vision":"unknown"}"#).attachmentsSupported, "unknown keeps the button")
        XCTAssertFalse(try status(#"{"images":true,"vision":"none"}"#).attachmentsSupported)
        XCTAssertFalse(try status(#"{"images":false,"vision":"native"}"#).attachmentsSupported)
        let old = try status(nil)
        XCTAssertNil(old.attachments)
        XCTAssertFalse(old.attachmentsSupported, "a plugin from before attachments")
        XCTAssertEqual(try status(#"{"images":"yes"}"#).attachmentsSupported, false, "a malformed block is not support")
        XCTAssertEqual(old.assistantName, "Nova", "the rest of the status still decodes")

        XCTAssertEqual(AttachmentSupport.refusalReason(nil), "old_plugin")
        XCTAssertEqual(AttachmentSupport.refusalReason(AttachmentSupport(images: false)), "no_images")
        XCTAssertEqual(AttachmentSupport.refusalReason(AttachmentSupport(images: true, vision: "none")), "no_vision")
    }

    func testTaskSharedDecodesKindAppAndName() {
        let info = WorkInfo(json: ["run_id": "run_1", "status": "completed", "shared": [
            ["kind": "screen", "app": "Xcode"], ["kind": "picture", "name": "header.png"],
            ["kind": "hologram"], ["kind": "file", "name": "Q3 report.pdf", "path": "/never/shown"]]])!
        XCTAssertEqual(info.shared, [SharedItem(number: 1, kind: .screen, app: "Xcode"),
                                     SharedItem(number: 2, kind: .picture, name: "header.png"),
                                     SharedItem(number: 4, kind: .file, name: "Q3 report.pdf")],
                       "numbers keep the server's positions for the image route")
        XCTAssertEqual(info.shared.map(\.label), ["Xcode window", "header.png", "Q3 report.pdf"])
        XCTAssertEqual(info.shared.map(\.isImage), [true, true, false])
        XCTAssertEqual(WorkInfo(json: ["run_id": "r", "status": "working"])?.shared, [])
        XCTAssertEqual(SharingRoute.sharedImage("run_1", 2), "/voice/shared-image/run_1/2")
    }

    func testATaskShowsUpToTwelveSharedThingsNumberedFromOne() {
        // The plugin keeps a task's first 12 (MAX_SHARED), so positions never renumber.
        XCTAssertEqual(SharedItem.maxItems, 12)
        let thirteen: [Any] = (1...13).map { ["kind": "picture", "name": "p\($0).png"] }
        let info = WorkInfo(json: ["run_id": "run_1", "status": "working", "shared": thirteen])!
        XCTAssertEqual(info.shared.count, 12)
        XCTAssertEqual(info.shared.map(\.number), Array(1...12))
        XCTAssertEqual(info.shared.first?.name, "p1.png")
        XCTAssertEqual(info.shared.last?.name, "p12.png", "the first twelve, at their server positions")
        XCTAssertEqual(SharingRoute.sharedImage("run_1", 12), "/voice/shared-image/run_1/12")
    }

    func testSnapshotDecodesScreenCapturesAttachmentsAndHold() {
        let snap = snapshot(["screen": ["on": true, "seq": 2, "declared": "ready"],
                             "captures": [["capture_id": "cap_1"], ["capture_id": "cap_1"], ["nope": 1]],
                             "attachments": [["id": "att_1", "kind": "picture", "name": NSNull(), "app": NSNull(), "state": "pending"],
                                             ["id": "att_2", "kind": "file", "name": "report.pdf", "app": NSNull(), "state": "sending"]],
                             "hold": NSNull()])
        XCTAssertEqual(snap.screen, ScreenSnapshot(on: true, seq: 2, declared: .ready))
        XCTAssertEqual(snap.captures, ["cap_1"])
        XCTAssertEqual(snap.attachments, [ServerAttachment(id: "att_1", kind: "picture"),
                                          ServerAttachment(id: "att_2", kind: "file", name: "report.pdf", sending: true)])
        XCTAssertNil(snap.hold)
        XCTAssertTrue(snap.hasHold, "null says there's no hold")

        let older = snapshot([:])
        XCTAssertNil(older.screen)
        XCTAssertEqual(older.captures, [])
        XCTAssertNil(older.attachments, "a plugin from before attachments says nothing about them")
        XCTAssertFalse(older.hasHold)
        XCTAssertEqual(snapshot(["screen": ["on": false, "seq": 0, "declared": NSNull()]]).screen?.declared, nil)
    }

    func testHoldDecodesLeniently() {
        XCTAssertEqual(ScreenHold(json: ["state": "waiting", "task_id": "call_1", "expires_at": 100.0]),
                       ScreenHold(state: "waiting", taskID: "call_1", expiresAt: Date(timeIntervalSince1970: 100)))
        XCTAssertEqual(ScreenHold(json: ["state": "waiting"])?.line, "Waiting for your screen")
        XCTAssertEqual(ScreenHold(json: [:])?.line, "Waiting for your screen", "no state reads as waiting")
        XCTAssertEqual(ScreenHold(json: ["status": "expired"])?.line, "Not sent: screen sharing was off")
        XCTAssertEqual(ScreenHold(json: ["state": "superseded"])?.isWaiting, false)
        XCTAssertNil(ScreenHold(json: ["state": "sent"])?.line, "a held request that went ahead shows as its task")
        XCTAssertEqual(ScreenHold(json: ["state": "expired", "text": "Not sent: you turned sharing on too late"])?.line,
                       "Not sent: you turned sharing on too late", "the plugin's own words win")
        XCTAssertEqual(ScreenHold(json: "Waiting for your screen")?.isWaiting, true)
        XCTAssertNil(ScreenHold(json: NSNull()))
        XCTAssertNil(ScreenHold(json: 3))

        var s = ready()
        s = reduce(s, .interaction(snapshot(["hold": ["state": "waiting"]])), now: t0 + 1)
        XCTAssertEqual(s.sharing.hold?.line, "Waiting for your screen")
        s = reduce(s, .interaction(snapshot([:])), now: t0 + 2)
        XCTAssertNotNil(s.sharing.hold, "a snapshot without the key changes nothing")
        s = reduce(s, .interaction(snapshot(["hold": NSNull()])), now: t0 + 3)
        XCTAssertNil(s.sharing.hold)
    }

    // MARK: Sharing per call

    func testSharingStartsOffForEveryCallAndResumeAndIsDeclaredAgain() throws {
        var s = sharingOn()
        XCTAssertEqual(s.sharing.button, .on)
        s = run([.endRequested, .serverClosed(.complete, error: nil)], from: s)
        XCTAssertEqual(s.sharing.button, .unavailable, "an ended call keeps nothing")
        XCTAssertFalse(s.sharing.isOn)

        s = run([.startRequested], from: s)
        XCTAssertEqual(s.sharing, CallSharing(), "a new call starts with sharing off")

        // Pause and resume: off again; the resumed session declares again.
        s = sharingOn()
        s = run([.pauseRequested, .sessionClosed], from: s)
        XCTAssertEqual(s.connection, .paused)
        XCTAssertFalse(s.sharing.isOn, "nothing is captured while paused")
        s = run([.resumeRequested], from: s)
        XCTAssertEqual(s.resumeFrom, "vi_1")
        XCTAssertFalse(s.sharing.isOn)
        s = run([.sharing(.declared(.ready)), .sessionAdmitted(interactionID: "vi_2")], from: s)
        XCTAssertEqual(s.sharing.button, .off, "a resume starts with sharing off")
        XCTAssertNil(s.sharing.outgoing, "and sends nothing until the button is pressed")
        s = run([.sharing(.toggle)], from: s)
        XCTAssertEqual(s.sharing.outgoing, ScreenToggle(on: true, seq: 1), "the resumed call counts from 1 again")

        let sdp = "v=0\r\n"
        let resumed = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, resumeFrom: "vi_1", screen: .ready)) as? [String: String]
        XCTAssertEqual(resumed, ["sdp": sdp, "resume_from": "vi_1", "screen": "ready"])
        let blocked = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp, screen: .noPermission)) as? [String: String]
        XCTAssertEqual(blocked, ["sdp": sdp, "screen": "no_permission"])
        let plain = try JSONSerialization.jsonObject(with: sessionRequestBody(sdp: sdp)) as? [String: String]
        XCTAssertEqual(plain, ["sdp": sdp], "no declaration, no key: an older plugin never sees it")
    }

    func testAToggleWhileConnectingIsQueuedAndSentOnAdmission() {
        var s = run([.startRequested, .sharing(.declared(.ready)), .sharing(.toggle)])
        XCTAssertEqual(s.sharing.button, .on, "the button answers at once")
        XCTAssertNil(s.sharing.outgoing, "nothing to send to yet")
        s = run([.sessionAdmitted(interactionID: "vi_1")], from: s)
        XCTAssertEqual(s.sharing.outgoing, ScreenToggle(on: true, seq: 1))
        s = run([.sharing(.state(ScreenToggle(on: false, seq: 0)))], from: s)
        XCTAssertEqual(s.sharing.button, .on, "an older report doesn't undo the toggle on its way")
        s = run([.sharing(.state(ScreenToggle(on: true, seq: 1)))], from: s)
        XCTAssertEqual(s.sharing.button, .on)
        XCTAssertTrue(s.sharing.isOn)

        // On, then off again before admission: nothing needs sending (a call starts off).
        s = run([.startRequested, .sharing(.declared(.ready)), .sharing(.toggle), .sharing(.toggle),
                 .sessionAdmitted(interactionID: "vi_2")])
        XCTAssertNil(s.sharing.outgoing)
        XCTAssertEqual(s.sharing.button, .off)
    }

    func testTheButtonShowsTheConfirmedStateAndRollsBackOnFailure() {
        var s = run([.sharing(.toggle)], from: ready())
        XCTAssertEqual(s.sharing.outgoing, ScreenToggle(on: true, seq: 1))
        XCTAssertEqual(s.sharing.button, .on, "optimistic")
        s = run([.sharing(.toggleFailed(seq: 1, reason: nil))], from: s)
        XCTAssertEqual(s.sharing.button, .off, "rolled back")
        XCTAssertEqual(s.sharing.notice?.text, "Screen sharing didn't change. Try again.")

        // Two quick presses: the first answer doesn't flip the button back.
        s = run([.sharing(.toggle), .sharing(.toggle)], from: ready())
        XCTAssertEqual(s.sharing.outgoing, ScreenToggle(on: false, seq: 2))
        s = run([.sharing(.state(ScreenToggle(on: true, seq: 1)))], from: s)
        XCTAssertEqual(s.sharing.button, .off)
        s = run([.sharing(.toggleFailed(seq: 1, reason: nil))], from: s)
        XCTAssertEqual(s.sharing.button, .off, "a failure for an older toggle changes nothing")
        s = run([.sharing(.toggleFailed(seq: 2, reason: nil))], from: s)
        XCTAssertEqual(s.sharing.button, .on, "back to what the plugin confirmed")

        // The plugin turned it off itself ("stop looking at my screen"): its seq moves ours on.
        s = run([.sharing(.state(ScreenToggle(on: false, seq: 7)))], from: sharingOn())
        XCTAssertEqual(s.sharing.button, .off)
        s = run([.sharing(.toggle)], from: s)
        XCTAssertEqual(s.sharing.outgoing, ScreenToggle(on: true, seq: 8), "never below a seq the plugin has seen")

        // A snapshot published before the toggle, arriving after its answer, changes nothing.
        s = reduce(sharingOn(), .interaction(snapshot(["screen": ["on": false, "seq": 0, "declared": "ready"]])), now: t0 + 5)
        XCTAssertEqual(s.sharing.button, .on, "a late, older snapshot never flips the button back")

        // A snapshot reconciles the same way.
        s = reduce(sharingOn(), .interaction(snapshot(["screen": ["on": false, "seq": 3, "declared": "ready"]])), now: t0 + 5)
        XCTAssertEqual(s.sharing.button, .off)
        let other = reduce(sharingOn(), .interaction(snapshot(["screen": ["on": false, "seq": 9]], id: "vi_other")), now: t0 + 5)
        XCTAssertEqual(other.sharing.button, .on, "another call's snapshot changes nothing")
    }

    func testButtonStatesAndThePermissionLine() {
        XCTAssertEqual(run([.startRequested, .sessionAdmitted(interactionID: "vi_1")]).sharing.button, .unavailable)
        var s = run([.startRequested, .sharing(.declared(.noPermission)), .sessionAdmitted(interactionID: "vi_1"), .sessionStarted])
        XCTAssertEqual(s.sharing.button, .needsPermission)
        s = run([.sharing(.toggle)], from: s)
        XCTAssertEqual(s.sharing.button, .needsPermission, "the button never turns on without permission")
        XCTAssertNil(s.sharing.outgoing)
        XCTAssertEqual(s.sharing.notice?.text, "Screen Recording is off · Set up")
        XCTAssertEqual(s.sharing.notice?.action, .openScreenSettings)
        XCTAssertEqual(present(s).secondary, "Screen Recording is off · Set up", "it shows on the status line")
        s = reduce(s, .tick, now: t0 + ShareNotice.lifetime + 1)
        XCTAssertNil(s.sharing.notice, "and fades")
        XCTAssertEqual(present(s).secondary, "Go ahead")

        let undeclared = run([.sharing(.toggle)], from: run([.startRequested, .sessionAdmitted(interactionID: "vi_1"), .sessionStarted]))
        XCTAssertNil(undeclared.sharing.outgoing)
        XCTAssertNil(undeclared.sharing.notice)

        let refused = run([.sharing(.toggle), .sharing(.toggleFailed(seq: 1, reason: "not_declared"))], from: ready())
        XCTAssertEqual(refused.sharing.button, .unavailable)
    }

    func testCaptureRequestsAreHonoredOnlyWhileSharingIsOnAndOnce() {
        XCTAssertEqual(run([.sharing(.captureRequested("cap_1"))], from: ready()).sharing.captures, [],
                       "never while sharing is off")
        var s = run([.sharing(.captureRequested("cap_1")), .sharing(.captureRequested("cap_1")),
                     .sharing(.captureRequested("cap_2"))], from: sharingOn())
        XCTAssertEqual(s.sharing.captures, ["cap_1", "cap_2"])
        s = run([.sharing(.toggle), .sharing(.captureRequested("cap_3"))], from: s)
        XCTAssertEqual(s.sharing.captures, ["cap_1", "cap_2"], "turned off: the newest choice counts at once")
        let pausing = run([.pauseRequested, .sharing(.captureRequested("cap_4"))], from: sharingOn())
        XCTAssertEqual(pausing.sharing.captures, [])
        let connecting = run([.startRequested, .sharing(.declared(.ready)), .sharing(.toggle), .sharing(.captureRequested("cap_5"))])
        XCTAssertEqual(connecting.sharing.captures, [], "nothing to capture for before admission")
    }

    func testHintPulsesTheButton() {
        let s = run([.sharing(.hint("off")), .sharing(.hint("off"))], from: ready())
        XCTAssertEqual(s.sharing.hintPulse, 2)
        XCTAssertEqual(s.sharing.hint, "off")
    }

    // MARK: Pending pictures and files

    func testUpToThreeWaitAndAFourthIsRefusedWithALine() {
        var s = run((1...4).map { .sharing(.added(picture("p\($0)"))) }, from: ready())
        XCTAssertEqual(s.sharing.visibleAttachments.map(\.id), ["p1", "p2", "p3"])
        XCTAssertEqual(s.sharing.notice?.text, "Up to 3 at a time")
        XCTAssertTrue(s.sharing.hasLocalAttachments)
        s = run([.sharing(.removing("p2")), .sharing(.added(picture("p5")))], from: s)
        XCTAssertEqual(s.sharing.visibleAttachments.map(\.id), ["p1", "p3", "p5"], "a removed one frees its place at once")
    }

    func testAnUploadIsPendingUntilARequestTakesIt() {
        var s = run([.sharing(.added(picture("p1"))), .sharing(.uploading("p1", interactionID: "vi_1")),
                     .sharing(.uploaded("p1", serverID: "att_1", interactionID: "vi_1"))], from: ready())
        XCTAssertEqual(s.sharing.attachments.first?.phase, .pending)
        XCTAssertEqual(s.sharing.attachments.first?.serverID, "att_1")
        XCTAssertFalse(s.sharing.hasLocalAttachments)

        // A snapshot taken before the upload landed doesn't count as "sent".
        s = reduce(s, .interaction(snapshot(["attachments": []])), now: t0 + 1)
        XCTAssertEqual(s.sharing.attachments.count, 1)
        s = reduce(s, .interaction(snapshot(["attachments": [["id": "att_1", "kind": "picture", "state": "sending"]]])), now: t0 + 1.5)
        XCTAssertEqual(s.sharing.attachments.first?.phase, .sending)
        XCTAssertTrue(s.sharing.attachments.first?.seen == true)
        s = reduce(s, .interaction(snapshot(["attachments": []])), now: t0 + 2)
        XCTAssertEqual(s.sharing.attachments, [], "listed, then gone: it went with the request")

        // Polling can miss the pending moment: unlisted well after the upload means sent.
        var polled = run([.sharing(.added(picture("p1"))), .sharing(.uploading("p1", interactionID: "vi_1")),
                          .sharing(.uploaded("p1", serverID: "att_1", interactionID: "vi_1"))], from: ready())
        polled = reduce(polled, .interaction(snapshot(["attachments": []])), now: t0 + CallSharing.unseenGrace + 2)
        XCTAssertEqual(polled.sharing.attachments, [])

        // An older plugin's snapshot (no attachments key) never empties the row.
        var older = run([.sharing(.added(picture("p1"))), .sharing(.uploading("p1", interactionID: "vi_1")),
                         .sharing(.uploaded("p1", serverID: "att_1", interactionID: "vi_1"))], from: ready())
        older = reduce(older, .interaction(snapshot([:])), now: t0 + 60)
        XCTAssertEqual(older.sharing.attachments.count, 1)
    }

    func testRefusalsDropTheItemWithTheirLine() {
        func refused(_ reason: String?) -> VoiceState {
            run([.sharing(.added(picture("p1"))), .sharing(.uploading("p1", interactionID: "vi_1")),
                 .sharing(.refused("p1", reason: reason))], from: ready())
        }
        XCTAssertEqual(refused("too_many").sharing.notice?.text, "Up to 3 at a time")
        XCTAssertEqual(refused("too_large").sharing.notice?.text, "That's too big to send")
        XCTAssertEqual(refused("unsupported").sharing.notice?.text, "Only pictures and files")
        XCTAssertNil(refused("ended").sharing.notice, "nothing to say when the call ended")
        XCTAssertEqual(refused(nil).sharing.notice?.text, "Couldn't attach that")
        XCTAssertEqual(refused("too_large").sharing.attachments, [])

        XCTAssertEqual(run([.sharing(.notice("old_plugin"))], from: ready()).sharing.notice?.text, "Update Speakeasy on your Hermes to share")
        XCTAssertEqual(run([.sharing(.notice("no_vision"))], from: ready()).sharing.notice?.text, "Your Hermes can't read pictures")
        XCTAssertEqual(run([.sharing(.notice(AttachmentFailure.tooLarge.rawValue))], from: ready()).sharing.notice?.text,
                       "That's too big to send", "an item that couldn't be encoded says why")
        XCTAssertNil(shareRefusalLine("closed"))
        XCTAssertEqual(shareRefusalLine("sent"), "Already sent with your request")
    }

    func testPauseKeepsWhatWasntSentAndResumeSendsItAgain() {
        var s = run([.sharing(.added(picture("p1"))), .sharing(.added(picture("p2"))), .sharing(.added(picture("p3"))),
                     .sharing(.uploading("p1", interactionID: "vi_1")), .sharing(.uploading("p2", interactionID: "vi_1")),
                     .sharing(.uploaded("p1", serverID: "att_1", interactionID: "vi_1")),
                     .sharing(.uploaded("p2", serverID: "att_2", interactionID: "vi_1"))], from: ready())
        s = reduce(s, .interaction(snapshot(["attachments": [["id": "att_1", "kind": "picture", "state": "pending"],
                                                              ["id": "att_2", "kind": "picture", "state": "sending"]]])), now: t0 + 1)
        s = run([.pauseRequested, .sessionClosed], from: s, at: t0 + 2)
        XCTAssertEqual(s.connection, .paused)
        XCTAssertEqual(s.sharing.attachments.map(\.id), ["p1", "p3"], "p2 already went with its request")
        XCTAssertEqual(s.sharing.attachments.map(\.phase), [.local, .local])
        XCTAssertNil(s.sharing.attachments.first?.serverID, "the plugin dropped its copy at the pause")
        XCTAssertEqual(run([.sharing(.added(picture("p4")))], from: s).sharing.attachments.count, 3, "drops while paused wait too")

        s = run([.resumeRequested, .sharing(.declared(.ready)), .sessionAdmitted(interactionID: "vi_2")], from: s, at: t0 + 3)
        XCTAssertTrue(s.sharing.hasLocalAttachments, "the client uploads them to the resumed call")
        s = run([.sharing(.uploading("p1", interactionID: "vi_2")),
                 .sharing(.uploaded("p1", serverID: "att_9", interactionID: "vi_2"))], from: s, at: t0 + 4)
        XCTAssertEqual(s.sharing.attachments.first?.serverID, "att_9")
        // A late answer from the paused call's upload changes nothing.
        s = run([.sharing(.uploaded("p1", serverID: "att_old", interactionID: "vi_1"))], from: s, at: t0 + 5)
        XCTAssertEqual(s.sharing.attachments.first?.serverID, "att_9")
        // Its snapshots are this call's: the paused call's no longer count.
        s = reduce(s, .interaction(snapshot(["attachments": []], id: "vi_1")), now: t0 + 60)
        XCTAssertEqual(s.sharing.attachments.count, 2)
    }

    func testAnUploadThatLandsAsTheCallPausesGoesAgainLater() {
        var s = run([.sharing(.added(picture("p1"))), .sharing(.uploading("p1", interactionID: "vi_1")), .pauseRequested],
                    from: ready())
        s = run([.sharing(.uploaded("p1", serverID: "att_1", interactionID: "vi_1"))], from: s)
        XCTAssertEqual(s.sharing.attachments.first?.phase, .local)
        s = run([.sharing(.uploading("p1", interactionID: "vi_1")), .sharing(.uploadDeferred("p1", interactionID: "vi_1"))],
                from: s)
        XCTAssertEqual(s.sharing.attachments.first?.phase, .local)
        XCTAssertEqual(s.sharing.attachments.count, 1)
    }

    func testAnItemTheCallTurnedAwayIsNotRetriedOnThatCall() {
        // 404 or 409 `ended` while the call still looks live (a gateway restart, a pause the client
        // saw fail): offering it to the same call again would only be refused again, in a loop.
        var s = run([.sharing(.added(picture("p1"))), .sharing(.added(picture("p2"))),
                     .sharing(.uploading("p1", interactionID: "vi_1")), .sharing(.uploadDeferred("p1", interactionID: "vi_1"))],
                    from: ready())
        XCTAssertEqual(s.connection, .live)
        XCTAssertEqual(s.sharing.attachments.first?.phase, .local, "kept on this device")
        XCTAssertEqual(s.sharing.attachments.first?.deferredFor, "vi_1")
        XCTAssertTrue(s.sharing.hasLocalAttachments, "still shown, waiting")
        XCTAssertEqual(s.sharing.uploadable(to: "vi_1").map(\.id), ["p2"], "never rescheduled for the call that refused it")

        // Later events on the same call (another upload landing, a new drop) don't re-arm it.
        s = run([.sharing(.uploading("p2", interactionID: "vi_1")),
                 .sharing(.uploaded("p2", serverID: "att_2", interactionID: "vi_1")),
                 .sharing(.added(picture("p3")))], from: s)
        XCTAssertEqual(s.sharing.uploadable(to: "vi_1").map(\.id), ["p3"])
        XCTAssertEqual(s.sharing.uploadable(to: "vi_9").map(\.id), ["p1", "p3"], "any other call takes it")
    }

    func testATurnedAwayItemIsUploadedAgainOnceANewInteractionIsAdmitted() {
        var s = run([.sharing(.added(picture("p1"))), .sharing(.uploading("p1", interactionID: "vi_1")),
                     .sharing(.uploadDeferred("p1", interactionID: "vi_1"))], from: ready())
        XCTAssertEqual(s.sharing.uploadable(to: "vi_1"), [])
        s = run([.pauseRequested, .sessionClosed], from: s, at: t0 + 2)
        XCTAssertEqual(s.connection, .paused)
        XCTAssertEqual(s.sharing.attachments.map(\.id), ["p1"], "kept through the pause")

        s = run([.resumeRequested, .sharing(.declared(.ready)), .sessionAdmitted(interactionID: "vi_2"), .sessionStarted],
                from: s, at: t0 + 3)
        XCTAssertNil(s.sharing.attachments.first?.deferredFor, "a new session forgets the old one's refusal")
        XCTAssertEqual(s.sharing.uploadable(to: "vi_2").map(\.id), ["p1"], "rescheduled for the resumed call")
        s = run([.sharing(.uploading("p1", interactionID: "vi_2")),
                 .sharing(.uploaded("p1", serverID: "att_9", interactionID: "vi_2"))], from: s, at: t0 + 4)
        XCTAssertEqual(s.sharing.attachments.first?.phase, .pending)
        XCTAssertEqual(s.sharing.attachments.first?.serverID, "att_9")
    }

    func testRemovingHidesAtOnceAndRestoresIfThePluginKeepsIt() {
        var s = run([.sharing(.added(picture("p1"))), .sharing(.uploading("p1", interactionID: "vi_1")),
                     .sharing(.uploaded("p1", serverID: "att_1", interactionID: "vi_1")), .sharing(.removing("p1"))], from: ready())
        XCTAssertEqual(s.sharing.visibleAttachments, [])
        XCTAssertEqual(s.sharing.attachments.count, 1, "kept until the plugin let go of it")
        let failed = run([.sharing(.removeFailed("p1"))], from: s)
        XCTAssertEqual(failed.sharing.visibleAttachments.map(\.id), ["p1"])
        XCTAssertEqual(failed.sharing.notice?.text, "Couldn't remove that. Try again.")
        s = run([.sharing(.removed("p1"))], from: s)
        XCTAssertEqual(s.sharing.attachments, [])
        XCTAssertNil(run([.sharing(.removeFailed("p1"))], from: s).sharing.notice, "nothing to restore")

        // Removed while uploading and then refused: no line about something the user removed.
        let quiet = run([.sharing(.added(picture("p2"))), .sharing(.uploading("p2", interactionID: "vi_1")),
                         .sharing(.removing("p2")), .sharing(.refused("p2", reason: "too_large"))], from: ready())
        XCTAssertEqual(quiet.sharing.attachments, [])
        XCTAssertNil(quiet.sharing.notice)
    }

    func testAnEndedCallKeepsNothing() {
        var s = run([.sharing(.added(picture("p1"))), .sharing(.hint("off"))], from: sharingOn())
        s = reduce(s, .interaction(snapshot(["hold": ["state": "waiting"]])), now: t0 + 1)
        s = run([.endRequested, .serverClosed(.complete, error: nil)], from: s, at: t0 + 2)
        XCTAssertEqual(s.sharing.attachments, [])
        XCTAssertNil(s.sharing.hold)
        XCTAssertEqual(s.sharing.button, .unavailable)
    }

    func testADropWithNoCallOpenWaitsForTheNextCall() {
        // After a call ended: kept, with a line that says where it went.
        let ended = run([.endRequested, .serverClosed(.complete, error: nil)], from: ready())
        var s = run([.sharing(.added(picture("p1"))), .tick], from: ended, at: t0 + 5)
        XCTAssertEqual(s.sharing.visibleAttachments.map(\.id), ["p1"], "not wiped by the closed call")
        XCTAssertEqual(s.sharing.attachments.first?.forNextCall, true)
        XCTAssertEqual(s.sharing.notice?.text, CallSharing.nextCallLine)
        XCTAssertEqual(present(s).secondary, CallSharing.nextCallLine, "on the status line with no call")
        XCTAssertEqual(present(s).tone, .success)

        // The next call carries it and is what that call uploads once admitted.
        s = run([.startRequested, .sharing(.declared(.ready)), .sessionAdmitted(interactionID: "vi_2")], from: s, at: t0 + 6)
        XCTAssertEqual(s.sharing.uploadable(to: "vi_2").map(\.id), ["p1"])
        XCTAssertNil(s.sharing.notice, "a new call starts with a clean status line")
        s = run([.sharing(.uploading("p1", interactionID: "vi_2")),
                 .sharing(.uploaded("p1", serverID: "att_1", interactionID: "vi_2"))], from: s, at: t0 + 7)
        XCTAssertEqual(s.sharing.attachments.first?.phase, .pending)
        XCTAssertEqual(s.sharing.attachments.first?.forNextCall, false, "that call's now")
        s = run([.endRequested, .serverClosed(.complete, error: nil)], from: s, at: t0 + 8)
        XCTAssertEqual(s.sharing.attachments, [], "uploaded to a call that ended: gone with it, as before")
    }

    func testADropOnTheBoardSurvivesClosingItAndKeepsTheCap() {
        var s = run([.workOnlyOpened] + (1...4).map { .sharing(.added(picture("p\($0)"))) })
        XCTAssertEqual(s.sharing.visibleAttachments.map(\.id), ["p1", "p2", "p3"])
        XCTAssertEqual(s.sharing.notice?.text, "Up to 3 at a time", "the cap holds with no call")
        s = run([.reset], from: s)
        XCTAssertEqual(s.sharing.visibleAttachments.map(\.id), ["p1", "p2", "p3"], "closing the board keeps them")
        s = run([.sharing(.removing("p2"))], from: s)
        XCTAssertEqual(s.sharing.attachments.map(\.id), ["p1", "p3"], "the ✕ still takes one off")
        s = run([.startRequested], from: s)
        XCTAssertEqual(s.sharing.uploadable(to: "vi_1").map(\.id), ["p1", "p3"])
    }

    func testADropDuringACallIsStillTheCalls() {
        let s = run([.sharing(.added(picture("p1")))], from: ready())
        XCTAssertEqual(s.sharing.attachments.first?.forNextCall, false)
        XCTAssertNil(s.sharing.notice, "no next-call line during a call")
        let paused = run([.pauseRequested, .sessionClosed, .sharing(.added(picture("p2")))], from: s)
        XCTAssertEqual(paused.connection, .paused)
        XCTAssertEqual(paused.sharing.attachments.map(\.forNextCall), [false, false], "a paused call is still open")
    }

    // MARK: Requests

    func testScreenAndCaptureBodiesAreExactly() throws {
        func object(_ data: Data) throws -> NSDictionary { try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? NSDictionary) }
        XCTAssertEqual(String(decoding: try ScreenToggle(on: true, seq: 3).body(), as: UTF8.self), #"{"on":true,"seq":3}"#)
        XCTAssertEqual(String(decoding: try ScreenToggle(on: false, seq: 0).body(), as: UTF8.self), #"{"on":false,"seq":0}"#)
        XCTAssertEqual(try object(CaptureReport.capturing.body()), ["status": "capturing"])
        XCTAssertEqual(try object(CaptureReport.uploading.body()), ["status": "uploading"])
        XCTAssertEqual(try object(CaptureReport.failed(.secureInput).body()), ["status": "failed", "reason": "secure_input"])
        XCTAssertEqual(ScreenToggle(json: ["on": true, "seq": 2]), ScreenToggle(on: true, seq: 2))
        XCTAssertNil(ScreenToggle(json: ["on": 1, "seq": 2]))
    }

    func testUploadHeaders() {
        XCTAssertEqual(attachmentUploadHeaders(kind: .screen, mimeType: "image/jpeg", filename: "screen.jpg", captureID: "cap_1", app: "Xcode"),
                       ["X-Speakeasy-Kind": "screen", "Content-Type": "image/jpeg", "X-Speakeasy-Capture-Id": "cap_1",
                        "X-Speakeasy-App": "Xcode"], "a capture carries its request id and app, never a file name")
        XCTAssertEqual(attachmentUploadHeaders(kind: .picture, mimeType: "image/jpeg", filename: "Header idea.jpg", captureID: "cap_1"),
                       ["X-Speakeasy-Kind": "picture", "Content-Type": "image/jpeg", "X-Speakeasy-Filename": "Header%20idea.jpg"],
                       "pictures never carry a capture id")
        let file = attachmentUploadHeaders(kind: .file, mimeType: "application/pdf", filename: "Résumé (final).pdf")
        XCTAssertEqual(file["X-Speakeasy-Filename"], "R%C3%A9sum%C3%A9%20%28final%29.pdf", "UTF-8, percent-encoded")
        XCTAssertTrue(file.values.allSatisfy { $0.allSatisfy(\.isASCII) }, "header values stay ASCII")
        XCTAssertEqual(attachmentUploadHeaders(kind: .file, mimeType: "text/plain", filename: " ")["X-Speakeasy-Filename"], "file",
                       "a file always has a name")
        XCTAssertEqual(attachmentUploadHeaders(kind: .screen, mimeType: "image/jpeg", captureID: "cap_1", app: "Bärbel's Notes")["X-Speakeasy-App"],
                       "B%C3%A4rbel%27s%20Notes")
        XCTAssertNil(attachmentUploadHeaders(kind: .screen, mimeType: "image/jpeg", captureID: "cap_1", app: " ")["X-Speakeasy-App"])
    }

    func testRoutes() {
        XCTAssertEqual(SharingRoute.screen("vi_1"), "/voice/interactions/vi_1/screen")
        XCTAssertEqual(SharingRoute.attachments("vi_1"), "/voice/interactions/vi_1/attachments")
        XCTAssertEqual(SharingRoute.attachment("vi_1", "att_2"), "/voice/interactions/vi_1/attachments/att_2")
        XCTAssertEqual(SharingRoute.capture("vi_1", "cap_3"), "/voice/interactions/vi_1/captures/cap_3")
        XCTAssertEqual(SharingRoute.capture("vi/../x", "cap 3"), "/voice/interactions/vi%2F%2E%2E%2Fx/captures/cap%203")
    }
}
