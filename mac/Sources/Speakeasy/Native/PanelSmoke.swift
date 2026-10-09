import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// `--panel-smoke [--snapshot-dir DIR]`: offline, in-process event-level checks
/// of the native panel. Real NSEvents (scroll wheel, mouse down/drag/up, click)
/// are dispatched through `NSWindow.sendEvent` — the same entry point
/// WindowServer events use — and outcomes are read back from the live AppKit
/// view tree and window frame. No network, microphone, Accessibility or
/// Screen Recording permission is needed.
@MainActor
enum PanelSmoke {
    private static var log: [String] = []
    private static var ok = true
    private static var retained: NativeVoiceClient?

    static func run(config: AppConfig, snapshotDir: String?) {
        let client = NativeVoiceClient(config: config)
        retained = client
        Task { @MainActor in
            await workScroll(client)
            await workStatusWraps(client)
            await transcriptScroll(client)
            await drag(client)
            await endedPanel(client)
            if let dir = snapshotDir { await snapshots(client, dir: dir) }
            print("panel-smoke " + (ok ? "ok" : "FAILED"))
            log.forEach { print("  " + $0) }
            exit(ok ? 0 : 1)
        }
    }

    // MARK: Fixtures

    static let longStatus = "Cross-checking United's live status page against FlightAware and the inbound aircraft before texting a friend"

    static func workState(milestones: Int = 22) -> VoiceState {
        var (s, _) = PreviewFixtures.state("working")!
        let now = s.now
        s.workInfo?.shortStatus = longStatus
        for i in 0..<milestones {
            s.workInfo?.events.append(WorkEventItem(kind: "milestone",
                text: "Milestone \(i + 1): checked another source for the UA 123 departure", at: now - Double(60 - i)))
        }
        if milestones > 0 {
            s.workInfo?.result = WorkResult(spoken: nil, full: String(repeating: "United 123 departs Newark at 9:05 AM from gate C71 and is on time. ", count: 12))
        }
        s.work = deriveWork(s, now: now)
        return s
    }

    // MARK: 1. Work view scrolls (and survives live ticks)

    static func workScroll(_ c: NativeVoiceClient) async {
        c.showPreview(workState(), workExpanded: true)
        await settle()
        guard let sv = scrollViews(c).first else { fail("work: no NSScrollView in the panel"); return }
        let doc = sv.documentView?.frame.height ?? 0
        record("work: scrollViews=\(scrollViews(c).count) viewport=\(Int(sv.contentView.bounds.height)) document=\(Int(doc)) panel=\(Int(c.panel.window.frame.height))")
        check(doc > sv.contentView.bounds.height + 40, "work: content is taller than the viewport")
        let before = sv.contentView.bounds.origin.y
        await scroll(c, over: sv, dy: -60, times: 5)
        let after = sv.contentView.bounds.origin.y
        record("work: wheel down clipOrigin.y \(Int(before)) -> \(Int(after))")
        check(after > before + 50, "work: wheel scrolls the Work view")
        // A live call re-publishes state every 0.5 s (tick) and on each work update.
        for i in 0..<4 {
            var s = c.model.state
            s.now += 0.5
            if i == 2 { s.workInfo?.events.append(WorkEventItem(kind: "milestone", text: "New milestone mid-read", at: s.now)) }
            c.model.state = s
            c.panel.setNeedsResize()
            await settle(0.2)
        }
        let kept = sv.contentView.bounds.origin.y
        record("work: after 4 live updates clipOrigin.y=\(Int(kept))")
        check(abs(kept - after) < 2, "work: scroll position survives live updates")
    }

    // MARK: 2. Work "Now" status is not truncated

    static func workStatusWraps(_ c: NativeVoiceClient) async {
        c.showPreview(workState(milestones: 0), workExpanded: true)
        await settle()
        let width = Tokens.width - 28
        func height(_ view: some View) -> CGFloat {
            NSHostingView(rootView: view.frame(width: width, alignment: .leading)).fittingSize.height
        }
        let one = height(StatusText(text: "Checking", tone: .plain, clickable: false))
        let now = height(WorkDetailView.nowStatus(longStatus, tone: .glimmer))
        let header = height(StatusText(text: longStatus, tone: .glimmer, clickable: false, maxLines: 2))
        record("status: oneLine=\(Int(one))pt workNow=\(Int(now))pt header=\(Int(header))pt (\(longStatus.count) chars, \(Int(width))pt wide)")
        check(now >= one * 2 - 1, "status: Work 'Now' status wraps to show the full text")
        check(header > one + 1 && header <= one * 2 + 1, "status: header status wraps to at most two lines")
        // Rendered through the live panel: the Work document grows by the extra lines.
        let docHeight = { scrollViews(c).first?.documentView?.frame.height ?? 0 }
        let withLong = docHeight()
        var s = c.model.state; s.workInfo?.shortStatus = "Checking"; s.work = deriveWork(s, now: s.now)
        c.model.state = s; c.model.shownStatus = "Checking"; c.panel.setNeedsResize()
        await settle(0.6)
        let withShort = docHeight()
        record("status: live Work document height long=\(Int(withLong)) short=\(Int(withShort))")
        check(withLong - withShort >= one - 1, "status: live Work panel lays out the extra status lines")
    }

    // MARK: 3. Transcript scrolls, holds while reading, follows at bottom

