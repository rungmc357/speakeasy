import Combine
import Foundation
import SpeakeasyCore

/// The app's one store of decisions acted on here and not yet confirmed by the server (SPK-79): the
/// call panel / board (`VoicePanelModel`), the session pages (iPhone `HistoryStore`, the Mac History
/// window) and the Live Activity all read and write this, so a pick or ✕ made on one surface hides
/// the card on every other at once, and the server's `question_state` clears it (`reconcile`).
/// Persisted, so a relaunch mid-send neither resurrects a dismissed card nor forgets a pick.
@MainActor
public final class PendingPicks: ObservableObject {
    /// The app's store. Inside a unit-test run every caller gets its own unsaved store, so one
    /// test's picks never leak into the next (tests that check sharing pass one store around).
    public static var shared: PendingPicks {
        if isUnitTestRun { return PendingPicks(defaults: nil) }
        return app
    }
    private static let app = PendingPicks()
    private nonisolated static let isUnitTestRun = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    public nonisolated static let defaultsKey = "speakeasy.pendingPicks"

    @Published public private(set) var store: PickStore
    private let defaults: UserDefaults?
    private let key: String

    /// `defaults` nil: kept in memory only.
    public init(defaults: UserDefaults? = .standard, key: String = PendingPicks.defaultsKey) {
        self.defaults = defaults; self.key = key
        store = defaults.map { PickStore.load($0, key: key) } ?? PickStore()
        // The old per-surface answer store is retired: the server records every answer now.
        defaults?.removeObject(forKey: "speakeasy.questionAnswers")
    }

    public subscript(id: String) -> PickStore.Entry? { store[id] }

    public func set(_ id: String, _ entry: PickStore.Entry?) {
        var next = store
        next.set(id, entry)
        commit(next)
    }

    public func remove(_ ids: String...) {
        var next = store
        for id in ids { next.remove(id) }
        commit(next)
    }

    /// Drop picks the server settled. `state(id)`: the server's state for that question id, nil when
    /// this surface doesn't have the task loaded (kept).
    public func reconcile(state: (String) -> QuestionState?) {
        var next = store
        next.reconcile(state: state)
        commit(next)
    }

    public func update(_ change: (inout PickStore) -> Void) {
        var next = store
        change(&next)
        commit(next)
    }

    private func commit(_ next: PickStore) {
        guard next != store else { return }
        store = next
        if let defaults { store.save(defaults, key: key) }
    }
}
