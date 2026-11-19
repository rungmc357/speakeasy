import AppKit
import SpeakeasyClient
import SpeakeasyCore
import SwiftUI
import XCTest
@testable import Speakeasy

/// SPK-543: the floating call bar is one rounded 320 × 56 pt bar, filled edge to edge. Its fill used to
/// be as tall as its row (about 46 pt), so the clear panel showed the desktop above and below it.
@MainActor
final class FloatingCallBarFillTests: XCTestCase {
    private let presentation = MacNowPresentation.shared
    private var windows: [NSWindow] = []

    override func setUp() async throws {
        presentation.enabled = true
        presentation.callStartedAt = Date().addingTimeInterval(-185)
    }

    override func tearDown() async throws {
        presentation.enabled = false
        presentation.callStartedAt = nil
        windows.removeAll()
    }

    func testTheBarIsFilledFromItsTopRowToItsBottomRow() throws {
        for appearance in [NSAppearance.Name.aqua, .darkAqua] {
            let bar = try draw(connection: .live, appearance: appearance)
            let centre = bar.pixelsWide / 2
            XCTAssertGreaterThan(alpha(bar, centre, 0), 0.5, "top row is see-through (\(appearance.rawValue))")
            XCTAssertGreaterThan(alpha(bar, centre, bar.pixelsHigh - 1), 0.5, "bottom row is see-through (\(appearance.rawValue))")
        }
    }

    func testAPausedBarIsFilledTheSameWay() throws {
        let bar = try draw(connection: .paused, appearance: .darkAqua)
        let centre = bar.pixelsWide / 2
        XCTAssertGreaterThan(alpha(bar, centre, 0), 0.5, "top row is see-through")
        XCTAssertGreaterThan(alpha(bar, centre, bar.pixelsHigh - 1), 0.5, "bottom row is see-through")
    }

    func testTheCornersStayRounded() throws {
        let bar = try draw(connection: .live, appearance: .darkAqua)
        let right = bar.pixelsWide - 1, bottom = bar.pixelsHigh - 1
        for (x, y) in [(0, 0), (right, 0), (0, bottom), (right, bottom)] {
            XCTAssertLessThan(alpha(bar, x, y), 0.5, "corner (\(x), \(y)) is filled, so the bar is no longer rounded")
        }
    }

    /// The view the floating panel hosts, in a clear window like the panel's, drawn with its alpha at 2x.
    private func draw(connection: ConnectionPhase, appearance: NSAppearance.Name) throws -> NSBitmapImageRep {
        let voice = VoicePanelModel()
        voice.state.connection = connection
        voice.state.mic = .live
        voice.shownStatus = "Looking at desk options."
        let size = NSSize(width: MacNowLayout.callBarSize.width, height: MacNowLayout.callBarSize.height)
        let view = NSHostingView(rootView: MacNowVoiceSurface(voice: voice))
        view.sizingOptions = []
        view.appearance = NSAppearance(named: appearance)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.isOpaque = false
        window.backgroundColor = .clear
        window.contentView = view
        windows.append(window)
        view.layoutSubtreeIfNeeded()
        let scale = 2
        let bitmap = try XCTUnwrap(NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int(size.width) * scale, pixelsHigh: Int(size.height) * scale,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0))
        bitmap.size = size
        view.cacheDisplay(in: view.bounds, to: bitmap)
        return bitmap
    }

    private func alpha(_ bitmap: NSBitmapImageRep, _ x: Int, _ y: Int) -> CGFloat {
        bitmap.colorAt(x: x, y: y)?.alphaComponent ?? 0
    }
}