    static func transcriptScroll(_ c: NativeVoiceClient) async {
        var (s, _) = PreviewFixtures.state("speaking")!
        s.exchange = Exchange(you: "Walk me through everything on the calendar this week, with the details.",
                              assistant: String(repeating: "Tuesday you have the design review at two, then a call with Dana at four thirty. ", count: 14),
                              replyStarted: true)
        c.showPreview(s, workExpanded: false, captionExpanded: true)
        await settle()
        guard let sv = scrollViews(c).first else { fail("transcript: no NSScrollView in the panel"); return }
        let maxY = { (sv.documentView?.frame.height ?? 0) - sv.contentView.bounds.height }
        let start = sv.contentView.bounds.origin.y
        record("transcript: viewport=\(Int(sv.contentView.bounds.height)) document=\(Int(sv.documentView?.frame.height ?? 0)) clipOrigin.y=\(Int(start)) max=\(Int(maxY()))")
        check(abs(start - maxY()) < 4 && maxY() > 20, "transcript: opens showing the latest words")
        await scroll(c, over: sv, dy: 40, times: 4)   // wheel up = earlier text
        let up = sv.contentView.bounds.origin.y
        record("transcript: wheel up clipOrigin.y \(Int(start)) -> \(Int(up))")
        check(up < start - 50, "transcript: wheel scrolls back through the transcript")
        for i in 0..<5 {
            var st = c.model.state; st.exchange.assistant += "More words \(i) arriving live. "; st.now += 0.5
            c.model.state = st; c.panel.setNeedsResize()
            await settle(0.2)
        }
        let held = sv.contentView.bounds.origin.y
        record("transcript: after 5 live deltas clipOrigin.y=\(Int(held))")
        check(abs(held - up) < 4, "transcript: reading position holds while the assistant keeps talking")
        await scroll(c, over: sv, dy: -400, times: 4)
        for i in 0..<3 {
            var st = c.model.state; st.exchange.assistant += "Tail \(i) with enough words to wrap onto a brand new caption line. "
            c.model.state = st; c.panel.setNeedsResize()
            await settle(0.25)
        }
        let follow = sv.contentView.bounds.origin.y
        record("transcript: back at bottom, after 3 deltas clipOrigin.y=\(Int(follow)) max=\(Int(maxY()))")
        check(abs(follow - maxY()) < 4, "transcript: follows new words when already at the bottom")
    }

    // MARK: 4. Panel is draggable and stays where the user puts it

