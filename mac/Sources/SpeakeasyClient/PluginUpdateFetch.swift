import Foundation
import SpeakeasyCore

/// Network half of the plugin-update notice (the parsing/comparison is `UpdateCheck` in the core).
/// Shared by the Mac and iPhone apps.
public enum PluginUpdateFetch {
    /// The newest published plugin version from the repo's `plugin.yaml`, or nil if it can't be read.
    public static func latestPluginVersion(userAgent: String, session: URLSession = .shared) async -> String? {
        var request = URLRequest(url: UpdateCheck.pluginManifestURL, timeoutInterval: 15)
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return UpdateCheck.parsePluginVersion(data)
    }

    /// One line of what's new in `version`, from the repo CHANGELOG's entry title; nil if unavailable.
    public static func whatsNew(version: String, userAgent: String, session: URLSession = .shared) async -> String? {
        var request = URLRequest(url: PluginUpdateFlow.changelogURL, timeoutInterval: 15)
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let text = String(data: data, encoding: .utf8) else { return nil }
        return PluginUpdateFlow.changelogHeadline(text, version: version)
    }

    /// What to send a Hermes agent to update the plugin (copy button; the Update button sends the same
    /// as a job). Hermes' own updater, no forced reload: it loads by itself after any call.
    public static let updateRequest = PluginUpdateFlow.requestText(version: nil)
}
