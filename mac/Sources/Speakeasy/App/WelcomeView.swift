import AppKit
import SwiftUI
import SpeakeasyCore

/// A separate front door: entering setup never changes the existing pairing/permission flow.
struct MacWelcomeView: View {
    @Binding var journey: WelcomeJourney
    var onConnect: () -> Void
    @State private var privacy = false
    @State private var demo = false
    @State private var copied = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        GeometryReader { geo in
            VStack(alignment: .leading, spacing: geo.size.height < 540 ? 8 : 16) {
                HStack {
                    if journey.page > 0 || journey.route == .newcomer {
                        Button("Back") { moveBack() }
                    }
                    Spacer()
                    Button("Skip") {
                        if journey.page == 3 && journey.route == .intro { onConnect() }
                        else { change { journey.skip() } }
                    }
                    .accessibilityIdentifier("welcome-skip")
                }.frame(height: 24)
                if journey.route == .newcomer {
                    newcomer
                } else {
                    FitToSpace(height: journey.page < 2 ? 300 : 220) {
                        Group {
                            switch journey.page {
                            case 0: CallClip()
                            case 1: TaskClip()
                            case 2: YoursClip()
                            default: ConnectClip()
                            }
                        }
                    }
                    .frame(height: max(88, min(300, geo.size.height - 300)))
                    .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 8) {
                        Text(WelcomeJourney.headlines[journey.page])
                            .font(.system(size: geo.size.height < 540 ? 30 : 40, weight: .semibold, design: .serif))
                            .accessibilityAddTraits(.isHeader)
                        Text(WelcomeJourney.lines[journey.page])
                            .font(.body).foregroundStyle(WelcomePalette.text(0.66))
                        if journey.page == 2 {
                            Button("Who hears what") { privacy = true }
                                .foregroundStyle(WelcomePalette.accent)
                        }
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    footer
                }
            }
            .buttonStyle(.plain)
            .padding(24)
            .frame(maxWidth: 528)
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .foregroundStyle(WelcomePalette.fg)
        .background(WelcomeBackground())
        .tint(WelcomePalette.accent)
        .background {
            WelcomeKeys { code in
                guard !privacy && !demo else { return false }
                if code == 123 { moveBack(); return true }
                if journey.route == .intro && (code == 124 || code == 36 || code == 76) {
                    moveForward(); return true
                }
                return false
            }
        }
        .onChange(of: journey.page) { page in
            if page == 3 && !AppModel.isAutomatedRun { UserDefaults.standard.set(true, forKey: WelcomeJourney.seenKey) }
        }
        .sheet(isPresented: $privacy) {
            VStack(alignment: .leading, spacing: 24) {
                Text("Who hears what").font(.system(.title, design: .serif).weight(.semibold))
                Text("Your agent, sessions, files and tools stay on your computer. With the ChatGPT voice, OpenAI hears the call itself — what you say and what the voice says back, including short result summaries — but not your sessions, files or tools.")
                    .fixedSize(horizontal: false, vertical: true)
                Button("Got it") { privacy = false }.keyboardShortcut(.defaultAction)
            }.padding(32).frame(width: 440).background(WelcomeBackground())
        }
        .sheet(isPresented: $demo) {
            HowItWorksDemo { demo = false; onConnect() }
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                ForEach(0..<4) { page in
                    Capsule().fill(page == journey.page ? WelcomePalette.accent : WelcomePalette.text(0.18))
                        .frame(width: page == journey.page ? 24 : 8, height: 8)
                }
            }.accessibilityElement(children: .ignore).accessibilityLabel("Page \(journey.page + 1) of 4")
            VStack(spacing: 8) {
                if journey.page == 3 {
                    Button(action: onConnect) { Text("I have Hermes").welcomeButton(filled: true) }
                        .keyboardShortcut(.defaultAction).accessibilityIdentifier("welcome-pair")
                    HStack(spacing: 16) {
                        Button { change { journey.route = .newcomer } } label: {
                            Text("I'm new to Hermes").welcomeButton(filled: false)
                        }.accessibilityIdentifier("welcome-new")
                        Button("See how it works") { demo = true }
                            .frame(maxWidth: .infinity, minHeight: 40).accessibilityIdentifier("welcome-demo")
                    }
                } else {
                    Button { moveForward() } label: { Text("Continue").welcomeButton(filled: true) }
                        .keyboardShortcut(.defaultAction).accessibilityIdentifier("welcome-continue")
                    Color.clear.frame(height: 40).accessibilityHidden(true)
                }
            }
        }
    }

    private var newcomer: some View {
        VStack(alignment: .leading, spacing: 24) {
            Text("Get Hermes on your computer.")
                .font(.system(size: 30, weight: .semibold, design: .serif))
                .accessibilityAddTraits(.isHeader)
            Text("About 10 min").foregroundStyle(WelcomePalette.text(0.55))
            VStack(alignment: .leading, spacing: 16) {
                Label("Install Hermes", systemImage: "arrow.down.circle")
                Label("Sign in with your ChatGPT account", systemImage: "person.crop.circle")
                Label("Add Speakeasy. It sends this Mac a link.", systemImage: "link")
            }.font(.body)
            Text("Copy the install command to get started. The guided setup walks you through signing in; then ask Hermes to add Speakeasy.")
                .foregroundStyle(WelcomePalette.text(0.66)).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(WelcomeJourney.installCommand, forType: .string)
                copied = true
            } label: { Text(copied ? "Copied" : "Copy install command").welcomeButton(filled: true) }
                .keyboardShortcut(.defaultAction).accessibilityIdentifier("welcome-install")
            Button("I have Hermes now") { onConnect() }
                .frame(maxWidth: .infinity, minHeight: 32)
        }
    }

    private func change(_ action: () -> Void) {
        withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.25), action)
    }
    private func moveForward() {
        guard !privacy && !demo && journey.route == .intro else { return }
        if journey.page == 3 { onConnect() }
        else { change { journey.forward() } }
    }
    private func moveBack() {
        guard !privacy && !demo else { return }
        change { journey.back() }
    }
}