    static func drag(_ c: NativeVoiceClient) async {
        var (s, _) = PreviewFixtures.state("speaking")!
        s.exchange = Exchange(you: "Short.", assistant: "Short reply.", replyStarted: true)
        c.panel.resetPlacement()
        c.showPreview(s, workExpanded: false)
        await settle()
        let panel = c.panel.window
        let home = panel.frame
        record("drag: default frame \(fmt(home))")
        let h = panel.frame.height
        // First drag while the panel is NOT key (the user's first touch on a
        // non-activating panel), then the rest once it is.
        record("drag: panel key before first drag=\(panel.isKeyWindow)")
        for (label, point) in [("title", NSPoint(x: 70, y: h - 22)),
                               ("status", NSPoint(x: 70, y: h - 42)),
                               ("header gap", NSPoint(x: 150, y: h - 8)),
                               ("caption", NSPoint(x: 150, y: 20))] {
            // Compare the top-left corner: the panel is top-anchored once placed.
            let origin = NSPoint(x: panel.frame.minX, y: panel.frame.maxY)
            await mouseDrag(panel, from: point, by: NSSize(width: -60, height: 40))
            let now = NSPoint(x: panel.frame.minX, y: panel.frame.maxY)
            let dx = now.x - origin.x, dy = now.y - origin.y
            record("drag(\(label)): topLeft \(fmt(origin)) -> \(fmt(now)) delta=(\(Int(dx)),\(Int(dy))) frame=\(fmt(panel.frame))")
            check(abs(dx + 60) < 2 && abs(dy - 40) < 2, "drag(\(label)): panel follows the pointer")
        }
        let placed = panel.frame
        // Live updates resize the panel; it must stay where the user put it.
        var st = c.model.state
        st.exchange = Exchange(you: "A longer question now.", assistant: String(repeating: "A reply long enough to grow the panel. ", count: 8), replyStarted: true)
        c.model.state = st; c.model.captionExpanded = true; c.panel.setNeedsResize()
        await settle(0.7)
        check(panel.frame.height > placed.height + 30, "drag: panel really grew (\(Int(placed.height)) -> \(Int(panel.frame.height)))")
        record("drag: after grow \(fmt(placed)) -> \(fmt(panel.frame))")
        check(abs(panel.frame.minX - placed.minX) < 1 && abs(panel.frame.maxY - placed.maxY) < 1,
              "drag: growing keeps the user's position (top-left anchored)")
        check(abs(panel.frame.origin.x - home.origin.x) > 50, "drag: panel no longer snaps back to bottom-center")
        c.panel.hide(); c.panel.show(); await settle(0.4)
        check(abs(panel.frame.minX - placed.minX) < 1, "drag: hide/show keeps the user's position")
        // Clicks on controls still act and do not move the panel.
        record("drag: panel key before clicks=\(panel.isKeyWindow)")
        var ended = false
        let previous = c.model.onEnd
        c.model.onEnd = { ended = true }
        let before = panel.frame.origin
        let endPoint = NSPoint(x: panel.frame.width - 64, y: panel.frame.height - 32)   // End (30pt circle) sits left of the close (x) button
        await click(panel, at: endPoint)
        // Harness limit (same on the unfixed base): the first synthetic click
        // after a *different* interaction on this inactive, non-key panel is
        // consumed by SwiftUI's gesture reset; a real pointer is unaffected.
        if !ended { record("drag: first synthetic End click absorbed (harness limit); clicking again"); await click(panel, at: endPoint) }
        c.model.onEnd = previous
        record("drag: End click fired=\(ended) moved=\(before != panel.frame.origin)")
        check(ended && before == panel.frame.origin, "drag: End button still clicks without moving the panel")
        c.model.captionExpanded = false; c.panel.setNeedsResize(); await settle(0.5)
        await click(panel, at: NSPoint(x: 150, y: 30))
        record("drag: caption tap expanded=\(c.model.captionExpanded)")
        check(c.model.captionExpanded, "drag: tapping the collapsed caption still expands it")
        // Dragging from the collapsed caption moves the panel and does NOT also expand it.
        c.model.captionExpanded = false; c.panel.setNeedsResize(); await settle(0.5)
        let o2 = panel.frame.origin
        await mouseDrag(panel, from: NSPoint(x: 150, y: 30), by: NSSize(width: 30, height: -20))
        record("drag: caption drag moved=(\(Int(panel.frame.minX - o2.x)),\(Int(panel.frame.minY - o2.y))) expanded=\(c.model.captionExpanded)")
        check(!c.model.captionExpanded, "drag: dragging the caption does not also tap it")
        // Presses inside the transcript scroll area select/scroll, not move.
        c.model.captionExpanded = true; c.panel.setNeedsResize(); await settle(0.6)
        if let tsv = scrollViews(c).first {
            let o3 = panel.frame.origin
            let p = tsv.convert(NSPoint(x: tsv.bounds.midX, y: tsv.bounds.midY), to: nil)
            await mouseDrag(panel, from: p, by: NSSize(width: 40, height: 0))
            check(panel.frame.origin == o3, "drag: dragging inside the transcript does not move the panel")
        }
        // Off-screen drags are clamped back onto the visible frame after resize.
        await mouseDrag(panel, from: NSPoint(x: 70, y: panel.frame.height - 22), by: NSSize(width: -6000, height: -6000))
        c.panel.setNeedsResize(); await settle(0.5)
        if let visible = (panel.screen ?? NSScreen.main)?.visibleFrame {
            record("drag: after off-screen drag \(fmt(panel.frame)) visible=\(fmt(visible))")
            check(visible.insetBy(dx: -1, dy: -1).contains(panel.frame), "drag: panel is kept on screen")
        }
        c.panel.resetPlacement(); c.panel.setNeedsResize(); await settle(0.5)
        check(abs(panel.frame.midX - home.midX) < 2 && abs(panel.frame.minY - home.minY) < 2, "drag: reset returns to the default spot")
        await resizeChecks(c)
    }

