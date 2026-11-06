import XCTest
@testable import SpeakeasyCore

final class SlashSkillsTests: XCTestCase {
    let skills = DemoSlashSkills.all

    func testOnlyALeadingSlashWordOpensTheMenu() {
        XCTAssertEqual(SlashMenu.commandQuery("/"), "")
        XCTAssertEqual(SlashMenu.commandQuery("/sou"), "sou")
        XCTAssertNil(SlashMenu.commandQuery("/soup-scaler for twelve"))   // past the command: closed
        XCTAssertNil(SlashMenu.commandQuery("scale it /sou"))
        XCTAssertNil(SlashMenu.commandQuery("/usr/local"))                // a path, not a skill
        XCTAssertNil(SlashMenu.commandQuery(""))
    }

    func testRankingPrefixThenWordThenAnywhere() {
        XCTAssertEqual(SlashMenu.rank(skills, "ga").map(\.command), ["garden-plan"])
        XCTAssertEqual(SlashMenu.rank(skills, "plan").first?.command, "garden-plan")      // word start
        XCTAssertEqual(SlashMenu.rank(skills, "scal").map(\.command), ["soup-scaler"])
        XCTAssertEqual(SlashMenu.rank(skills, "grocery").map(\.command), ["meal-prep"])   // description
        XCTAssertEqual(SlashMenu.rank(skills, "").count, min(skills.count, SlashMenu.limit))
        XCTAssertEqual(SlashMenu.rank(skills, "pdf_sum").map(\.command), ["pdf-summary"])
        XCTAssertTrue(SlashMenu.rank(skills, "zzz").isEmpty)
    }

    func testMenuOpensFiltersPicksAndDismisses() {
        var menu = SlashMenu(skills: skills)
        menu.update(text: "/")
        XCTAssertTrue(menu.isOpen)
        menu.update(text: "/so")
        XCTAssertEqual(menu.highlightedSkill?.command, "soup-scaler")
        XCTAssertEqual(menu.pick(menu.matches[0], in: "/so"), "/soup-scaler ")
        menu.update(text: "/soup-scaler ")
        XCTAssertFalse(menu.isOpen)
        menu.update(text: "/")
        menu.move(1); XCTAssertEqual(menu.highlighted, 1)
        menu.move(-2); XCTAssertEqual(menu.highlighted, menu.matches.count - 1)
        menu.update(text: "/m")
        XCTAssertEqual(menu.highlighted, 0)     // a new list starts at the top
        menu.dismiss()
        XCTAssertFalse(menu.isOpen)
        menu.update(text: "/m")
        XCTAssertFalse(menu.isOpen)            // stays dismissed for the same word
        menu.update(text: "/me")
        XCTAssertTrue(menu.isOpen)             // a new word opens it again
    }

    func testLeadingSkill() {
        let menu = SlashMenu(skills: skills)
        XCTAssertEqual(menu.leadingSkill(in: "/soup-scaler for twelve")?.command, "soup-scaler")
        XCTAssertEqual(menu.leadingSkill(in: "/Soup_Scaler ")?.command, "soup-scaler")
        XCTAssertNil(menu.leadingSkill(in: "/soup-scaler"))          // still typing
        XCTAssertNil(menu.leadingSkill(in: "/nope for twelve"))
        XCTAssertNil(menu.leadingSkill(in: "make /soup-scaler"))
    }

    func testNoSkillsNeverOpens() {
        var menu = SlashMenu()
        menu.update(text: "/")
        XCTAssertFalse(menu.isOpen)
    }

    func testJSONAndCapability() throws {
        let skill = SlashSkill(json: ["command": "trip-packing", "name": "trip-packing", "description": "Pack.", "category": "travel"])
        XCTAssertEqual(skill?.title, "Trip packing")
        XCTAssertNil(SlashSkill(json: ["name": "no command"]))
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"slash_skills": true}"#.utf8))
        XCTAssertTrue(caps.slashSkills)
        XCTAssertFalse(try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8)).slashSkills)
    }
}
