import SwiftUI
import SpeakeasyCore
#if canImport(UIKit)
import UIKit
#endif

/// The secure box: a key a skill needs (lock, brass) or the Mac password for one sudo command (shield,
/// amber, the command shown in full). Password AutoFill works (`.password` content type), the value is
/// never shown unless the user taps Show, and the field is cleared the moment it is sent.
public struct SecureInputCard: View {
    public let request: SecureInputRequest
    public var onSubmit: (String) async -> String?
    /// Login cards: (username, password, code). nil = the host can't save logins.
    public var onSubmitLogin: ((String, String, String) async -> String?)?
    public var onSkip: () async -> Void
    /// Opens with the value visible (screenshots and UI tests only).
    public var startRevealed: Bool
    /// A fake value already typed (screenshots only).
    public var previewValue: String

    @State private var value = ""
    @State private var identifier = ""
    @State private var code = ""
    @FocusState private var identifierFocused: Bool
    @State private var revealed = false
    @State private var sending = false
    @State private var error: String?
    @State private var now = Date()
    @FocusState private var focused: Bool
    @Environment(\.colorScheme) private var scheme

    public init(request: SecureInputRequest, startRevealed: Bool = false, previewValue: String = "",
                previewIdentifier: String = "",
                onSubmit: @escaping (String) async -> String?,
                onSubmitLogin: ((String, String, String) async -> String?)? = nil,
                onSkip: @escaping () async -> Void) {
        self.request = request; self.onSubmit = onSubmit; self.onSkip = onSkip; self.startRevealed = startRevealed
        self.previewValue = previewValue; self.previewIdentifier = previewIdentifier; self.onSubmitLogin = onSubmitLogin
    }
    public var previewIdentifier: String

