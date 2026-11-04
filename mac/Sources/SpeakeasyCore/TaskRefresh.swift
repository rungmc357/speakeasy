import Foundation

/// Polls that refresh running tasks one work row at a time (a paused call, the Live Activity
/// following jobs after a call) while another path (the after-call board poll, the event stream)
/// sends whole task lists. The board has one source of truth: the current list. A poll never
/// writes back the copy it started from; it applies only the rows it fetched, onto the list as it
/// is now, and a row never goes back to an older state.
public enum TaskRefresh {
    /// `fresh`: task id → the work row just fetched for it.
    public static func merge(_ current: [TaskItem], fresh: [String: WorkInfo]) -> [TaskItem] {
        guard !fresh.isEmpty else { return current }
        return current.map { task in
            guard let info = fresh[task.id], keeps(task.info, over: info) == false else { return task }
            var next = task
            next.info = info
            return next
        }
    }

    /// True when `old` should stay: it already settled and the fetched row would reopen it, or the
    /// fetched row is older than what the list has.
    static func keeps(_ old: WorkInfo, over new: WorkInfo) -> Bool {
        let oldSettled = old.isTerminal || old.status == "rejected"
        let newSettled = new.isTerminal || new.status == "rejected"
        if oldSettled && !newSettled { return true }
        if let a = old.updatedAt ?? old.updated, let b = new.updatedAt ?? new.updated, b < a { return true }
        return false
    }
}
