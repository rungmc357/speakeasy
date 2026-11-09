import SwiftUI
#if os(iOS)
import UIKit
#endif
import SpeakeasyCore

/// Home's quiet island. Opening it reveals one live item at a time, never a second board.
public struct NowWorkStrip: View {
    public let items: [NowItem]
    public let background: [NowBackgroundItem]
    public var onOpen: (NowItem) -> Void
    public var onBackground: (NowBackgroundItem) -> Void
    public var onBoard: () -> Void
    /// The plugin's pill words ("3 need you · 5 running"); nil: counted here.
    public var pill: String?
    /// Home's top line (SPK-259): the strip is only the pill; the host draws `NowWorkStripPanel` over
    /// the thread while it's open (`onExpandedChange`), so opening it never moves the conversation.
    public var dropDown = false
    /// Bumped by the host to fold an open strip (the thread scrolled, a tap outside).
    public var collapseToken = 0
    /// Tells the host the strip opened or folded (to catch a tap outside it).
    public var onExpandedChange: ((Bool) -> Void)?
    @State private var expanded = false
    @State private var pull: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion


    public init(items: [NowItem], background: [NowBackgroundItem], onOpen: @escaping (NowItem) -> Void,
                onBackground: @escaping (NowBackgroundItem) -> Void, onBoard: @escaping () -> Void, pill: String? = nil,
                dropDown: Bool = false, collapseToken: Int = 0, onExpandedChange: ((Bool) -> Void)? = nil) {
        self.pill = pill
        self.dropDown = dropDown; self.collapseToken = collapseToken; self.onExpandedChange = onExpandedChange
        self.items = items; self.background = background; self.onOpen = onOpen
        self.onBackground = onBackground; self.onBoard = onBoard
    }
    private var live: [NowItem] { items.filter { [.question, .working, .waiting, .stopped].contains($0.kind) } }
    private var needs: Int { live.filter { $0.kind == .question }.count }
    private var running: Int { live.filter { $0.kind == .working }.count + background.count }
    private var keys: [String] { live.map { "i:" + $0.id } + background.map { "b:" + $0.id } }
    /// What the pill says, read as one control.
    var pillLabel: String {
        if let pill { return pill }
        let counts = running > 0 ? "\(running) running" : "Work"
        return needs > 0 ? "\(counts) · \(needs) needs you" : counts
    }

    public var body: some View {
        if !keys.isEmpty {
            VStack(spacing: 8) {
                ViewThatFits(in: .horizontal) {
                    if let pill {
                        // One line at any volume: the counts, never a cut-off title.
                        HStack(spacing: 8) {
                            Text(pill).fixedSize().foregroundStyle(needs > 0 ? Color.accentColor : Color.primary)
                            Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2.weight(.semibold)).accessibilityHidden(true)
                        }
                    }
                    HStack(spacing: 8) {
                        Text(running > 0 ? "\(running) running" : "Work").fixedSize()
                        if needs > 0 { Text("· \(needs) needs you").foregroundStyle(Color.accentColor).fixedSize() }
                        Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2.weight(.semibold)).accessibilityHidden(true)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(running > 0 ? "\(running) running" : "Work").fixedSize()
                            Spacer(minLength: 0)
                            Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2.weight(.semibold)).accessibilityHidden(true)
                        }
                        if needs > 0 { Text("\(needs) needs you").foregroundStyle(Color.accentColor).fixedSize(horizontal: false, vertical: true) }
                    }.padding(.vertical, 6)
                }
                .font(.footnote.weight(.semibold)).padding(.horizontal, 16).frame(minHeight: 36)
                .contentShape(Capsule())
                .onTapGesture { toggle() }
                .gesture(DragGesture(minimumDistance: 10).onChanged { value in
                    guard abs(value.translation.height) > abs(value.translation.width) else { return }
                    pull = max(0, min(value.translation.height, 156))
                }.onEnded { value in
                    let vertical = abs(value.translation.height) > abs(value.translation.width)
                    let open = vertical && value.translation.height > 120
                    withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.9)) {
                        if open { expanded = false }
                        else if vertical { expanded = value.translation.height > 35 }
                        pull = 0
                    }
                    if open { onBoard() }
                })
                // One control: its words, not a second "Go Down" element for the chevron. Combined
                // (not ignored) so the element keeps its children's on-screen frame under the
                // scroll-edge bar; an ignored container reported a frame below the drawn pill.
                .accessibilityElement(children: .combine)
                .accessibilityLabel(pillLabel)
                .accessibilityAddTraits(.isButton)
                .accessibilityAction { toggle() }
                .accessibilityIdentifier("home-work-capsule")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                #if os(iOS)
                // Typing needs the thread: an open strip folds back to the pill when the keyboard
                // comes up, so it never covers the conversation above the composer.
                .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
                    if expanded { withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.9)) { expanded = false } }
                }
                #endif
                if !dropDown && (expanded || pull > 0) {
                    NowWorkStripPanel(items: items, background: background, onOpen: onOpen, onBackground: onBackground,
                                      onBoard: onBoard, onClose: { expanded = false })
                    .frame(height: expanded ? nil : pull, alignment: .top)
                    .clipped()
                }
            }
            .frame(maxWidth: .infinity)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: expanded ? CleanGrid.cardRadius : 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: expanded ? CleanGrid.cardRadius : 18, style: .continuous).strokeBorder(Color.primary.opacity(0.08)))
            .onChange(of: collapseToken) { _, _ in
                if expanded { withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.9)) { expanded = false } }
            }
            .onChange(of: expanded) { _, open in onExpandedChange?(open) }
        }
    }
    private func toggle() {
        withAnimation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 0.88)) { expanded.toggle() }
    }
}

