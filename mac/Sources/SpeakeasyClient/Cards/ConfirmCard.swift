import SwiftUI
import SpeakeasyCore

/// One preview/approval/receipt layout for every consequential action. The server alone owns
/// lifecycle and button values. Approval happens on the preview itself, never a second pop-up.
struct ConfirmCard: View {
    let card: ViewCard
    @Environment(\.cardFit) private var fit
    @Environment(\.cardStyle) private var style
    @Environment(\.cardHosted) private var hosted
    private var details: JSONValue { card["details"] }
    private var state: String { card["state"].string ?? "pending" }
    private var compact: Bool { fit == .compact }
    private var symbol: String {
        switch card["type"].string {
        case "checkout": return "bag"
        case "send", "invite": return "paperplane"
        case "book": return "calendar"
        case "pay": return "creditcard"
        case "post": return "text.bubble"
        case "home": return "lock.open"
        case "delete_share": return "doc.on.doc"
        default: return "checkmark.shield"
        }
    }
    @Environment(\.widgetSlim) private var slim
    /// Accessibility text sizes: a label goes above its value (SPK-478), so a total never breaks
    /// inside the number and a label never hyphenates in its narrow column.
    @Environment(\.dynamicTypeSize) private var typeSize
    private var stacked: Bool { typeSize.isAccessibilitySize }
    /// A slim Home widget (SPK-358 pass 4): a pending checkout's item, total and who/where on one
    /// quiet line, so its Buy / Cancel buttons never fall under the composer; the full card (one tap)
    /// shows the breakdown.
    private var slimCheckout: Bool { slim && card["type"].string == "checkout" && state == "pending" }
    var body: some View {
        if slimCheckout { slimBody } else { fullBody }
    }
    private var slimBody: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(card["items"].array.prefix(2).enumerated()), id: \.offset) { _, item in
                HStack(alignment: .center, spacing: 10) {
                    if item["photo_url"].url != nil {
                        ArtifactPhoto(url: item["photo_url"].url, symbol: "shippingbox")
                            .frame(width: 40, height: 40).clipShape(RoundedRectangle(cornerRadius: 9))
                    }
                    VStack(alignment: .leading, spacing: 1) {
                        Text(AgentMarkdown.attributed(item["name"].string ?? "")).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Text(ConfirmCard.itemFacts(item)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: 0)
                }
            }
            let who = [details["merchant"].string, details["address"].string,
                       details["payment_last4"].string.map { "•••• \($0)" }].compactMap { $0 }
            if !who.isEmpty {
                Text(who.joined(separator: " · ")).font(.caption).foregroundStyle(style.secondary).lineLimit(1)
            }
            amount("Total", key: "total", prominent: true)
        }
        .modifier(ConfirmSurface(style: style, hosted: hosted))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("confirm-card")
    }
    private var fullBody: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: style.capsHeaders ? 10 : CleanGrid.glyphGap) {
                if style.capsHeaders {
                    Image(systemName: state == "done" ? "checkmark.circle.fill" : symbol)
                        .font(.system(size: 20)).foregroundStyle(style.emphasis)
                } else {
                    Image(systemName: state == "done" ? "checkmark" : symbol)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(state == "done" ? style.good : state == "pending" ? style.needsYou : style.secondary)
                        .frame(width: CleanGrid.glyphTile, height: CleanGrid.glyphTile)
                        .background(GlyphTileBackground())
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(stateTitle).font(.caption.weight(.semibold))
                        .foregroundStyle(state == "pending" && !style.capsHeaders ? style.needsYou : style.secondary)
                    Text(AgentMarkdown.attributed(card["title"].string ?? "Preview"))
                        .font(.title3.weight(.semibold)).foregroundStyle(style.ink).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            if state == "done" { receipt }
            else {
                if card["type"].string == "checkout" { checkout }
                else { facts }
                if let body = details["body"].string {
                    Text(AgentMarkdown.attributed(body))
                        .font(.body).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: style.wellRadius, style: .continuous).fill(style.well))
                }
                ForEach(Array(card["fields"].array.enumerated()), id: \.offset) { _, field in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(AgentMarkdown.attributed(field["label"].string ?? ""))
                            if field["guessed"].bool == true {
                                Label("Please check", systemImage: "questionmark.circle").foregroundStyle(style.warn)
                            }
                        }.font(.caption)
                        Text(AgentMarkdown.attributed(field["value"].string ?? "")).font(.body)
                    }
                }
                if state == "pending" {
                    Text("Nothing happens until you approve.").font(.caption).foregroundStyle(.secondary)
                } else if state == "approved" {
                    Label("Approved · waiting for confirmation", systemImage: "clock")
                        .font(.callout).foregroundStyle(.secondary)
                } else if state == "cancelled" {
                    Text(card["type"].string == "checkout" ? "Cancelled. Your cart was left unchanged." : "Cancelled. Nothing was sent.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if let error = card["error"].string {
                    Label { Text(AgentMarkdown.attributed(error)) } icon: { Image(systemName: "exclamationmark.circle") }
                        .font(.callout).foregroundStyle(style.warn)
                }
            }
        }
        .modifier(ConfirmSurface(style: style, hosted: hosted))
        .accessibilityIdentifier("confirm-card")
    }
    private var stateTitle: String {
        switch state {
        case "done": return "Confirmed"
        case "approved": return "Approved"
        case "cancelled": return "Cancelled"
        case "unverified": return "Please check"
        default: return "Ready for your approval"
        }
    }
    private var checkout: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(card["items"].array.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .top, spacing: 12) {
                    // No product photo: the text takes the row (a grey box with a glyph read as a missing image).
                    if item["photo_url"].url != nil {
                        ArtifactPhoto(url: item["photo_url"].url, symbol: "shippingbox")
                            .frame(width: compact ? 58 : 72, height: compact ? 58 : 72)
                            .clipShape(RoundedRectangle(cornerRadius: 12))
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        // One item the title already names ("Buy Oakline Standing Desk 60 in."): its row
                        // keeps the variant and quantity, not the same name a second time.
                        let named = card["items"].array.count == 1 && (item["name"].string).map { n in
                            (card["title"].string ?? "").localizedCaseInsensitiveContains(n) } == true
                        if !named {
                            Text(AgentMarkdown.attributed(item["name"].string ?? "")).font(.body.weight(.semibold))
                        }
                        if let description = item["description"].string {
                            Text(AgentMarkdown.attributed(description)).font(.caption).foregroundStyle(.secondary)
                        }
                        Text(ConfirmCard.itemFacts(item)).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                }
            }
            facts
            Divider()
            amount("Subtotal", key: "subtotal")
            amount("Tax", key: "tax")
            amount("Shipping", key: "shipping")
            Divider()
            amount("Total", key: "total", prominent: true)
        }
    }
    private static let fields: [(String, String)] = [
        ("merchant", "From"), ("recipient", "To"), ("payee", "Payee"), ("account", "Account"),
        ("subject", "Subject"), ("address", "Deliver to"), ("delivery", "Delivery"),
        ("payment_last4", "Payment"), ("when", "When"), ("party", "Party"), ("price", "Price / deposit"),
        ("cancellation", "Cancellation"), ("attendees", "Attendees"), ("conflicts", "Conflicts"),
        ("arrival", "Arrival"), ("consequences", "What changes"), ("final_charge", "Final charge / refund"),
        ("effective", "Effective"), ("destination", "Destination"), ("target", "Goes live"),
        ("tests", "Tests"), ("changes", "Changes"), ("device", "Device"),
        ("items_description", "Items")]
    private var facts: some View {
        VStack(alignment: .leading, spacing: 8) {
            if card["type"].string == "book", let photo = details["photo_url"].url {
                ArtifactPhoto(url: photo, symbol: "calendar").frame(height: compact ? 90 : 140)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
            }
            ForEach(Self.fields, id: \.0) { key, label in
                if let value = details[key].string {
                    let shown = Text(AgentMarkdown.attributed(key == "payment_last4" ? "•••• \(value)" : value))
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                    if stacked {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(label).font(.caption).foregroundStyle(style.secondary)
                            shown
                        }
                    } else {
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            Text(label).font(.caption).foregroundStyle(style.secondary).frame(width: 82, alignment: .leading)
                            shown
                        }
                    }
                }
            }
            if card["type"].string == "pay" { amount("Total", key: "total", prominent: true) }
        }
    }
    private func amount(_ label: String, key: String, prominent: Bool = false) -> some View {
        let value = Text((details["currency"].string == "USD" ? "$" : (details["currency"].string ?? "") + " ") + (details[key].string ?? "—"))
            .monospacedDigit()
        return Group {
            if stacked {
                // The number on its own line, never broken inside (it shrinks before it wraps).
                VStack(alignment: .leading, spacing: 2) {
                    Text(label).foregroundStyle(prominent ? .primary : .secondary)
                    value.lineLimit(1).minimumScaleFactor(0.5)
                }
            } else {
                HStack {
                    Text(label).foregroundStyle(prominent ? .primary : .secondary)
                    Spacer(minLength: 12)
                    value
                }
            }
        }
        .font(prominent ? .title3.weight(.semibold) : .callout)
    }
    private var receipt: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(AgentMarkdown.attributed(card["receipt"]["summary"].string ?? "Confirmed"))
            if let reference = card["receipt"]["reference"].string {
                Text("Confirmation \(reference)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if let delivery = card["receipt"]["delivery"].string { Text(AgentMarkdown.attributed(delivery)).font(.callout) }
            if let track = card["receipt"]["track_url"].url {
                Link(destination: track) { Label("Track", systemImage: "shippingbox") }.font(.callout.weight(.semibold))
            }
        }
    }
}

