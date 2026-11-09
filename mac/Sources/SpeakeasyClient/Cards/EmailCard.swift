import SwiftUI
import SpeakeasyCore
#if os(macOS)
import AppKit
#else
import UIKit
#endif

// The email card (b73): a drafted email shows as itself, right where the job finished, with every
// recipient by name and address, the thread it answers and anyone the job couldn't find. Tap it for
// the whole email: change recipients, subject or body, then Send, Save draft or Discard. Send has
// no "are you sure"; it waits 5 seconds with Undo first (D6).

/// The host's way to run one of the card's own buttons (an action with a value), or nil where the
/// server can't take card buttons.
public struct CardSendKey: EnvironmentKey { public static let defaultValue: ((CardAction) -> Void)? = nil }
public extension EnvironmentValues {
    var cardSend: ((CardAction) -> Void)? {
        get { self[CardSendKey.self] }
        set { self[CardSendKey.self] = newValue }
    }
}

/// What this device did to a draft since the card was drawn, by draft id. It outlives the card
/// view (Home's list recycles rows), so a send in its Undo window, or one already asked for,
/// still shows as such after scrolling away and back.
@MainActor public final class EmailCardMemory: ObservableObject {
    public static let shared = EmailCardMemory()
    public enum Phase: Equatable {
        case waiting(String, until: Date)   // "send" | "discard", with Undo until then
        case asked(String)                  // sent to Hermes: "send" | "save" | "discard"
    }
    @Published public private(set) var phase: [String: Phase] = [:]
    /// Edits made on this device not yet sent or saved (the card shows them).
    @Published public var edits: [String: EmailCardInfo.Fields] = [:]
    private var timers: [String: Task<Void, Never>] = [:]
    public static var undoSeconds: Double = 5

    public func schedule(_ draftID: String, verb: String, run: @escaping @MainActor () -> Void) {
        timers[draftID]?.cancel()
        phase[draftID] = .waiting(verb, until: Date().addingTimeInterval(Self.undoSeconds))
        timers[draftID] = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(Self.undoSeconds))
            guard !Task.isCancelled, let self else { return }
            self.timers[draftID] = nil
            self.phase[draftID] = .asked(verb)
            run()
        }
    }

    public func undo(_ draftID: String) {
        timers[draftID]?.cancel(); timers[draftID] = nil
        phase[draftID] = nil
    }

    public func asked(_ draftID: String, _ verb: String) { phase[draftID] = .asked(verb) }

    #if DEBUG
    /// Shots and tests: a card frozen mid-Undo.
    public func debugSet(_ draftID: String, _ value: Phase?) { phase[draftID] = value }
    #endif
}

struct EmailCard: View {
    let card: ViewCard
    let info: EmailCardInfo
    @ObservedObject private var memory = EmailCardMemory.shared
    @Environment(\.cardSend) private var send
    @Environment(\.cardFit) private var fit
    @Environment(\.cardStyle) private var look
    @State private var details = false
    @State private var addFor: String?
    @State private var copied = false