    private var isSudo: Bool { request.kind == .sudo }
    private var isLogin: Bool { request.kind == .login }
    /// Keys are brass (the brand); an admin password is a warmer warning orange; a website login is
    /// a calm blue (it's the browser's, not the Mac's): the three never look alike.
    private var accent: Color {
        switch request.kind {
        case .sudo: return Self.adminOrange
        case .login, .code: return Self.loginBlue
        case .secret: return BrandPalette.brass
        }
    }
    /// Text on the orange / blue fills: white on light's deep tones, near-black on dark's bright ones (AA both ways).
    static let onAccent = Color.adaptive(dark: Color(hex: 0x10131a), light: .white)
    static let loginBlue = Color.adaptive(dark: Color(hex: 0x6aa8ff), light: Color(hex: 0x1f5fbf))
    static let adminOrange = Color.adaptive(dark: Color(hex: 0xff8a3d), light: Color(hex: 0xb4470f))
    private var canSend: Bool {
        !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !sending
            && (!isLogin || !identifier.trimmingCharacters(in: .whitespaces).isEmpty)
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            header
            if isSudo { commandBlock } else if isLogin { siteBlock } else { purpose }
            if isLogin { identifierField }
            field
            if isLogin { codeField }
            whereItGoes
            if let error {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.footnote).foregroundStyle(BrandPalette.red)
                    .accessibilityIdentifier("secure-input-error")
            }
            buttons
        }
        .padding(22)
        .frame(maxWidth: 520, alignment: .leading)
        .onAppear {
            revealed = startRevealed
            if !previewValue.isEmpty { value = previewValue }
            if !previewIdentifier.isEmpty { identifier = previewIdentifier }
            if isLogin && identifier.isEmpty { identifierFocused = true } else { focused = true }
        }
        .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { now = $0 }
        .onDisappear { value = ""; code = "" }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("secure-input-card-\(request.kind.rawValue)")
    }

    // MARK: parts

    private var header: some View {
        HStack(alignment: .top, spacing: 14) {
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(accent.opacity(scheme == .dark ? 0.18 : 0.12))
                Image(systemName: isSudo ? "lock.shield.fill" : isLogin ? "person.badge.key.fill"
                      : request.kind == .code ? "number.square.fill" : "key.fill")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(accent)
            }
            .frame(width: 44, height: 44)
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Image(systemName: "lock.fill").font(.system(size: 10, weight: .bold))
                    Text(request.userStarted ? "SECURE" : isSudo ? "ADMIN PASSWORD · NEEDS YOU" : isLogin ? "WEBSITE LOGIN · NEEDS YOU"
                         : request.kind == .code ? "ONE-TIME CODE · NEEDS YOU" : "SECURE · NEEDS YOU")
                        .font(.system(size: 11, weight: .semibold)).tracking(0.6)
                    Spacer(minLength: 8)
                    if !request.userStarted {
                        Text(countdown).font(.system(size: 11, weight: .medium)).monospacedDigit()
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("\(request.secondsLeft(now: now)) seconds left")
                    }
                }
                .foregroundStyle(accent)
                Text(request.title)
                    .font(.system(.title3, design: .default).weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("secure-input-title")
                if !request.askedBy.isEmpty {
                    Text(request.askedBy).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                }
            }
        }
    }

    @ViewBuilder private var purpose: some View {
        let lines = [request.help, request.prompt == request.label ? "" : request.prompt].filter { !$0.isEmpty }
        if !lines.isEmpty {
            Text(lines.joined(separator: "\n")).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Login: the site, as the browser sees it, so AutoFill and the user agree on where this goes.
    private var siteBlock: some View {
        HStack(spacing: 10) {
            Image(systemName: "globe").foregroundStyle(accent).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(request.site).font(.system(.callout, design: .rounded).weight(.semibold))
                    .accessibilityIdentifier("secure-input-site")
                if !request.origin.isEmpty && request.origin != request.site {
                    Text(request.origin).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(accent.opacity(scheme == .dark ? 0.12 : 0.07)))
    }

    private func boxed<Content: View>(_ isFocused: Bool, @ViewBuilder _ content: () -> Content) -> some View {
        content()
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(scheme == .dark ? Color.white.opacity(0.07) : Color.white))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(isFocused ? accent.opacity(0.9) : Color.secondary.opacity(0.25), lineWidth: isFocused ? 1.5 : 1))
    }

    private var identifierField: some View {
        boxed(identifierFocused) {
            TextField("Username or email", text: $identifier)
                .textContentType(.username)
                .autocorrectionDisabled(true)
                #if os(iOS) || os(visionOS)
                .textInputAutocapitalization(.never)
                .keyboardType(.emailAddress)
                #endif
                .focused($identifierFocused)
                .textFieldStyle(.plain)
                .onSubmit { focused = true }
                .accessibilityIdentifier("secure-input-username")
        }
    }

    private var codeField: some View {
        boxed(false) {
            TextField("One-time code (optional)", text: $code)
                #if os(iOS) || os(visionOS)
                .textContentType(.oneTimeCode)
                .keyboardType(.numberPad)
                #endif
                .font(code.isEmpty ? .body : .system(.body, design: .monospaced))
                .textFieldStyle(.plain)
                .accessibilityIdentifier("secure-input-code")
        }
    }

    private var commandBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView(.vertical) {
                Text(request.command)
                    .font(.system(.callout, design: .monospaced).weight(.medium))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
                    .accessibilityIdentifier("secure-input-command")
            }
            .frame(maxHeight: 170)
            .fixedSize(horizontal: false, vertical: true)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(scheme == .dark ? Color.black.opacity(0.45) : Color(hex: 0x1e1912).opacity(0.06)))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(accent.opacity(0.45), lineWidth: 1))
            Label("Runs with administrator rights. Only allow it if you expected this.",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.footnote).foregroundStyle(.secondary)
                .symbolRenderingMode(.multicolor)
        }
    }

    private var field: some View {
        HStack(spacing: 8) {
            Group {
                if revealed {
                    TextField(request.fieldPlaceholder, text: $value)
                        .font(.system(.body, design: .monospaced))
                } else {
                    SecureField(request.fieldPlaceholder, text: $value)
                }
            }
            #if os(iOS) || os(visionOS)
            .textContentType(request.kind == .code ? .oneTimeCode : .password)
            #else
            .textContentType(.password)
            #endif
            .autocorrectionDisabled(true)
            #if os(iOS) || os(visionOS)
            .textInputAutocapitalization(.never)
            .keyboardType(.asciiCapable)
            #endif
            .privacySensitive()
            .focused($focused)
            .submitLabel(.done)
            .onSubmit { if canSend { send() } }
            .textFieldStyle(.plain)
            .accessibilityIdentifier("secure-input-field")
            Button { revealed.toggle() } label: {
                Image(systemName: revealed ? "eye.slash" : "eye")
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.plain).foregroundStyle(.secondary)
            .accessibilityLabel(revealed ? "Hide" : "Show")
            .accessibilityIdentifier("secure-input-show")
            #if !os(macOS)
            // A system paste button: pasting from it never triggers the "Allow Paste" prompt.
            // (On the Mac, ⌘V in the field is the paste.)
            PasteButton(payloadType: String.self) { strings in
                if let first = strings.first {
                    let clean = first.trimmingCharacters(in: .whitespacesAndNewlines)
                    Task { @MainActor in value = clean }
                }
            }
            .labelStyle(.iconOnly)
            .buttonBorderShape(.capsule)
            .controlSize(.small)
            .tint(accent)
            .accessibilityIdentifier("secure-input-paste")
            #endif
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .fill(scheme == .dark ? Color.white.opacity(0.07) : Color.white))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .strokeBorder(focused ? accent.opacity(0.9) : Color.secondary.opacity(0.25), lineWidth: focused ? 1.5 : 1))
    }

    private var whereItGoes: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: isSudo || request.kind == .code ? "clock.arrow.circlepath" : "lock.shield")
                .font(.footnote.weight(.semibold)).foregroundStyle(accent)
                .accessibilityHidden(true)
            Text(request.whereItGoes).font(.footnote).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("secure-input-where")
        }
    }

    private var buttons: some View {
        HStack(spacing: 12) {
            Button {
                value = ""
                Task { sending = true; await onSkip(); sending = false }
            } label: {
                Text(request.skipButton).frame(maxWidth: .infinity)
            }
            .buttonStyle(SecureSecondaryButtonStyle())
            .accessibilityIdentifier("secure-input-skip")
            Button(action: send) {
                HStack(spacing: 6) {
                    if sending { ProgressView().controlSize(.small) }
                    else { Image(systemName: isSudo ? "checkmark.shield.fill" : request.kind == .code ? "arrow.up.circle.fill" : "lock.fill") }
                    Text(request.primaryButton)
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(SecurePrimaryButtonStyle(fill: accent, text: request.kind == .secret ? BrandPalette.onBrass : Self.onAccent))
            .disabled(!canSend)
            .keyboardShortcut(.defaultAction)
            .accessibilityIdentifier("secure-input-save")
        }
    }

    private var countdown: String {
        let left = request.secondsLeft(now: now)
        return String(format: "%d:%02d", left / 60, left % 60)
    }

    private func send() {
        guard canSend else { return }
        let entered = value, user = identifier, typedCode = code
        value = ""          // the field never keeps it
        code = ""
        revealed = false
        sending = true
        error = nil
        Task {
            let failure: String?
            if isLogin, let onSubmitLogin {
                failure = await onSubmitLogin(user, entered, typedCode)
            } else {
                failure = await onSubmit(entered)
            }
            sending = false
            error = failure
        }
    }
}

/// The filled primary button: always the card's accent (a system prominent button goes grey in an
/// inactive Mac window), dimmed while there's nothing to send.
struct SecurePrimaryButtonStyle: ButtonStyle {
    let fill: Color
    let text: Color
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .foregroundStyle(text)
            .padding(.vertical, 9).padding(.horizontal, 14)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(fill))
            .opacity(enabled ? (configuration.isPressed ? 0.8 : 1) : 0.38)
            .contentShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// Skip / Don't allow: the same size as the primary, quiet.
struct SecureSecondaryButtonStyle: ButtonStyle {
    @Environment(\.colorScheme) private var scheme

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.medium))
            .foregroundStyle(.primary)
            .padding(.vertical, 9).padding(.horizontal, 14)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(scheme == .dark ? Color.white.opacity(configuration.isPressed ? 0.18 : 0.11)
                                      : Color.black.opacity(configuration.isPressed ? 0.1 : 0.05)))
            .contentShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// Shows the oldest open request as a sheet that can't be swiped away: Save or Skip.
