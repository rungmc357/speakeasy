// swift-tools-version: 6.0
import PackageDescription

// SpeakeasyCore (pure logic) and SpeakeasyClient (server API, WebRTC call engine, call
// client, panel model) build for macOS and iOS; the Speakeasy Mac app stays macOS-only
// (it imports AppKit throughout). The iPhone app links the two libraries from its own project.
let package = Package(
    name: "Speakeasy",
    platforms: [.macOS(.v14), .iOS(.v17), .visionOS(.v2)],
    products: [
        .library(name: "SpeakeasyCore", targets: ["SpeakeasyCore"]),
        .library(name: "SpeakeasyClient", targets: ["SpeakeasyClient"]),
        .executable(name: "Speakeasy", targets: ["Speakeasy"]),
    ],
    dependencies: [
        .package(url: "https://github.com/stasel/WebRTC.git", exact: "153.0.0"),
        // iPhone, iPad and Vision Pro: the same WebRTC, built by LiveKit. Its AVAudioEngine audio device
        // lets a call duck other apps' audio only while someone speaks (stasel has no such control, and no
        // visionOS slice). Class names carry an LK prefix; SpeakeasyClient/WebRTCNames.swift maps them back.
        .package(url: "https://github.com/livekit/webrtc-xcframework.git", exact: "150.7871.03"),
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.6.0"),
    ],
    targets: [
        .target(name: "SpeakeasyCore"),
        .target(name: "SpeakeasyClient", dependencies: [
            "SpeakeasyCore",
            .product(name: "WebRTC", package: "WebRTC", condition: .when(platforms: [.macOS])),
            .product(name: "LiveKitWebRTC", package: "webrtc-xcframework", condition: .when(platforms: [.iOS, .visionOS])),
        ]),
        .executableTarget(name: "Speakeasy", dependencies: [
            "SpeakeasyCore",
            "SpeakeasyClient",
            .product(name: "WebRTC", package: "WebRTC"),
            .product(name: "Sparkle", package: "Sparkle"),
        ]),
        .testTarget(name: "SpeakeasyCoreTests", dependencies: ["SpeakeasyCore"], resources: [.copy("Contract")]),
        .testTarget(name: "SpeakeasyClientTests", dependencies: ["SpeakeasyCore", "SpeakeasyClient"]),
    ],
    swiftLanguageModes: [.v5]
)
