import Foundation

/// Where opening a task lands. Every way into a task (a board tile, the task list, a notification,
/// a plugin-update job) opens its visual view: the session page, with the task card's live line and
/// "So far" recap, its tool steps, helpers and approvals. The old text job page (a "Now" line and a
/// plan) is only the fallback for what a session page can't show: a server without session pages
/// and a task with no session yet. Email drafts to send and pictures to look at are reviewed on the
/// session page too (`SessionReviewCards`).
public enum VisualTaskRoute {
    /// The session to open for this job, or nil to keep the panel's own job page.
    public static func sessionID(for job: Job, historySupported: Bool) -> String? {
        guard historySupported else { return nil }
        // The newest task's session: a follow-up may have moved the job into a newer one.
        return job.tasks.lazy.reversed().compactMap(\.info.sessionID).first
    }

    /// The session to open for one task (by id), via the job it belongs to when there is one.
    public static func sessionID(forTask taskID: String, in jobs: [Job], tasks: [TaskItem],
                                 historySupported: Bool) -> String? {
        guard historySupported else { return nil }
        if let job = jobs.first(where: { $0.tasks.contains { $0.id == taskID } }) {
            guard sessionID(for: job, historySupported: true) != nil else { return nil }
            // This task's own session first (it may be a linked task of a chat), else the job's.
            return job.tasks.first { $0.id == taskID }?.info.sessionID ?? sessionID(for: job, historySupported: true)
        }
        return tasks.first { $0.id == taskID }?.info.sessionID
    }
}

/// Which of the app's tasks a session page shows review cards for (email drafts to send, pictures
/// to look at): the tasks that ran in that session, and the tasks the page lists (a chat continued
/// from Discord or Telegram lists its linked tasks, which run in sessions of their own).
public enum SessionReviewCards {
    public static func taskIDs(sessionID: String, runIDs: Set<String>, tasks: [TaskItem]) -> Set<String> {
        Set(tasks.filter { t in
            t.info.sessionID == sessionID || (t.info.runID.map(runIDs.contains) ?? false)
        }.map(\.id))
    }

    /// The page's drafts, from the app's pinned drafts: one state with the panel, so sending one
    /// here clears it there.
    public static func drafts(_ pinned: [EmailDraft], taskIDs: Set<String>, tasks: [TaskItem]) -> [EmailDraft] {
        let mine = Set(tasks.filter { taskIDs.contains($0.id) }.flatMap(\.info.emailDrafts).map(\.draftID))
        return pinned.filter { mine.contains($0.draftID) }
    }

    /// The page's picture reviews (oldest first), from the app's pinned reviews.
    public static func reviews(_ pinned: [ImageReview], taskIDs: Set<String>) -> [ImageReview] {
        pinned.filter { taskIDs.contains($0.taskID) }
    }
}

/// A running task's two live lines on its session-page card: what it is doing now (the plugin's
/// `now_line`, built from its real steps; older plugins: its short status or last activity), and
/// what it has done so far (`so_far`, hidden when it only repeats the live line).
public enum TaskLiveLine {
    public static func now(_ info: WorkInfo) -> String? {
        for line in [info.nowLine, info.shortStatus, info.activity.last?.text] {
            if let t = line?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty { return t }
        }
        return nil
    }

    public static func soFar(_ info: WorkInfo) -> String? {
        guard let t = info.soFar?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty, t != now(info) else { return nil }
        return t
    }
}
