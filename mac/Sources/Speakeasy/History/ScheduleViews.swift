import AppKit
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

// History › Scheduled: the Hermes scheduled (cron) jobs of the paired Hermes (servers with
// `schedules`). The sidebar's Scheduled row opens the list on the right; a schedule opens its page
// (prompt, recent runs that open their session, Run now, Pause, Edit, Delete). New schedule takes
// one sentence and shows a preview card to confirm before anything is saved.

enum ScheduleLook {
    static func tint(_ status: ScheduledJob.RunStatus?) -> Color {
        switch status {
        case .ok: return Live.green
        case .failed, .missed: return Live.red
        case .running: return Live.brass
        case .cancelled, nil: return Color.secondary
        }
    }
    static func symbol(_ status: ScheduledJob.RunStatus?) -> String {
        switch status {
        case .ok: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.circle.fill"
        case .missed: return "clock.badge.exclamationmark"
        case .cancelled: return "stop.circle"
        case .running: return "circle.dotted"
        case nil: return "circle"
        }
    }
}

/// The sidebar's entry: "Scheduled", with what runs next.
struct ScheduledSidebarRow: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var schedules: SchedulesModel
    @State private var hover = false

    var body: some View {
        let on = store.showingSchedules
        Button { store.openSchedules() } label: {
            HStack(spacing: 10) {
                Circle().fill(on ? Live.brass.opacity(0.22) : Color.primary.opacity(0.06)).frame(width: 26, height: 26)
                    .overlay(Image(systemName: "clock.fill").font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(on ? Live.brass : Color.primary.opacity(0.6)))
                VStack(alignment: .leading, spacing: 1) {
                    Text("Scheduled").font(.system(size: 12.5, weight: .semibold))
                    Text(subtitle).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
                if schedules.activeCount > 0 {
                    Text("\(schedules.activeCount)").font(.system(size: 10.5, weight: .semibold).monospacedDigit())
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6).frame(height: 17)
                        .background(Capsule().fill(Color.primary.opacity(0.06)))
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(on ? HistoryLook.selected : hover ? HistoryLook.hover : .clear))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(on ? Live.brass.opacity(0.32) : .clear, lineWidth: 0.75))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Scheduled, \(subtitle)")
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier("history-scheduled")
    }

    private var subtitle: String {
        if let job = schedules.jobs.first(where: \.isRunning) { return "\(job.name) is running" }
        if let next = schedules.nextUp, let at = next.nextRun {
            return "Next: \(ScheduleFormat.next(at, now: store.now))"
        }
        if !schedules.loaded { return "Scheduled jobs on your Hermes" }
        return schedules.jobs.isEmpty ? "Nothing scheduled yet" : "All paused"
    }
}

/// The right side while Scheduled is open: the list, or one schedule.
struct SchedulesPane: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var schedules: SchedulesModel

    var body: some View {
        Group {
            if let id = schedules.selected {
                ScheduleDetailView(store: store, schedules: schedules, id: id).id(id)
            } else {
                ScheduleListView(store: store, schedules: schedules)
            }
        }
        .onAppear { schedules.startRefreshing() }
        .onDisappear { schedules.stopRefreshing() }
    }
}

