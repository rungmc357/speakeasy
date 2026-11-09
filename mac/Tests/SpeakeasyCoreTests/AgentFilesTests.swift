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

    // -- first load (SPK-264) ------------------------------------------------------------------

    /// Answers from a script: each call takes the next result (a thrown error or a list).
    final class Scripted: AgentFilesBackend {
        var script: [Result<AgentFileList, Error>]
        var calls = 0
        init(_ script: [Result<AgentFileList, Error>]) { self.script = script }
        func list(kind: String?, cursor: String?) async throws -> AgentFileList {
            calls += 1
            let next = script.isEmpty ? .success(AgentFileList(files: [])) : script.removeFirst()
            return try next.get()
        }
        func thumbnail(_ file: AgentFile) async throws -> Data { Data() }
        func download(_ file: AgentFile) async throws -> URL { URL(fileURLWithPath: "/dev/null") }
    }

    static let one = AgentFileList(files: [AgentFile(id: "a", name: "porch.png", kind: .image, bytes: 1, at: Date(), sessionID: "s")])

    @MainActor
    func testACancelledFirstLoadIsNotAConnectionError() async {
        // The page was rebuilt while it opened: its request was cancelled, the server was fine.
        let backend = Scripted([.failure(URLError(.cancelled)), .success(Self.one)])
        let model = AgentFilesModel(backend: backend)
        await model.reload()
        XCTAssertNil(model.error)
        XCTAssertFalse(model.loaded, "the next open loads again")
        model.loadIfNeeded()
        await model.waitForLoad()
        XCTAssertEqual(model.files.map(\.name), ["porch.png"])
        XCTAssertNil(model.error)
    }

    @MainActor
    func testLoadIfNeededSurvivesTheOpeningPageGoingAway() async {
        let backend = Scripted([.success(Self.one)])
        let model = AgentFilesModel(backend: backend)
        let page = Task { @MainActor in model.loadIfNeeded() }
        page.cancel()          // the page's own .task is cancelled; the model's load is not
        await page.value
        model.loadIfNeeded()   // a second open while the first load runs doesn't start another
        await model.waitForLoad()
        XCTAssertEqual(backend.calls, 1)
        XCTAssertEqual(model.files.map(\.name), ["porch.png"])
    }

    @MainActor
    func testASlowOrDroppedAnswerIsRetriedOnceQuietly() async {
        let backend = Scripted([.failure(URLError(.timedOut)), .success(Self.one)])
        let model = AgentFilesModel(backend: backend)
        model.retryDelay = 0.01
        await model.reload()
        XCTAssertEqual(backend.calls, 2)
        XCTAssertNil(model.error)
        XCTAssertEqual(model.files.count, 1)
    }

    @MainActor
    func testErrorsSayWhatHappened() async {
        let slow = Scripted([.failure(URLError(.timedOut)), .failure(URLError(.timedOut))])
        let model = AgentFilesModel(backend: slow)
        model.retryDelay = 0.01
        await model.reload()
        XCTAssertEqual(slow.calls, 2)
        XCTAssertEqual(model.error?.contains("taking too long"), true)
        XCTAssertEqual(model.error?.contains("Couldn't reach"), false)

        let broken = Scripted([.failure(ScheduleProblem(status: 500, message: "database is locked")),
                               .failure(ScheduleProblem(status: 500, message: "database is locked"))])
        let second = AgentFilesModel(backend: broken)
        second.retryDelay = 0.01
        await second.reload()
        XCTAssertEqual(second.error, "Your Hermes answered with an error while listing your files (database is locked). Try again in a moment.")

        // A refusal isn't retried and keeps the server's own words.
        let refused = Scripted([.failure(ScheduleProblem(status: 401, message: "Pair this device again."))])
        let third = AgentFilesModel(backend: refused)
        await third.reload()
        XCTAssertEqual(refused.calls, 1)
        XCTAssertEqual(third.error, "Pair this device again.")

        XCTAssertTrue(AgentFilesModel.message(URLError(.cannotConnectToHost)).hasPrefix("Couldn't reach your Hermes"))
    }

    func testOpenChatKnowsTheReplyAndTheHomeJob() {
        let row: [String: Any] = ["id": "abc", "name": "porch.png", "kind": "image", "session_id": "s1",
                                  "message_id": "m12", "home_run": "run_9"]
        let file = AgentFile(json: row)!
        XCTAssertEqual(file.messageID, "m12")
        XCTAssertEqual(file.homeRun, "run_9")
        XCTAssertTrue(file.hasChat)
        XCTAssertEqual(file.anchorRow(in: ["m3", "m12", "m14"]), "m12")
        XCTAssertEqual(file.anchorRow(in: ["m3", "m12-u1", "m12-u2"]), "m12-u1")   // one reply split into rows
        XCTAssertNil(file.anchorRow(in: ["m3", "m120"]))                            // never a lookalike id
        let helper = AgentFile(json: ["id": "d", "name": "plan.pdf", "session_id": "s2", "message_id": ""])!
        XCTAssertNil(helper.messageID)
        XCTAssertNil(helper.homeRun)
        XCTAssertNil(helper.anchorRow(in: ["m1"]))
        XCTAssertFalse(AgentFile(json: ["id": "e", "name": "x.txt"])!.hasChat)
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
