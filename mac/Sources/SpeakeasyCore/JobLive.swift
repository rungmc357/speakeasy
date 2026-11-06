import Foundation

// The live job page's plain logic (Mac redesign): the job's mood, the tool-call log, what a
// running tile shows small, and the board's greeting. Everything is read from what the server
// sent; nothing is made up when a field is missing.

/// The job's state as a feeling: the header's glow and the eyebrow's word.
public enum LiveMood: String, Equatable, Sendable {
    case working, needsYou, done, failed, quiet

    public static func of(_ job: Job) -> LiveMood {
        switch job.stage {
        case .needsYou: return .needsYou
        case .failed: return .failed
        case .running: return .working
        case .done: return job.setAside || job.newest.info.status != "completed" ? .quiet : .done
        }
    }

    public var word: String {
        switch self {
        case .working: return "Working"
        case .needsYou: return "Needs you"
        case .done: return "Done"
        case .failed: return "Didn't finish"
        case .quiet: return "Stopped"
        }
    }

    /// Something is still happening (the dot breathes, the clock ticks).
    public var isLive: Bool { self == .working || self == .needsYou }
}

/// The job's tool calls: the latest pinned, all of them on request, each one opening to its raw
/// call when the server sent it.
public enum ToolCallLog {
    /// The call to pin: the newest one still running, else the newest.
    public static func pinned(_ items: [JobActivityItem]) -> JobActivityItem? {
        items.last { $0.status == .running } ?? items.last
    }

    /// How long a call took (or has taken so far): `started_at`→`ended_at` when sent; a running
    /// call counts from its start (or its `at`). nil when the server didn't say.
    public static func duration(_ item: JobActivityItem, now: Date) -> TimeInterval? {
        if let start = item.startedAt {
            if let end = item.endedAt { return end >= start ? end.timeIntervalSince(start) : nil }
            return item.status == .running && now >= start ? now.timeIntervalSince(start) : nil
        }
        if item.status == .running, let at = item.at, now >= at { return now.timeIntervalSince(at) }
        return nil
    }

    /// "0.4 s", "12 s", "2 m 05 s", "1 h 02 m".
    public static func durationText(_ seconds: TimeInterval) -> String {
        let s = max(0, seconds)
        if s < 10 { return String(format: "%.1f s", (s * 10).rounded(.down) / 10) }
        let t = Int(s)
        if t < 60 { return "\(t) s" }
        if t < 3600 { return String(format: "%d m %02d s", t / 60, t % 60) }
        return String(format: "%d h %02d m", t / 3600, (t % 3600) / 60)
    }

    /// The row opens to show more: the raw arguments, the result, or the detail line.
    public static func expandable(_ item: JobActivityItem) -> Bool {
        item.args != nil || item.result != nil || item.detail != nil
    }

    /// The small line under a call's words, folded: its detail (a file, a command), never the raw
    /// tool name ("terminal", "web_extract"). Opened, nothing (the detail moves into the opened part).
    public static func subline(_ item: JobActivityItem, open: Bool) -> String? {
        guard !open, let detail = item.detail, detail != item.tool else { return nil }
        return detail
    }

    /// The disclosure under the pinned call: "All 15 tool calls".
    public static func allLabel(_ count: Int) -> String {
        count == 1 ? "1 tool call" : "All \(count) tool calls"
    }

    /// A folded group of tool calls in plain words, the plugin's activity phrasing
    /// (activity.py `humanize`): "Searched the web, read 2 pages, ran code". Never a raw tool name.
    public static func plainSummary(_ items: [JobActivityItem], limit: Int = 3) -> String {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for item in items where item.isToolCall {
            let key = family(item.tool, kind: item.kind)
            if counts[key] == nil { order.append(key) }
            counts[key, default: 0] += 1
        }
        let phrases = order.prefix(limit).map { phrase($0, count: counts[$0] ?? 1) }
        guard var text = phrases.isEmpty ? nil : phrases.joined(separator: ", ") else { return "" }
        if order.count > limit { text += "…" }
        return text.prefix(1).uppercased() + text.dropFirst()
    }

