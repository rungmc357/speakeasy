import AppKit
import SwiftUI
import SpeakeasyCore

/// SPK-214: the pictures and files an answer handed over (its `MEDIA:` lines), under the answer in
/// the thread instead of the raw path. Thumbnails and downloads go through the Files feature
/// (`/voice/files/{id}`: the same ids and checks as the Files pane); a click opens the file.
struct HistoryMessageFiles: View {
    let files: [AgentFile]
    @ObservedObject var files_: AgentFilesModel

    private var pictures: [AgentFile] { files.filter { $0.kind == .image } }
    private var others: [AgentFile] { files.filter { $0.kind != .image } }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !pictures.isEmpty {
                HStack(spacing: 8) {
                    ForEach(pictures.prefix(4)) { picture($0, height: pictures.count == 1 ? 240 : 150) }
                }
                .frame(maxWidth: pictures.count == 1 ? 420 : 640, alignment: .leading)
            }
            ForEach(others) { tile($0) }
            if let message = files_.openError {
                Text(message).font(.system(size: 11)).foregroundStyle(.orange)
            }
        }
    }

    private func picture(_ file: AgentFile, height: CGFloat) -> some View {
        Button { open(file) } label: {
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.06))
                if let data = files_.thumbs[file.id], let image = NSImage(data: data) {
                    Color.clear.overlay(Image(nsImage: image).resizable().scaledToFill())
                } else {
                    Image(systemName: "photo").font(.system(size: 22, weight: .light)).foregroundStyle(.secondary)
                }
                if files_.opening == file.id { Color.black.opacity(0.25); ProgressView().controlSize(.small) }
            }
            .frame(height: height).frame(maxWidth: .infinity)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(file.name)
        .task { await files_.thumbnail(for: file) }
        .accessibilityLabel("Picture, \(file.name)")
    }

    private func tile(_ file: AgentFile) -> some View {
        Button { open(file) } label: {
            HStack(spacing: 8) {
                Image(systemName: file.symbol).foregroundStyle(.secondary)
                Text(file.name).font(.system(size: 12.5, weight: .medium)).lineLimit(1)
                Text(AgentFileFormat.size(file.bytes)).font(.system(size: 11)).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.06)))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(file.typeWord), \(file.name)")
    }

    private func open(_ file: AgentFile) {
        Task { if let url = await files_.localCopy(file) { NSWorkspace.shared.open(url) } }
    }
}
