import SwiftUI
import AppKit
import SpeakeasyCore
import SpeakeasyClient

// The History composer's model chip and its popover (servers with `model_choice`): which model works
// this chat and how hard it thinks. Quiet by default ("GPT-5.5 · Medium" in secondary ink); brass
// when the chat has its own pick.

struct ModelChip: View {
    @ObservedObject var store: HistoryStore
    @State private var open = false
    @State private var hover = false

    var body: some View {
        if let picker = store.picker {
            Button { open.toggle() } label: {
                HStack(spacing: 5) {
                    Image(systemName: "sparkle").font(.system(size: 9, weight: .bold))
                        .foregroundStyle(picker.followsDefault ? Color.secondary : Live.brassText)
                    Text(picker.chipLabel).font(.system(size: 11, weight: .semibold)).lineLimit(1)
                    Image(systemName: "chevron.up.chevron.down").font(.system(size: 7.5, weight: .bold)).foregroundStyle(.tertiary)
                }
                .foregroundStyle(picker.followsDefault ? Color.primary.opacity(0.62) : Live.brassText)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(picker.followsDefault
                                           ? Color.primary.opacity(hover ? 0.08 : 0.045)
                                           : Live.brass.opacity(hover ? 0.18 : 0.12)))
                .overlay(Capsule().strokeBorder(picker.followsDefault ? Live.hairline : Live.brass.opacity(0.35), lineWidth: 0.5))
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .onHover { hover = $0 }
            .help("Model and reasoning for this chat")
            .accessibilityLabel("Model: \(picker.chipLabel)")
            .accessibilityIdentifier("model-chip")
            .popover(isPresented: $open, arrowEdge: .top) {
                ModelPopover(store: store, state: picker) { open = false }
            }
        }
    }
}

struct ModelPopover: View {
    @ObservedObject var store: HistoryStore
    @State var state: ModelPickerState
    var done: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Model").font(.system(size: 13, weight: .semibold))
                Spacer()
                if !state.followsDefault {
                    Button("Use default") { store.useDefaultModel(); done() }
                        .buttonStyle(.plain).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Live.brassText)
                }
            }
            .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 8)
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(state.catalog.providers) { provider in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 5) {
                                Text(provider.name.uppercased()).tracking(0.7)
                                if provider.local { Text("· ON YOUR MACHINES").tracking(0.5) }
                            }
                            .font(.system(size: 9.5, weight: .bold)).foregroundStyle(.tertiary)
                            .padding(.horizontal, 8).padding(.bottom, 2)
                            ForEach(provider.models) { row($0) }
                        }
                    }
                }
                .padding(.horizontal, 6).padding(.bottom, 6)
            }
            .frame(maxHeight: 280)
            .fixedSize(horizontal: false, vertical: true)
            Divider().overlay(Live.hairline)
            VStack(alignment: .leading, spacing: 8) {
                if state.levels.isEmpty {
                    Text("\(state.model?.name ?? "This model") doesn't use reasoning levels.")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                } else {
                    Text("REASONING").font(.system(size: 9.5, weight: .bold)).tracking(0.7).foregroundStyle(.tertiary)
                    Picker("Reasoning", selection: Binding(get: { state.reasoning ?? state.levels.first! },
                                                          set: { state.select($0) })) {
                        ForEach(state.levels) { level in
                            Text(state.levels.count > 4 ? level.short : level.label).tag(level)
                        }
                    }
                    .pickerStyle(.segmented).labelsHidden()
                    .accessibilityIdentifier("model-reasoning")
                }
                HStack(spacing: 8) {
                    Button { store.applyModel(state, scope: .makeDefault); done() } label: {
                        Text("Make default").font(.system(size: 12, weight: .medium)).foregroundStyle(Color.primary.opacity(0.8))
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .background(Capsule().fill(Color.primary.opacity(0.07)))
                    }
                    .buttonStyle(.plain)
                    .help("Every chat without its own pick uses this")
                    .accessibilityIdentifier("model-make-default")
                    Spacer()
                    Button { store.applyModel(state, scope: .chat); done() } label: {
                        Text("Use for this chat").font(.system(size: 12, weight: .semibold)).foregroundStyle(BrandPalette.onBrass)
                            .padding(.horizontal, 14).padding(.vertical, 6)
                            .background(Capsule().fill(Live.brass))
                    }
                    .buttonStyle(.plain)
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("model-use-for-chat")
                }
                .padding(.top, 2)
            }
            .padding(14)
        }
        .frame(width: 320)
    }

    private func row(_ m: ModelOption) -> some View {
        let on = state.model?.id == m.id
        return Button { withAnimation(.snappy(duration: 0.18)) { state.select(m) } } label: {
            HStack(spacing: 8) {
                Image(systemName: "checkmark").font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Live.brassText).opacity(on ? 1 : 0).frame(width: 12)
                Text(m.name).font(.system(size: 12.5, weight: on ? .semibold : .regular))
                Spacer(minLength: 6)
                if m.isDefault {
                    Text("Default").font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.secondary)
                        .padding(.horizontal, 6).padding(.vertical, 1.5)
                        .background(Capsule().fill(Color.primary.opacity(0.06)))
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(on ? Live.brass.opacity(0.12) : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("model-row-\(m.id)")
    }
}

/// The session header's "Worked on by GPT-5.5 · High" line: small and secondary, and only when the
/// chat ran on a model other than the composer chip's current choice.
struct ModelWorkedLine: View {
    let badge: ModelBadge
    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "sparkle").font(.system(size: 8, weight: .bold))
            Text("Ran on \(badge.label)")
        }
        .font(.system(size: 10.5)).foregroundStyle(.tertiary)
        .help(badge.served ? "Hermes ran this chat's latest work on \(badge.label)" : "Asked for \(badge.label)")
    }
}
