import XCTest
@testable import SpeakeasyCore

final class AgentFilesTests: XCTestCase {
    private let sample: [String: Any] = [
        "files": [
            ["id": "abcdefghijklmnopqrstuvwxyz", "name": "porch-paint-chart.png", "kind": "image", "mime": "image/png",
             "bytes": 48213, "at": 1_767_312_000.5, "session_id": "s1", "chat": "Porch repaint", "source": "Discord · #build"],
            ["id": "bcdefghijklmnopqrstuvwxyza", "name": "seed-plan.pdf", "kind": "document", "bytes": 412_000,
             "at": 1_767_300_000, "session_id": "s2", "chat": "", "source": "Speakeasy"],
            ["name": "no-id.png"],
        ],
        "kinds": [["id": "image", "label": "Images", "count": 1], ["id": "document", "label": "Docs", "count": 1]],
        "total": 2, "next": "60", "loading": false,
    ]

    func testParsesTheListAndSkipsBrokenRows() {
        let list = AgentFileList(json: sample)
        XCTAssertEqual(list.files.map(\.name), ["porch-paint-chart.png", "seed-plan.pdf"])
        XCTAssertEqual(list.files[0].kind, .image)
        XCTAssertEqual(list.files[0].sessionID, "s1")
        XCTAssertEqual(list.files[0].at.timeIntervalSince1970, 1_767_312_000.5, accuracy: 0.01)
        XCTAssertEqual(list.kinds.map(\.label), ["Images", "Docs"])
        XCTAssertEqual(list.next, "60")
        XCTAssertFalse(list.loading)
    }

    func testPathsNeverCarryAFilePath() {
        XCTAssertEqual(AgentFileList.path(), "/voice/files")
        XCTAssertEqual(AgentFileList.path(kind: "image", cursor: "60"), "/voice/files?kind=image&cursor=60")
        XCTAssertEqual(AgentFileList.filePath("abc"), "/voice/files/abc")
        XCTAssertEqual(AgentFileList.thumbPath("abc"), "/voice/files/abc/thumb")
        XCTAssertEqual(AgentFileList.filePath("../x"), "/voice/files/%2E%2E%2Fx")
    }

    func testDetailLineAndSymbols() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "America/New_York")!
        let now = cal.date(from: DateComponents(year: 2026, month: 3, day: 10, hour: 18))!
        let today = AgentFile(id: "a", name: "chart.png", kind: .image, bytes: 48_213,
                              at: cal.date(from: DateComponents(year: 2026, month: 3, day: 10, hour: 9, minute: 5))!,
                              sessionID: "s", chat: "Porch repaint")
        XCTAssertEqual(today.detail(now: now, calendar: cal), "Porch repaint · 9:05 AM · 48 KB")
        let yesterday = AgentFile(id: "b", name: "plan.pdf", kind: .document, bytes: 0,
                                  at: now.addingTimeInterval(-86_400), sessionID: "s")
        XCTAssertEqual(yesterday.detail(now: now, calendar: cal), "Yesterday")
        let old = AgentFile(id: "c", name: "x.csv", kind: .document, bytes: 1, at: cal.date(from: DateComponents(year: 2025, month: 7, day: 4))!, sessionID: "s")
        XCTAssertTrue(old.detail(now: now, calendar: cal).hasPrefix("Jul 4, 2025"))
        XCTAssertEqual(yesterday.symbol, "doc.richtext")
        XCTAssertEqual(old.symbol, "tablecells")
        XCTAssertEqual(yesterday.typeWord, "PDF")
        XCTAssertEqual(today.typeWord, "Picture")
    }

    func testSafeNameNeverEscapesItsFolder() {
        let f = AgentFile(id: "a", name: "../../etc/passwd", kind: .other, bytes: 1, at: Date(), sessionID: "s")
        XCTAssertFalse(f.safeName.contains("/"))
        XCTAssertEqual(AgentFileStore.localURL(for: f).deletingLastPathComponent().lastPathComponent, "a")
        let hidden = AgentFile(id: "b", name: ".env", kind: .other, bytes: 1, at: Date(), sessionID: "s")
        XCTAssertFalse(hidden.safeName.hasPrefix("."))
    }

    @MainActor
    func testModelChipsHideForOneKindAndFilterReloads() async {
        let demo = DemoAgentFilesBackend()
        let model = AgentFilesModel(backend: demo)
        await model.reload()
        XCTAssertEqual(model.files.count, demo.files.count)
        XCTAssertTrue(model.showsChips)
        await model.choose(kind: "image")
        XCTAssertTrue(model.files.allSatisfy { $0.kind == .image })
        XCTAssertEqual(model.kinds.count, 4)   // the chips still show every kind
        model.preview(AgentFileList(files: [demo.files[0]], kinds: [AgentFileKindCount(id: "image", label: "Images", count: 1)]))
        XCTAssertFalse(model.showsChips)
    }

    @MainActor
    func testWaitsWhileTheServerIsStillScanning() async {
        final class Slow: AgentFilesBackend {
            var calls = 0
            func list(kind: String?, cursor: String?) async throws -> AgentFileList {
                calls += 1
                return calls == 1 ? AgentFileList(files: [], loading: true)
                    : AgentFileList(files: [AgentFile(id: "a", name: "a.png", kind: .image, bytes: 1, at: Date(), sessionID: "s")])
            }
            func thumbnail(_ file: AgentFile) async throws -> Data { Data() }
            func download(_ file: AgentFile) async throws -> URL { URL(fileURLWithPath: "/dev/null") }
        }
        let backend = Slow()
        let model = AgentFilesModel(backend: backend)
        await model.reload()
        XCTAssertEqual(backend.calls, 2)
        XCTAssertEqual(model.files.map(\.name), ["a.png"])
        XCTAssertFalse(model.serverScanning)
    }

    @MainActor
    func testAGoneFileSaysSoAndLeavesTheList() async {
        final class Gone: AgentFilesBackend {
            let file = AgentFile(id: "gonefile", name: "a.pdf", kind: .document, bytes: 9, at: Date(), sessionID: "s")
            func list(kind: String?, cursor: String?) async throws -> AgentFileList { AgentFileList(files: [file]) }
            func thumbnail(_ file: AgentFile) async throws -> Data { Data() }
            func download(_ file: AgentFile) async throws -> URL { throw ScheduleProblem(status: 404, message: "file not found") }
        }
        let backend = Gone()
        let model = AgentFilesModel(backend: backend)
        await model.reload()
        let url = await model.localCopy(backend.file)
        XCTAssertNil(url)
        XCTAssertEqual(model.openError, "That file isn't on your Hermes computer anymore.")
        XCTAssertTrue(model.files.isEmpty)
    }

    @MainActor
    func testDemoFilesDownloadAndDrawPictures() async throws {
        let demo = DemoAgentFilesBackend()
        let image = demo.files.first { $0.kind == .image }!
        let data = try await demo.thumbnail(image)
        XCTAssertEqual(Array(data.prefix(4)), [0x89, 0x50, 0x4E, 0x47])
        let url = try await demo.download(image)
        XCTAssertEqual(url.lastPathComponent, image.name)
        defer { AgentFileStore.clear() }
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
    }

    func testCapabilityDecodes() throws {
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"files": true}"#.utf8))
        XCTAssertTrue(caps.files)
        XCTAssertFalse(try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8)).files)
    }
}
