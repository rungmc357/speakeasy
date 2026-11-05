import XCTest
@testable import SpeakeasyCore

/// Pictures and files staged on a device for a typed message or new job (plugin `attachments`).
final class StagedAttachmentTests: XCTestCase {
    func testAMessageNamesItsStagedAttachmentsOnlyWhenThereAreAny() {
        let plain = TaskMessageRequest(runID: "run_1", text: " Hi ", clientID: "c1")
        XCTAssertNil(plain.body["attachments"])
        let carrying = TaskMessageRequest(runID: "run_1", text: "", clientID: "c2", attachments: ["att_a", "att_b"])
        XCTAssertEqual(carrying.body["attachments"] as? [String], ["att_a", "att_b"])
        XCTAssertEqual(carrying.body["text"] as? String, "")
    }

    func testRoutesAndReply() {
        XCTAssertEqual(StagedAttachmentRoute.stage, "/voice/attachments")
        XCTAssertEqual(StagedAttachmentRoute.remove("att_1/x"), "/voice/attachments/att_1%2Fx")
        let reply = StagedAttachmentReply(json: ["id": "att_9", "kind": "file", "name": "lease.pdf"])
        XCTAssertEqual(reply?.id, "att_9"); XCTAssertEqual(reply?.name, "lease.pdf")
        XCTAssertNil(StagedAttachmentReply(json: ["kind": "file"]))
    }

    func testPhonePicturesFitTheLongerEdge() {
        let fitted = AttachmentPolicy.fittedSize(CGSize(width: 4032, height: 3024), longEdge: AttachmentPolicy.phoneLongEdge)
        XCTAssertEqual(fitted, CGSize(width: 2048, height: 1536))
        XCTAssertTrue(AttachmentPolicy.stagedFileTypes.contains("application/pdf"))
        XCTAssertFalse(AttachmentPolicy.stagedFileTypes.contains("application/zip"))
    }

    func testTheOutboxTakesAnAttachmentOnlyMessageAndSendsItsIDs() {
        var outbox = TaskOutbox()
        XCTAssertNil(outbox.enqueue(jobID: "j", runID: "run_1", taskName: "Garden", text: "  "))
        let pic = OutgoingAttachment(id: "att_1", kind: .picture, name: "IMG_0001.jpg")
        let m = outbox.enqueue(jobID: "j", runID: "run_1", taskName: "Garden", text: "", attachments: [pic])
        XCTAssertEqual(m?.attachments, [pic])
        XCTAssertEqual(m?.request(interactionID: nil).body["attachments"] as? [String], ["att_1"])
    }
}
