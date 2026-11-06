import XCTest
@testable import SpeakeasyCore

/// Agents: which Hermes profile a chat talks to (docs/API.md "Agents").
final class AgentChoiceTests: XCTestCase {
    func testRosterParsesTheServersShape() {
        let r = AgentRoster(json: [
            "agents": [["id": "default", "name": "Hermes", "default": true],
                       ["id": "garden", "name": "Garden helper", "description": "Plans the vegetable beds."]],
            "default": "default", "allowed": ["garden", "gone-away"],
            "available": [["id": "default", "name": "Hermes", "default": true], ["id": "garden", "name": "Garden helper"],
                          ["id": "research-desk", "name": "Research desk"]],
            "multiplexed": true,
        ])
        XCTAssertEqual(r.agents.map(\.id), ["default", "garden"])
        XCTAssertTrue(r.showsPicker)
        XCTAssertEqual(r.ownAgent?.name, "Hermes")
        XCTAssertEqual(r.agent(nil)?.id, "default")
        XCTAssertEqual(r.agent("garden")?.description, "Plans the vegetable beds.")
        XCTAssertNil(r.agent("research-desk"))           // available, but not allowed
        XCTAssertEqual(r.others.map(\.id), ["garden", "research-desk"])
        XCTAssertEqual(r.allowed, ["garden", "gone-away"])
    }

    func testOneAgentHidesThePicker() {
        let r = AgentRoster(json: ["agents": [["id": "default", "name": "Hermes", "default": true]], "default": "default"])
        XCTAssertFalse(r.showsPicker)
        XCTAssertFalse(AgentRoster(json: nil).showsPicker)   // an older plugin: nothing
    }

    func testAllowlistPatchBody() {
        let body = AgentRoster.allowedPatch(["garden"])
        XCTAssertEqual((body["agents"] as? [String: Any])?["allowed"] as? [String], ["garden"])
    }

    func testSessionsAndPagesCarryTheirAgent() {
        let row = HistorySession(json: ["id": "s1", "title": "Bed plan", "source": "speakeasy",
                                        "agent": ["id": "garden", "name": "Garden helper"]])
        XCTAssertEqual(row?.agent?.name, "Garden helper")
        XCTAssertNil(HistorySession(json: ["id": "s2"])?.agent)
        let page = HistoryConversation(json: ["session": ["id": "s1", "title": "Bed plan", "agent": ["id": "garden", "name": "Garden helper"]]])
        XCTAssertEqual(page?.agent?.id, "garden")
    }

    func testCapabilityFlag() throws {
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"agents": true}"#.utf8))
        XCTAssertTrue(caps.agents)
        XCTAssertFalse(try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8)).agents)
    }
}