    private var fields: EmailCardInfo.Fields { memory.edits[info.draftID] ?? info.fields }
    private var editedHere: Bool { memory.edits[info.draftID] != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            recipients
            VStack(alignment: .leading, spacing: 4) {
                if !fields.subject.isEmpty {
                    Text(fields.subject).font(.system(size: 15, weight: .semibold)).foregroundStyle(look.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let line = info.threadLine {
                    Label(line, systemImage: "arrowshape.turn.up.left")
                        .font(.system(size: 12.5)).foregroundStyle(look.secondary).lineLimit(1)
                        .accessibilityIdentifier("email-card-thread")
                }
            }
            Button { details = true } label: {
                Text(fields.body).font(.system(size: 14.5)).foregroundStyle(look.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(fit == .compact ? 3 : 6).fixedSize(horizontal: false, vertical: true)
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: look.wellRadius, style: .continuous).fill(look.well))
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Shows the whole email")
            .accessibilityIdentifier("email-card-body")
            ForEach(info.stillUnresolved(in: fields), id: \.self) { name in
                HStack(spacing: 6) {
                    Image(systemName: "person.crop.circle.badge.questionmark").foregroundStyle(look.warn)
                    Text("Couldn't find an address for \(name)").foregroundStyle(look.ink)
                    if info.status == .pending, send != nil {
                        Text("·").foregroundStyle(look.faint)
                        Button("Add") { addFor = name; details = true }.buttonStyle(.plain)
                            .foregroundStyle(look.emphasis).fontWeight(.semibold)
                            .accessibilityIdentifier("email-card-add-unresolved")
                    }
                }
                .font(.system(size: 13))
            }
            if let note = info.notes.first {
                Text(info.notes.count > 1 ? "\(note) (+\(info.notes.count - 1) in details)" : note)
                    .font(.system(size: 12.5)).foregroundStyle(look.secondary).lineLimit(2)
                    .accessibilityIdentifier("email-card-note")
            }
            footer
        }
        .contentShape(Rectangle())
        .onTapGesture { details = true }
        #if DEBUG
        .onAppear {
            // Shots: -emailCardDetails opens the whole email; -emailCardUndo freezes it mid-Undo.
            let args = ProcessInfo.processInfo.arguments
            if args.contains("-emailCardUndo"), memory.phase[info.draftID] == nil {
                memory.debugSet(info.draftID, .waiting("send", until: Date().addingTimeInterval(4.4 + 3600)))
            }
            if args.contains("-emailCardDetails") { DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { details = true } }
        }
        #endif
        .sheet(isPresented: $details, onDismiss: { addFor = nil }) {
            EmailDetailSheet(info: info, startAdding: addFor, act: act)
                .environment(\.cardStyle, look)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("email-card")
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "envelope.fill").font(.system(size: 13, weight: .semibold)).foregroundStyle(look.emphasis)
            Text(info.thread != nil ? "Reply" : "Email").font(.system(size: 13, weight: .semibold)).foregroundStyle(look.secondary)
            Spacer(minLength: 4)
            Text(stateWord).font(.system(size: 12, weight: .semibold)).foregroundStyle(stateColor)
                .accessibilityIdentifier("email-card-state")
        }
    }

    private var recipients: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let from = info.from { EmailRecipientsRow(label: "From", people: [EmailCardInfo.Person(typed: from) ?? .init(name: "", address: from)], limit: 1) }
            EmailRecipientsRow(label: "To", people: fields.to, limit: fit == .compact ? 1 : 3)
            if !fields.cc.isEmpty { EmailRecipientsRow(label: "Cc", people: fields.cc, limit: fit == .compact ? 1 : 3) }
            if !fields.bcc.isEmpty { EmailRecipientsRow(label: "Bcc", people: fields.bcc, limit: fit == .compact ? 1 : 2) }
        }
    }

    @ViewBuilder private var footer: some View {
        switch memory.phase[info.draftID] {
        case .waiting(let verb, let until)?:
            HStack(spacing: 12) {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let left = min(Int(EmailCardMemory.undoSeconds), max(1, Int(ceil(until.timeIntervalSince(context.date)))))
                    Text(verb == "send" ? "Sending in \(left) s" : "Discarding in \(left) s")
                        .font(.system(size: 14, weight: .medium)).foregroundStyle(look.ink).monospacedDigit()
                }
                Spacer(minLength: 4)
                Button("Undo") { memory.undo(info.draftID) }
                    .font(.system(size: 14, weight: .semibold)).foregroundStyle(look.emphasis).buttonStyle(.plain)
                    .frame(minHeight: 44).accessibilityIdentifier("email-card-undo")
            }
        case .asked?:
            EmptyView()
        case nil:
            if info.status == .pending, send != nil {
                HStack(spacing: 8) {
                    EmailCardButton(title: "Send", primary: true) { act("send", fields) }
                        .disabled(!fields.sendable)
                        .accessibilityIdentifier("email-card-send")
                    EmailCardButton(title: "Edit", primary: false) { details = true }
                        .accessibilityIdentifier("email-card-edit")
                    EmailCardButton(title: copied ? "Copied" : "Copy", primary: false) { copy() }
                        .accessibilityIdentifier("email-card-copy")
                }
            }
        }
    }

    private var stateWord: String {
        switch memory.phase[info.draftID] {
        case .asked("send")?: return "Sending…"
        case .asked("save")?: return editedHere ? "Saving…" : "Saving to Drafts…"
        case .asked("discard")?: return "Discarded"
        default: break
        }
        switch info.status {
        case .pending: return editedHere || info.edited ? "Draft · edited" : "Draft"
        case .approved: return "Sending…"
        case .sent: return "Sent"
        case .denied: return "Discarded"
        case .revising: return "Revising…"
        case .failed: return "Didn't send"
        case .superseded: return "Replaced"
        }
    }

    private var stateColor: Color {
        switch info.status {
        case .sent: return look.good
        case .failed: return look.bad
        default: return look.secondary
        }
    }

    /// The email as text (headers, then the body), for pasting into a mail app.
    private func copy() {
        let f = fields
        var lines = ["To: " + f.to.map(\.wire).joined(separator: ", ")]
        if !f.cc.isEmpty { lines.append("Cc: " + f.cc.map(\.wire).joined(separator: ", ")) }
        if !f.bcc.isEmpty { lines.append("Bcc: " + f.bcc.map(\.wire).joined(separator: ", ")) }
        if !f.subject.isEmpty { lines.append("Subject: " + f.subject) }
        let text = lines.joined(separator: "\n") + "\n\n" + f.body
        #if os(macOS)
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
        #else
        UIPasteboard.general.string = text
        #endif
        copied = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { copied = false }
    }

    /// One of the card's buttons. Send and Discard wait 5 s with Undo; Save goes at once.
    private func act(_ verb: String, _ shown: EmailCardInfo.Fields) {
        guard let send else { return }
        let id = info.draftID, sha = info.sha256
        let changed = shown != info.fields
        if changed { memory.edits[id] = shown } else { memory.edits[id] = nil }
        let action: CardAction
        if verb == "discard" {
            action = CardAction(id: "discard", label: "Discard", value: .string(sha))
        } else if changed {
            action = CardAction(id: "edit", label: verb == "send" ? "Send" : "Save draft",
                                value: shown.value(sha256: sha, after: verb == "keep" ? nil : verb))
        } else if verb == "keep" {
            return
        } else {
            action = CardAction(id: verb, label: verb == "send" ? "Send" : "Save draft", value: .string(sha))
        }
        switch verb {
        case "send", "discard": memory.schedule(id, verb: verb) { send(action) }
        case "keep": send(action)   // edits kept on the server, nothing sent
        default: memory.asked(id, verb); send(action)
        }
    }
}