    static func family(_ tool: String?, kind: JobActivityItem.Kind) -> String {
        let t = (tool ?? "").lowercased().replacingOccurrences(of: "mcp__", with: "")
        switch t {
        case "web_search": return "web"
        case "web_extract", "web_fetch", "fetch": return "page"
        case "read_file": return "readfile"
        case "write_file": return "write"
        case "patch", "edit_file", "apply_patch", "str_replace_editor": return "edit"
        case "search_files", "grep", "glob": return "searchfiles"
        case "terminal", "bash", "shell", "run_command", "exec_command": return "command"
        case "execute_code": return "code"
        case "delegate_task": return "helper"
        case "skill_view": return "notes"
        case "skill_manage": return "updatenotes"
        default:
            if t.hasPrefix("browser") { return "browser" }
            if !t.isEmpty { return "tool" }
            switch kind {
            case .search: return "searchfiles"
            case .read: return "readfile"
            case .edit: return "edit"
            case .run: return "command"
            case .browse: return "page"
            default: return "tool"
            }
        }
    }

    static func phrase(_ family: String, count n: Int) -> String {
        func counted(_ one: String, _ verb: String, _ noun: String) -> String { n == 1 ? one : "\(verb) \(n) \(noun)" }
        switch family {
        case "web": return "searched the web"
        case "page": return counted("read a page", "read", "pages")
        case "readfile": return counted("read a file", "read", "files")
        case "write": return counted("wrote a file", "wrote", "files")
        case "edit": return counted("edited a file", "edited", "files")
        case "searchfiles": return "searched the files"
        case "command": return counted("ran a command", "ran", "commands")
        case "code": return "ran code"
        case "helper": return counted("handed work to a helper", "handed work to", "helpers")
        case "notes": return "read its notes"
        case "updatenotes": return "updated its notes"
        case "browser": return "worked in the browser"
        default: return counted("used a tool", "used", "tools")
        }
    }

    /// The section's open-state key in `VoicePanelModel.openSections`.
    public static func listKey(job: String) -> String { "\(job)|toolcalls" }
    public static func rowKey(job: String, item: String) -> String { "\(job)|toolcall:\(item)" }
}

/// What a running job's board tile draws small: its hero in miniature, else the plan step it's
/// on, else the call it's on now (never a step count).
public enum JobGlance: Equatable, Sendable {
    case trials(passed: Int, failed: Int, total: Int)
    case sources(count: Int, sites: [String])
    case diff(files: Int, added: Int, removed: Int)
    case now(String)

    public static func of(_ info: WorkInfo) -> JobGlance? {
        switch JobHero.select(info) {
        case let .trials(total, passed, failed): return .trials(passed: passed, failed: failed, total: total)
        case let .sources(items): return .sources(count: items.count, sites: items.suffix(4).map(\.site))
        case let .diff(files, added, removed, _): return .diff(files: files, added: added, removed: removed)
        case .steps, .series, nil:
            if let s = JobProgress.currentStep(info) { return .now(s) }
            if let p = ToolCallLog.pinned(info.activity) { return .now(p.text) }
            return nil
        }
    }
}

/// The board's top line.
public enum BoardGreeting {
    public static func greeting(hour: Int) -> String {
        switch hour {
        case 5..<12: return "Good morning"
        case 12..<17: return "Good afternoon"
        case 17..<22: return "Good evening"
        default: return "Hello"
        }
    }

    /// "1 needs you · 1 new answer · 2 running".
    public static func summary(_ s: JobBoard.Sections) -> String {
        var parts: [String] = []
        if !s.needsYou.isEmpty { parts.append("\(s.needsYou.count) need\(s.needsYou.count == 1 ? "s" : "") you") }
        if !s.newAnswers.isEmpty { parts.append(s.newAnswers.count == 1 ? "1 new answer" : "\(s.newAnswers.count) new answers") }
        if !s.running.isEmpty { parts.append("\(s.running.count) running") }
        if s.needsYou.isEmpty && s.newAnswers.isEmpty && s.running.isEmpty && !s.doneToday.isEmpty {
            parts.append("\(s.doneToday.count) done today")
        }
        return parts.joined(separator: " · ")
    }

    /// What the empty board offers to try (said out loud or typed).
    public static let suggestions = [
        "Find out why the checkout test keeps failing",
        "Compare our pricing with three competitors",
        "Summarize this week's support tickets",
    ]
}