public struct SecureInputPresenter: ViewModifier {
    @ObservedObject var center: SecureInputCenter
    /// The card's measured height: the sheet opens exactly that tall, so the buttons are never below the
    /// fold (the login card is taller than a `.medium` sheet).
    @State private var cardHeight: CGFloat = 0

    public func body(content: Content) -> some View {
        content.sheet(item: Binding(get: { center.current }, set: { _ in })) { request in
            ScrollView {
                SecureInputCard(request: request,
                                onSubmit: { await center.submit(request, value: $0) },
                                onSubmitLogin: { await center.submitLogin(request, identifier: $0, password: $1, code: $2) },
                                onSkip: { await center.skip(request) })
                    .frame(maxWidth: .infinity)
                    .background(GeometryReader { proxy in
                        Color.clear.preference(key: SecureInputCardHeight.self, value: proxy.size.height)
                    })
            }
            .scrollBounceBehavior(.basedOnSize)
            .onPreferenceChange(SecureInputCardHeight.self) { cardHeight = $0 }
            #if os(iOS)
            .presentationDetents(cardHeight > 0 ? [.height(cardHeight), .large] : [.medium, .large])
            .presentationDragIndicator(.hidden)
            #endif
            .interactiveDismissDisabled(true)
        }
    }
}

private struct SecureInputCardHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

public extension View {
    /// Present Speakeasy's secure box whenever the plugin has one open for this device.
    func secureInputSheet(_ center: SecureInputCenter) -> some View { modifier(SecureInputPresenter(center: center)) }
}

/// Fixtures for previews, screenshots and UI tests (obviously fake, never real values).
public enum SecureInputFixtures {
    public static let weatherKey = SecureInputRequest(
        id: "sec_fixture_weather_key_000", kind: .secret, job: "Weather for the Lisbon trip", envVar: "OPENWEATHER_API_KEY",
        label: "OpenWeather API key", prompt: "OpenWeather API key", help: "Get a free key at openweathermap.org/api.",
        skill: "weather", expiresAt: Date().addingTimeInterval(118))
    public static let sudo = SecureInputRequest(
        id: "sec_fixture_sudo_command_00", kind: .sudo, job: "Fix the Mac mini's SSH", label: "Your Mac's password",
        command: "sudo launchctl kickstart -k system/com.openssh.sshd", expiresAt: Date().addingTimeInterval(97))
    public static let login = SecureInputRequest(
        id: "sec_fixture_login_example0", kind: .login, job: "Book the dinner reservation", label: "Your login for example.com",
        origin: "https://www.example.com", site: "www.example.com", expiresAt: Date().addingTimeInterval(171))
}