/// Classic: its own faint box with a brass edge. Glass: the shared card surface (or none when the
/// host draws one around the card and its approval bar).
struct ConfirmSurface: ViewModifier {
    let style: CardStyle
    let hosted: Bool
    func body(content: Content) -> some View {
        if hosted {
            content
        } else if style.surface == .glass {
            content.cardSurface(style)
        } else {
            content.padding(16)
                .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Color.primary.opacity(0.035)))
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(JobLook.brass.opacity(0.28)))
        }
    }
}

struct ConfirmActionBar: View {
    let taskID: String
    let card: ViewCard
    @ObservedObject var model: VoicePanelModel
    @State private var editing = false
    @State private var draft = ""
    @State private var subject = ""
    @State private var saving = false
    @State private var editError: String?
    @Environment(\.cardStyle) private var style
    var body: some View {
        HStack(spacing: 12) {
            if card["type"].string == "send", card["state"].string == "pending" {
                Button("Edit") {
                    draft = card["details"]["body"].string ?? ""
                    subject = card["details"]["subject"].string ?? ""
                    editError = nil; editing = true
                }.buttonStyle(.borderless)
            }
            ForEach(card.actions) { action in
                let busy = model.cardActionBusy.contains("\(taskID)|\(card.viewIndex)|\(action.id)")
                if action.id == "cancel" {
                    Button { model.runCardAction(taskID: taskID, card: card, action: action) } label: {
                        Image(systemName: "xmark").frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("Cancel")
                    .disabled(busy)
                } else {
                    Button { model.runCardAction(taskID: taskID, card: card, action: action) } label: {
                        HStack {
                            if busy { ProgressView().controlSize(.small) }
                            Text(action.label).font(.body.weight(.semibold))
                        }.frame(maxWidth: .infinity, minHeight: 34)
                    }
                    .buttonStyle(.borderedProminent).tint(style.action).foregroundStyle(style.onAction)
                    .buttonBorderShape(style.capsHeaders ? .automatic : .capsule)
                    .disabled(busy).accessibilityIdentifier("confirm-approve")
                }
            }
        }
        .sheet(isPresented: $editing) {
            NavigationStack {
                VStack(alignment: .leading, spacing: 12) {
                    if card["details"]["subject"].string != nil {
                        TextField("Subject", text: $subject).textFieldStyle(.roundedBorder)
                    }
                    TextEditor(text: $draft).frame(minHeight: 180).accessibilityLabel("Full draft")
                    if let editError { Text(editError).foregroundStyle(.red).font(.caption) }
                }
                .padding().navigationTitle("Edit draft")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { editing = false }.disabled(saving) }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save draft") {
                            saving = true
                            var payload: [String: JSONValue] = ["sha256": card["sha256"], "body": .string(draft)]
                            if card["details"]["subject"].string != nil { payload["subject"] = .string(subject) }
                            let action = CardAction(id: "edit", label: "Save draft", value: .object(payload))
                            model.onCardAction(taskID, card, action) { ok, message in
                                saving = false
                                if ok { editing = false } else { editError = message }
                            }
                        }.disabled(saving || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
            }
            #if os(macOS)
            .frame(minWidth: 420, minHeight: 320)
            #endif
        }
    }
}

extension ConfirmCard {
    /// "Qty 1 · 60 in. · Walnut top, black frame" under a name that already says 60 in. and a
    /// description that already says the colour: each fact once.
    static func itemFacts(_ item: JSONValue) -> String {
        let said = [item["name"].string, item["description"].string].compactMap { $0 }.joined(separator: " ").lowercased()
        let facts = [item["size"].string, item["colour"].string].compactMap { $0 }
            // "Walnut / black" under "Walnut top, black frame" says it twice: every word already said goes.
            .filter { f in !said.contains(f.lowercased())
                && !f.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }).allSatisfy { said.contains($0) } }
        return ([item["quantity"].string.map { "Qty \($0)" }].compactMap { $0 } + facts).joined(separator: " · ")
    }
}
