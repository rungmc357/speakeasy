import Foundation

/// Where opening a task lands. Every way into a task (a board tile, the task list, a notification,
/// a plugin-update job) opens its visual view: the session page, with the task card's live line and
/// "So far" recap, its tool steps, helpers and approvals. The old text job page (a "Now" line and a
/// plan) is only the fallback for what a session page can't show yet: a server without session
/// pages, a task with no session yet, email drafts to review and send, and pictures to look at.
public enum VisualTaskRoute {
    /// The session to open for this job, or nil to keep the panel's own job page.
    public static func sessionID(for job: Job, historySupported: Bool) -> String? {
        guard historySupported else { return nil }
        if job.stage == .needsYou(.review) || job.lookTask != nil { return nil }
        if job.tasks.contains(where: { !$0.info.pendingDrafts.isEmpty }) { return nil }
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
        guard let task = tasks.first(where: { $0.id == taskID }), task.info.pendingDrafts.isEmpty,
              task.info.reviewImages.isEmpty || task.info.reviewIsIncidental else { return nil }
        return task.info.sessionID
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
