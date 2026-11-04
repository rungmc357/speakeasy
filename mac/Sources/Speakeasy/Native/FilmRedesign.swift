import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// `SPEAKEASY_FILM_SET=redesign`: every state of the redesigned job page and board, in a narrow
// panel and a wide one. Invented, work-first data only.

extension FilmFrames {
    /// Copies the live fields (decoded the real way, from JSON) onto a scripted task.
    static func withLive(_ t: TaskItem, _ json: [String: Any]) -> TaskItem {
        var t = t
        var base: [String: Any] = ["run_id": t.info.runID ?? "run", "status": t.info.status]
        base.merge(json) { $1 }
        guard let decoded = WorkInfo(json: base) else { return t }
        t.info.activity = decoded.activity; t.info.findings = decoded.findings
        t.info.touched = decoded.touched; t.info.hero = decoded.hero
        return t
    }

    /// One tool call, `ago` seconds before now, `took` seconds long (nil: still running).
    static func call(_ id: String, _ kind: String, _ text: String, ago: Double, took: Double?, now: Date,
                     tool: String? = nil, args: String? = nil, result: String? = nil, detail: String? = nil,
                     failed: Bool = false) -> [String: Any] {
        var o: [String: Any] = ["id": id, "kind": kind, "text": text, "at": (now - ago).timeIntervalSince1970,
                                "started_at": (now - ago).timeIntervalSince1970,
                                "status": took == nil ? "running" : failed ? "failed" : "done"]
        if let took { o["ended_at"] = (now - ago + took).timeIntervalSince1970 }
        if let tool { o["tool"] = tool }
        if let args { o["args"] = args }
        if let result { o["result"] = result }
        if let detail { o["detail"] = detail }
        return o
    }

