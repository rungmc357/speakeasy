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

    public init(taskID: String, card: ViewCard, model: VoicePanelModel) {
        self.taskID = taskID; self.card = card; self.model = model
    }

    /// Whether this card shows any buttons at all.
    @MainActor public static func hasActions(_ card: ViewCard, model: VoicePanelModel) -> Bool {
        (model.capabilities?.cardActions == true && !card.actions.isEmpty) || MapsLink.url(card) != nil && mapsKinds.contains(card.kind)
            || copies(card)
    }
    /// Copy is offered under cards whose text is worth pasting (a chart copies its numbers).
    /// DEBUG `-demoNoCardCopy`: none, for website films that frame a chart on its own.
    static func copies(_ card: ViewCard) -> Bool {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-demoNoCardCopy") { return false }
        #endif
        return copyKinds.contains(card.kind)
    }
    static let mapsKinds: Set<String> = ["place", "route", "transit"]
    static let copyKinds: Set<String> = ["chart", "stats", "list", "steps", "fact", "conversion", "math", "translation",
                                         "definition", "comparison", "nutrition", "message_draft", "draft"]

    public var body: some View {
        let server = model.capabilities?.cardActions == true ? card.actions : []
        HStack(spacing: 6) {
            ForEach(server) { action in
                let busy = model.cardActionBusy.contains("\(taskID)|\(card.viewIndex)|\(action.id)")
                CardButton(title: action.label, style: action.style, busy: busy) {
                    if action.confirm { confirming = action } else { model.runCardAction(taskID: taskID, card: card, action: action) }
                }
                .disabled(busy)
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
            Spacer(minLength: 0)
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

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if busy { ProgressView().controlSize(.mini) }
                else if let icon { Image(systemName: icon).font(.system(size: 10.5, weight: .semibold)) }
                Text(title).font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
            }
            .padding(.horizontal, 10).frame(height: 26)
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
        case .primary: return JobStyle.onBlue
        case .destructive: return Color(red: 1, green: 0.62, blue: 0.58)
        case .secondary: return JobStyle.blueInk
        }
    }
    private var bg: Color {
        switch style {
        case .primary: return JobStyle.blue
        case .destructive: return JobStyle.stopRed.opacity(0.3)
        case .secondary: return JobStyle.blueInk.opacity(0.14)
        }
    }
}

/// A card with its buttons under it.
public struct ActionableCard: View {
    var taskID: String
    var card: ViewCard
    @ObservedObject var model: VoicePanelModel
    public init(taskID: String, card: ViewCard, model: VoicePanelModel) { self.taskID = taskID; self.card = card; self.model = model }
    public var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ViewCardView(card).environment(\.questionActions, model.questionActions(for: taskID))
            if card.kind != "question", CardActionBar.hasActions(card, model: model) {
                CardActionBar(taskID: taskID, card: card, model: model)
            }
        }
    }
}

/// "rain until 3 PM" → "Rain until 3 PM": only the first letter changes (`.capitalized` gave "3 Pm").
public func sentenceCase(_ text: String) -> String {
    guard let first = text.first else { return text }
    return first.uppercased() + text.dropFirst()
}