struct ScheduleListView: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var schedules: SchedulesModel
    @State private var creating = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if creating || schedules.draft.showsCard || !schedules.draft.text.isEmpty {
                    ScheduleComposer(schedules: schedules, now: store.now, dictation: store.scheduleDictation,
                                     micVisible: store.voice.micVisible) { id in
                        creating = false
                        if let id { schedules.selected = id }
                    } cancel: { creating = false; schedules.startDraft() }
                }
                content
            }
            .frame(maxWidth: HistoryLook.readable, alignment: .leading)
            .padding(.horizontal, 28).padding(.top, 34).padding(.bottom, 24)
            .frame(maxWidth: .infinity)
        }
        .accessibilityIdentifier("schedules-list")
    }

    private var header: some View {
        HStack(alignment: .bottom, spacing: 10) {
            VStack(alignment: .leading, spacing: 6) {
                // Same words as the iPhone ("Hermes time · New York"): never the raw zone id, one line.
                HStack(spacing: 6) {
                    Image(systemName: "clock.arrow.circlepath").font(.system(size: 10.5, weight: .bold))
                    Text(ScheduleList.zoneLabel(schedules.timezone))
                        .lineLimit(1).truncationMode(.tail)
                }
                .font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Live.brass)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("schedules-zone")
                Text("Scheduled jobs").font(.system(size: 24, weight: .bold))
                Text("What your Hermes does on its own, on a schedule. Results go where each one says.")
                    .font(.system(size: 12.5, weight: .medium)).foregroundStyle(Color.primary.opacity(0.6))
            }
            Spacer()
            if !creating && !schedules.draft.showsCard {
                Button { creating = true; schedules.startDraft() } label: {
                    Label("New schedule", systemImage: "plus").font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 12).frame(height: 28)
                        .background(Capsule().fill(Live.brass)).foregroundStyle(JobLook.brassInk)
                }
                .buttonStyle(.plain)
                .keyboardShortcut("n", modifiers: [.command, .shift])
                .help("New schedule (⇧⌘N)")
                .accessibilityIdentifier("schedules-new")
            }
        }
    }

    @ViewBuilder private var content: some View {
        if schedules.loading {
            HStack(spacing: 8) { LiveSpinner(size: 12); Text("Loading…").font(.system(size: 12)).foregroundStyle(.secondary) }
                .frame(maxWidth: .infinity).padding(.top, 40)
        } else if let error = schedules.error {
            VStack(spacing: 8) {
                Text(error).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center)
                Button("Try again") { Task { await schedules.reload() } }.buttonStyle(.plain)
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(Live.brassText)
            }
            .frame(maxWidth: .infinity).padding(.top, 40)
        } else if schedules.jobs.isEmpty {
            if !creating { empty }
        } else {
            ForEach(schedules.sections, id: \.0) { section, jobs in
                VStack(alignment: .leading, spacing: 8) {
                    Text(section.rawValue.uppercased()).font(.system(size: 10, weight: .bold)).tracking(0.9).foregroundStyle(.tertiary)
                    VStack(spacing: 0) {
                        ForEach(Array(jobs.enumerated()), id: \.element.id) { i, job in
                            if i > 0 { Rectangle().fill(Live.hairline).frame(height: 1).padding(.leading, 52) }
                            ScheduleRowView(job: job, now: store.now) { schedules.selected = job.id }
                        }
                    }
                    .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
                    .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
                }
            }
        }
    }

    private var empty: some View {
        VStack(spacing: 10) {
            Image(systemName: "clock.badge.checkmark").font(.system(size: 30, weight: .light)).foregroundStyle(Live.brass.opacity(0.85))
            Text("Nothing scheduled yet").font(.system(size: 14, weight: .semibold))
            Text("Ask your Hermes to do something every morning, like “a weather brief at 7 AM”.")
                .font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 360)
            Button { creating = true; schedules.startDraft() } label: {
                Label("New schedule", systemImage: "plus").font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 12).frame(height: 28)
                    .background(Capsule().fill(Live.brass)).foregroundStyle(JobLook.brassInk)
            }
            .buttonStyle(.plain).padding(.top, 4)
        }
        .frame(maxWidth: .infinity).padding(.top, 50)
    }
}

