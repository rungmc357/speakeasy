import AppKit
import SpeakeasyClient
import SpeakeasyCore
import UniformTypeIdentifiers

/// Pictures and files dropped on or pasted into the call panel ("Look at this"). Reads a pasteboard
/// (Finder files, file promises from Photos, Mail and browsers, or raw image data such as a copied
/// screenshot), encodes each item off the main thread with `AttachmentEncoder`, and hands the results
/// to the panel model: `onAttach` for what encoded, `onAttachRefused` (a short status line) for what
/// didn't. Promised files land in a private temporary folder that's removed once they're encoded;
/// nothing else touches the disk.
@MainActor
enum PanelAttachments {
    /// Raw picture types, read only when the pasteboard carries no files.
    static let imageTypes: [NSPasteboard.PasteboardType] = [
        .png, .tiff, NSPasteboard.PasteboardType(UTType.jpeg.identifier), NSPasteboard.PasteboardType(UTType.heic.identifier),
    ]

    /// What the panel registers for as a drop destination. Web links are taken too, only to say
    /// they can't be sent (like folders and apps).
    static var draggedTypes: [NSPasteboard.PasteboardType] {
        [.fileURL] + NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) } + imageTypes + [.URL]
    }

    /// The panel takes pictures and files whenever it's up and this Hermes reads pictures: during a
    /// call (a paused one too: they go after Resume), or with none open (after one ended, on the
    /// board), when they wait on the panel and go with the next call.
    static func accepting(_ model: VoicePanelModel) -> Bool {
        model.attachmentsSupported
    }

    private static let fileOptions: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]

    /// The pasteboard holds a file, a promised file, a picture or a link. Not all of them can be sent
    /// (a folder, an app, a file over 10 MB, a web link): those are refused with a short line.
    static func hasAttachable(_ pasteboard: NSPasteboard) -> Bool {
        pasteboard.canReadObject(forClasses: [NSFilePromiseReceiver.self, NSURL.self], options: fileOptions)
            || pasteboard.availableType(from: imageTypes + [.URL]) != nil
    }

    private enum Source {
        case file(URL)
        case promise(NSFilePromiseReceiver)
        case image(Data)
    }

    /// Files and promised files first, in pasteboard order; raw picture data only when there are
    /// none (an image dragged out of a browser carries both, and the promised file is the original).
    private static func sources(_ pasteboard: NSPasteboard) -> [Source] {
        var found: [Source] = []
        for object in pasteboard.readObjects(forClasses: [NSFilePromiseReceiver.self, NSURL.self], options: fileOptions) ?? [] {
            if let promise = object as? NSFilePromiseReceiver {
                found.append(.promise(promise))
            } else if let url = object as? URL, url.isFileURL {
                found.append(.file(url))
            }
        }
        if found.isEmpty, let type = pasteboard.availableType(from: imageTypes), let data = pasteboard.data(forType: type) {
            found.append(.image(data))
        }
        return found
    }

    /// Reads what's on `pasteboard` and attaches it; `imageName` names raw picture data ("Pasted
    /// image"). Takes no more than the free places on the pending row; anything past them gets
    /// "Up to 3 at a time".
    static func take(from pasteboard: NSPasteboard, into model: VoicePanelModel, imageName: String) {
        let found = sources(pasteboard)
        guard !found.isEmpty else {
            model.onAttachRefused(AttachmentFailure.unsupported.rawValue)
            return
        }
        let room = AttachmentPolicy.maxAttachments - model.pendingAttachments.count
        guard room > 0 else {
            model.onAttachRefused("too_many")
            return
        }
        if found.count > room { model.onAttachRefused("too_many") }
        var jobs: [@Sendable () throws -> EncodedAttachment] = []
        for source in found.prefix(room) {
            switch source {
            case .file(let url): jobs.append { try AttachmentEncoder.encode(fileAt: url) }
            case .image(let data): jobs.append { try AttachmentEncoder.encodeImage(data, name: imageName) }
            case .promise(let receiver): receive(receiver, into: model)
            }
        }
        encode(jobs, into: model)
    }

    /// Encodes in order, off the main thread, attaching each item as it's ready (pictures are
    /// decoded and re-encoded, so a big one takes a moment).
    private static func encode(_ jobs: [@Sendable () throws -> EncodedAttachment], into model: VoicePanelModel) {
        guard !jobs.isEmpty else { return }
        Task.detached(priority: .userInitiated) {
            for job in jobs {
                let result = Result { try job() }
                await MainActor.run { deliver(result, to: model) }
            }
        }
    }

    /// A promised file (Photos, Mail, a browser image): written by its app into a private temporary
    /// folder, encoded, then deleted.
    private static func receive(_ receiver: NSFilePromiseReceiver, into model: VoicePanelModel) {
        let fm = FileManager.default
        let folder = fm.temporaryDirectory.appendingPathComponent("Speakeasy-drops", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        do {
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        } catch {
            model.onAttachRefused("failed")
            return
        }
        let expected = max(1, receiver.fileTypes.count)
        let received = Counter()
        let queue = OperationQueue()
        queue.qualityOfService = .userInitiated
        receiver.receivePromisedFiles(atDestination: folder, options: [:], operationQueue: queue) { url, error in
            // On `queue`, once per promised file.
            let result: Result<EncodedAttachment, Error> = error.map { .failure($0) }
                ?? Result { try AttachmentEncoder.encode(fileAt: url) }
            try? FileManager.default.removeItem(at: url)
            if received.next() >= expected { try? FileManager.default.removeItem(at: folder) }
            Task { @MainActor in deliver(result, to: model) }
        }
    }

    private static func deliver(_ result: Result<EncodedAttachment, Error>, to model: VoicePanelModel) {
        switch result {
        case .success(let item):
            model.onAttach([item])
        case .failure(let error):
            // Encoding failures carry the plugin's reason words; anything else is "Couldn't attach that".
            model.onAttachRefused((error as? AttachmentFailure)?.rawValue ?? "failed")
        }
    }
}

/// Counts promised files as they arrive (the reader runs on an operation queue).
private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    func next() -> Int {
        lock.lock(); defer { lock.unlock() }
        value += 1
        return value
    }
}
