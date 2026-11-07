import SwiftUI
import SpeakeasyCore

/// Settings: which app opens music and places from a card. "Ask each time" is the state before
/// the first Play/Maps tap (and after the user clears it); nothing is detected.
public struct OpenLinksSection: View {
    @AppStorage(OpenInPrefs.musicKey) private var music: String = ""
    @AppStorage(OpenInPrefs.mapsKey) private var maps: String = ""
    public init() {}
    public var body: some View {
        Section {
            Picker("Music", selection: $music) {
                Text("Ask each time").tag("")
                ForEach(MusicApp.allCases, id: \.self) { Text($0.label).tag($0.rawValue) }
            }
            .accessibilityIdentifier("settings-open-music")
            Picker("Maps", selection: $maps) {
                Text("Ask each time").tag("")
                ForEach(MapsApp.allCases, id: \.self) { Text($0.label).tag($0.rawValue) }
            }
            .accessibilityIdentifier("settings-open-maps")
        } header: {
            Text("Open links in")
        } footer: {
            Text("Play and Maps buttons on cards open here.")
        }
    }
}