/// One schedule in the list: name, when it runs, the next run (prominent), the last result.
struct ScheduleRowView: View {
    let job: ScheduledJob
    var now: Date
    var action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 12) {
                Circle().fill(job.isRunning ? Live.brass.opacity(0.18) : Color.primary.opacity(0.06)).frame(width: 28, height: 28)
                    .overlay {
                        if job.isRunning { LiveSpinner(size: 12) }
                        else {
                            Image(systemName: job.isPaused ? "pause.fill" : job.isFinished ? "checkmark" : "clock")
                                .font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Color.primary.opacity(0.6))
                        }
                    }
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(job.name).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                        Spacer(minLength: 6)
                        nextRun
                    }
                    Text([job.scheduleLabel, job.deliveryLabel].joined(separator: " · "))
                        .font(.system(size: 11.5, weight: .medium)).foregroundStyle(.secondary).lineLimit(1)
                    if !job.lastResult.isEmpty {
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Image(systemName: ScheduleLook.symbol(job.lastStatus)).font(.system(size: 9.5, weight: .bold))
                                .foregroundStyle(ScheduleLook.tint(job.lastStatus))
                            Text(job.lastResult).font(.system(size: 11.5)).foregroundStyle(Color.primary.opacity(0.7)).lineLimit(2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, 1)
                    }
                }
                Image(systemName: "chevron.right").font(.system(size: 10, weight: .bold)).foregroundStyle(.tertiary).padding(.top, 4)
            }
            .padding(.horizontal, 12).padding(.vertical, 11)
            .background(hover ? HistoryLook.hover : .clear)
            .contentShape(Rectangle())
            .opacity(job.isDimmed ? 0.55 : 1)
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibility)
        .accessibilityIdentifier("schedule-row-\(job.id)")
    }

    @ViewBuilder private var nextRun: some View {
        if job.isRunning {
            Text("Running").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(Live.brassText)
        } else if job.isPaused {
            Text("Paused").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
        } else if let at = job.nextRun {
            Text(ScheduleFormat.next(at, now: now)).font(.system(size: 11.5, weight: .semibold).monospacedDigit())
                .foregroundStyle(Live.brassText).fixedSize()
        } else if job.isFinished {
            Text("Done").font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.secondary)
        }
    }

    private var accessibility: String {
        var parts = [job.name, job.scheduleLabel]
        if job.isPaused { parts.append("paused") }
        if job.isRunning { parts.append("running") }
        if let at = job.nextRun, !job.isPaused { parts.append("next \(ScheduleFormat.next(at, now: now))") }
        if job.lastStatus != nil { parts.append("last run \(ScheduleFormat.statusWord(job.lastStatus))") }
        return parts.joined(separator: ", ")
    }
}

/// New schedule: one sentence, then the preview card to confirm.
struct ScheduleComposer: View {
    @ObservedObject var schedules: SchedulesModel
    var now: Date
    @ObservedObject var dictation: ScheduleDictation
    /// No mic during a call (the call has it).
    var micVisible: Bool = true
    var done: (String?) -> Void
    var cancel: () -> Void
    @FocusState private var focused: Bool

