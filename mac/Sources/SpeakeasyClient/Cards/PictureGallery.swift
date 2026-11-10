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
    @State private var expanded: ImageCard?

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
                            Button { expanded = card } label: {
                                loaded[card.number]!.resizable().scaledToFit()
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
            loaded = [:]; page = nil; expanded = nil
            for card in images {
                guard !Task.isCancelled, let data = await load(card.number) else { continue }
                let image = await PictureDecode.image(data)
                guard !Task.isCancelled else { return }
                if let image { loaded[card.number] = image }
            }
        }
        .sheet(item: $expanded) { selected in
            NavigationStack {
                TabView(selection: Binding(get: { expanded?.number ?? selected.number }, set: { n in
                    expanded = visible.first { $0.number == n }
                })) {
                    ForEach(visible) { card in
                        loaded[card.number]!.resizable().scaledToFit().tag(card.number)
                    }
                }
                #if os(iOS)
                .tabViewStyle(.page)
                #endif
                .background(Color.black)
                .navigationTitle(selected.name)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { expanded = nil } } }
            }
            .frame(minWidth: 280, minHeight: 300)
            .preferredColorScheme(.dark)
        }
    }
}
