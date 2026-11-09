import SwiftUI
import AppKit
import UniformTypeIdentifiers
import QuickLook
import SpeakeasyCore
import SpeakeasyClient

// Files your agent made (servers with `files`): the sidebar row and its pane. A grid of every file
// Hermes handed over in a reply, across all chats; click for Quick Look, drag out to Finder, and
// "Open chat" goes to the conversation it came from.

struct FilesSidebarRow: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var files: AgentFilesModel
    @State private var hover = false

    var body: some View {
        let on = store.showingFiles
        Button { store.openFiles() } label: {
            HStack(spacing: 10) {
                Circle().fill(on ? Live.brass.opacity(0.22) : Color.primary.opacity(0.06)).frame(width: 26, height: 26)
                    .overlay(Image(systemName: "doc.on.doc.fill").font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(on ? Live.brass : Color.primary.opacity(0.6)))
                VStack(alignment: .leading, spacing: 1) {
                    Text("Files").font(.system(size: 12.5, weight: .semibold))
                    Text(subtitle).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(on ? HistoryLook.selected : hover ? HistoryLook.hover : .clear))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(on ? Live.brass.opacity(0.32) : .clear, lineWidth: 0.75))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Files, \(subtitle)")
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier("history-files")
    }

    private var subtitle: String {
        if let newest = files.files.first { return "Newest: \(newest.name)" }
        return "What your agent made for you"
    }
}

struct FilesPane: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var files: AgentFilesModel
    @State private var preview: URL?

    private let columns = [GridItem(.adaptive(minimum: 168, maximum: 220), spacing: 14)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if files.showsChips { chips }
                content
            }
            .frame(maxWidth: 960, alignment: .leading)
            .padding(.horizontal, 28).padding(.top, 34).padding(.bottom, 24)
            .frame(maxWidth: .infinity)
        }
        .quickLookPreview($preview)
        .task { files.loadIfNeeded() }
        .accessibilityIdentifier("files-pane")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "doc.on.doc.fill").font(.system(size: 10, weight: .bold))
                Text("FILES").tracking(1.0)
            }
            .font(.system(size: 10.5, weight: .bold)).foregroundStyle(Live.brass)
            Text("Files your agent made").font(.system(size: 24, weight: .bold))
            Text("Everything Hermes sent you in a reply, from every chat. Click one to look at it, drag it to Finder to keep it.")
                .font(.system(size: 12.5, weight: .medium)).foregroundStyle(Color.primary.opacity(0.6))
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var chips: some View {
        HStack(spacing: 6) {
            chip(nil, "All")
            ForEach(files.kinds, id: \.id) { k in chip(k.id, k.label) }
        }
    }

    private func chip(_ id: String?, _ label: String) -> some View {
        let on = files.kind == id
        return Button { Task { await files.choose(kind: id) } } label: {
            Text(label).font(.system(size: 12, weight: .semibold))
                .padding(.horizontal, 11).frame(height: 26)
                .background(Capsule().fill(on ? Live.brass.opacity(0.22) : Color.primary.opacity(0.06)))
                .overlay(Capsule().strokeBorder(on ? Live.brass.opacity(0.4) : .clear, lineWidth: 0.75))
                .foregroundStyle(on ? Color.primary : Color.primary.opacity(0.7))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier("files-chip-\(id ?? "all")")
    }

    @ViewBuilder private var content: some View {
        if let error = files.error, files.files.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text(error).font(.system(size: 13)).foregroundStyle(.secondary)
                Button("Try again") { Task { await files.reload() } }
            }
            .padding(.top, 20)
        } else if !files.loaded || (files.files.isEmpty && files.serverScanning) {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Finding your files…").font(.system(size: 13)).foregroundStyle(.secondary)
            }
            .padding(.top, 20)
        } else if files.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("No files yet").font(.system(size: 15, weight: .semibold))
                Text("Files Hermes makes for you will collect here: pictures, documents, recordings and more.")
                    .font(.system(size: 13)).foregroundStyle(.secondary)
            }
            .padding(.top, 20)
            .accessibilityIdentifier("files-empty")
        } else {
            if let message = files.openError {
                Text(message).font(.system(size: 12.5)).foregroundStyle(Color.red.opacity(0.85))
                    .onTapGesture { files.openError = nil }
            }
            LazyVGrid(columns: columns, alignment: .leading, spacing: 14) {
                ForEach(files.files) { file in
                    FileCard(file: file, files: files, now: store.now) { open(file) } openChat: { store.openChat(of: file) }
                        .task { await files.thumbnail(for: file) }
                        .onAppear { if file.id == files.files.last?.id { Task { await files.loadMore() } } }
                }
            }
        }
    }

    private func open(_ file: AgentFile) {
        Task { if let url = await files.localCopy(file) { preview = url } }
    }
}

