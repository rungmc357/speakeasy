import XCTest
@testable import SpeakeasyCore

/// The Scheduled screen's states, against the in-memory demo backend (no server).
@MainActor
final class SchedulesModelTests: XCTestCase {
    private func model() -> (SchedulesModel, DemoScheduleBackend) {
        let backend = DemoScheduleBackend()
        return (SchedulesModel(backend: backend), backend)
    }

    func testLoadsAndSections() async {
        let (m, _) = model()
        XCTAssertFalse(m.loaded)
        await m.reload()
        XCTAssertTrue(m.loaded)
        XCTAssertNil(m.error)
        XCTAssertEqual(m.sections.map(\.0), [.upcoming, .paused, .done])
        XCTAssertEqual(m.sections[0].1.first?.id, "build", "running first")
        XCTAssertEqual(m.activeCount, 3)
        XCTAssertNotNil(m.nextUp)
    }

    func testPauseResumeRunNowNotices() async {
        let (m, _) = model()
        await m.reload()
        await m.pause("garden")
        XCTAssertEqual(m.job("garden")?.isPaused, true)
        XCTAssertNil(m.job("garden")?.nextRun)
        XCTAssertEqual(m.notices["garden"]?.isError, false)
        await m.runNow("garden")
        XCTAssertEqual(m.notices["garden"]?.text, "Running now, and resumed. Results in a minute.")
        XCTAssertEqual(m.job("garden")?.isPaused, false)
        await m.runNow("inbox")
        XCTAssertEqual(m.notices["inbox"]?.text, "Running now. Results in a minute.")
    }

    func testEditSkipsUnchangedAndSaves() async {
        let (m, backend) = model()
        await m.reload()
        await m.load("inbox")
        let before = backend.detailsByID["inbox"]
        let unchanged = await m.save("inbox", schedule: "0 8 * * 1-5", prompt: before?.prompt)
        XCTAssertTrue(unchanged)
        XCTAssertNil(m.notices["inbox"], "nothing changed, nothing sent")
        let saved = await m.save("inbox", schedule: "weekdays at 9", prompt: "Summarize my inbox, briefly.")
        XCTAssertTrue(saved)
        XCTAssertEqual(m.details["inbox"]?.prompt, "Summarize my inbox, briefly.")
        XCTAssertEqual(m.job("inbox")?.scheduleLabel, "Weekdays at 9:00 AM")
        XCTAssertEqual(m.notices["inbox"]?.text, "Saved.")
    }

    func testDeleteForgetsAndClosesThePage() async {
        let (m, _) = model()
        await m.reload()
        m.selected = "reading"
        let ok = await m.delete("reading")
        XCTAssertTrue(ok)
        XCTAssertNil(m.job("reading"))
        XCTAssertNil(m.selected)
    }

    func testDraftPreviewThenConfirm() async {
        let (m, _) = model()
        await m.reload()
        let count = m.jobs.count
        XCTAssertFalse(m.draft.canPreview)
        m.startDraft("every weekday at 8 summarize my inbox")
        XCTAssertTrue(m.draft.canPreview)
        await m.previewDraft()
        let card = try? XCTUnwrap(m.draft.preview)
        XCTAssertEqual(card?.scheduleLabel, "Weekdays at 8:00 AM")
        XCTAssertEqual(card?.prompt, "Summarize my inbox")
        XCTAssertTrue(m.draft.showsCard)
        XCTAssertEqual(m.jobs.count, count, "a preview saves nothing")
        m.draft.name = "Inbox"
        m.draft.deliver = "telegram"
        let id = await m.confirmDraft()
        XCTAssertNotNil(id)
        XCTAssertEqual(m.jobs.count, count + 1)
        XCTAssertEqual(m.job(id!)?.name, "Inbox")
        XCTAssertEqual(m.job(id!)?.deliveryLabel, "Telegram")
        XCTAssertEqual(m.draft, ScheduleDraft(), "the draft clears once saved")
        XCTAssertTrue(m.notices[id!]?.text.hasPrefix("Scheduled. Next: ") ?? false)
    }

    func testDraftRefusalShowsTheSentence() async {
        let (m, _) = model()
        m.startDraft("every day at 9")
        await m.previewDraft()
        XCTAssertEqual(m.draft.error, "What should it do? Add that after the time, like “… summarize my inbox”.")
        XCTAssertFalse(m.draft.showsCard)
        let none = await m.confirmDraft()
        XCTAssertNil(none, "nothing to confirm without a card")
        m.draft.edit()
        XCTAssertEqual(m.draft.phase, .writing)
    }

    func testFailuresAreSentences() async {
        final class Down: ScheduleBackend {
            func list() async throws -> ScheduleList { throw URLError(.notConnectedToInternet) }
            func detail(_ id: String) async throws -> ScheduleDetail { throw ScheduleProblem(status: 404, message: "I couldn't find that schedule.") }
            func preview(text: String) async throws -> SchedulePreview { throw ScheduleProblem(status: 400, message: "nope") }
            func preview(schedule: String, prompt: String) async throws -> SchedulePreview { throw ScheduleProblem(status: 400, message: "nope") }
            func create(_ preview: SchedulePreview, name: String?, deliver: String?) async throws -> ScheduleDetail { throw ScheduleProblem(status: 400, message: "nope") }
            func pause(_ id: String) async throws -> ScheduleDetail { throw ScheduleProblem(status: 409, message: "It's running right now.") }
            func resume(_ id: String) async throws -> ScheduleDetail { throw ScheduleProblem(status: 409, message: "x") }
            func runNow(_ id: String) async throws -> ScheduleDetail { throw ScheduleProblem(status: 409, message: "It's running right now.") }
            func update(_ id: String, schedule: String?, prompt: String?, name: String?) async throws -> ScheduleDetail { throw ScheduleProblem(status: 400, message: "x") }
            func delete(_ id: String) async throws { throw ScheduleProblem(status: 404, message: "gone") }
        }
        let m = SchedulesModel(backend: Down())
        await m.reload()
        XCTAssertEqual(m.error, "Couldn't reach your Hermes. Check the connection and try again.")
        await m.runNow("x")
        XCTAssertEqual(m.notices["x"], .init("It's running right now.", isError: true))
        XCTAssertTrue(m.busy.isEmpty)
        await m.load("x")
        XCTAssertEqual(m.detailErrors["x"], "I couldn't find that schedule.")
    }
}
