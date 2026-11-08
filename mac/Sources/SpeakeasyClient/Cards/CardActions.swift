import SwiftUI
import SpeakeasyCore
#if os(macOS)
import AppKit
#else
import UIKit
#endif

/// The buttons under a card (contract §2): the server's own actions (only on servers with
/// `card_actions`), then what the app can do by itself: open a place or route in Maps, copy.
/// A tap runs the action and the result comes back as a toast.
public struct CardActionBar: View {
    var taskID: String
    var card: ViewCard
    @ObservedObject var model: VoicePanelModel
    @State private var confirming: CardAction?
    @State private var copied = false
    @Environment(\.openURL) private var openURL
    @Environment(\.cardStyle) private var style

    public init(taskID: String, card: ViewCard, model: VoicePanelModel) {
        self.taskID = taskID; self.card = card; self.model = model
    }

    /// Whether this card shows any buttons at all.
    @MainActor public static func hasActions(_ card: ViewCard, model: VoicePanelModel) -> Bool {
        !serverActions(card, model: model).isEmpty || MapsLink.url(card) != nil && mapsKinds.contains(card.kind)
            || copies(card) || !CardLinks.links(card).isEmpty
    }
    /// The server's buttons the row draws (a card that places its own, like a day's Move, keeps them).
    @MainActor static func serverActions(_ card: ViewCard, model: VoicePanelModel) -> [CardAction] {
        model.capabilities?.cardActions == true && !CardLinks.inCardActions.contains(card.kind) ? card.actions : []
    }
    /// Copy is offered under cards whose text is worth pasting (a chart copies its numbers; a stats grid
    /// alone under a lone Copy read as filler, SPK-231 reps).
    /// DEBUG `-demoNoCardCopy`: none, for website films that frame a chart on its own.
    static func copies(_ card: ViewCard) -> Bool {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-demoNoCardCopy") { return false }
        #endif
        return copyKinds.contains(card.kind)
    }
    static let mapsKinds: Set<String> = ["place", "route", "transit"]
    static let copyKinds: Set<String> = ["chart", "list", "steps", "fact", "conversion", "math", "translation",
                                         "definition", "comparison", "nutrition", "message_draft", "draft"]

    public var body: some View {
        let server = Self.serverActions(card, model: model)
        HStack(spacing: style.capsHeaders ? 6 : 8) {
            ForEach(server) { action in
                let busy = model.cardActionBusy.contains("\(taskID)|\(card.viewIndex)|\(action.id)")
                CardButton(title: action.label, style: action.style, busy: busy) {
                    if action.confirm { confirming = action } else { model.runCardAction(taskID: taskID, card: card, action: action) }
                }
                .disabled(busy)
            }
            ForEach(CardLinks.links(card)) { link in
                CardButton(title: link.label, icon: link.symbol, style: .secondary) { openURL(link.url) }
            }
            if Self.mapsKinds.contains(card.kind), let url = MapsLink.url(card) {
                CardButton(title: card.kind == "place" ? "Open in Maps" : "Directions", icon: "map", style: .secondary) {
                    openURL(url)
                }
            }
            if Self.copies(card) {
                CardButton(title: copied ? "Copied" : "Copy", icon: copied ? "checkmark" : "doc.on.doc", style: .secondary) {
                    Self.copy(CardText.plain(card))
                    copied = true
                    model.showToast("Copied")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { copied = false }
                }
            }
            if style.capsHeaders { Spacer(minLength: 0) }
        }
        .confirmationDialog(confirming.map { Self.confirmTitle($0, card) } ?? "", isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }), titleVisibility: .visible) {
            if let action = confirming {
                Button(action.label, role: action.style == .destructive ? .destructive : nil) {
                    model.runCardAction(taskID: taskID, card: card, action: action)
                }
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    /// The question a confirming button asks: what will happen, in plain words.
    public static func confirmTitle(_ action: CardAction, _ card: ViewCard) -> String {
        confirmTitle(id: action.id, label: action.label, card)
    }
    public static func confirmTitle(id: String, label: String, _ card: ViewCard) -> String {
        if card.kind == "draft", id == "send" {
            return "Send this \(card["channel"].string == "email" ? "email" : "message") to \(card["to"].string ?? "them")?"
        }
        if card.kind == "compare", id == "go" { return label + "?" }
        return label
    }

    static func copy(_ text: String) {
        #if os(macOS)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #else
        UIPasteboard.general.string = text
        #endif
    }
}

struct CardButton: View {
    var title: String
    var icon: String? = nil
    var style: CardAction.Style
    var busy = false
    var action: () -> Void
    @State private var hover = false
    @Environment(\.cardStyle) private var look

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if busy { ProgressView().controlSize(.mini) }
                else if let icon { Image(systemName: icon).font(.system(size: look.capsHeaders ? 10.5 : 13, weight: .semibold)) }
                Text(title).font(.system(size: look.capsHeaders ? 11.5 : 15, weight: .semibold)).lineLimit(1)
            }
            .padding(.horizontal, look.capsHeaders ? 10 : 14).frame(height: look.capsHeaders ? 26 : 40)
            .frame(maxWidth: look.capsHeaders ? nil : .infinity)
            .foregroundStyle(fg)
            .background(Capsule().fill(bg.opacity(hover ? 1 : 0.85)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .accessibilityLabel(title)
    }
    private var fg: Color {
        switch style {
        case .primary: return look.onAction
        case .destructive: return look.capsHeaders ? Color(red: 1, green: 0.62, blue: 0.58) : look.bad
        case .secondary: return look.quietInk
        }
    }
    private var bg: Color {
        switch style {
        case .primary: return look.action
        case .destructive: return look.capsHeaders ? JobStyle.stopRed.opacity(0.3) : look.bad.opacity(0.16)
        case .secondary: return look.quietFill
        }
    }
}

/// A card with its buttons under it.
public struct ActionableCard: View {
    var taskID: String
    var card: ViewCard
    @ObservedObject var model: VoicePanelModel
    public init(taskID: String, card: ViewCard, model: VoicePanelModel) { self.taskID = taskID; self.card = card; self.model = model }
    @Environment(\.cardStyle) private var style
    public var body: some View {
        // Glass looks: the card and its buttons share one surface. Flat (the Mac): buttons under it.
        let hosted = style.surface == .glass && card.kind != "question"
        VStack(alignment: .leading, spacing: hosted ? 14 : 6) {
            ViewCardView(card)
                .environment(\.questionActions, model.questionActions(for: taskID))
                .environment(\.cardHosted, hosted)
                .environment(\.cardRunAction, model.capabilities?.cardActions == true ? { id in
                    if let action = card.actions.first(where: { $0.id == id }) { model.runCardAction(taskID: taskID, card: card, action: action) }
                } : nil)
            if card.kind == "confirm", model.capabilities?.cardActions == true {
                ConfirmActionBar(taskID: taskID, card: card, model: model)
            } else if card.kind != "question", CardActionBar.hasActions(card, model: model) {
                CardActionBar(taskID: taskID, card: card, model: model)
            }
        }
        .cardSurface(style, hosted: !hosted)
    }
}

/// "rain until 3 PM" → "Rain until 3 PM": only the first letter changes (`.capitalized` gave "3 Pm").
public func sentenceCase(_ text: String) -> String {
    guard let first = text.first else { return text }
    return first.uppercased() + text.dropFirst()
}