private struct FileCard: View {
    let file: AgentFile
    @ObservedObject var files: AgentFilesModel
    let now: Date
    var open: () -> Void
    var openChat: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 8) {
                tile
                VStack(alignment: .leading, spacing: 2) {
                    Text(file.name).font(.system(size: 12.5, weight: .semibold)).lineLimit(2).truncationMode(.middle)
                    Text(file.detail(now: now)).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(hover ? HistoryLook.hover : Color.primary.opacity(0.025)))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.primary.opacity(0.07), lineWidth: 0.75))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .onDrag { dragProvider() }
        .contextMenu {
            Button("Quick Look") { open() }
            Button("Open") { Task { if let url = await files.localCopy(file) { NSWorkspace.shared.open(url) } } }
            Button("Save to Downloads") { Task { await saveToDownloads() } }
            if !file.sessionID.isEmpty { Divider(); Button("Open chat") { openChat() } }
        }
        .help("\(file.name) · \(file.detail(now: now))")
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(file.typeWord), \(file.name), \(file.detail(now: now))")
        .accessibilityIdentifier("files-card-\(file.id)")
    }

    private var tile: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06))
            if let data = files.thumbs[file.id], let image = NSImage(data: data) {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                VStack(spacing: 6) {
                    Image(systemName: file.symbol).font(.system(size: 26, weight: .light)).foregroundStyle(Live.brass)
                    Text(file.typeWord).font(.system(size: 10, weight: .bold)).tracking(0.6).foregroundStyle(.secondary)
                }
            }
            if files.opening == file.id {
                Color.black.opacity(0.25)
                ProgressView().controlSize(.small)
            }
        }
        .frame(height: 118)
        .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
    }

    /// Drag out to Finder: the local copy when it's here, else fetched when dropped.
    private func dragProvider() -> NSItemProvider {
        if let url = AgentFileStore.cached(file), let provider = NSItemProvider(contentsOf: url) { return provider }
        let provider = NSItemProvider()
        provider.suggestedName = file.name
        let type = UTType(filenameExtension: file.fileExtension)?.identifier ?? UTType.data.identifier
        let files = self.files, file = self.file
        provider.registerFileRepresentation(forTypeIdentifier: type, fileOptions: [], visibility: .all) { done in
            Task { @MainActor in
                if let url = await files.localCopy(file) { done(url, false, nil) }
                else { done(nil, false, CocoaError(.fileReadUnknown)) }
            }
            return nil
        }
        return provider
    }

    private func saveToDownloads() async {
        guard let url = await files.localCopy(file),
              let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first else { return }
        var target = downloads.appendingPathComponent(file.safeName)
        let base = target.deletingPathExtension().lastPathComponent, ext = target.pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: target.path) {
            target = downloads.appendingPathComponent(ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)")
            n += 1
        }
        do {
            try FileManager.default.copyItem(at: url, to: target)
            NSWorkspace.shared.activateFileViewerSelecting([target])
        } catch {
            files.openError = "Couldn't save it to Downloads."
        }
    }
}
