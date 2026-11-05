import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// An email a task drafted: who it goes to, what it says, and Send / Deny / Revise.
/// Every action carries the sha256 of the draft on screen; a 409 means it changed.
struct EmailDraftCard: View {
    /// "Show all" grows the body to scroll inside the card, but never so tall that the card's
    /// Show less / Send / Deny / Revise row falls off a laptop screen and strands the user.
    static func expandedBodyHeight(screen: NSScreen? = NSScreen.main) -> CGFloat {
        let visible = screen?.visibleFrame.height ?? 800
        return EmailDraftLayout.expandedBodyHeight(visibleScreenHeight: visible)
    }

    var draft: EmailDraft
    @ObservedObject var model: VoicePanelModel
    private var bodyExpanded: Bool { model.expandedDraftIDs.contains(draft.id) }
    @State private var revising = false
    @State private var instructions = ""
    @FocusState private var cardFocused: Bool
    @FocusState private var fieldFocused: Bool
    @Environment(\.colorScheme) private var scheme

    private var status: EmailDraft.Status { model.displayedStatus(of: draft) }
    private var actionable: Bool { status == .pending && !model.draftBusy.contains(draft.draftID) }
    private var notice: String? { model.draftNotice[draft.draftID] }

    var body: some View {
        if status == .superseded {
            collapsed
        } else {
            card
        }
    }

    private var collapsed: some View {
        HStack(spacing: 6) {
            Image(systemName: "envelope").font(.system(size: 10))
            Text("Email draft · \(draft.subject.isEmpty ? "(no subject)" : draft.subject) · replaced")
                .lineLimit(1).truncationMode(.tail)
        }
        .font(.system(size: 10.5)).foregroundStyle(.tertiary)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Email draft \(draft.subject), replaced by a newer draft")
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Image(systemName: "envelope.fill").font(.system(size: 11, weight: .semibold)).foregroundStyle(Tokens.blue)
                Text("Email draft").font(.system(size: 11.5, weight: .semibold))
                Spacer()
                if let label = EmailDraft(draftID: draft.draftID, sha256: draft.sha256, status: status).stateLabel {
                    Text(label).font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(status == .failed ? Tokens.red : (status == .sent ? Tokens.green.ink : .secondary))
                        .accessibilityLabel("Status: \(label)")
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                field("From", draft.from)
                field("To", draft.to.joined(separator: ", "))
                if !draft.cc.isEmpty { field("Cc", draft.cc.joined(separator: ", ")) }
                if !draft.bcc.isEmpty { field("Bcc", draft.bcc.joined(separator: ", ")) }
                field("Subject", draft.subject.isEmpty ? "(no subject)" : draft.subject, bold: true)
            }
            ScrollView {
                Text(draft.body).font(.system(size: 11.5)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(8)
            }
            .frame(maxHeight: bodyExpanded ? Self.expandedBodyHeight() : 130)
            .fixedSize(horizontal: false, vertical: !bodyExpanded && draft.body.count < 240)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.05)))
            .accessibilityLabel("Email body")
            .accessibilityValue(draft.body)
            if draft.body.count >= 240 || draft.body.split(separator: "\n").count > 7 {
                Button(bodyExpanded ? "Show less" : "Show all") {
                    if bodyExpanded { model.expandedDraftIDs.remove(draft.id) } else { model.expandedDraftIDs.insert(draft.id) }
                }
                    .buttonStyle(.link).font(.system(size: 10.5))
                    .accessibilityLabel(bodyExpanded ? "Collapse email body" : "Expand email body")
            }
            if let notice {
                Label(notice, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 10.5, weight: .medium)).foregroundStyle(Tokens.amber.ink)
            }
            if revising && actionable {
                VStack(alignment: .leading, spacing: 6) {
                    TextField("What should change?", text: $instructions, axis: .vertical)
                        .textFieldStyle(.roundedBorder).font(.system(size: 11.5)).lineLimit(1...4)
                        .focused($fieldFocused)
                        .accessibilityLabel("What should change in this email?")
                        .onSubmit(sendRevision)
                    HStack(spacing: 8) {
                        Spacer()
                        PillButton(title: "Cancel", action: { revising = false; instructions = ""; model.objectWillChange.send() })
                        PillButton(title: "Send revision", prominent: true, action: sendRevision)
                            .disabled(instructions.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
            } else {
                HStack(spacing: 8) {
                    Text(actionable ? "Or just say what to change." : "").font(.system(size: 11)).foregroundStyle(.secondary)
                    Spacer()
                    PillButton(title: "Deny", destructive: true, action: { model.onDraftAction(draft, .deny, nil) })
                        .accessibilityLabel("Deny: discard this email")
                    PillButton(title: "Revise", action: { revising = true; fieldFocused = true; model.objectWillChange.send() })
                        .accessibilityLabel("Revise this email")
                    PillButton(title: "Send", prominent: true, action: approve)
                        .accessibilityLabel("Approve and send this email")
                        .help("Send this email (⌘Return while the card is focused)")
                }
                .disabled(!actionable)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Tokens.blue.opacity(scheme == .dark ? 0.10 : 0.12)))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous)
            .strokeBorder(cardFocused ? Tokens.blue : Tokens.blue.opacity(0.4), lineWidth: cardFocused ? 1.5 : 0.75))
        .focusable()
        .focused($cardFocused)
        .focusEffectDisabled()
        // Return alone does nothing destructive; ⌘Return approves only while this card has focus.
        .onKeyPress(.return, phases: .down) { press in
            guard press.modifiers.contains(.command), cardFocused, actionable, !revising else { return .ignored }
            approve()
            return .handled
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Email draft to \(draft.to.joined(separator: ", ")), subject \(draft.subject)")
    }

    private func field(_ label: String, _ value: String, bold: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(label).font(.system(size: 11.5)).foregroundStyle(.secondary).frame(width: 46, alignment: .leading)
            Text(value).font(.system(size: 11.5, weight: bold ? .semibold : .regular))
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }

    private func approve() { if actionable { model.onDraftAction(draft, .approve, nil) } }

    private func sendRevision() {
        let text = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, actionable else { return }
        model.onDraftAction(draft, .revise, text)
        revising = false; instructions = ""
    }
}