    var body: some View {
        let d = schedules.draft
        VStack(alignment: .leading, spacing: 12) {
            if d.showsCard, let p = d.preview {
                previewCard(p, saving: d.phase == .saving)
            } else {
                HStack(spacing: 6) {
                    Image(systemName: "plus.circle.fill").font(.system(size: 10.5, weight: .bold))
                    Text("NEW SCHEDULE").font(.system(size: 10.5, weight: .bold)).tracking(0.9)
                }
                .foregroundStyle(Live.brass)
                HStack(spacing: 8) {
                    TextField("Every weekday at 8, summarize my inbox", text: $schedules.draft.text, axis: .vertical)
                        .textFieldStyle(.plain).font(.system(size: 14)).lineLimit(1...4)
                        .focused($focused)
                        .onSubmit { Task { await schedules.previewDraft() } }
                        .onExitCommand { cancel() }
                        .accessibilityIdentifier("schedules-draft")
                    if d.phase == .reading { LiveSpinner(size: 12) }
                    else if micVisible { micButton }
                }
                .padding(.horizontal, 12).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(HistoryLook.field))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(focused ? Live.brass.opacity(0.55) : Live.hairline, lineWidth: focused ? 1 : 0.5))
                if dictation.listening {
                    Text("Listening… say when it should run and what it should do. Tap the mic again when you're done.")
                        .font(.system(size: 11)).foregroundStyle(Live.brass).fixedSize(horizontal: false, vertical: true)
                } else if let problem = dictation.problem {
                    Text(problem).font(.system(size: 11.5)).foregroundStyle(Live.red).fixedSize(horizontal: false, vertical: true)
                } else if let error = d.error {
                    Text(error).font(.system(size: 11.5)).foregroundStyle(Live.red).fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(micVisible ? "Type or tap the mic and say when and what: “every morning at 7 check the garden forecast”, “every 2 hours check the build”."
                         : "Say when and what. Try “every morning at 7 check the garden forecast” or “every 2 hours check the build”.")
                        .font(.system(size: 11)).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 8) {
                    Spacer()
                    HistoryPillButton(title: "Cancel") { cancel() }
                    HistoryPillButton(title: "Preview", primary: true) { Task { await schedules.previewDraft() } }
                        .disabled(!d.canPreview).opacity(d.canPreview ? 1 : 0.5)
                }
            }
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Live.card))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Live.brass.opacity(0.28), lineWidth: 0.75))
        .onAppear { focused = true }
        .onDisappear { dictation.cancel() }
        .accessibilityIdentifier("schedules-composer")
    }

    private var micButton: some View {
        Button {
            dictation.toggle(into: schedules) { focused = true }
        } label: {
            Image(systemName: dictation.listening ? "stop.fill" : "mic.fill")
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(dictation.listening ? Color.white : Live.brass)
                .frame(width: 24, height: 24)
                .background(Circle().fill(dictation.listening ? Live.red : Live.brass.opacity(0.14)))
        }
        .buttonStyle(.plain)
        .help(dictation.listening ? "Stop" : "Say it")
        .accessibilityLabel(dictation.listening ? "Stop dictating" : "Say the schedule")
        .accessibilityIdentifier("schedules-mic")
    }

    private func previewCard(_ p: SchedulePreview, saving: Bool) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Image(systemName: "checkmark.seal.fill").font(.system(size: 10.5, weight: .bold))
                Text("CHECK BEFORE SAVING").font(.system(size: 10.5, weight: .bold)).tracking(0.9)
            }
            .foregroundStyle(Live.brass)
            Text(p.scheduleLabel).font(.system(size: 20, weight: .bold))
            if let next = p.nextRun {
                HStack(spacing: 6) {
                    Text("Next").foregroundStyle(.secondary)
                    Text(ScheduleFormat.when(next, now: now)).fontWeight(.semibold).foregroundStyle(Live.brassText)
                    if p.nextRuns.count > 1 {
                        Text("· then " + p.nextRuns.dropFirst().prefix(2).map { ScheduleFormat.when($0, now: now) }.joined(separator: ", "))
                            .foregroundStyle(.tertiary)
                    }
                }
                .font(.system(size: 12.5).monospacedDigit())
            }
            if let assumed = p.assumed {
                Label("No time was said, so it runs at \(assumed). Edit to change it.", systemImage: "info.circle")
                    .font(.system(size: 11.5)).foregroundStyle(Live.amber)
            }
            Text(p.prompt).font(.system(size: 13)).foregroundStyle(Color.primary.opacity(0.88))
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12).padding(.vertical, 9).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Live.well))
            HStack(spacing: 14) {
                LabeledContent("Name") {
                    TextField("Name", text: $schedules.draft.name).textFieldStyle(.roundedBorder).frame(maxWidth: 240)
                }
                LabeledContent("Send results to") {
                    Picker("", selection: $schedules.draft.deliver) {
                        ForEach(p.targets) { Text($0.label).tag($0.id) }
                    }
                    .labelsHidden().fixedSize()
                }
            }
            .font(.system(size: 12))
            if let error = schedules.draft.error {
                Text(error).font(.system(size: 11.5)).foregroundStyle(Live.red).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                if saving { LiveSpinner(size: 12); Text("Saving…").font(.system(size: 11.5)).foregroundStyle(.secondary) }
                Spacer()
                HistoryPillButton(title: "Edit") { schedules.draft.edit() }
                HistoryPillButton(title: "Save schedule", primary: true) {
                    Task { let id = await schedules.confirmDraft(); if id != nil { done(id) } }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(saving)
                .accessibilityIdentifier("schedules-save")
            }
        }
        .accessibilityIdentifier("schedules-preview")
    }
}

