import XCTest
@testable import SpeakeasyCore

final class SchedulesTests: XCTestCase {
    private func json(_ s: String) -> Any { try! JSONSerialization.jsonObject(with: Data(s.utf8)) }

    private var ny: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/New_York")!
        return c
    }
    private let us = Locale(identifier: "en_US")
    /// Thu Nov 5 2026, 3:30 PM New York.
    private var now: Date { ISO8601DateFormatter().date(from: "2026-11-05T15:30:00-05:00")! }
    private func at(_ s: String) -> Date { ISO8601DateFormatter().date(from: s)! }

    func testCapabilityFlag() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"schedules":true,"history":true}"#.utf8))
        XCTAssertTrue(on.schedules)
        let old = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"history":true}"#.utf8))
        XCTAssertFalse(old.schedules, "an older plugin hides Scheduled")
    }

    func testListDecodes() {
        let list = ScheduleList(json: json(#"""
        {"schedules": [
          {"id": "a1", "name": "Morning garden check", "schedule_label": "Weekdays at 8:00 AM", "kind": "cron",
           "next_run_at": "2026-11-06T08:00:00-05:00", "last_run_at": "2026-11-05T08:00:41.512345-05:00",
           "last_status": "ok", "last_result": "Light frost tonight.", "enabled": true, "state": "scheduled",
           "running": false, "delivery_label": "Telegram"},
          {"id": "b2", "name": "Reading list", "schedule_label": "Mondays at 9:00 AM", "next_run_at": null,
           "last_status": "failed", "last_result": "The feed timed out.", "enabled": false, "state": "paused",
           "delivery_label": "Discord · #books"},
          {"id": "c3", "name": "Build check", "schedule_label": "Every 2 hours", "kind": "interval",
           "next_run_at": "2026-11-05T16:00:00-05:00", "last_status": "running", "running": true, "enabled": true,
           "state": "scheduled"},
          {"name": "no id"}],
         "timezone": "America/New_York"}
        """#))
        XCTAssertEqual(list.schedules.count, 3)
        XCTAssertEqual(list.timezone, "America/New_York")
        let a = list.schedules[0]
        XCTAssertEqual(a.scheduleLabel, "Weekdays at 8:00 AM")
        XCTAssertEqual(a.nextRun, at("2026-11-06T08:00:00-05:00"))
        XCTAssertNotNil(a.lastRun, "microsecond ISO times decode")
        XCTAssertEqual(a.lastStatus, .ok)
        XCTAssertFalse(a.isDimmed)
        XCTAssertTrue(a.canRunNow)
        let b = list.schedules[1]
        XCTAssertTrue(b.isPaused && b.isDimmed)
        XCTAssertNil(b.nextRun)
        XCTAssertEqual(b.deliveryLabel, "Discord · #books")
        let c = list.schedules[2]
        XCTAssertTrue(c.isRunning)
        XCTAssertFalse(c.canRunNow, "no Run now while it runs")
    }

    func testSectionsOrderRunningThenSoonestThenPausedThenDone() {
        let jobs = [
            ScheduledJob(id: "late", name: "Late", scheduleLabel: "x", nextRun: now.addingTimeInterval(7200)),
            ScheduledJob(id: "p", name: "Paused", scheduleLabel: "x", enabled: false, state: .paused),
            ScheduledJob(id: "soon", name: "Soon", scheduleLabel: "x", nextRun: now.addingTimeInterval(600)),
            ScheduledJob(id: "done", name: "Once", scheduleLabel: "x", kind: "once", enabled: false, state: .completed),
            ScheduledJob(id: "run", name: "Running", scheduleLabel: "x", nextRun: now.addingTimeInterval(9000), state: .running),
        ]
        let sections = ScheduleList.sections(jobs)
        XCTAssertEqual(sections.map(\.0), [.upcoming, .paused, .done])
        XCTAssertEqual(sections[0].1.map(\.id), ["run", "soon", "late"])
        XCTAssertFalse(jobs[3].canRunNow, "a one-time job that ran can't run again")
    }

    func testDetailDecodesRunsWithSessions() {
        let d = ScheduleDetail(json: json(#"""
        {"id": "a1", "name": "Morning garden check", "schedule_label": "Weekdays at 8:00 AM", "state": "scheduled",
         "enabled": true, "prompt": "Check the forecast for frost.", "skills": ["weather"], "schedule_text": "0 8 * * 1-5",
         "queued": true, "resumed": false,
         "runs": [{"id": "r1", "started_at": "2026-11-05T08:00:02-05:00", "finished_at": "2026-11-05T08:00:41-05:00",
                   "status": "ok", "result": "Light frost tonight.", "session_id": "cron_a1_20261105_080002"},
                  {"id": "r2", "started_at": "2026-11-04T08:00:02-05:00", "status": "failed", "result": "",
                   "error": "Timed out.", "session_id": null}]}
        """#))
        XCTAssertNotNil(d)
        XCTAssertEqual(d?.prompt, "Check the forecast for frost.")
        XCTAssertEqual(d?.scheduleText, "0 8 * * 1-5")
        XCTAssertEqual(d?.runs.count, 2)
        XCTAssertEqual(d?.runs[0].sessionID, "cron_a1_20261105_080002")
        XCTAssertEqual(d?.runs[0].duration, 39)
        XCTAssertNil(d?.runs[1].sessionID)
        XCTAssertEqual(d?.runs[1].status, .failed)
        XCTAssertEqual(d?.queued, true)
        XCTAssertEqual(ScheduleDetail.path("a1"), "/voice/schedules/a1")
        XCTAssertEqual(ScheduleDetail.path("../x"), "/voice/schedules/%2E%2E%2Fx")
    }

    func testPreviewSummaryAndCreateBody() throws {
        let p = try XCTUnwrap(SchedulePreview(json: json(#"""
        {"schedule": "0 8 * * 1-5", "schedule_label": "Weekdays at 8:00 AM", "kind": "cron",
         "prompt": "Summarize my inbox", "name": "Summarize my inbox",
         "next_run_at": "2026-11-09T08:00:00-05:00",
         "next_runs": ["2026-11-09T08:00:00-05:00", "2026-11-10T08:00:00-05:00"], "assumed": null,
         "deliver": "local", "delivery_targets": [{"id": "local", "label": "Saved only"}, {"id": "telegram", "label": "Telegram"}]}
        """#)))
        XCTAssertEqual(p.summary(now: now, calendar: ny, locale: us), "Weekdays at 8:00 AM · next: Mon 8:00 AM")
        XCTAssertEqual(p.targets.map(\.id), ["local", "telegram"])
        let body = p.createBody(name: "  Inbox  ", deliver: "telegram")
        XCTAssertEqual(body["confirmed"] as? Bool, true)
        XCTAssertEqual(body["schedule"] as? String, "0 8 * * 1-5")
        XCTAssertEqual(body["name"] as? String, "Inbox")
        XCTAssertEqual(body["deliver"] as? String, "telegram")
        XCTAssertEqual(p.createBody(name: "   ")["name"] as? String, "Summarize my inbox")
        XCTAssertNil(SchedulePreview.requestBody(text: "   "))
        XCTAssertNil(SchedulePreview.requestBody(schedule: "weekdays", prompt: ""))
        XCTAssertEqual(SchedulePreview.requestBody(text: " every day at 9 water ")?["text"] as? String, "every day at 9 water")
    }

    func testRelativeTimes() {
        let c = ny
        XCTAssertEqual(ScheduleFormat.next(now.addingTimeInterval(30), now: now, calendar: c, locale: us), "Now")
        XCTAssertEqual(ScheduleFormat.next(now.addingTimeInterval(12 * 60 + 5), now: now, calendar: c, locale: us), "in 12 min")
        XCTAssertEqual(ScheduleFormat.next(at("2026-11-05T18:00:00-05:00"), now: now, calendar: c, locale: us), "Today 6:00 PM")
        XCTAssertEqual(ScheduleFormat.next(at("2026-11-06T08:00:00-05:00"), now: now, calendar: c, locale: us), "Tomorrow 8:00 AM")
        XCTAssertEqual(ScheduleFormat.next(at("2026-11-09T08:00:00-05:00"), now: now, calendar: c, locale: us), "Mon 8:00 AM")
        XCTAssertEqual(ScheduleFormat.next(at("2026-11-20T09:00:00-05:00"), now: now, calendar: c, locale: us), "Nov 20, 9:00 AM")
        XCTAssertEqual(ScheduleFormat.last(now.addingTimeInterval(-300), now: now, calendar: c, locale: us), "5 min ago")
        XCTAssertEqual(ScheduleFormat.last(at("2026-11-04T18:00:00-05:00"), now: now, calendar: c, locale: us), "Yesterday 6:00 PM")
        XCTAssertEqual(ScheduleFormat.duration(39), "39 s")
        XCTAssertEqual(ScheduleFormat.duration(185), "3 min")
        XCTAssertEqual(ScheduleFormat.duration(4200), "1 hr 10 min")
        XCTAssertEqual(ScheduleFormat.statusWord(nil), "Not run yet")
        XCTAssertEqual(ScheduleFormat.statusWord(.failed), "Didn't finish")
    }
}
