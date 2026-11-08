import XCTest
@testable import SpeakeasyCore

/// Build 70 was withdrawn for "hi" on Home showing as a started task: a New chat's send is a job
/// only when the server made one, and an older plugin's job path is never used for small talk.
final class NewChatSendTests: XCTestCase {
    func testSmallTalkMatchesThePluginsRule() {
        for text in ["hi", "Hi!", "hello there", "hey", "how's it going", "how\u{2019}s it going?",
                     "hey, how's it going?", "Good morning! How are you?", "hi how are you doing today",
                     "what's up", "how's your day going", "um"] {
            XCTAssertTrue(NewChatSend.isSmallTalk(text), text)
        }
        for text in ["Find me a standing desk under $400", "hi can you check the parcel",
                     "hey how's it going, can you check my flight", "how's the weather in Lisbon",
                     "Hey, dim the porch light", "good morning, what's on my calendar", "No", ""] {
            XCTAssertFalse(NewChatSend.isSmallTalk(text), text)
        }
    }

    func testGreetingGoesAsChatWhenTheServerAnswersSmallTalk() {
        XCTAssertEqual(NewChatSend.route("hi", hasAttachments: false, serverChats: true), .chat)
        XCTAssertEqual(NewChatSend.route("how's it going", hasAttachments: false, serverChats: true), .chat)
    }

    func testGreetingToAnOlderPluginIsHeldBackNotMadeAJob() {
        XCTAssertEqual(NewChatSend.route("hi", hasAttachments: false, serverChats: false), .updateHermes)
        XCTAssertEqual(NewChatSend.route("how’s it going", hasAttachments: false, serverChats: false), .updateHermes)
        XCTAssertTrue(NewChatSend.updateHermes.contains("Update Speakeasy on your Hermes"))
    }

    func testARealAskIsAJobOnEveryServer() {
        for chats in [true, false] {
            XCTAssertEqual(NewChatSend.route("Check if my flight on Friday is on time", hasAttachments: false, serverChats: chats), .job)
        }
        // A picture with "hi" is something to look at.
        XCTAssertEqual(NewChatSend.route("hi", hasAttachments: true, serverChats: false), .job)
    }

    func testChatReplyIsNeverAJob() {
        let reply: [String: Any] = ["ok": true, "delivered_as": "chat", "reply": "Hi! Nothing's started.",
                                    "run_id": NSNull(), "task_id": NSNull(), "session_id": NSNull()]
        XCTAssertEqual(NewChatSend.outcome(reply), .chat(reply: "Hi! Nothing's started."))
    }

    func testJobOnlyWhenTheServerMadeOne() {
        XCTAssertEqual(NewChatSend.outcome(["ok": true, "delivered_as": "new_run", "run_id": "run_1", "task_id": "job_1",
                                            "session_id": "s1"]),
                       .job(runID: "run_1", sessionID: "s1"))
        // A plugin from before delivered_as, with the session only on its task.
        XCTAssertEqual(NewChatSend.outcome(["ok": true, "run_id": "run_2", "task": ["session_id": "s2"]]),
                       .job(runID: "run_2", sessionID: "s2"))
        // Nothing started: not ok, no run at all, a chat with no words, or a delivery this app doesn't know.
        XCTAssertNil(NewChatSend.outcome(["ok": false, "error": "nope"]))
        XCTAssertNil(NewChatSend.outcome(["ok": true]))
        XCTAssertNil(NewChatSend.outcome(["ok": true, "delivered_as": "chat"]))
        XCTAssertNil(NewChatSend.outcome(["ok": true, "delivered_as": "something_new", "run_id": "run_3"]))
    }

    func testBody() {
        let body = NewChatSend.body(text: "  hi  ", attachments: [], clientID: "c1", choice: nil, agent: nil)
        XCTAssertEqual(body?["text"] as? String, "hi")
        XCTAssertEqual(body?["source"] as? String, "typed")
        XCTAssertEqual(body?["client_id"] as? String, "c1")
        XCTAssertNil(body?["attachments"])
        XCTAssertNil(body?["chat_replies"], "an older server refuses the unknown key")
        let asking = NewChatSend.body(text: "hi", attachments: [], clientID: "c1", choice: nil, agent: nil, serverChats: true)
        XCTAssertEqual(asking?["chat_replies"] as? Bool, true)
        XCTAssertEqual(NewChatSend.body(text: "", attachments: ["att_1"], clientID: "c2", choice: nil, agent: "work")?["agent"] as? String, "work")
        XCTAssertNil(NewChatSend.body(text: "   ", attachments: [], clientID: "c3", choice: nil, agent: nil))
    }
}