struct EmailRecipientsRow: View {
    let label: String
    let people: [EmailCardInfo.Person]
    var limit = 3
    @Environment(\.cardStyle) private var look
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label).font(.system(size: 12.5, weight: .medium)).foregroundStyle(look.faint)
                .frame(width: 34, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(people.prefix(limit).enumerated()), id: \.offset) { _, p in
                    // Name, then the full address (never cut: it wraps under the name when tight).
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 6) { name(p); address(p) }
                        VStack(alignment: .leading, spacing: 0) { name(p); address(p) }
                    }
                }
                if people.count > limit {
                    Text("+\(people.count - limit) more").font(.system(size: 12.5)).foregroundStyle(look.secondary)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("email-card-\(label.lowercased())")
    }
    @ViewBuilder private func name(_ p: EmailCardInfo.Person) -> some View {
        if !p.name.isEmpty {
            // "Pat Doe · Lark & Finch": the firm the ask named, only when the address is that firm's.
            (Text(p.name).foregroundStyle(look.ink)
                + Text(p.org.map { " · \($0)" } ?? "").foregroundStyle(look.secondary))
                .font(.system(size: 13.5, weight: .medium)).lineLimit(1)
        }
    }
    private func address(_ p: EmailCardInfo.Person) -> some View {
        Text(p.address).font(.system(size: 12.5)).foregroundStyle(look.secondary).lineLimit(1).truncationMode(.middle)
            .textSelection(.enabled)
    }
}

struct EmailCardButton: View {
    let title: String
    let primary: Bool
    var destructive = false
    let action: () -> Void
    @Environment(\.cardStyle) private var look
    @Environment(\.isEnabled) private var enabled
    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 14.5, weight: .semibold)).lineLimit(1)
                .padding(.horizontal, 16).frame(minHeight: 44).frame(maxWidth: .infinity)
                .background(Capsule().fill(primary ? look.action : look.quietFill))
                .foregroundStyle(primary ? look.onAction : destructive ? look.bad : look.quietInk)
                .contentShape(Capsule())
                .opacity(enabled ? 1 : 0.45)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - The whole email

struct EmailDetailSheet: View {
    let info: EmailCardInfo
    var startAdding: String?
    let act: (String, EmailCardInfo.Fields) -> Void
    @ObservedObject private var memory = EmailCardMemory.shared
    @Environment(\.dismiss) private var dismiss
    @Environment(\.cardStyle) private var look
    @Environment(\.cardSend) private var send
    @State private var fields: EmailCardInfo.Fields
    @State private var showCc: Bool
    @State private var showBcc: Bool
    @State private var tapped: String?
    @State private var quoteOpen = false

