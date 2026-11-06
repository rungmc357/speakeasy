import SwiftUI
import AppKit
import SpeakeasyCore
import SpeakeasyClient

// New chat's agent chip (servers with `agents`, more than one Hermes profile allowed in Settings ›
// Agents): which agent the chat talks to. It sits in the window's title bar, top right of the page.
// A chat already on another agent shows that agent's name there instead (it can't move).

struct AgentChip: View {
    @ObservedObject var store: HistoryStore
    @State private var open = false
    @State private var hover = false

    var body: some View {
        if let current = store.newChatAgentInfo, let roster = store.roster {
            let picked = !current.isDefault && current.id != roster.defaultID
            Button { open.toggle() } label: {
                HStack(spacing: 6) {
                    AgentMark(name: current.name, size: 16, accent: picked)
                    Text(current.name).font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
                    Image(systemName: "chevron.up.chevron.down").font(.system(size: 7.5, weight: .bold)).foregroundStyle(.tertiary)
                }
                .foregroundStyle(picked ? Live.brassText : Color.primary.opacity(0.7))
                .padding(.leading, 4).padding(.trailing, 9).padding(.vertical, 4)
                .background(Capsule().fill(picked ? Live.brass.opacity(hover ? 0.18 : 0.12)
                                                  : Color.primary.opacity(hover ? 0.08 : 0.045)))
                .overlay(Capsule().strokeBorder(picked ? Live.brass.opacity(0.35) : Live.hairline, lineWidth: 0.5))
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .onHover { hover = $0 }
            .help("Which agent this chat talks to")
            .accessibilityLabel("Agent: \(current.name)")
            .accessibilityIdentifier("agent-chip")
            .popover(isPresented: $open, arrowEdge: .bottom) {
                AgentPopover(roster: roster, selected: current.id) { id in store.pickAgent(id); open = false }
            }
        }
    }
}

struct AgentPopover: View {
    let roster: AgentRoster
    let selected: String
    var pick: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Talk to").font(.system(size: 13, weight: .semibold))
                .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 6)
            VStack(alignment: .leading, spacing: 2) {
                ForEach(roster.agents) { agent in row(agent) }
            }
            .padding(.horizontal, 6)
            Divider().overlay(Live.hairline).padding(.top, 6)
            Text("Each agent has its own memory, skills and model. The chat stays with the one you pick. Choose which ones show here in Settings › Agents.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14).padding(.vertical, 10)
        }
        .frame(width: 300)
    }

    private func row(_ agent: AgentInfo) -> some View {
        Button { pick(agent.id) } label: {
            HStack(alignment: .top, spacing: 9) {
                AgentMark(name: agent.name, size: 22, accent: agent.id == selected)
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 5) {
                        Text(agent.name).font(.system(size: 12.5, weight: .semibold))
                        if agent.isDefault || agent.id == roster.defaultID {
                            Text("Main").font(.system(size: 9.5, weight: .bold)).foregroundStyle(.tertiary)
                        }
                    }
                    if let d = agent.description {
                        Text(d).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 4)
                if agent.id == selected {
                    Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(Live.brassText)
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(agent.id == selected ? Live.brass.opacity(0.1) : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("agent-row-\(agent.id)")
    }
}

/// A chat on another agent: its name, quiet, where New chat has the chip.
struct AgentBadge: View {
    let agent: AgentInfo
    var body: some View {
        HStack(spacing: 6) {
            AgentMark(name: agent.name, size: 16, accent: true)
            Text(agent.name).font(.system(size: 11.5, weight: .semibold)).lineLimit(1)
        }
        .foregroundStyle(Live.brassText)
        .padding(.leading, 4).padding(.trailing, 9).padding(.vertical, 4)
        .background(Capsule().fill(Live.brass.opacity(0.1)))
        .help(agent.description.map { "\(agent.name): \($0)" } ?? "This chat talks to \(agent.name)")
        .accessibilityLabel("Agent: \(agent.name)")
        .accessibilityIdentifier("agent-badge")
    }
}

/// The agent's initial in a small circle.
struct AgentMark: View {
    let name: String
    var size: CGFloat
    var accent: Bool
    var body: some View {
        Text(String(name.prefix(1)).uppercased())
            .font(.system(size: size * 0.55, weight: .bold))
            .foregroundStyle(accent ? JobLook.brassInk : Color.primary.opacity(0.7))
            .frame(width: size, height: size)
            .background(Circle().fill(accent ? Live.brass : Color.primary.opacity(0.1)))
    }
}