    /// Edge resize through real mouse events: right edge widens, bottom edge
    /// gives the transcript more room, top-left stays put, a band press never taps.
    static func resizeChecks(_ c: NativeVoiceClient) async {
        let panel = c.panel.window
        c.model.captionExpanded = true; c.panel.setNeedsResize(); await settle(0.6)
        // Move up from the bottom-of-screen default so growing down has room
        // (at the screen edge the on-screen clamp rightly pushes the panel up).
        await mouseDrag(panel, from: NSPoint(x: 70, y: panel.frame.height - 22), by: NSSize(width: 0, height: 500))
        await settle(0.4)
        let before = panel.frame
        let viewportBefore = scrollViews(c).first?.frame.height ?? 0
        var ended = false
        let previous = c.model.onEnd
        c.model.onEnd = { ended = true }
        // Right edge, mid-height: +120pt wider.
        await mouseDrag(panel, from: NSPoint(x: panel.frame.width - 3, y: panel.frame.height / 2), by: NSSize(width: 120, height: 0))
        await settle(0.4)
        record("resize: right edge \(fmt(before)) -> \(fmt(panel.frame)) width=\(c.model.panelWidth)")
        check(abs(panel.frame.width - (before.width + 120)) < 2, "resize: dragging the right edge widens the panel")
        check(abs(panel.frame.minX - before.minX) < 1 && abs(panel.frame.maxY - before.maxY) < 1,
              "resize: widening keeps the top-left corner")
        // Bottom edge: +100pt taller transcript.
        let mid = panel.frame
        await mouseDrag(panel, from: NSPoint(x: panel.frame.width / 2, y: 3), by: NSSize(width: 0, height: -100))
        await settle(0.5)
        let viewportAfter = scrollViews(c).first?.frame.height ?? 0
        record("resize: bottom edge \(fmt(mid)) -> \(fmt(panel.frame)) transcript viewport \(Int(viewportBefore)) -> \(Int(viewportAfter)) extra=\(c.model.extraHeight)")
        check(viewportAfter - viewportBefore > 90, "resize: dragging the bottom edge gives the transcript more room")
        check(abs(panel.frame.maxY - mid.maxY) < 1, "resize: growing taller keeps the top edge")
        check(!ended, "resize: pressing an edge never fires a button")
        c.model.onEnd = previous
        // Bottom-right corner, a few points inside the rounded edge: both ways at once.
        let cornerBefore = panel.frame
        await mouseDrag(panel, from: NSPoint(x: panel.frame.width - 12, y: 12), by: NSSize(width: 60, height: -50))
        await settle(0.5)
        record("resize: corner \(fmt(cornerBefore)) -> \(fmt(panel.frame))")
        check(panel.frame.width - cornerBefore.width > 50, "resize: corner drag widens the panel")
        check(panel.frame.height - cornerBefore.height > 40, "resize: corner drag makes it taller")
        check(abs(panel.frame.maxY - cornerBefore.maxY) < 1, "resize: corner drag keeps the top edge")
        check(!ended, "resize: pressing the corner never fires a button")
        // Cursor zones: a handle view sits on each edge and the corner.
        if let container = panel.contentView as? PanelContainerView {
            let b = container.bounds
            func kind(_ p: NSPoint) -> ResizeHandleView.Kind? { (container.hitTest(p) as? ResizeHandleView)?.kind }
            check(kind(NSPoint(x: b.width - 4, y: b.height / 2)) == .right, "cursor: right edge shows the left-right cursor")
            check(kind(NSPoint(x: b.width / 2, y: 3)) == .bottom, "cursor: bottom edge shows the up-down cursor")
            check(kind(NSPoint(x: b.width - 10, y: 10)) == .corner, "cursor: corner shows the diagonal cursor")
            check(kind(NSPoint(x: b.width / 2, y: b.height / 2)) == nil, "cursor: the middle of the panel has no resize cursor")
        } else {
            fail("cursor: panel content is not the resize container")
        }
        // Transcript grab bar: dragging it down gives the transcript more room.
        c.model.captionExpanded = false; c.model.extraHeight = 0; c.panel.setNeedsResize(); await settle(0.6)
        if let grip = gripViews(c).first {
            let point = grip.convert(NSPoint(x: grip.bounds.midX, y: grip.bounds.midY), to: nil)
            let before = scrollViews(c).first?.frame.height ?? 0
            await mouseDrag(panel, from: point, by: NSSize(width: 0, height: -80))
            await settle(0.6)
            let after = scrollViews(c).first?.frame.height ?? 0
            record("resize: transcript grip viewport \(Int(before)) -> \(Int(after)) expanded=\(c.model.captionExpanded)")
            check(c.model.captionExpanded, "transcript grip: dragging opens the transcript")
            check(after - before > 60, "transcript grip: dragging down makes the transcript taller")
            check(!ended, "transcript grip: dragging never fires a button")
        } else {
            fail("transcript grip: no grab bar under the transcript")
        }
        // Clamped to the minimum width.
        await mouseDrag(panel, from: NSPoint(x: panel.frame.width - 3, y: panel.frame.height / 2), by: NSSize(width: -2000, height: 0))
        await settle(0.4)
        check(abs(panel.frame.width - Tokens.minWidth) < 1, "resize: width never goes below the minimum")
        // Persisted, then reset.
        let saved = UserDefaults.standard.string(forKey: "Speakeasy.panelSize") ?? "nil"
        record("resize: saved size \(saved)")
        check(saved != "nil", "resize: size is remembered")
        c.panel.resetPlacement(); await settle(0.4)
        check(abs(panel.frame.width - Tokens.width) < 1 && c.model.extraHeight == 0, "resize: reset restores the default size")
    }

    /// A failed task names why in its row and explains the fix in its detail.
    static func failedTask(_ c: NativeVoiceClient, dir: String) async {
        guard let (state, _) = PreviewFixtures.state("failed") else { fail("failed: no fixture"); return }
        c.showPreview(state, workExpanded: true)
        await settle()
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/failed-tasklist.png")); record("snapshot \(dir)/failed-tasklist.png") }
        catch { fail("snapshot failed: \(error)") }
        c.model.selectedTaskID = "f1"
        await settle()
        check(c.model.selectedTask?.info.failure?.label == "Out of credits", "failed: the task carries its reason")
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/failed-detail.png")); record("snapshot \(dir)/failed-detail.png") }
        catch { fail("snapshot failed: \(error)") }
        c.model.selectedTaskID = nil
    }

