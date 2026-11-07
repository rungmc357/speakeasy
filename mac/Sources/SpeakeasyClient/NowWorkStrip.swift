import SwiftUI
import SpeakeasyCore

/// Home's quiet island. Opening it reveals one live item at a time, never a second board.
public struct NowWorkStrip: View {
    public let items: [NowItem]
    public let background: [NowBackgroundItem]
    public var onOpen: (NowItem) -> Void
    public var onBackground: (NowBackgroundItem) -> Void
    public var onBoard: () -> Void
    @State private var expanded = false
    @State private var page: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion


    public init(items: [NowItem], background: [NowBackgroundItem], onOpen: @escaping (NowItem) -> Void,
                onBackground: @escaping (NowBackgroundItem) -> Void, onBoard: @escaping () -> Void) {
        self.items = items; self.background = background; self.onOpen = onOpen
        self.onBackground = onBackground; self.onBoard = onBoard
    }
    private var live: [NowItem] { items.filter { [.question, .working, .waiting, .stopped].contains($0.kind) } }
    private var needs: Int { live.filter { $0.kind == .question }.count }
    private var running: Int { live.filter { $0.kind == .working }.count + background.count }
    private var keys: [String] { live.map { "i:" + $0.id } + background.map { "b:" + $0.id } }

    public var body: some View {
        if !keys.isEmpty {
            VStack(spacing: 8) {
                Button { toggle() } label: {
                    HStack(spacing: 8) {
                        Circle().fill(needs > 0 ? Color.accentColor : Color.secondary).frame(width: 6, height: 6)
                        Text(running > 0 ? "\(running) running" : "Work")
                        if needs > 0 { Text("· \(needs) needs you").foregroundStyle(Color.accentColor) }
                        Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2.weight(.semibold))
                    }
                    .font(.footnote.weight(.semibold)).padding(.horizontal, 16).frame(minHeight: 36)
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain).accessibilityIdentifier("home-work-capsule")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                if expanded {
                    ScrollView(.horizontal) {
                        HStack(spacing: 0) {
                            ForEach(live) { item in
                                Button {
                                    expanded = false; onOpen(item)
                                } label: {
                                    stripPage(title: item.title, line: item.line,
                                              source: item.kind == .question ? "Needs you" : item.sourceLabel ?? "This conversation",
                                              image: item.thumbs.first)
                                }
                                .buttonStyle(.plain).containerRelativeFrame(.horizontal).id("i:" + item.id)
                                .accessibilityIdentifier("home-work-item")
                            }
                            ForEach(background) { item in
                                Button { expanded = false; onBackground(item) } label: {
                                    stripPage(title: item.title, line: item.line, source: item.sourceLabel ?? "Background", image: nil)
                                }
                                .buttonStyle(.plain).containerRelativeFrame(.horizontal).id("b:" + item.id)
                                .accessibilityIdentifier("home-work-item")
                            }
                        }.scrollTargetLayout()
                    }
                    .scrollTargetBehavior(.paging).scrollPosition(id: $page).scrollIndicators(.hidden)
                    .frame(height: 92)
                    HStack(spacing: 5) {
                        ForEach(keys, id: \.self) { key in
                            Circle().fill(key == (page ?? keys.first) ? Color.primary : Color.primary.opacity(0.2))
                                .frame(width: 5, height: 5)
                        }
                    }.accessibilityHidden(true)
                    Button("Open Work") { expanded = false; onBoard() }.font(.footnote.weight(.medium))
                        .buttonStyle(.plain).accessibilityIdentifier("home-work-board")
                        .padding(.bottom, 12)
                }
            }
            .frame(maxWidth: .infinity)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: expanded ? 24 : 18))
            .overlay(RoundedRectangle(cornerRadius: expanded ? 24 : 18).strokeBorder(Color.primary.opacity(0.08)))
            .onChange(of: keys) { _, keys in if let page, !keys.contains(page) { self.page = keys.first } }
        }
    }
    private func toggle() {
        withAnimation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 0.88)) { expanded.toggle() }
    }
    private func stripPage(title: String, line: String, source: String, image: String?) -> some View {
        HStack(spacing: 10) {
            if let image, let url = URL(string: image) {
                AsyncImage(url: url) { state in
                    if let image = state.image { image.resizable().scaledToFill() }
                    else { Image(systemName: "rectangle.stack").foregroundStyle(.secondary) }
                }.frame(width: 48, height: 56).clipShape(RoundedRectangle(cornerRadius: 10))
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(source).font(.caption).foregroundStyle(.secondary)
                Text(AgentMarkdown.attributed(title)).font(.subheadline.weight(.semibold)).lineLimit(1)
                Text(AgentMarkdown.attributed(line)).font(.footnote).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14).frame(maxWidth: .infinity, minHeight: 88, alignment: .leading)
        .contentShape(Rectangle())
    }
}