/// One schedule's page.
struct ScheduleDetailView: View {
    @ObservedObject var store: HistoryStore
    @ObservedObject var schedules: SchedulesModel
    let id: String
    @State private var editing = false
    @State private var editWhen = ""
    @State private var editWhat = ""
    @State private var confirmDelete = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Button { schedules.selected = nil } label: {
                    Label("Scheduled", systemImage: "chevron.left").font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Live.brassText)
                }
                .buttonStyle(.plain).keyboardShortcut("[", modifiers: .command)
                .accessibilityIdentifier("schedule-back")
                if let job = schedules.job(id) {
                    header(job)
                    actions(job)
                    if let n = schedules.notices[id] {
                        Text(n.text).font(.system(size: 12, weight: .medium))
                            .foregroundStyle(n.isError ? Live.red : Live.green)
                            .accessibilityIdentifier("schedule-notice")
                    }
                    if editing { editor } else if let d = schedules.details[id] { promptCard(d) }
                    if let d = schedules.details[id] { runsCard(d) }
                    else if let error = schedules.detailErrors[id] {
                        Text(error).font(.system(size: 12)).foregroundStyle(.secondary)
                    } else {
                        HStack(spacing: 8) { LiveSpinner(size: 12); Text("Loading runs…").font(.system(size: 12)).foregroundStyle(.secondary) }
                    }
                } else if let error = schedules.detailErrors[id] {
                    Text(error).font(.system(size: 12)).foregroundStyle(.secondary).padding(.top, 30)
                } else {
                    LiveSpinner(size: 13).padding(.top, 30)
                }
            }
            .frame(maxWidth: HistoryLook.readable, alignment: .leading)
            .padding(.horizontal, 28).padding(.top, 34).padding(.bottom, 24)
            .frame(maxWidth: .infinity)
        }
        .background(alignment: .top) {
            MoodGlow(mood: schedules.job(id)?.isRunning == true ? .working : .quiet).frame(height: 240).opacity(0.7)
        }
        .task { await schedules.load(id) }
        .confirmationDialog("Delete “\(schedules.job(id)?.name ?? "this schedule")”?", isPresented: $confirmDelete) {
            Button("Delete", role: .destructive) { Task { _ = await schedules.delete(id) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("It stops running and is removed from your Hermes. Past results stay in History.")
        }
        .accessibilityIdentifier("schedule-page")
    }

    private func header(_ job: ScheduledJob) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                LiveDot(color: job.isRunning ? Live.brass : job.isPaused ? Color.secondary : Live.green, size: 6, active: job.isRunning)
                Text(job.isRunning ? "RUNNING" : job.isPaused ? "PAUSED" : job.isFinished ? "DONE" : "SCHEDULED").tracking(1.0)
                Text("·").foregroundStyle(.secondary)
                Text(job.deliveryLabel.uppercased()).tracking(0.6).foregroundStyle(Color.primary.opacity(0.55)).lineLimit(1)
            }
            .font(.system(size: 10.5, weight: .bold))
            .foregroundStyle(job.isRunning ? Live.brass : job.isPaused ? Color.secondary : Live.green)
            Text(job.name).font(.system(size: 24, weight: .bold)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            Text(job.scheduleLabel).font(.system(size: 14, weight: .medium)).foregroundStyle(Color.primary.opacity(0.7))
            Group {
                if let at = job.nextRun, !job.isPaused {
                    (Text("Next run ").foregroundColor(.secondary) + Text(ScheduleFormat.next(at, now: store.now)).foregroundColor(Live.brassText).bold())
                } else if job.isPaused {
                    Text("Paused. Resume to put it back on its schedule.").foregroundStyle(.secondary)
                } else if job.isFinished {
                    Text("Ran once. Make a new schedule to run it again.").foregroundStyle(.secondary)
                }
            }
            .font(.system(size: 13).monospacedDigit())
        }
    }

    private func actions(_ job: ScheduledJob) -> some View {
        let busy = schedules.busy.contains(id)
        return HStack(spacing: 8) {
            // Paused: Resume leads (Run now would resume it too, so it's the second choice).
            if job.isPaused {
                HistoryPillButton(title: "Resume", primary: true) { Task { await schedules.resume(id) } }
                    .accessibilityIdentifier("schedule-pause")
                if job.canRunNow {
                    HistoryPillButton(title: "Run once now") { Task { await schedules.runNow(id) } }
                        .help("Runs it now and resumes its schedule")
                        .accessibilityIdentifier("schedule-run")
                }
            } else {
                if job.canRunNow {
                    HistoryPillButton(title: "Run now", primary: true) { Task { await schedules.runNow(id) } }
                        .accessibilityIdentifier("schedule-run")
                }
                if !job.isFinished {
                    HistoryPillButton(title: "Pause") { Task { await schedules.pause(id) } }
                        .accessibilityIdentifier("schedule-pause")
                }
            }
            if !job.isFinished {
                HistoryPillButton(title: editing ? "Cancel edit" : "Edit") { toggleEdit() }
                    .accessibilityIdentifier("schedule-edit")
            }
            Spacer()
            if busy { LiveSpinner(size: 12) }
            Button { confirmDelete = true } label: {
                Label("Delete", systemImage: "trash").font(.system(size: 12, weight: .semibold)).foregroundStyle(Live.red)
            }
            .buttonStyle(.plain).help("Delete this schedule")
            .accessibilityIdentifier("schedule-delete")
        }
        .disabled(busy)
    }

    private func toggleEdit() {
        if editing { editing = false; return }
        let d = schedules.details[id]
        editWhen = d?.job.scheduleLabel ?? ""
        editWhat = d?.prompt ?? ""
        editing = true
    }

    private var editor: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("WHEN").font(.system(size: 10, weight: .bold)).tracking(0.9).foregroundStyle(.tertiary)
            TextField("Weekdays at 9 AM", text: $editWhen).textFieldStyle(.plain).font(.system(size: 13.5))
                .padding(.horizontal, 10).frame(height: 32)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(HistoryLook.field))
                .accessibilityIdentifier("schedule-edit-when")
            Text("In words (“weekdays at 9”, “every 2 hours”) or cron (0 9 * * 1-5).")
                .font(.system(size: 10.5)).foregroundStyle(.tertiary)
            Text("WHAT IT DOES").font(.system(size: 10, weight: .bold)).tracking(0.9).foregroundStyle(.tertiary).padding(.top, 4)
            TextEditor(text: $editWhat).font(.system(size: 13)).scrollContentBackground(.hidden)
                .frame(minHeight: 90, maxHeight: 200)
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(HistoryLook.field))
                .accessibilityIdentifier("schedule-edit-prompt")
            HStack {
                Spacer()
                HistoryPillButton(title: "Cancel") { editing = false }
                HistoryPillButton(title: "Save", primary: true) {
                    let when = editWhen == schedules.job(id)?.scheduleLabel ? nil : editWhen
                    Task { if await schedules.save(id, schedule: when, prompt: editWhat) { editing = false } }
                }
                .keyboardShortcut("s", modifiers: .command)
            }
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Live.brass.opacity(0.28), lineWidth: 0.75))
    }

    private func promptCard(_ d: ScheduleDetail) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(d.scriptOnly ? "SCRIPT" : "WHAT IT DOES").font(.system(size: 10, weight: .bold)).tracking(0.9).foregroundStyle(.tertiary)
            Text(d.prompt.isEmpty ? "Runs a script; no prompt." : d.prompt)
                .font(.system(size: 13.5)).lineSpacing(2).foregroundStyle(Color.primary.opacity(0.9))
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            if !d.skills.isEmpty {
                Text("Uses " + d.skills.joined(separator: ", ")).font(.system(size: 11)).foregroundStyle(.tertiary)
            }
        }
        .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
    }

    private func runsCard(_ d: ScheduleDetail) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("RECENT RUNS").font(.system(size: 10, weight: .bold)).tracking(0.9).foregroundStyle(.tertiary)
            if d.runs.isEmpty {
                Text("It hasn't run yet.").font(.system(size: 12)).foregroundStyle(.secondary).padding(.vertical, 6)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(d.runs.enumerated()), id: \.element.id) { i, run in
                        if i > 0 { Rectangle().fill(Live.hairline).frame(height: 1).padding(.leading, 36) }
                        ScheduleRunRow(run: run, now: store.now) { if let s = run.sessionID { store.open(s) } }
                    }
                }
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Live.card))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Live.hairline, lineWidth: 0.5))
            }
        }
    }
}