    static func snapshots(_ c: NativeVoiceClient, dir: String) async {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        for (name, badge, listening) in [("menubar-glyph", false, false), ("menubar-glyph-badge", true, false),
                                         ("menubar-glyph-listening", false, true), ("menubar-glyph-badge-listening", true, true)] {
            let image = BrandGlyph.menuBarImage(badge: badge, listening: listening, height: 128)
            if let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
               let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
                record("snapshot \(dir)/\(name).png")
            }
        }
        c.showPreview(workState(milestones: 4), workExpanded: true)
        await settle()
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/work-long-status.png")); record("snapshot \(dir)/work-long-status.png") }
        catch { fail("snapshot failed: \(error)") }
        await longStrings(c, dir: dir)
        await roomListening(c, dir: dir)
        await slim(c, dir: dir)
        // Screens for the website: the task list, a card result, and an approval.
        for name in ["tasklist", "products", "approval", "detail", "home"] {
            guard let (state, _) = PreviewFixtures.state(name) else { continue }
            c.showPreview(state, workExpanded: name == "products" || name == "detail")
            await settle()
            do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/site-\(name).png")); record("snapshot \(dir)/site-\(name).png") }
            catch { fail("snapshot failed: \(error)") }
        }
        await failedTask(c, dir: dir)
        await designReview(c, dir: dir)
        await lookingAt(c, dir: dir)
        if let (draft, _) = PreviewFixtures.state("email-draft") {
            c.showPreview(draft, workExpanded: false)
            await settle()
            check(!c.model.pinnedDrafts.isEmpty, "email draft: pinned in the call panel")
            do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/email-draft.png")); record("snapshot \(dir)/email-draft.png") }
            catch { fail("snapshot failed: \(error)") }
            // "Show all": the body grows, but the card's Show less / Send row must stay inside the screen.
            if let first = c.model.pinnedDrafts.first {
                var long = draft
                let longBody = Array(repeating: "- Booked via Amex Travel, trip details, flights and hotels", count: 40).joined(separator: "\n")
                long.tasks = long.tasks.map { t in
                    var t = t; t.info.emailDrafts = t.info.emailDrafts.map { d in var d = d; d.body = longBody; return d }; return t }
                if var w = long.workInfo { w.emailDrafts = w.emailDrafts.map { d in var d = d; d.body = longBody; return d }; long.workInfo = w }
                c.showPreview(long, workExpanded: false)
                c.model.expandedDraftIDs.insert(first.id)
                await settle()
                let win = c.panel.window; let visible = (win.screen ?? NSScreen.main)?.visibleFrame ?? .zero
                record("email draft expanded: panel \(Int(win.frame.height))pt tall, screen \(Int(visible.height))pt, inside=\(visible.contains(win.frame))")
                check(visible.contains(win.frame), "email draft: expanded card stays on screen")
                do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/email-draft-expanded.png")) } catch { fail("snapshot failed: \(error)") }
            }
        }
    }

    // MARK: A finished design pops up for review, pages through, and dismisses

    static func designReview(_ c: NativeVoiceClient, dir: String) async {
        guard let (state, _) = PreviewFixtures.state("design-review") else { fail("design review: fixture missing"); return }
        let savedLoader = c.model.loadProductImage
        c.model.loadProductImage = { _, n in PreviewFixtures.sampleDesign(variant: n - 1) }
        defer { c.model.loadProductImage = savedLoader }
        c.showPreview(state, workExpanded: false)
        await settle(1.2)
        check(c.model.pinnedReviews.count == 1 && c.model.pinnedReviews.first?.images.count == 3,
              "design review: finished images pinned in the call panel without opening the task")
        let win = c.panel.window; let visible = (win.screen ?? NSScreen.main)?.visibleFrame ?? .zero
        record("design review: panel \(Int(win.frame.height))pt tall, screen \(Int(visible.height))pt")
        check(visible.contains(win.frame), "design review: card stays on screen")
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/design-review.png")); record("snapshot \(dir)/design-review.png") }
        catch { fail("snapshot failed: \(error)") }
        // Arrow keys step through the images: real key events delivered through the panel window.
        func arrow(_ code: UInt16) -> NSEvent? {
            NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                             windowNumber: win.windowNumber, context: nil, characters: "", charactersIgnoringModifiers: "",
                             isARepeat: false, keyCode: code)
        }
        c.model.reviewIndex["run_preview"] = 0
        if let right = arrow(124) { win.sendEvent(right) }
        await settle(0.3)
        check(c.model.reviewIndex["run_preview"] == 1, "design review: right arrow shows the next image")
        if let left = arrow(123) { win.sendEvent(left); win.sendEvent(left) }
        await settle(0.3)
        check(c.model.reviewIndex["run_preview"] == 2, "design review: left arrow goes back and wraps to the last image")
        if let down = arrow(125) { win.sendEvent(down) }
        await settle(0.2)
        check(c.model.reviewIndex["run_preview"] == 2, "design review: other keys leave the images alone")
        c.model.reviewIndex["run_preview"] = 1
        await settle(0.8)
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/design-review-next.png")); record("snapshot \(dir)/design-review-next.png") }
        catch { fail("snapshot failed: \(error)") }
        // Three waiting reviews (the cap) on a laptop-height screen: every card's buttons stay on screen.
        var many = state
        many.tasks = (1...4).map { i in
            var info = state.workInfo!
            info.runID = "run_design_\(i)"; info.title = "Design option \(i)"; info.reviewSettledAt = Date() - Double(10 - i)
            return TaskItem(id: "design_\(i)", info: info)
        }
        c.showPreview(many, workExpanded: false)
        await settle(1.2)
        check(c.model.pinnedReviews.map(\.runID) == ["run_design_2", "run_design_3", "run_design_4"],
              "design review: only the three most recent reviews are pinned")
        record("design review x3: panel \(Int(win.frame.height))pt tall, inside=\(visible.contains(win.frame))")
        check(visible.contains(win.frame), "design review: three cards stay on screen")
        check(win.frame.height < visible.height - 40, "design review: three cards fit without running off the screen")
        check(ImageReviewLayout.focused(c.model.pinnedReviews, picked: c.model.focusedReviewID) == "run_design_4",
              "design review: the newest review is open, older ones are compact rows")
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/design-review-three.png")); record("snapshot \(dir)/design-review-three.png") }
        catch { fail("snapshot failed: \(error)") }
        // Dismiss hides the card right away (the api is told outside preview mode).
        c.showPreview(state, workExpanded: false)
        await settle(0.5)
        c.model.onDismissReview("run_preview")
        await settle(0.6)
        check(c.model.pinnedReviews.isEmpty, "design review: Dismiss hides the card")
        check(c.model.state.tasks.first?.info.images.count == 3, "design review: images stay in the task after Dismiss")
    }

    // MARK: A running task shows what it's looking at (list thumbnail + detail)

    static func lookingAt(_ c: NativeVoiceClient, dir: String) async {
        guard let (state, _) = PreviewFixtures.state("looking") else { fail("looking: fixture missing"); return }
        let saved = c.model.loadLiveImage
        var asked: [String] = []
        c.model.loadLiveImage = { run in asked.append(run); return PreviewFixtures.sampleDesign(variant: 1) }
        defer { c.model.loadLiveImage = saved }
        c.showPreview(state, workExpanded: false)
        await settle(1.0)
        check(asked.contains("run_looking"), "looking: task list loads the live thumbnail for the running task")
        check(!asked.contains("run_other"), "looking: no thumbnail for a task with nothing to show")
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/looking-list.png")); record("snapshot \(dir)/looking-list.png") }
        catch { fail("snapshot failed: \(error)") }
        c.showPreview(state, workExpanded: true)
        c.model.selectedTaskID = "looking"
        await settle(1.0)
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/looking-detail.png")); record("snapshot \(dir)/looking-detail.png") }
        catch { fail("snapshot failed: \(error)") }
        // A new frame (seq moves) is fetched again.
        let before = asked.count
        var next = state
        next.tasks[1].info.liveImage?.seq = 4
        c.model.state = next; c.panel.setNeedsResize()
        await settle(0.8)
        check(asked.count > before, "looking: a new seq refetches the live image")
        // Spoken "show me": the server's show action opens that task's detail with its live image.
        c.showPreview(state, workExpanded: false)
        c.model.selectedTaskID = nil
        await settle(0.4)
        c.dispatch(.show(ShowRequest(taskID: "looking", runID: "run_looking", image: .live, seq: 1)))
        await settle(0.8)
        check(c.model.workExpanded && c.model.selectedTaskID == "looking", "show me: opens the task's live image")
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/show-me.png")); record("snapshot \(dir)/show-me.png") }
        catch { fail("snapshot failed: \(error)") }
        c.model.selectedTaskID = nil
    }

    // MARK: Long header and task strings stay readable

    static let longTaskStatus = "Comparing three hotel rates near the venue and checking which ones include breakfast"

    /// Renders the connecting header and a task list with long text, for a visual check.
    static func longStrings(_ c: NativeVoiceClient, dir: String) async {
        guard var (s, _) = PreviewFixtures.state("tasklist") else { fail("long strings: no tasklist fixture"); return }
        if !s.tasks.isEmpty { s.tasks[0].info.shortStatus = longTaskStatus + " before booking" }
        c.showPreview(s, workExpanded: false)
        c.model.shownStatus = "Opening microphone and warming up audio"
        await settle()
        let panelHeight = c.panel.window.frame.height
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/long-strings.png")); record("snapshot \(dir)/long-strings.png (panel \(Int(panelHeight))pt)") }
        catch { fail("snapshot failed: \(error)") }
        guard var (connecting, _) = PreviewFixtures.state("listening") else { return }
        connecting.connection = .connecting
        connecting.interactionID = nil
        c.showPreview(connecting, workExpanded: false)
        c.model.shownStatus = "Opening microphone and warming up audio"
        await settle()
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/long-connecting.png")); record("snapshot \(dir)/long-connecting.png") }
        catch { fail("snapshot failed: \(error)") }
        // Listening while connecting: the words already heard show under "Listening".
        var early = connecting
        early.exchange = Exchange()
        early.transcript = []
        early.earlyListening = true
        early.earlyHeard = "Turn on the bedroom lamps"
        c.showPreview(early, workExpanded: false)
        c.model.shownStatus = present(early).secondary
        await settle()
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/early-listening.png")); record("snapshot \(dir)/early-listening.png") }
        catch { fail("snapshot failed: \(error)") }
        guard let (listening, _) = PreviewFixtures.state("listening") else { return }
        c.showPreview(listening, workExpanded: false)
        c.model.tourActive = true
        await settle()
        check(c.panel.window.frame.height > 0, "tour strip renders")
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/tour.png")); record("snapshot \(dir)/tour.png") }
        catch { fail("snapshot failed: \(error)") }
        c.model.tourActive = false
        // The orb at rest and at full voice (the level meter is off in previews, so set it).
        guard var (speaking, _) = PreviewFixtures.state("tasklist") else { return }
        speaking.connection = .live
        c.showPreview(speaking, workExpanded: false)
        c.model.orbLevel = 0
        await settle()
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/orb-rest.png")); record("snapshot \(dir)/orb-rest.png") }
        catch { fail("snapshot failed: \(error)") }
        c.model.orbLevel = 1
        await settle()
        do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/orb-loud.png")); record("snapshot \(dir)/orb-loud.png") }
        catch { fail("snapshot failed: \(error)") }
        c.model.orbLevel = 0
    }

    // MARK: Listening mode (during a call): the ear button, the strip in each state, and notes

    static func roomListening(_ c: NativeVoiceClient, dir: String) async {
        guard let (live, _) = PreviewFixtures.state("listening"), let (paused, _) = PreviewFixtures.state("paused") else {
            fail("listening mode: fixtures missing"); return
        }
        func snap(_ name: String) {
            do { try c.panel.snapshot(to: URL(fileURLWithPath: dir + "/\(name).png")); record("snapshot \(dir)/\(name).png") }
            catch { fail("snapshot failed: \(error)") }
        }
        // No call: no ear button (listening mode exists only during a call).
        c.showPreview(VoiceState(), workExpanded: false)
        c.model.roomOffered = true
        await settle()
        snap("room-idle-no-button")
        // A live call: the ear button sits with the call's controls.
        c.showPreview(live, workExpanded: false)
        await settle()
        snap("room-offered-in-call")
        // Turned on: the call is paused and the strip shows what listening mode is doing.
        c.showPreview(paused, workExpanded: false)
        await settle()
        let pausedHeight = c.panel.window.frame.height
        let now = Date()
        let since = now.addingTimeInterval(-12 * 60 - 4)
        let states: [(String, RoomListeningState, Set<RoomWarning>, String?)] = [
            ("room-preparing", .preparing(.downloadingModel(fraction: 0.4)), [], nil),
            ("room-listening", .listening(since: since), [], nil),
            ("room-warnings", .listening(since: since), [.bluetoothInput, .speechRecognitionDenied], nil),
            ("room-not-hearing", .notHearing(since: since), [], nil),
            ("room-failed", .failed(.transcriberStopped), [], nil),
        ]
        for (name, state, warnings, notice) in states {
            c.model.room = presentRoom(state, now: now, heardWords: true, warnings: warnings)
            c.model.roomNotice = notice
            c.surface.setNeedsResize()
            await settle()
            if state.isOn { check(c.panel.window.frame.height > pausedHeight, "\(name): the listening strip adds height") }
            snap(name)
        }
        // The call didn't come back after listening was turned off: what was heard is kept to ask about.
        c.showPreview(VoiceState(), workExpanded: false)
        c.model.room = RoomPresentation(title: "Listening mode kept what it heard", detail: "The call didn't come back",
                                        hint: "Ask about it (starts a call) or discard it · kept for 30 minutes",
                                        tone: .warning, isOn: false)
        c.model.roomCanAsk = true
        c.model.roomNotice = nil
        c.surface.setNeedsResize()
        await settle()
        snap("room-held")
        c.model.roomCanAsk = false
        // The call ended while listening: a note says it stopped.
        c.model.room = nil
        c.model.roomNotice = "The call ended, so listening mode stopped · what it heard was discarded"
        c.surface.setNeedsResize()
        await settle()
        snap("room-notice")
        // Back in the live call the strip is gone.
        c.model.room = presentRoom(.listening(since: since), now: now, heardWords: true)
        c.model.roomNotice = nil
        c.showPreview(live, workExpanded: false)
        await settle()
        snap("room-hidden-in-call")
        c.model.room = nil
        c.model.roomOffered = false
        c.surface.setNeedsResize()
    }

    // MARK: Slim mode shrinks a busy panel to the controls, and comes back

    static func slim(_ c: NativeVoiceClient, dir: String) async {
        let saved = c.model.slim
        defer { c.model.slim = saved }
        guard let (s, _) = PreviewFixtures.state("tasklist") else { fail("slim: no tasklist fixture"); return }
        c.model.slim = false
        c.showPreview(s, workExpanded: false)
        await settle()
        let full = c.panel.window.frame.height
        try? c.panel.snapshot(to: URL(fileURLWithPath: dir + "/slim-off.png"))
        c.model.onToggleSlim()
        await settle()
        let slim = c.panel.window.frame.height
        try? c.panel.snapshot(to: URL(fileURLWithPath: dir + "/slim-on.png"))
        record("slim: panel height \(Int(full)) -> \(Int(slim))")
        check(slim < full - 40, "slim: panel shrinks to the controls")
        c.model.onToggleSlim()
        await settle()
        let back = c.panel.window.frame.height
        record("slim: back to \(Int(back))")
        check(abs(back - full) < 2, "slim: full panel comes back at the same size")
    }

    /// The post-call panel: close (x) hides it; the hotkey decision never leaves it stuck.
    static func endedPanel(_ c: NativeVoiceClient) async {
        guard let (ended, _) = PreviewFixtures.state("ended") else { fail("ended: fixture missing"); return }
        c.showPreview(ended, workExpanded: false)
        await settle()
        check(c.panelVisible, "ended: panel shows the finished state")
        check(hotkeyAction(connection: c.model.state.connection, panelVisible: c.panelVisible) == .hidePanel,
              "ended: hotkey while shown hides")
        c.model.onClosePanel()
        await settle(0.2)
        check(!c.panelVisible, "ended: close (x) hides the panel")
        check(hotkeyAction(connection: c.model.state.connection, panelVisible: c.panelVisible) == .startCall,
              "ended: hotkey while hidden starts a call")
    }

    // MARK: Event helpers

    static func gripViews(_ c: NativeVoiceClient) -> [TranscriptGripNSView] {
        func walk(_ v: NSView) -> [TranscriptGripNSView] { ((v as? TranscriptGripNSView).map { [$0] } ?? []) + v.subviews.flatMap(walk) }
        return c.panel.window.contentView.map(walk)?.filter { !$0.isHiddenOrHasHiddenAncestor && $0.frame.height > 0 } ?? []
    }

    static func scrollViews(_ c: NativeVoiceClient) -> [NSScrollView] {
        func walk(_ v: NSView) -> [NSScrollView] { ((v as? NSScrollView).map { [$0] } ?? []) + v.subviews.flatMap(walk) }
        return c.panel.window.contentView.map(walk)?.filter { !$0.isHiddenOrHasHiddenAncestor && $0.frame.height > 0 } ?? []
    }

    /// Mouse-wheel scroll (discrete, unphased) at the centre of `sv`,
    /// routed the way AppKit routes wheel events: hit-test the pointer in the
    /// window, then `scrollWheel(with:)` on the hit view, which walks the
    /// responder chain up to the enclosing scroll view. (`NSWindow.sendEvent`
    /// can't be used for wheels here: it resolves the target from the real
    /// cursor position, which a test must not move.)
    static func scroll(_ c: NativeVoiceClient, over sv: NSScrollView, dy: Int, times: Int) async {
        let window = c.panel.window
        let point = sv.convert(NSPoint(x: sv.bounds.midX, y: sv.bounds.midY), to: nil)
        guard let root = window.contentView, let hit = root.hitTest(root.superview?.convert(point, from: nil) ?? point) else {
            record("scroll: nothing hit at \(fmt(point))"); return
        }
        record("scroll: hit \(type(of: hit)) inside \(type(of: sv))")
        func wheel(_ delta: Int) -> NSEvent? {
            guard let seed = NSEvent.mouseEvent(with: .mouseMoved, location: point, modifierFlags: [],
                                                timestamp: ProcessInfo.processInfo.systemUptime,
                                                windowNumber: window.windowNumber, context: nil, eventNumber: 0,
                                                clickCount: 0, pressure: 0),
                  let cg = seed.cgEvent else { return nil }
            cg.type = .scrollWheel
            cg.setIntegerValueField(.scrollWheelEventIsContinuous, value: 0)
            cg.setIntegerValueField(.scrollWheelEventDeltaAxis1, value: Int64(delta / 10))
            cg.setIntegerValueField(.scrollWheelEventPointDeltaAxis1, value: Int64(delta))
            cg.setDoubleValueField(.scrollWheelEventFixedPtDeltaAxis1, value: Double(delta) / 10)
            return NSEvent(cgEvent: cg)
        }
        for _ in 0..<times {
            if let e = wheel(dy) { hit.scrollWheel(with: e) }
            await settle(0.03)
        }
        await settle(0.3)
    }

    /// Real mouse sequences carry a fresh event number per press; SwiftUI's
    /// button tracking keys off it, so reusing one number drops clicks.
    private static var eventNumber = 1000
    static func mouse(_ type: NSEvent.EventType, _ window: NSWindow, _ point: NSPoint) -> NSEvent {
        if type == .leftMouseDown { eventNumber += 1 }
        return NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                  windowNumber: window.windowNumber, context: nil, eventNumber: eventNumber, clickCount: 1,
                                  pressure: type == .leftMouseUp ? 0 : 1)!
    }

    /// Down at `point` (window coords), drag in steps by `delta` (screen space), up.
    /// Each event location is relative to the window's *current* frame, as
    /// WindowServer reports it while a window moves under the pointer.
    static func mouseDrag(_ window: NSWindow, from point: NSPoint, by delta: NSSize) async {
        let startScreen = window.convertPoint(toScreen: point)
        window.sendEvent(mouse(.leftMouseDown, window, point))
        await settle(0.03)
        let steps = 6
        for i in 1...steps {
            let f = CGFloat(i) / CGFloat(steps)
            let screen = NSPoint(x: startScreen.x + delta.width * f, y: startScreen.y + delta.height * f)
            window.sendEvent(mouse(.leftMouseDragged, window, window.convertPoint(fromScreen: screen)))
            await settle(0.02)
        }
        let end = NSPoint(x: startScreen.x + delta.width, y: startScreen.y + delta.height)
        window.sendEvent(mouse(.leftMouseUp, window, window.convertPoint(fromScreen: end)))
        await settle(0.3)
    }

    /// Hover, down, up, hover — each in its own runloop turn, through the
    /// window's `sendEvent` (so `VoicePanel.sendEvent`'s drag logic sees it).
    static func click(_ window: NSWindow, at point: NSPoint) async {
        for e in [hover(window, point), mouse(.leftMouseDown, window, point), mouse(.leftMouseUp, window, point), hover(window, point)] {
            window.sendEvent(e)
            await settle(0.05)
        }
        await settle(0.3)
    }

    static func hover(_ window: NSWindow, _ point: NSPoint) -> NSEvent {
        NSEvent.mouseEvent(with: .mouseMoved, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                           windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 0, pressure: 0)!
    }

    static func settle(_ seconds: Double = 0.8) async {
        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
    }

    static func fmt(_ p: NSPoint) -> String { "(\(Int(p.x)),\(Int(p.y)))" }
    static func fmt(_ r: NSRect) -> String { "(\(Int(r.minX)),\(Int(r.minY)) \(Int(r.width))x\(Int(r.height)))" }
    static func record(_ s: String) { log.append(s) }
    static func fail(_ s: String) { ok = false; log.append("FAIL " + s) }
    static func check(_ condition: Bool, _ label: String) {
        if condition { log.append("PASS " + label) } else { fail(label) }
    }
}
