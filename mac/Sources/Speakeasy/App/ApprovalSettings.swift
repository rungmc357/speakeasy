import AppKit
import SpeakeasyCore
import SpeakeasyClient
import SwiftUI

// MARK: Approvals (read-only)

/// Settings › Approvals (servers with `approval_rules`): how this Hermes handles risky commands, in
/// the server's plain words. Nothing here changes a rule yet.
struct ApprovalSettings: View {
    @EnvironmentObject var app: AppModel

    var body: some View {
        if let api = app.api {
            ApprovalRulesForm(load: { try await api.approvalRules() })
        } else {
            Form { Text("Pair this Mac to see your approval rules.").foregroundStyle(.secondary) }.formStyle(.grouped)
        }
    }
}

struct ApprovalRulesForm: View {
    var load: () async throws -> ApprovalRules
    @State private var rules: ApprovalRules?
    @State private var error: String?
    @State private var loading = false

    var body: some View {
        Form {
            if let rules {
                ForEach(rules.sections) { section in
                    if section.id == "mode" {
                        ModeSection(rules: rules, section: section)
                    } else {
                        Section {
                            if section.rows.isEmpty, let empty = section.empty {
                                Text(empty).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                            ForEach(section.rows) { ApprovalRuleRowView(row: $0) }
                        } header: { Text(section.title) } footer: {
                            if let footer = section.footer { FooterText(text: footer) }
                        }
                    }
                }
                Section {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: "lock").foregroundStyle(.secondary).accessibilityHidden(true)
                        Text(rules.displayNote)
                            .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        Button("Check again") { Task { await reload() } }.disabled(loading)
                    }
                    .accessibilityIdentifier("approvals-note")
                }
            } else {
                Section {
                    HStack {
                        Text(error ?? "Loading your approval rules…").foregroundStyle(.secondary)
                        Spacer()
                        if error != nil { Button("Try again") { Task { await reload() } }.disabled(loading) }
                        else { ProgressView().controlSize(.small) }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .accessibilityIdentifier("settings-approvals")
        .task { await reload() }
    }

    private func reload() async {
        loading = true
        defer { loading = false }
        do { rules = try await load(); error = nil }
        catch { if rules == nil { self.error = "Couldn't load your approval rules. \(error.localizedDescription)" } }
    }
}

/// The headline: how risky commands are handled, big, with its explanation and the wait time.
private struct ModeSection: View {
    let rules: ApprovalRules
    let section: ApprovalRuleSection

    var body: some View {
        Section {
            if let head = section.rows.first(where: { $0.id == "mode" }) {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: rules.modeSymbol)
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(BrandPalette.brass)
                        .frame(width: 30, height: 30)
                        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(BrandPalette.brass.opacity(0.15)))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(head.title).font(.headline)
                        if let detail = head.detail {
                            Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(.vertical, 2)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("approvals-mode")
            }
            ForEach(section.rows.filter { $0.id != "mode" }) { ApprovalRuleRowView(row: $0) }
        } header: { Text(section.title) }
    }
}

struct ApprovalRuleRowView: View {
    let row: ApprovalRuleRow

    var body: some View {
        Group {
            if let code = row.code {
                HStack {
                    Text(row.title)
                    Spacer(minLength: 12)
                    Text(code).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill(Color.primary.opacity(0.07)))
                }
            } else if let value = row.value {
                LabeledContent {
                    Text(value)
                } label: {
                    Text(row.title)
                    if let detail = row.detail { Text(detail) }
                }
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.title)
                    if let detail = row.detail {
                        Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct FooterText: View {
    let text: String
    var body: some View {
        Text(text).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.leading)
            .frame(maxWidth: .infinity, alignment: .leading).fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 10)
    }
}

/// `SETTINGS_SHOT=approvals --settings-calls-snapshot <dir>`: the Approvals tab with invented rules
/// (light or dark via `HISTORY_APPEARANCE`), as a PNG. Development only; no server.
@MainActor
enum ApprovalSettingsSnapshot {
    static func run(dir: String) {
        if let look = ProcessInfo.processInfo.environment["HISTORY_APPEARANCE"] {
            NSApp.appearance = NSAppearance(named: look == "light" ? .aqua : .darkAqua)
        }
        let size = NSSize(width: 560, height: 960)
        let host = NSHostingController(rootView: ApprovalRulesForm(load: { ApprovalRules.sample })
            .frame(width: size.width, height: size.height))
        let window = NSWindow(contentViewController: host)
        window.setContentSize(size)
        window.makeKeyAndOrderFront(nil)
        let name = "mac-settings-approvals-\(ProcessInfo.processInfo.environment["HISTORY_APPEARANCE"] ?? "dark")"
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if let view = window.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
                print("snapshot \(name)")
            }
            exit(0)
        }
    }
}
