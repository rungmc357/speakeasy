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
    /// Sheet form (phone/iPad): the card scrolls on its own from the top, with Save / Skip pinned in a
    /// bar at the bottom, so at the largest text sizes the title is seen first and the buttons are
    /// always reachable (SPK-274). The Mac window keeps the plain card.
    public var pinsButtons: Bool

    @State private var value = ""
    @State private var identifier = ""
    @State private var code = ""
    @FocusState private var identifierFocused: Bool
    @State private var revealed = false
    @State private var sending = false
    @State private var error: String?
    @State private var now = Date()
    @FocusState private var focused: Bool
    @State private var bodyHeight: CGFloat = 0
    @State private var barHeight: CGFloat = 0
    @State private var barTop: CGFloat = 0
    @State private var scrollTop: CGFloat = 0
    @Environment(\.colorScheme) private var scheme
    @Environment(\.dynamicTypeSize) private var typeSize
    /// Accessibility text sizes: the header and the field's Show/Paste stack vertically.
    private var large: Bool { typeSize.isAccessibilitySize }

    public init(request: SecureInputRequest, startRevealed: Bool = false, previewValue: String = "",
                previewIdentifier: String = "", pinsButtons: Bool = false,
                onSubmit: @escaping (String) async -> String?,
                onSubmitLogin: ((String, String, String) async -> String?)? = nil,
                onSkip: @escaping () async -> Void) {
        self.request = request; self.onSubmit = onSubmit; self.onSkip = onSkip; self.startRevealed = startRevealed
        self.previewValue = previewValue; self.previewIdentifier = previewIdentifier; self.onSubmitLogin = onSubmitLogin
        self.pinsButtons = pinsButtons
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
        Group {
            if pinsButtons { pinned } else { plain }
        }
        .onAppear {
            revealed = startRevealed
            if !previewValue.isEmpty { value = previewValue }
            if !previewIdentifier.isEmpty { identifier = previewIdentifier }
            // At accessibility sizes the keyboard would cover half the sheet and scroll the title away
            // before the user has read what is being asked and by which job (SPK-274): they tap the field.
            #if os(iOS) || os(visionOS)
            if large { return }
            #endif
            if isLogin && identifier.isEmpty { identifierFocused = true } else { focused = true }
        }
        .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { now = $0 }
        .onDisappear { value = ""; code = "" }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("secure-input-card-\(request.kind.rawValue)")
    }

    /// Everything above the buttons.
    private var details: some View {
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
        }
    }

    private var plain: some View {
        VStack(alignment: .leading, spacing: 18) {
            details
            buttons
        }
        .padding(22)
        .frame(maxWidth: 520, alignment: .leading)
    }

    /// Sheet form: scrolls from the top; the buttons sit in a bar pinned to the bottom. Reports its
    /// natural height (details + bar) so the sheet can open exactly that tall when it fits.
    private var pinned: some View {
        ScrollView {
            details
                .padding(.horizontal, 22).padding(.top, 22).padding(.bottom, 12)
                .frame(maxWidth: 520, alignment: .leading)
                .frame(maxWidth: .infinity)
                .background(GeometryReader { proxy in
                    Color.clear
                        .onAppear { bodyHeight = proxy.size.height }
                        .onChange(of: proxy.size.height) { _, h in bodyHeight = h }
                })
        }
        .defaultScrollAnchor(.top)
        .scrollBounceBehavior(.basedOnSize)
        .background(GeometryReader { proxy in
            Color.clear
                .onAppear { scrollTop = proxy.frame(in: .named(Self.space)).minY }
                .onChange(of: proxy.frame(in: .named(Self.space)).minY) { _, y in scrollTop = y }
        })
        .safeAreaInset(edge: .bottom, spacing: 0) {
            buttons
                // The bar's buttons stop growing at the first accessibility size: at the largest sizes
                // two full-size buttons would take half the screen from what is being asked.
                .dynamicTypeSize(...DynamicTypeSize.accessibility1)
                .padding(.horizontal, 22).padding(.top, 12).padding(.bottom, 16)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
                // A bar only when the details scroll under it; otherwise the buttons sit on the card.
                .background { if overflows { Rectangle().fill(.bar) } }
                .overlay(alignment: .top) { if overflows { Divider() } }
                .background(GeometryReader { proxy in
                    Color.clear
                        .onAppear { barHeight = proxy.size.height; barTop = proxy.frame(in: .named(Self.space)).minY }
                        .onChange(of: proxy.size.height) { _, h in barHeight = h }
                        .onChange(of: proxy.frame(in: .named(Self.space)).minY) { _, y in barTop = y }
                })
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("secure-input-buttons")
        }
        .coordinateSpace(.named(Self.space))
        .preference(key: SecureInputCardHeight.self, value: bodyHeight > 0 ? bodyHeight + barHeight : 0)
    }

    private static let space = "secure-input-sheet"
    /// The details are taller than the space above the button bar (they scroll under it).
    private var overflows: Bool { barTop > scrollTop && bodyHeight > barTop - scrollTop + 1 }

    // MARK: parts

    private var header: some View {
        // Accessibility sizes: the icon above the words, so the title gets the full width.
        let layout = large ? AnyLayout(VStackLayout(alignment: .leading, spacing: 10))
                           : AnyLayout(HStackLayout(alignment: .top, spacing: 14))
        return layout {
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
                    Text(request.askedBy).font(.subheadline).foregroundStyle(.secondary)
                        .lineLimit(large ? nil : 2)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("secure-input-asked-by")
                }
            }
        }
    }

    @ViewBuilder private var purpose: some View {
        let lines = [request.reason, request.help, request.prompt == request.label ? "" : request.prompt].filter { !$0.isEmpty }
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

    private func boxed<Content: View>(_ isFocused: Bool, vertical: CGFloat = 10, @ViewBuilder _ content: () -> Content) -> some View {
        content()
            .padding(.horizontal, 14).padding(.vertical, vertical)
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

    /// The secret's field. Show and Paste sit beside it, or, at accessibility sizes, on their own row
    /// under it with words, so the placeholder is never squeezed behind them (SPK-274).
    @ViewBuilder private var field: some View {
        if large {
            VStack(alignment: .leading, spacing: 10) {
                boxed(focused) { valueField }
                // Side by side when they fit, else one above the other; never squeezed into a sliver.
                // They stop growing at the first accessibility size (they're labelled controls, not text).
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { showButton(withTitle: true); pasteButton }
                    VStack(alignment: .leading, spacing: 10) { showButton(withTitle: true); pasteButton }
                }
                .dynamicTypeSize(...DynamicTypeSize.accessibility1)
            }
        } else {
            // One row, at least 44 pt tall (no extra vertical padding), so Show is a full 44 pt touch
            // target while the box stays about as tall as the other fields. The Mac keeps its pointer-
            // sized eye and the same height as the username/code fields.
            boxed(focused, vertical: Self.rowPadding) {
                HStack(spacing: 4) {
                    valueField
                    showButton(withTitle: false)
                    pasteButton
                }
                .frame(minHeight: Self.showSide)
            }
        }
    }

    #if os(macOS)
    private static let showSide: CGFloat = 28
    private static let rowPadding: CGFloat = 5
    #else
    private static let showSide: CGFloat = 44
    private static let rowPadding: CGFloat = 0
    #endif

    private var valueField: some View {
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
    }

    /// Show / Hide: a 44 pt touch target at every size (pointer-sized on the Mac).
    @ViewBuilder private func showButton(withTitle: Bool) -> some View {
        Button { revealed.toggle() } label: {
            if withTitle {
                Label(revealed ? "Hide" : "Show", systemImage: revealed ? "eye.slash" : "eye")
                    .lineLimit(1)
                    .fixedSize()
                    .padding(.horizontal, 14)
                    .frame(minWidth: 44, minHeight: 44)
                    .background(Capsule().fill(Color.secondary.opacity(0.14)))
                    .contentShape(Capsule())
            } else {
                Image(systemName: revealed ? "eye.slash" : "eye")
                    .frame(width: Self.showSide, height: Self.showSide)
                    .contentShape(Rectangle())
            }
        }
        .buttonStyle(.plain)
        .foregroundStyle(withTitle ? Color.primary : Color.secondary)
        .accessibilityLabel(revealed ? "Hide" : "Show")
        .accessibilityIdentifier("secure-input-show")
    }

    @ViewBuilder private var pasteButton: some View {
        #if !os(macOS)
        // A system paste button: pasting from it never triggers the "Allow Paste" prompt.
        // (On the Mac, ⌘V in the field is the paste.)
        let paste = PasteButton(payloadType: String.self) { strings in
            if let first = strings.first {
                let clean = first.trimmingCharacters(in: .whitespacesAndNewlines)
                Task { @MainActor in value = clean }
            }
        }
        if large {
            // Words beside the icon and the system's large size: a 44 pt target that says what it does.
            paste.labelStyle(.titleAndIcon).buttonBorderShape(.capsule).controlSize(.large)
                .tint(accent).accessibilityIdentifier("secure-input-paste")
        } else {
            paste.labelStyle(.iconOnly).buttonBorderShape(.capsule).controlSize(.small)
                .tint(accent).accessibilityIdentifier("secure-input-paste")
        }
        #endif
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

    @ViewBuilder private var buttons: some View {
        if request.stacksButtons || large {
            VStack(spacing: 10) { saveButton; skipButton }
        } else {
            HStack(spacing: 12) { skipButton; saveButton }
        }
    }

    private var skipButton: some View {
        Button {
            value = ""
            Task { sending = true; await onSkip(); sending = false }
        } label: {
            Text(request.skipButton).multilineTextAlignment(.center).frame(maxWidth: .infinity)
        }
        .buttonStyle(SecureSecondaryButtonStyle())
        .accessibilityIdentifier("secure-input-skip")
    }

    private var saveButton: some View {
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

    public func body(content: Content) -> some View {
        content.sheet(item: Binding(get: { center.current }, set: { _ in })) { request in
            SecureInputSheetContent(request: request, center: center)
        }
    }
}

/// The sheet's body: the card in its pinned form. It opens exactly as tall as the card when that fits
/// (the login card is taller than a `.medium` sheet), otherwise full height, scrolled to the top with
/// Save / Skip pinned at the bottom (SPK-274: at the largest text the old sheet opened mid-card).
private struct SecureInputSheetContent: View {
    let request: SecureInputRequest
    @ObservedObject var center: SecureInputCenter
    @State private var cardHeight: CGFloat = 0

    var body: some View {
        SecureInputCard(request: request, pinsButtons: true,
                        onSubmit: { await center.submit(request, value: $0) },
                        onSubmitLogin: { await center.submitLogin(request, identifier: $0, password: $1, code: $2) },
                        onSkip: { await center.skip(request) })
            .onPreferenceChange(SecureInputCardHeight.self) { cardHeight = $0 }
            .secureInputDetents(cardHeight)
            .interactiveDismissDisabled(true)
    }
}

public extension View {
    /// Detents for a sheet holding a pinned `SecureInputCard` (`pinsButtons: true`): the card's own
    /// height when measured, else medium; large is always offered and the system caps a too-tall
    /// height at large. Read the height with `.onPreferenceChange(SecureInputCardHeight.self)`.
    @ViewBuilder func secureInputDetents(_ cardHeight: CGFloat) -> some View {
        #if os(iOS)
        self.presentationDetents(cardHeight > 0 ? [.height(cardHeight), .large] : [.medium, .large])
            .presentationDragIndicator(.hidden)
        #else
        self
        #endif
    }
}

/// The natural height a pinned `SecureInputCard` reports (details + its button bar).
public struct SecureInputCardHeight: PreferenceKey {
    public static let defaultValue: CGFloat = 0
    public static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
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
        skill: "weather", expiresAt: Date().addingTimeInterval(118),
        reason: "The Weather skill needs this key for current conditions and forecasts. Your agent asked for it because this task uses that skill.",
        skipLabel: "Skip — the agent has another way")
    public static let sudo = SecureInputRequest(
        id: "sec_fixture_sudo_command_00", kind: .sudo, job: "Fix the Mac mini's SSH", label: "Your Mac's password",
        command: "sudo launchctl kickstart -k system/com.openssh.sshd", expiresAt: Date().addingTimeInterval(97))
    public static let login = SecureInputRequest(
        id: "sec_fixture_login_example0", kind: .login, job: "Book the dinner reservation", label: "Your login for example.com",
        origin: "https://www.example.com", site: "www.example.com", expiresAt: Date().addingTimeInterval(171))
}