    static func redesignBeats() -> [Shot] {
        let now = Date()
        func base(call: Bool = true) -> VoiceState {
            var s = VoiceState(); s.now = now
            if call { s.connection = .live; s.interactionID = "vi_redesign" } else { s.workOnly = true }
            return s
        }
        func done(_ s: VoiceState) -> VoiceState { var st = s; st.work = deriveWork(st, now: now); return st }

        // A flaky test, rerun 50 times: trials hero, findings, a long tool log, touched.
        var flaky = task("t", "working", "The checkout test keeps failing on CI. Find out why and fix it.",
                         title: "Fix the flaky checkout test", short: "Rerunning the checkout test 50 times",
                         age: 6, now: now, milestones: ["Found the race in the payment mock", "Fixed the wait"])
        flaky.info.startedAt = now - 248
        let flakyCalls: [[String: Any]] = [
            call("a1", "search", "Searched the files for “checkout”", ago: 244, took: 1.8, now: now, tool: "search_files",
                 args: "checkout", result: "3 matches\ncheckout.spec.ts:12  test('checkout', async ({ page }) => {\ncheckout.tsx:4  export function Checkout() {\npayment-mock.ts:1  // payment mock for checkout"),
            call("a2", "read", "Read checkout.spec.ts", ago: 241, took: 0.6, now: now, tool: "read_file", args: "checkout.spec.ts",
                 result: "40  await page.goto('/checkout')\n41  await page.waitForTimeout(500)\n42  await page.click('#pay')\n43  await expect(page.getByText('Order placed')).toBeVisible()"),
            call("a3", "read", "Read playwright.config.ts", ago: 239, took: 0.4, now: now, tool: "read_file", args: "playwright.config.ts"),
            call("a4", "run", "Looked at the test's history", ago: 236, took: 1.1, now: now, tool: "terminal",
                 args: "git log --oneline -5 -- checkout.spec.ts", result: "9c1e2f4 Speed up checkout page\nb7a0d13 Add payment mock"),
            call("a5", "run", "Ran the test 12 times", ago: 232, took: 61, now: now, tool: "terminal",
                 args: "npx playwright test checkout.spec.ts --repeat-each=12",
                 result: "10 passed, 2 failed (1.2m)\n  checkout.spec.ts:43  Timed out waiting for “Order placed”", failed: true),
            call("a6", "think", "Thought it over", ago: 170, took: 3, now: now),
            call("a7", "read", "Read payment-mock.ts", ago: 166, took: 0.5, now: now, tool: "read_file", args: "payment-mock.ts"),
            call("a8", "search", "Searched the web for “playwright wait for mock server”", ago: 160, took: 2.6, now: now,
                 tool: "web_search", args: "playwright wait for mock server ready",
                 result: "5 results\n1. Fixtures | Playwright\n2. Waiting for a mock server to be ready"),
            call("a9", "browse", "Read playwright.dev/docs/test-fixtures", ago: 156, took: 3.9, now: now, tool: "web_extract",
                 args: "https://playwright.dev/docs/test-fixtures"),
            call("a10", "edit", "Edited payment-mock.ts", ago: 148, took: 0.7, now: now, tool: "patch", args: "payment-mock.ts",
                 result: "+  export const mockReady = new Promise(r => server.on('listening', r))"),
            call("a11", "edit", "Edited checkout.spec.ts", ago: 140, took: 0.6, now: now, tool: "patch", args: "checkout.spec.ts",
                 result: "-  await page.waitForTimeout(500)\n+  await mockReady"),
            call("a12", "run", "Ran the test 50 times", ago: 132, took: nil, now: now, tool: "terminal",
                 args: "npx playwright test checkout.spec.ts --repeat-each=50", detail: "31 of 50 done"),
        ]
        flaky = withLive(flaky, [
            "activity": flakyCalls,
            "findings": [["id": "f1", "text": "The test clicks Pay before the payment mock is listening: fails about 1 run in 6"],
                         ["id": "f2", "text": "A fixed 500 ms wait was standing in for “mock ready”"],
                         ["id": "f3", "text": "Waiting on the mock's ready signal instead: 31 passes so far, no failures"]],
            "touched": [["kind": "file", "label": "payment-mock.ts", "detail": "+9 −1"],
                        ["kind": "file", "label": "checkout.spec.ts", "detail": "+3 −2"],
                        ["kind": "url", "label": "playwright.dev", "url": "https://playwright.dev/docs/test-fixtures"],
                        ["kind": "command", "label": "npx playwright test"]],
            "hero": ["type": "trials", "total": 50, "passed": 31, "failed": 0],
        ])

        // Research: sources hero.
        var research = task("r", "working", "Compare our pricing with three competitors", title: "Pricing vs. three competitors",
                            short: "Reading Fabrikam's plans page", age: 4, now: now, milestones: ["Found the three closest competitors"])
        research.info.startedAt = now - 372
        research.info.plan = ["Pick the competitors", "Read their pricing pages", "Line up the plans", "Write it up"]
        research.info.stepIndex = 2; research.info.stepCount = 4
        research = withLive(research, [
            "activity": [
                call("b1", "search", "Searched the web for “project tool pricing per seat”", ago: 360, took: 2.1, now: now, tool: "web_search"),
                call("b2", "browse", "Read northwind.example/pricing", ago: 350, took: 4.2, now: now, tool: "web_extract",
                     args: "https://northwind.example/pricing", result: "Starter $8/seat · Team $14/seat · Business $22/seat"),
                call("b3", "browse", "Read contoso.example/plans", ago: 300, took: 5.0, now: now, tool: "web_extract",
                     args: "https://contoso.example/plans"),
                call("b4", "browse", "Read a review on g2.example", ago: 220, took: 3.4, now: now, tool: "web_extract"),
                call("b5", "browse", "Read fabrikam.example/pricing", ago: 18, took: nil, now: now, tool: "web_extract",
                     args: "https://fabrikam.example/pricing"),
            ],
            "findings": [["id": "g1", "text": "Northwind and Contoso both charge per seat; Fabrikam is flat per workspace"],
                         ["id": "g2", "text": "We're the only one without an annual discount"]],
            "touched": [["kind": "url", "label": "northwind.example", "url": "https://northwind.example/pricing"],
                        ["kind": "url", "label": "contoso.example", "url": "https://contoso.example/plans"],
                        ["kind": "url", "label": "g2.example", "url": "https://g2.example/compare"]],
            "hero": ["type": "sources", "items": [["title": "Pricing · Northwind", "site": "northwind.example"],
                                                  ["title": "Plans and pricing", "site": "contoso.example"],
                                                  ["title": "Northwind vs Contoso", "site": "g2.example"],
                                                  ["title": "Pricing · Fabrikam", "site": "fabrikam.example"]]],
        ])

        // A code change, done: diff hero on an answer.
        var dash = task("d", "completed", "The sales dashboard is slow. Fix it.", title: "Speed up the dashboard", short: "Done",
                        age: 900, now: now,
                        result: WorkResult(spoken: "The dashboard loads in 0.8 seconds now, down from 6. One query was scanning every order; it has an index now.",
                                           full: nil, label: "Dashboard 6 s → 0.8 s"),
                        milestones: ["Timed every query", "Added an index", "Checked the load time"])
        dash.info.startedAt = now - 900 - 11 * 60; dash.info.finishedAt = now - 900
        dash = withLive(dash, [
            "activity": [
                call("c1", "run", "Timed every query on the page", ago: 1500, took: 42, now: now, tool: "terminal", args: "npm run profile -- /sales"),
                call("c2", "read", "Read orders_repository.ts", ago: 1400, took: 0.5, now: now, tool: "read_file"),
                call("c3", "edit", "Added an index on orders.created_at", ago: 1200, took: 0.8, now: now, tool: "patch",
                     args: "migrations/0042_orders_created_at.sql"),
                call("c4", "run", "Checked the load time", ago: 960, took: 38, now: now, tool: "terminal",
                     args: "npm run profile -- /sales", result: "p50 0.8 s (was 6.1 s)"),
            ],
            "findings": [["id": "h1", "text": "One query scanned all 2.1M orders on every load"],
                         ["id": "h2", "text": "With an index on created_at the page loads in 0.8 s"]],
            "touched": [["kind": "file", "label": "0042_orders_created_at.sql", "detail": "+6"],
                        ["kind": "file", "label": "orders_repository.ts", "detail": "+4 −9"]],
            "hero": ["type": "diff", "files": 2, "added": 10, "removed": 9,
                     "preview": "+CREATE INDEX orders_created_at\n+  ON orders (created_at);\n-  .where('created_at', '>', since)\n+  .whereIndexed('created_at', since)\n   .limit(500)"],
        ])

        // A planned job, no hero: the step path.
        var onboard = task("o", "working", "Write the onboarding email sequence", title: "Onboarding email sequence",
                           short: "Drafting email 3 of 5", age: 3, now: now,
                           milestones: ["Read the current welcome email", "Listed what new users ask first"])
        onboard.info.startedAt = now - 9 * 60
        onboard.info.plan = ["Read the current welcome email", "List what new users ask first", "Draft the five emails", "Check tone and length"]
        onboard.info.stepIndex = 3; onboard.info.stepCount = 4
        onboard = withLive(onboard, [
            "activity": [
                call("e1", "read", "Read welcome-email.md", ago: 500, took: 0.4, now: now, tool: "read_file"),
                call("e2", "search", "Searched support tickets for “how do I”", ago: 420, took: 2.0, now: now, tool: "search_files"),
                call("e3", "edit", "Drafting email 3: inviting your team", ago: 40, took: nil, now: now, tool: "write_file",
                     args: "onboarding/03-invite-your-team.md"),
            ],
        ])

        // No hero, no plan: no picture; the page leads with the status line, findings and tool calls.
        var tickets = task("k", "working", "Summarize this week's support tickets", title: "This week's support tickets",
                           short: "Reading tickets", age: 2, now: now)
        tickets.info.startedAt = now - 3 * 60
        tickets = withLive(tickets, [
            "activity": [
                call("k1", "search", "Searched tickets from this week", ago: 170, took: 1.2, now: now, tool: "search_tickets"),
                call("k2", "read", "Read 40 tickets about billing", ago: 150, took: 6, now: now, tool: "read_tickets"),
                call("k3", "read", "Read 22 tickets about login", ago: 110, took: 4, now: now, tool: "read_tickets"),
                call("k4", "think", "Grouped them by cause", ago: 70, took: 5, now: now),
                call("k5", "read", "Read 15 tickets about exports", ago: 40, took: 3, now: now, tool: "read_tickets"),
                call("k6", "run", "Counted repeat issues", ago: 20, took: 1.4, now: now, tool: "terminal"),
                call("k7", "read", "Reading the rest", ago: 8, took: nil, now: now, tool: "read_tickets"),
            ],
            "findings": [["id": "k1", "text": "Half the billing tickets are the same card-declined message"]],
        ])

        // A decision, with its work so far.
        let pricingQ = card(["kind": "question", "question": "Which plan should the comparison put us against?",
                             "context": "Each competitor has a plan near ours, but the seat limits differ.",
                             "options": [["text": "Their Team plans", "detail": "Closest on price."],
                                         ["text": "Their Business plans", "detail": "Closest on features."]],
                             "recommended": 0])
        var ask = task("q", "working", "Write up the pricing comparison", title: "Pricing write-up",
                       short: "Needs your pick", age: 30, now: now, result: WorkResult(spoken: nil, full: nil, views: [pricingQ]),
                       milestones: ["Lined up the plans"])
        ask.info.updatedAt = now - 4; ask.info.startedAt = now - 7 * 60
        ask = withLive(ask, [
            "activity": [call("q1", "read", "Read the plan table", ago: 90, took: 0.5, now: now, tool: "read_file"),
                         call("q2", "message", "Asked which plans to compare", ago: 30, took: 0.1, now: now)],
            "findings": [["id": "q1", "text": "Seat limits differ: Team plans cap at 25, Business at 200"]],
        ])

        // A failure.
        var deploy = task("f", "failed", "Deploy the pricing page to staging", title: "Deploy pricing page", short: "Failed",
                          age: 400, now: now)
        deploy.info.startedAt = now - 520; deploy.info.finishedAt = now - 400
        deploy.info.failureReason = "The staging server refused the upload: its disk is full."
        deploy = withLive(deploy, [
            "activity": [call("x1", "run", "Built the site", ago: 510, took: 64, now: now, tool: "terminal", args: "npm run build"),
                         call("x2", "run", "Uploaded to staging", ago: 440, took: 38, now: now, tool: "terminal",
                              args: "rsync -az dist/ staging:/srv/site", result: "rsync: write failed: No space left on device (28)", failed: true)],
        ])

        // An answer (no live fields: an older plugin, the page as before).
        var weather = task("w", "completed", "Will it rain for Saturday's team offsite?", title: "Saturday offsite weather",
                           short: "Done", age: 120, now: now,
                           result: WorkResult(spoken: "Dry until about 4 pm on Saturday, then light rain. Plan the outdoor part for the morning.",
                                              full: nil))
        weather.info.startedAt = now - 160; weather.info.finishedAt = now - 120
        var notes = task("n", "completed", "Tidy up the release notes", title: "Release notes", short: "Done", age: 3000, now: now,
                         result: WorkResult(spoken: "Release notes are tidied.", full: nil, label: "Release notes tidied"))
        notes.info.finishedAt = now - 3000
        var invoice = task("i", "completed", "Send the March invoices", title: "March invoices", short: "Done", age: 5000, now: now,
                           result: WorkResult(spoken: "Sent.", full: nil, label: "12 invoices sent"))
        invoice.info.finishedAt = now - 5000

        var out: [Shot] = []
        let narrow: CGFloat = 380, wide: CGFloat = 700
        func both(_ name: String, _ s: VoiceState, _ open: String?, extra: @escaping @MainActor (VoicePanelModel) -> Void = { _ in }) {
            out.append(Shot("\(name)-narrow", done(s), open) { m in m.panelWidth = narrow; extra(m) })
            out.append(Shot("\(name)-wide", done(s), open) { m in m.panelWidth = wide; extra(m) })
        }

        var s = base(); s.tasks = [notes, invoice, weather, deploy, ask, research, flaky]
        both("redesign-01-home", s, nil) { m in m.extraHeight = 260 }
        s = base(); s.tasks = []
        out.append(Shot("redesign-02-empty-narrow", done(s), nil) { m in m.panelWidth = narrow })
        s = base(); s.tasks = [flaky]
        both("redesign-03-trials", s, "task:t") { m in m.extraHeight = 320 }
        both("redesign-04-tool-log-open", s, "task:t") { m in
            m.extraHeight = 320
            m.openSections.insert(ToolCallLog.listKey(job: "task:t"))
            m.openSections.insert(ToolCallLog.rowKey(job: "task:t", item: "a5"))
        }
        s = base(); s.tasks = [research]
        both("redesign-05-sources", s, "task:r") { m in m.extraHeight = 260 }
        s = base(); s.tasks = [dash]
        both("redesign-06-diff-done", s, "task:d") { m in m.extraHeight = 260 }
        s = base(); s.tasks = [onboard]
        both("redesign-07-steps", s, "task:o") { m in m.extraHeight = 160 }
        s = base(); s.tasks = [tickets]
        out.append(Shot("redesign-08-activity-narrow", done(s), "task:k") { m in m.panelWidth = narrow; m.extraHeight = 160 })
        s = base(); s.tasks = [ask]
        out.append(Shot("redesign-09-needs-you-narrow", done(s), "task:q") { m in m.panelWidth = narrow; m.extraHeight = 200 })
        s = base(); s.tasks = [deploy]
        out.append(Shot("redesign-10-failed-narrow", done(s), "task:f") { m in m.panelWidth = narrow; m.extraHeight = 120 })
        s = base(); s.tasks = [weather]
        out.append(Shot("redesign-11-answer-no-activity-narrow", done(s), "task:w") { m in m.panelWidth = narrow })
        s = base(call: false); s.tasks = [flaky]
        out.append(Shot("redesign-12-reply-typed-narrow", done(s), "task:t") { m in
            m.panelWidth = narrow; m.extraHeight = 220
            m.replyDrafts["task:t"] = "Also run it on Safari before you call it fixed."
        })
        return out
    }
}
