import SwiftUI
import SpeakeasyCore

/// One generous picture, or a snapping gallery. The host supplies authenticated bytes; paths
/// and credentials never enter the view. Missing images take no space.
public struct PictureGallery: View {
    public let images: [ImageCard]
    public let identity: String
    public var height: CGFloat
    public let load: (Int) async -> Data?
    @State private var loaded: [Int: Image] = [:]
    @State private var page: Int?
    /// Which picture the full-screen viewer shows. Kept apart from `showing` so paging inside the
    /// viewer never changes the sheet's identity (that dismissed and re-presented it on every swipe).
    @State private var viewing: Int?
    @State private var showing = false
    /// The reply the pictures were last loaded for; a refresh of the same reply keeps an open viewer.
    @State private var loadedFor: String?

    public init(images: [ImageCard], identity: String, height: CGFloat = 220,
                load: @escaping (Int) async -> Data?) {
        self.images = Array(images.prefix(6)); self.identity = identity
        self.height = height; self.load = load
    }

    public var body: some View {
        let visible = images.filter { loaded[$0.number] != nil }
        VStack(spacing: 8) {
            if !visible.isEmpty {
                ScrollView(.horizontal) {
                    LazyHStack(spacing: 10) {
                        ForEach(visible) { card in
                            Button { viewing = card.number; showing = true } label: {
                                (loaded[card.number] ?? Image(systemName: "photo")).resizable().scaledToFit()
                                    .frame(maxWidth: .infinity).frame(height: height)
                                    .background(Color.primary.opacity(0.04))
                                    .clipShape(RoundedRectangle(cornerRadius: 16))
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(card.name)
                            .accessibilityHint("Opens the full picture")
                            .containerRelativeFrame(.horizontal) { width, _ in
                                visible.count == 1 ? width : width * 0.9
                            }
                            .id(card.number)
                        }
                    }
                    .scrollTargetLayout()
                }
                .scrollIndicators(.hidden)
                .scrollTargetBehavior(.viewAligned)
                .scrollPosition(id: $page)
                .frame(height: height)
                .accessibilityIdentifier("picture-gallery")
                if visible.count > 1 {
                    HStack(spacing: 5) {
                        ForEach(visible) { card in
                            Capsule().fill(card.number == (page ?? visible[0].number) ? Color.primary : Color.primary.opacity(0.2))
                                .frame(width: card.number == (page ?? visible[0].number) ? 14 : 5, height: 5)
                        }
                    }.accessibilityHidden(true)
                }
            }
        }
        .task(id: identity + images.map { String($0.number) }.joined(separator: "/")) {
            let numbers = Set(images.map(\.number))
            if loadedFor == identity {
                // Same reply, its picture list changed: keep what's still there, and the viewer open on it.
                loaded = loaded.filter { numbers.contains($0.key) }
                if let n = viewing, !numbers.contains(n) { showing = false; viewing = nil }
                if let p = page, !numbers.contains(p) { page = nil }
            } else {
                loaded = [:]; page = nil; showing = false; viewing = nil
            }
            loadedFor = identity
            for card in images where loaded[card.number] == nil {
                guard !Task.isCancelled, let data = await load(card.number) else { continue }
                let image = await PictureDecode.image(data)
                guard !Task.isCancelled else { return }
                if let image { loaded[card.number] = image }
            }
        }
        .sheet(isPresented: $showing, onDismiss: {
            // Leave the inline gallery on the picture the person was last looking at.
            if let n = viewing, visible.contains(where: { $0.number == n }) { page = n }
        }) {
            let current = visible.first { $0.number == viewing } ?? visible.first
            NavigationStack {
                TabView(selection: Binding(get: { viewing ?? current?.number ?? 0 }, set: { viewing = $0 })) {
                    ForEach(visible) { card in
                        if let image = loaded[card.number] {
                            image.resizable().scaledToFit().tag(card.number)
                        }
                    }
                }
                #if os(iOS)
                .tabViewStyle(.page)
                #endif
                .background(Color.black)
                .navigationTitle(current?.name ?? "")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { showing = false } } }
            }
            .frame(minWidth: 280, minHeight: 300)
            .preferredColorScheme(.dark)
        }
    }
}