struct ScheduleRunRow: View {
    let run: ScheduleRun
    var now: Date
    var open: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: open) {
            HStack(alignment: .top, spacing: 10) {
                Group {
                    if run.status == .running { LiveSpinner(size: 12) }
                    else { Image(systemName: ScheduleLook.symbol(run.status)).font(.system(size: 13, weight: .semibold)).foregroundStyle(ScheduleLook.tint(run.status)) }
                }
                .frame(width: 16).padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(run.startedAt.map { ScheduleFormat.last($0, now: now) } ?? "Earlier").font(.system(size: 12.5, weight: .semibold).monospacedDigit())
                        Text("·").foregroundStyle(.tertiary)
                        Text(ScheduleFormat.statusWord(run.status)).font(.system(size: 12)).foregroundStyle(ScheduleLook.tint(run.status))
                        if let s = run.duration, run.status != .running {
                            Text("· \(ScheduleFormat.duration(s))").font(.system(size: 11.5).monospacedDigit()).foregroundStyle(.tertiary)
                        }
                        Spacer()
                        if run.sessionID != nil {
                            if hover { Text("Open session").font(.system(size: 11, weight: .semibold)).foregroundStyle(Live.brassText) }
                            Image(systemName: "arrow.up.right").font(.system(size: 9, weight: .bold))
                                .foregroundStyle(hover ? AnyShapeStyle(Live.brassText) : AnyShapeStyle(.tertiary))
                        }
                    }
                    if !run.result.isEmpty {
                        Text(run.result).font(.system(size: 12)).foregroundStyle(Color.primary.opacity(0.75)).lineLimit(3)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if run.status == .running {
                        Text("Working on it now.").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 10)
            .background(hover && run.sessionID != nil ? HistoryLook.hover : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(run.sessionID == nil)
        .onHover { hover = $0 }
        .help(run.sessionID == nil ? "" : "Open this run's session")
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("schedule-run-\(run.id)")
    }
}

/// New schedule by voice: the History recorder (on-device transcription) fills the sentence; nothing
/// is sent until Preview and Save.
@MainActor
final class ScheduleDictation: ObservableObject {
    @Published private(set) var listening = false
    @Published private(set) var problem: String?
    var recorder: VoiceMessageRecording = VoiceMessageRecorder()

    func toggle(into schedules: SchedulesModel, done: @escaping () -> Void) {
        if listening { recorder.stop(); return }
        problem = nil
        let before = schedules.draft.text.trimmingCharacters(in: .whitespacesAndNewlines)
        recorder.authorize { [weak self] granted, _ in
            guard let self else { return }
            guard granted else { self.problem = "Speakeasy needs the microphone and speech recognition to listen."; return }
            let started = self.recorder.start(
                partial: { [weak schedules] text in
                    schedules?.draft.text = before.isEmpty ? text : before + " " + text
                },
                level: { _ in },
                finished: { [weak self, weak schedules] text, problem in
                    guard let self else { return }
                    self.listening = false
                    if let problem { self.problem = problem; return }
                    if let text, !text.isEmpty { schedules?.draft.text = before.isEmpty ? text : before + " " + text }
                    done()
                })
            self.listening = started
            if !started { self.problem = "Couldn't open the microphone." }
        }
    }

    func cancel() { if listening { recorder.cancel(); listening = false } }
}
