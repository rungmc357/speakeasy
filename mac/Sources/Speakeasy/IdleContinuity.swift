import AppKit
import Foundation
import SpeakeasyCore
import SpeakeasyClient
import UserNotifications

/// Idle (no-call) continuity: follows the last call's run via `GET /voice/work/latest`
/// and posts a macOS notification when it settles. All scheduling decisions live in
/// the pure `IdleWorkWatch`; this class only performs the side effects.
@MainActor
final class IdleContinuity: NSObject, UNUserNotificationCenterDelegate {
    private let api: ServerClient?
    private var watch = IdleWorkWatch()
    private var task: Task<Void, Never>?
    private var authorization: Bool?
    /// Opens the work-only panel (notification click or badge click).
    var onOpenWork: (() -> Void)?
    var onOpenSettings: (() -> Void)?
    /// Resumes the paused call (Resume button, or a click on a paused-call notice).
    var onResume: (() -> Void)?
    nonisolated static let pausedCategory = "speakeasy-paused"
    nonisolated static let pluginUpdateCategory = "speakeasy-plugin-update"
    nonisolated static let resumeAction = "speakeasy-resume"
    /// Fallback when notifications are unavailable: show a badge in the menu bar.
    var onBadge: ((Bool) -> Void)?

    init(config: AppConfig) {
        api = ServerClient(config: config)
        super.init()
        if Self.notificationsSupported {
            let center = UNUserNotificationCenter.current()
            center.delegate = self
            let resume = UNNotificationAction(identifier: Self.resumeAction, title: "Resume call", options: [.foreground])
            center.setNotificationCategories([UNNotificationCategory(identifier: Self.pausedCategory, actions: [resume],
                                                                     intentIdentifiers: [], options: [])])
        }
    }

    /// UNUserNotificationCenter traps outside a signed .app bundle (e.g. `swift run`).
    static var notificationsSupported: Bool {
        Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app"
    }

    func callStarted() {
        watch.stop()
        task?.cancel(); task = nil
        onBadge?(false)
    }

    func callEnded(lastWork: WorkInfo?) {
        watch.callEnded(lastWork: lastWork)
        schedule()
    }

    private func schedule() {
        task?.cancel()
        guard let api, let delay = watch.nextDelay else { task = nil; return }
        task = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            do {
                let work = try await api.work(runID: nil)
                guard let self, !Task.isCancelled else { return }
                if let notice = self.watch.observe(work) { self.deliver(notice) }
            } catch {
                guard let self, !Task.isCancelled else { return }
                self.watch.observeError()
            }
            self?.schedule()
        }
    }

    /// Heads-up while the call is paused: offers Resume.
    func pausedNotice(_ notice: WorkNotice) {
        deliver(notice, category: Self.pausedCategory)
    }

    /// Settings › General: a notification when work from an ended call finishes. Paused-call
    /// notices (which carry Resume) are always delivered.
    var notifyWhenDone = true

    /// One notice per release, when the running Hermes plugin trails the latest release.
    /// The app never changes Hermes or restarts its gateway.
    func notifyPluginUpdate(version: String) {
        let key = "notifiedPluginUpdateVersion"
        guard UserDefaults.standard.string(forKey: key) != version else { return }
        guard Self.notificationsSupported else { onBadge?(true); return }
        let center = UNUserNotificationCenter.current()
        Task { [weak self] in
            guard let self else { return }
            if self.authorization == nil {
                self.authorization = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
            }
            guard self.authorization == true else { self.onBadge?(true); return }
            let content = UNMutableNotificationContent()
            content.title = "Speakeasy plugin update available"
            content.body = "Speakeasy \(version) is available for your Hermes. Open Settings to update it."
            content.categoryIdentifier = Self.pluginUpdateCategory
            content.sound = .default
            do {
                try await center.add(UNNotificationRequest(identifier: "speakeasy-plugin-\(version)", content: content, trigger: nil))
                UserDefaults.standard.set(version, forKey: key)
            } catch { self.onBadge?(true) }
        }
    }

    private func deliver(_ notice: WorkNotice, category: String? = nil) {
        guard notifyWhenDone || category != nil else { onBadge?(true); return }
        guard Self.notificationsSupported else { onBadge?(true); return }
        let center = UNUserNotificationCenter.current()
        Task { [weak self] in
            guard let self else { return }
            if self.authorization == nil {
                // Asked lazily, the first time there is something to say.
                self.authorization = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
            }
            guard self.authorization == true else { self.onBadge?(true); return }
            let content = UNMutableNotificationContent()
            content.title = notice.title
            content.body = notice.body
            content.sound = .default
            if let category { content.categoryIdentifier = category }
            let request = UNNotificationRequest(identifier: "speakeasy-\(notice.runID ?? UUID().uuidString)-\(notice.kind)",
                                                content: content, trigger: nil)
            do { try await center.add(request) } catch { self.onBadge?(true) }
        }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let category = response.notification.request.content.categoryIdentifier
        let paused = category == Self.pausedCategory
        let action = response.actionIdentifier
        Task { @MainActor in
            self.onBadge?(false)
            if category == Self.pluginUpdateCategory {
                self.onOpenSettings?()
            } else if paused && (action == Self.resumeAction || action == UNNotificationDefaultActionIdentifier) {
                self.onResume?()
            } else {
                self.onOpenWork?()
            }
        }
        completionHandler()
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}