/// The open strip: one live item per page, its dots, and Open Work. Inside the pill's card, or on its
/// own as Home's drop-down over the thread (SPK-259).
public struct NowWorkStripPanel: View {
    public let items: [NowItem]
    public let background: [NowBackgroundItem]
    public var onOpen: (NowItem) -> Void
    public var onBackground: (NowBackgroundItem) -> Void
    public var onBoard: () -> Void
    /// Fold the strip (before any of the actions above).
    public var onClose: () -> Void
    @State private var page: String?

    public init(items: [NowItem], background: [NowBackgroundItem], onOpen: @escaping (NowItem) -> Void,
                onBackground: @escaping (NowBackgroundItem) -> Void, onBoard: @escaping () -> Void,
                onClose: @escaping () -> Void) {
        self.items = items; self.background = background; self.onOpen = onOpen
        self.onBackground = onBackground; self.onBoard = onBoard; self.onClose = onClose
    }
    private var live: [NowItem] { items.filter { [.question, .working, .waiting, .stopped].contains($0.kind) } }
    private var keys: [String] { live.map { "i:" + $0.id } + background.map { "b:" + $0.id } }

    public var body: some View {
        VStack(spacing: 8) {
            ScrollView(.horizontal) {
                HStack(spacing: 0) {
                    ForEach(live) { item in
                        Button {
                            onClose(); onOpen(item)
                        } label: {
                            stripPage(title: item.title, line: item.line,
                                      source: item.kind == .question ? "Needs you" : item.sourceLabel ?? "This conversation",
                                      image: item.thumbs.first)
                        }
                        .buttonStyle(.plain).containerRelativeFrame(.horizontal).id("i:" + item.id)
                        .accessibilityIdentifier("home-work-item")
                    }
                    ForEach(background) { item in
                        Button { onClose(); onBackground(item) } label: {
                            stripPage(title: item.title, line: item.line, source: item.sourceLabel ?? "Background", image: nil)
                        }
                        .buttonStyle(.plain).containerRelativeFrame(.horizontal).id("b:" + item.id)
                        .accessibilityIdentifier("home-work-item")
                    }
                }.scrollTargetLayout()
            }
            // Leading anchors: Home's thread sets a bottom default anchor that this pager would
            // otherwise inherit, which left it showing the end of one page and the start of
            // the next (seen live on the strip).
            .scrollTargetBehavior(.paging).scrollPosition(id: $page, anchor: .leading)
            .defaultScrollAnchor(.leading).scrollIndicators(.hidden)
            .frame(height: 92)
            HStack(spacing: 5) {
                ForEach(keys, id: \.self) { key in
                    Circle().fill(key == (page ?? keys.first) ? Color.primary : Color.primary.opacity(0.2))
                        .frame(width: 5, height: 5)
                }
            }.accessibilityHidden(true)
            Button("Open Work") { onClose(); onBoard() }.font(.footnote.weight(.medium))
                .buttonStyle(.plain).accessibilityIdentifier("home-work-board")
                .padding(.bottom, 12)
        }
        .onChange(of: keys) { _, keys in if let page, !keys.contains(page) { self.page = keys.first } }
    }

    private func stripPage(title: String, line: String, source: String, image: String?) -> some View {
        HStack(spacing: CleanGrid.glyphGap) {
            if let image, let url = URL(string: image) {
                AsyncImage(url: url) { state in
                    if let image = state.image { image.resizable().scaledToFill() }
                    else { Image(systemName: "rectangle.stack").foregroundStyle(.secondary) }
                }.frame(width: 48, height: 56).clipShape(RoundedRectangle(cornerRadius: CleanGrid.tileRadius, style: .continuous))
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(source).font(.caption).foregroundStyle(.secondary)
                Text(AgentMarkdown.attributed(title)).font(.subheadline.weight(.semibold)).lineLimit(1)
                if !line.isEmpty {   // before a job's first real step: its title alone
                    // One line that changes in place, never two lines crossfading (SPK-231 R4).
                    LiveStatusLine(line, font: .footnote, color: .secondary)
                }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, CleanGrid.cardPadding).frame(maxWidth: .infinity, minHeight: 88, alignment: .leading)
        .contentShape(Rectangle())
    }
}