    init(info: EmailCardInfo, startAdding: String?, act: @escaping (String, EmailCardInfo.Fields) -> Void) {
        self.info = info; self.startAdding = startAdding; self.act = act
        let current = EmailCardMemory.shared.edits[info.draftID] ?? info.fields
        _fields = State(initialValue: current)
        _showCc = State(initialValue: !current.cc.isEmpty)
        _showBcc = State(initialValue: !current.bcc.isEmpty)
    }

    private var editable: Bool { info.status == .pending && send != nil && EmailCardMemory.shared.phase[info.draftID] == nil }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(info.thread != nil ? "Reply" : "Email").font(.headline).foregroundStyle(look.ink)
                Spacer()
                Button("Done") { keepAndClose() }.fontWeight(.semibold).foregroundStyle(look.emphasis)
                    .buttonStyle(.plain).frame(minHeight: 44).accessibilityIdentifier("email-sheet-done")
            }
            .padding(.horizontal, 20).padding(.top, 14)
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let from = info.from {
                        EmailRecipientsRow(label: "From", people: [EmailCardInfo.Person(typed: from) ?? .init(name: "", address: from)], limit: 1)
                    }
                    EmailPeopleEditor(label: "To", people: $fields.to, editable: editable,
                                      prefill: startAdding.map { "\($0) " }, focusFirst: startAdding != nil)
                    if showCc { EmailPeopleEditor(label: "Cc", people: $fields.cc, editable: editable, focusFirst: tapped == "cc") }
                    if showBcc { EmailPeopleEditor(label: "Bcc", people: $fields.bcc, editable: editable, focusFirst: tapped == "bcc") }
                    if editable, !showCc || !showBcc {
                        HStack(spacing: 18) {
                            if !showCc { Button("Add Cc") { tapped = "cc"; showCc = true } }
                            if !showBcc { Button("Add Bcc") { tapped = "bcc"; showBcc = true } }
                        }
                        .font(.system(size: 14, weight: .medium)).foregroundStyle(look.secondary).buttonStyle(.plain)
                        .frame(minHeight: 44)
                    }
                    ForEach(info.stillUnresolved(in: fields), id: \.self) { name in
                        Label("Couldn't find an address for \(name). Add it above.", systemImage: "person.crop.circle.badge.questionmark")
                            .font(.system(size: 13)).foregroundStyle(look.ink)
                    }
                    Divider()
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Subject").font(.system(size: 12.5, weight: .medium)).foregroundStyle(look.faint)
                        if editable {
                            TextField("Subject", text: $fields.subject).font(.system(size: 16, weight: .semibold))
                                .textFieldStyle(.plain).accessibilityIdentifier("email-sheet-subject")
                        } else {
                            Text(fields.subject).font(.system(size: 16, weight: .semibold)).textSelection(.enabled)
                        }
                        if let line = info.threadLine {
                            Text(line).font(.system(size: 12.5)).foregroundStyle(look.secondary)
                        }
                    }
                    if editable {
                        TextEditor(text: $fields.body).font(.system(size: 15.5)).scrollContentBackground(.hidden)
                            .frame(minHeight: 220).padding(8)
                            .background(RoundedRectangle(cornerRadius: look.wellRadius, style: .continuous).fill(look.well))
                            .accessibilityIdentifier("email-sheet-body")
                    } else {
                        Text(fields.body).font(.system(size: 15.5)).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if let quoted = info.threadLast {
                        VStack(alignment: .leading, spacing: 8) {
                            Button { withAnimation(.easeOut(duration: 0.2)) { quoteOpen.toggle() } } label: {
                                HStack(spacing: 6) {
                                    Image(systemName: quoteOpen ? "chevron.down" : "chevron.right").font(.system(size: 11, weight: .semibold))
                                    Text(["Replying to", quoted.from, quoted.when].compactMap { $0 }.joined(separator: " · "))
                                        .lineLimit(1)
                                }
                                .font(.system(size: 13, weight: .medium)).foregroundStyle(look.secondary)
                                .frame(minHeight: 44).contentShape(Rectangle())
                            }
                            .buttonStyle(.plain).accessibilityIdentifier("email-sheet-quote")
                            if quoteOpen {
                                Text(quoted.text).font(.system(size: 14)).foregroundStyle(look.secondary).textSelection(.enabled)
                                    .padding(.leading, 12)
                                    .overlay(alignment: .leading) { Rectangle().fill(look.hairline).frame(width: 2) }
                            }
                        }
                    }
                    if !info.attachments.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Attachments").font(.system(size: 12.5, weight: .medium)).foregroundStyle(look.faint)
                            ForEach(info.attachments, id: \.self) { name in
                                Label(name, systemImage: "paperclip").font(.system(size: 14)).foregroundStyle(look.ink)
                            }
                        }
                    }
                    if !info.notes.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(info.notes, id: \.self) { Text($0) }
                        }
                        .font(.system(size: 13)).foregroundStyle(look.secondary)
                    }
                }
                .padding(20)
            }
            if editable {
                HStack(spacing: 8) {
                    EmailCardButton(title: "Discard", primary: false, destructive: true) { act("discard", fields); dismiss() }
                        .accessibilityIdentifier("email-sheet-discard")
                    EmailCardButton(title: "Save draft", primary: false) { act("save", fields); dismiss() }
                        .accessibilityIdentifier("email-sheet-save")
                    EmailCardButton(title: "Send", primary: true) { act("send", fields); dismiss() }
                        .disabled(!fields.sendable)
                        .accessibilityIdentifier("email-sheet-send")
                }
                .padding(.horizontal, 20).padding(.vertical, 12)
            }
        }
        .background(look.fill.opacity(0.0001))
        #if os(macOS)
        .frame(minWidth: 520, idealWidth: 560, minHeight: 560, idealHeight: 680)
        #else
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        #endif
        .accessibilityIdentifier("email-sheet")
    }

    /// Done keeps the changes on this device (the card shows them; Send or Save carries them).
    private func keepAndClose() {
        if editable, fields != info.fields { EmailCardMemory.shared.edits[info.draftID] = fields }
        dismiss()
    }
}

