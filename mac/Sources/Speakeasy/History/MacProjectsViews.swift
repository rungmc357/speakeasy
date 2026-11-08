import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// SPK-254: where Home works, on the Mac. The same ProjectsModel as the phone (the plugin stores the
// choice, so both agree): a sidebar section (Home first, then each project with its activity line,
// expandable to its three newest chats, New project last) and a composer chip beside the model menu.

enum MacProjectTint {
    static func color(_ c: ProjectColor?) -> Color {
        switch c {
        case .blue: return .blue
        case .teal: return .teal
        case .green: return .green
        case .yellow: return .yellow
        case .orange: return .orange
        case .red: return .red
        case .pink: return .pink
        case .purple: return .purple
        case .gray, .none: return .gray
        }
    }
}

/// The sidebar's Projects section (nothing on servers without projects, or with none yet).
struct MacProjectsSidebarSection: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var projects: ProjectsModel
    @State private var naming = false
    @State private var name = ""

    var body: some View {
        if projects.supported && projects.loaded {
            VStack(alignment: .leading, spacing: 1) {
                row(id: nil, symbol: HomeSpace.homeSymbol, color: nil, name: HomeSpace.homeName, line: "Not in a project")
                if !projects.list.projects.isEmpty {
                    Text("PROJECTS").font(.system(size: 10, weight: .bold)).tracking(0.6).foregroundStyle(.tertiary)
                        .padding(.leading, 10).padding(.top, 8).padding(.bottom, 2)
                        .accessibilityAddTraits(.isHeader)
                }
                ForEach(projects.list.projects) { p in
                    row(id: p.id, symbol: p.symbol, color: p.color, name: p.name, line: p.activityLine(), running: p.running > 0)
                    if projects.expanded.contains(p.id) { inline(p) }
                }
                Button { name = ""; naming = true } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "plus").font(.system(size: 11, weight: .semibold)).frame(width: 26, height: 26)
                        Text("New project").font(.system(size: 12))
                        Spacer(minLength: 0)
                    }
                    .foregroundStyle(.secondary).padding(.horizontal, 10).frame(height: 32).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("projects-new")
            }
            .alert("New project", isPresented: $naming) {
                TextField("Name", text: $name)
                Button("Create") {
                    let n = name
                    Task { _ = await projects.create(NewProject(name: n, select: true)) }
                }
                Button("Cancel", role: .cancel) {}
            } message: { Text("New jobs from Home go into it.") }
        }
    }

    private func row(id: String?, symbol: String, color: ProjectColor?, name: String, line: String, running: Bool = false) -> some View {
        let on = projects.list.selected == id
        return HStack(spacing: 0) {
            Button { Task { await projects.select(id) } } label: {
                HStack(spacing: 10) {
                    RoundedRectangle(cornerRadius: 7, style: .continuous)
                        .fill(id == nil ? Color.primary.opacity(0.06) : MacProjectTint.color(color).opacity(0.18))
                        .frame(width: 26, height: 26)
                        .overlay(Image(systemName: symbol).font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(id == nil ? Color.primary.opacity(0.6) : MacProjectTint.color(color)))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(name).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                        HStack(spacing: 4) {
                            if running { Circle().fill(Live.brass).frame(width: 5, height: 5).accessibilityHidden(true) }
                            Text(line).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer(minLength: 4)
                }
                .padding(.leading, 10).padding(.vertical, 5)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(id == nil ? HomeSpace.homeRow : "\(name), \(line)")
            .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
            .accessibilityIdentifier("project-row-\(id ?? "home")")
            if let id {
                Button { withAnimation(.easeInOut(duration: 0.18)) { projects.toggleExpanded(id) } } label: {
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(projects.expanded.contains(id) ? 90 : 0))
                        .frame(width: 28, height: 28).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(projects.expanded.contains(id) ? "Hide \(name)'s chats" : "Show \(name)'s chats")
            }
        }
        .padding(.trailing, 4)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(on ? HistoryLook.selected : .clear))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(on ? Live.brass.opacity(0.32) : .clear, lineWidth: 0.75))
    }

    @ViewBuilder private func inline(_ p: HermesProject) -> some View {
        let recent = projects.pages[p.id]?.recentChats() ?? []
        VStack(alignment: .leading, spacing: 0) {
            ForEach(recent) { chat in
                Button { store.open(chat.id) } label: {
                    Text(chat.title).font(.system(size: 12)).lineLimit(1).foregroundStyle(.primary.opacity(0.8))
                        .frame(maxWidth: .infinity, minHeight: 24, alignment: .leading).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            if projects.pages[p.id] != nil && recent.isEmpty {
                Text("No chats yet").font(.system(size: 12)).foregroundStyle(.tertiary).frame(minHeight: 24)
            }
        }
        .padding(.leading, 46).padding(.trailing, 10).padding(.bottom, 4)
    }
}

/// Home's composer chip on the Mac: house + Home by default; a menu titled Project.
struct MacProjectChip: View {
    @ObservedObject var projects: ProjectsModel

    var body: some View {
        if projects.showsChip {
            Menu {
                Section("Project") {
                    Button { Task { await projects.select(nil) } } label: {
                        Label(HomeSpace.homeRow, systemImage: projects.selected == nil ? "checkmark" : HomeSpace.homeSymbol)
                    }
                    ForEach(projects.list.projects) { p in
                        Button { Task { await projects.select(p.id) } } label: {
                            Label(p.name, systemImage: projects.list.selected == p.id ? "checkmark" : p.symbol)
                        }
                    }
                }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: projects.chipSymbol).font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(projects.selected == nil ? Color.secondary : MacProjectTint.color(projects.selected?.color))
                    Text(projects.chipName).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("Where the jobs you start from Home go")
            .accessibilityLabel("Working in \(projects.chipName)")
            .accessibilityIdentifier("home-project-chip")
        }
    }
}
