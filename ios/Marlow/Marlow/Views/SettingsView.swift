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
    @State private var showingUnpairConfirmation = false
    @State private var isUnpairing = false
    @StateObject private var yourDay = YourDayModel()
    @AppStorage(EffortPreference.storageKey) private var effortRaw = ThinkingEffort.default.rawValue
    @AppStorage(LocationPreference.storageKey) private var locationRaw = LocationSharing.default.rawValue

    var body: some View {
        NavigationStack {
            Form {
                Section("Status") {
                    LabeledContent("Server", value: APIClient.host)
                    LabeledContent("Connection", value: connectionStatus)
                    if let deviceToken = session.deviceToken {
                        LabeledContent("Device", value: String(deviceToken.suffix(6)))
                    }
                }

                Section {
                    NavigationLink {
                        FacePicker()
                    } label: {
                        HStack(spacing: 12) {
                            MarlowAvatar(size: 36)
                            Text("Appearance")
                        }
                    }
                } header: {
                    Text("Marlow")
                } footer: {
                    Text("Also changes Marlow's icon on your Home Screen when you tap Done.")
                }

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
                    Text("Higher effort makes replies slower, not more expensive. Applies to your next message.")
                }

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
                    } header: {
                        Text("Your day")
                    } footer: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Marlow tidies up after your day ends, and checks your inbox during it.")
                            if let message = yourDay.message {
                                Text(message).foregroundStyle(.red)
                            }
                        }
                    }
                }

                VoiceSettingsSection()

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

                // Both sides, deliberately adjacent and in the same format:
                // the question this answers is "do these two agree?", which is
                // exactly what a feature-branch deploy makes worth asking.
                Section("Build") {
                    LabeledContent("App", value: "\(gitBranch)@\(gitCommit)")
                    LabeledContent("Server", value: store.serverBuild ?? "—")
                }

                Section {
                    Button("Unpair", role: .destructive) {
                        showingUnpairConfirmation = true
                    }
                    .disabled(isUnpairing)
                }
            }
            // Under a UI fixture only the chat's store is fake. Everything
            // this screen writes is real: Unpair clears the Keychain token,
            // and the picker sets the install's own preference. A fixture run on a
            // paired phone must not be able to reach any of it, so the form
            // still renders, for screenshots, and does nothing.
            .disabled(isUIFixture)
            // The box is the only copy of the day, so it is asked on every
            // open; the section appears once it has answered.
            .task {
                guard !isUIFixture, let deviceToken = session.deviceToken else { return }
                await yourDay.load(api: APIClient(), deviceToken: deviceToken)
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .confirmationDialog(
                "Unpair from Marlow?",
                isPresented: $showingUnpairConfirmation,
                titleVisibility: .visible
            ) {
                Button("Unpair", role: .destructive) {
                    unpair()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("This stops active work and revokes this device on Marlow. Reconnecting needs a new pairing code.")
            }
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