struct EmailPeopleEditor: View {
    let label: String
    @Binding var people: [EmailCardInfo.Person]
    var editable: Bool
    var prefill: String?
    var focusFirst = false
    @State private var typed = ""
    @State private var invalid = false
    @State private var adding = false
    @FocusState private var focused: Bool
    @Environment(\.cardStyle) private var look

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 12.5, weight: .medium)).foregroundStyle(look.faint)
            ForEach(Array(people.enumerated()), id: \.offset) { index, p in
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 1) {
                        if !p.name.isEmpty {
                            (Text(p.name).foregroundStyle(look.ink)
                                + Text(p.org.map { " · \($0)" } ?? "").foregroundStyle(look.secondary))
                                .font(.system(size: 15, weight: .medium))
                        }
                        Text(p.address).font(.system(size: 13.5)).foregroundStyle(look.secondary).textSelection(.enabled)
                    }
                    Spacer(minLength: 4)
                    if editable {
                        Button { people.remove(at: index) } label: {
                            Image(systemName: "minus.circle.fill").font(.system(size: 18)).foregroundStyle(look.faint)
                                .frame(width: 44, height: 44).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Remove \(p.label)")
                        .accessibilityIdentifier("email-remove-\(label.lowercased())-\(index)")
                    }
                }
            }
            if editable && !adding {
                Button { adding = true; focused = true } label: {
                    Label("Add", systemImage: "plus").font(.system(size: 14, weight: .medium)).foregroundStyle(look.secondary)
                        .frame(minHeight: 36).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Add to \(label)")
                .accessibilityIdentifier("email-add-\(label.lowercased())-button")
            }
            if editable && adding {
                HStack(spacing: 8) {
                    TextField("Name <address> or address", text: $typed)
                        .textFieldStyle(.plain).font(.system(size: 14.5))
                        #if os(iOS)
                        .textInputAutocapitalization(.never).keyboardType(.emailAddress).autocorrectionDisabled()
                        #endif
                        .focused($focused)
                        .onSubmit(add)
                        .accessibilityIdentifier("email-add-\(label.lowercased())")
                    if !typed.isEmpty {
                        Button("Add", action: add).font(.system(size: 14, weight: .semibold)).foregroundStyle(look.emphasis)
                            .buttonStyle(.plain).frame(minHeight: 44)
                    }
                }
                .padding(.horizontal, 12).frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(invalid ? look.bad : look.hairline))
                if invalid {
                    Text("That needs an email address, like name@example.com.").font(.system(size: 12.5)).foregroundStyle(look.bad)
                }
            }
        }
        .onAppear {
            if let prefill, typed.isEmpty { typed = prefill }
            if focusFirst || prefill != nil { adding = true; focused = true }
        }
    }

    private func add() {
        guard let person = EmailCardInfo.Person(typed: typed) else { invalid = !typed.isEmpty; return }
        invalid = false
        if !people.contains(where: { $0.address.lowercased() == person.address.lowercased() }) { people.append(person) }
        typed = ""
    }
}
