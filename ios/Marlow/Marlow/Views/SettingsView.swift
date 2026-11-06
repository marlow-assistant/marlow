import CoreLocation
import SwiftUI

/// Reached via the toolbar gear. Was a bare "Unpair" button in the nav bar;
/// pulled behind a settings screen once there was more than one thing to show
/// here, so unpairing isn't one accidental tap away from the chat itself.
struct SettingsView: View {
    @EnvironmentObject private var session: SessionStore
    @EnvironmentObject private var location: LocationProvider
    @ObservedObject var store: ChatStore
    /// ChatView's, so the switch here and the card it raises agree.
    @ObservedObject var whatsNew: WhatsNewStore
    /// `ChatView` owns the live `DictationController`; Unpair needs to stop
    /// it the same way backgrounding does (`ChatStore.cancelActiveWork()`
    /// exists for exactly this class of bug — work outliving the token it
    /// was using), so the stop call is handed in rather than this view
    /// reaching for a `DictationController` of its own.
    var stopDictation: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    @State private var isUnpairing = false
    @StateObject private var yourDay = YourDayModel()
    @AppStorage(EffortPreference.storageKey) private var effortRaw = ThinkingEffort.default.rawValue
    @AppStorage(LocationPreference.storageKey) private var locationRaw = LocationSharing.default.rawValue
    @AppStorage(FacePreference.storageKey) private var storedFaceID = MarlowFace.default.id
    /// Read so a change redraws the Voice row.
    @AppStorage(VoicePreference.voiceKey) private var voiceID = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Appearance and voice") {
                    NavigationLink {
                        FacePicker()
                    } label: {
                        LabeledContent {
                            Text(MarlowFace.with(id: storedFaceID).propName)
                        } label: {
                            HStack(spacing: 12) {
                                MarlowAvatar(size: 28)
                                Text("Avatar")
                            }
                        }
                    }
                    NavigationLink {
                        VoiceSettingsView()
                    } label: {
                        LabeledContent {
                            Text(voiceName)
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "waveform.circle.fill")
                                    .font(.title2)
                                    .frame(width: 28)
                                    .foregroundStyle(.secondary)
                                Text("Voice")
                            }
                        }
                    }
                }
                .disabled(isUIFixture)

                // Effort is a LATENCY dial on this workload, not a cost one:
                // measured at ~2,900 thinking tokens against 5.8M cache reads
                // in a day (docs/tasks/model-and-effort-config.md). The footer
                // says so plainly rather than letting the picker imply savings.
                Section {
                    Picker("Thinking", selection: effortSelection) {
                        ForEach(ThinkingEffort.allCases) { level in
                            Text(level.displayName).tag(level)
                        }
                    }
                    .pickerStyle(.segmented)
                    // Read-only, and shown in the same section so the model and
                    // effort a reply came from can be read together (the chat
                    // header's "Sonnet/Low" chip was removed 2026-10-12, so this
                    // is the only place). There is no model picker on
                    // purpose: switching models pays a full cold prefix read.
                    LabeledContent("Model", value: store.serverModel?.capitalizedModelName ?? "—")
                } header: {
                    Text("Intelligence")
                } footer: {
                    Text("Higher effort gives better answers, but can be slower to respond.")
                }
                .disabled(isUIFixture)

                Section {
                    Picker("Location", selection: locationSelection) {
                        ForEach(LocationSharing.allCases) { mode in
                            Text(mode.displayName).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                } header: {
                    Text("Location")
                } footer: {
                    Text(locationFooter)
                }
                .disabled(isUIFixture)

                if let times = yourDay.times {
                    Section {
                        LabeledContent("Starts") {
                            QuarterHourPicker(date: dayBinding(.start, times.start))
                        }
                        LabeledContent("Ends") {
                            QuarterHourPicker(date: dayBinding(.end, times.end))
                        }
                        // Absent from a box that predates check-ins.
                        if let hours = times.checkinEveryHours {
                            Picker("Check-ins", selection: checkinBinding(hours)) {
                                ForEach(YourDay.checkinOptions, id: \.self) { n in
                                    Text(YourDay.checkinLabel(n)).tag(n)
                                }
                            }
                            .pickerStyle(.menu)
                        }
                        // Absent from a box that predates to-do reminders.
                        if let renudge = times.renudge {
                            Toggle("Repeat reminders daily", isOn: renudgeBinding(renudge))
                        }
                    } header: {
                        Text("Your day")
                    } footer: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Marlow tidies up after your day ends, and checks your inbox during it.")
                            if times.renudge != nil {
                                Text("If you don't act on a reminder, it comes back a day later until it's done, dropped or snoozed.")
                            }
                            if let message = yourDay.message {
                                Text(message).foregroundStyle(.red)
                            }
                        }
                    }
                    .disabled(isUIFixture)
                }

                // The other half of the update card (docs/tasks/whats-new.md):
                // the card's hint points at this switch.
                Section {
                    NavigationLink("What's new") {
                        WhatsNewList(store: whatsNew)
                    }
                    Toggle("Show after updates", isOn: Binding(
                        get: { !whatsNew.muted },
                        set: { whatsNew.setMuted(!$0) }
                    ))
                } header: {
                    Text("Updates")
                } footer: {
                    Text("When Marlow is updated, what changed appears the next time you open the app.")
                }
                .disabled(isUIFixture)

                Section {
                    NavigationLink("Server connection") {
                        ServerConnectionView(
                            host: APIClient.host,
                            connection: connectionStatus,
                            device: session.deviceToken.map { String($0.suffix(6)) },
                            isUnpairing: isUnpairing,
                            isDisabled: isUIFixture,
                            unpair: unpair
                        )
                    }
                    NavigationLink("About") {
                        AboutView(
                            appBuild: "\(gitBranch)@\(gitCommit)",
                            serverBuild: store.serverBuild ?? "—"
                        )
                    }
                }
            }
            // Under a UI fixture only the chat's store is fake. Everything
            // this screen writes is real: Unpair clears the Keychain token,
            // and the picker sets the install's own preference. A fixture run on a
            // paired phone must not be able to reach any of it, so each section
            // that writes still renders, for screenshots, and does nothing.
            // Server connection and About stay open to look at; Unpair is
            // disabled inside.
            // The box is the only copy of the day, so it is asked on every
            // open; the section appears once it has answered.
            .task {
                guard !isUIFixture, let deviceToken = session.deviceToken else { return }
                await yourDay.load(api: APIClient(), deviceToken: deviceToken)
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Done") {
                        // The icon follows the face here, not in the picker:
                        // each change raises iOS's own alert, and trying a few
                        // faces meant one alert per tap (Mike, 2026-10-16).
                        // Closing by swipe leaves it to the next activation's
                        // reconcile.
                        Task { await AppIconSync.reconcile() }
                        dismiss()
                    }
                }
            }
        }
    }

    private var isUIFixture: Bool {
        #if DEBUG
        UIFixture.state != nil
        #else
        false
        #endif
    }

    /// Revokes this device on the box first, so a token copied off this phone
    /// stops working too (docs/tasks/funnel-ingress.md, step 2). Signs out
    /// whether or not the box answered: an unreachable box must not trap the
    /// phone in a session it asked to leave. A 401 means the box had already
    /// forgotten it, which is the same outcome.
    private func unpair() {
        stopDictation()
        store.cancelActiveWork()
        guard let deviceToken = session.deviceToken else {
            session.signOut()
            return
        }
        isUnpairing = true
        Task {
            var notice: String?
            do {
                try await APIClient().unpair(deviceToken: deviceToken, timeout: 8)
            } catch APIError.server(let status, _) where status == 401 {
                // Already revoked on the box.
            } catch {
                notice = "Unpaired on this phone, but Marlow couldn't be reached to revoke it. "
                    + "Ask whoever runs the box to revoke this device (deploy/devices.sh)."
            }
            session.signOut(notice: notice)
        }
    }

    /// Bridges the stored `String` to the enum the picker selects over.
    /// Writing through `EffortPreference` rather than assigning `effortRaw`
    /// directly keeps every write on one path — the send side reads that same
    /// key, and a raw assignment here would be the easy way to introduce a
    /// value it can't parse.
    private var effortSelection: Binding<ThinkingEffort> {
        Binding(
            get: { ThinkingEffort(rawValue: effortRaw) ?? .default },
            set: { effort in
                EffortPreference.set(effort)
                // The same level on every client (`PreferenceSync`).
                PreferenceSync.picked(.effort)
                guard let deviceToken = session.deviceToken else { return }
                Task { await PreferenceSync.send(api: APIClient(), deviceToken: deviceToken) }
            }
        )
    }

    /// Same bridge as `effortSelection`, for the same reason.
    private var locationSelection: Binding<LocationSharing> {
        Binding(
            get: { LocationSharing(rawValue: locationRaw) ?? .default },
            set: { LocationPreference.set($0) }
        )
    }

    /// A picker's `Date` for the box's "HH:MM". A pick posts just that field;
    /// the model puts the old time back if the box refuses it.
    private func dayBinding(_ field: YourDayModel.Field, _ hm: String) -> Binding<Date> {
        Binding(
            get: { YourDay.date(from: hm) ?? Date() },
            set: { picked in
                guard let deviceToken = session.deviceToken else { return }
                let value = YourDay.string(from: picked)
                Task { await yourDay.set(field, to: value, api: APIClient(), deviceToken: deviceToken) }
            }
        )
    }

    private func checkinBinding(_ hours: Int) -> Binding<Int> {
        Binding(
            get: { hours },
            set: { picked in
                guard let deviceToken = session.deviceToken else { return }
                Task { await yourDay.setCheckin(picked, api: APIClient(), deviceToken: deviceToken) }
            }
        )
    }

    private func renudgeBinding(_ on: Bool) -> Binding<Bool> {
        Binding(
            get: { on },
            set: { picked in
                guard let deviceToken = session.deviceToken else { return }
                Task { await yourDay.setRenudge(picked, api: APIClient(), deviceToken: deviceToken) }
            }
        )
    }

    /// The explanation, unless the setting is on and iOS has the permission
    /// off: then the picker silently does nothing, which is worse than a
    /// footer saying why.
    private var locationFooter: String {
        let sharing = LocationSharing(rawValue: locationRaw) ?? .default
        if sharing != .off, location.authorizationStatus == .denied || location.authorizationStatus == .restricted {
            return "Location is turned off for Marlow in iOS Settings, so nothing is sent."
        }
        return "Sent with each message so Marlow knows where you are. Approximate shares your city; Precise shares your exact position."
    }

    private var voiceName: String {
        _ = voiceID
        return VoicePreference.voice().name
    }

    private var connectionStatus: String {
        if store.reauthNeeded { return "Needs reauthorization" }
        if store.queuePaused { return "Paused" }
        return "Connected"
    }

    private var gitBranch: String {
        Bundle.main.infoDictionary?["GitBranch"] as? String ?? "unknown"
    }

    private var gitCommit: String {
        Bundle.main.infoDictionary?["GitCommit"] as? String ?? "unknown"
    }
}

#Preview {
    SettingsView(store: ChatStore(deviceToken: "preview-token"), whatsNew: WhatsNewStore(deviceToken: "preview-token"))
        .environmentObject(SessionStore())
        .environmentObject(LocationProvider())
}
