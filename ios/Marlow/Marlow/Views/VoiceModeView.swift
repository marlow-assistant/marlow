import AVFoundation
import SwiftUI

/// Voice mode's screen (docs/tasks/voice-mode-hands-free.md): everything else
/// goes away and one big button takes its place, like a car-mode screen —
/// usable at a glance, with a thumb. Press to talk, press to send, press to
/// interrupt. Leaving puts Mike back in the chat, where every voice turn is
/// already an ordinary bubble: they went through the same send and the same
/// server transcript.
///
/// Shown as an overlay inside `ChatView`, not a full-screen cover, so the
/// approval card (`PermissionOverlay`, layered above it) still appears over it.
/// It owns the mode's lifetime: the audio session, the screen staying awake,
/// and the controller all start when it appears and end when it goes.
struct VoiceModeView: View {
    @ObservedObject var store: ChatStore
    let onClose: () -> Void
    @ObservedObject private var models: VoiceModelStore = .shared

    /// Voice mode reads with Phonon, so until it's downloaded the mode is a
    /// download screen (Mike, 2026-10-21: Apple's voice is "basically
    /// unusable"). The model can only be deleted from Settings, which voice
    /// mode covers; were it deleted mid-mode, this would end the mode and
    /// show the download screen.
    var body: some View {
        if models.state(of: .phonon) == .ready {
            VoiceModeSession(store: store, onClose: onClose)
        } else {
            VoiceDownloadScreen(models: models, onClose: onClose)
        }
    }
}

private struct VoiceModeSession: View {
    @ObservedObject var store: ChatStore
    let onClose: () -> Void

    // Split in two so every object is built inside a `StateObject` autoclosure,
    // once per mode. Building them in `init` ran on every `ChatView` render —
    // the 50ms typing tick while a reply streams — and allocated an audio
    // engine and recognizer each time (`/code-review high`, 2026-10-20).
    @StateObject private var recorder = DictationController(sessionRole: .voiceMode)

    var body: some View {
        VoiceModeScreen(store: store, recorder: recorder, onClose: onClose)
    }
}

/// What voice mode shows until Phonon is downloaded: what it needs, the
/// download, and the way out. When the download completes, `VoiceModeView`
/// swaps in the mode itself.
private struct VoiceDownloadScreen: View {
    @ObservedObject var models: VoiceModelStore
    let onClose: () -> Void

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "waveform")
                .font(.system(size: 44))
                .foregroundStyle(.tint)
            Text("Voice mode needs a voice")
                .font(.title2.weight(.semibold))
            Text("Marlow reads replies aloud with Phonon, a voice that runs on this iPhone. It's a one-time download of \(models.downloadSize(of: .phonon)).")
                .foregroundStyle(.secondary)
            switch models.state(of: .phonon) {
            case .downloading(let fraction):
                VoiceDownloadProgress(fraction: fraction) { models.cancel(.phonon) }
            case .preparing:
                VoicePreparing()
            case .failed(let message):
                Text(message)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                download("Try again")
            case .absent, .ready:
                download("Download")
            }
        }
        .multilineTextAlignment(.center)
        .padding(.horizontal, 32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .topLeading) {
            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 20, weight: .semibold))
                    .frame(width: 44, height: 44)
            }
            .accessibilityLabel("Leave voice mode")
            .padding(.horizontal, 12)
        }
        .background(Color(.systemBackground).ignoresSafeArea())
    }

    private func download(_ title: String) -> some View {
        Button(title) { models.download(.phonon) }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
    }
}

private struct VoiceModeScreen: View {
    @ObservedObject var store: ChatStore
    @ObservedObject var recorder: DictationController
    let onClose: () -> Void

    @StateObject private var voice: VoiceModeController
    @Environment(\.scenePhase) private var scenePhase
    @State private var audioError: String?
    /// ~2em of `.title2`, and grows with Dynamic Type like the caption does.
    @ScaledMetric(relativeTo: .title2) private var captionGap: CGFloat = 44
    /// Which screen owns the mode's shared state (the reply hook, the session,
    /// the idle timer). A close-and-reopen inside the fade can run the old
    /// screen's `onDisappear` after the new one's `onAppear`; without this it
    /// tore the new mode down (`/code-review high`, 2026-10-20).
    @State private var token = UUID()
    private static var owner: UUID?

    init(store: ChatStore, recorder: DictationController, onClose: @escaping () -> Void) {
        self.store = store
        self.recorder = recorder
        self.onClose = onClose
        // `VoiceChoice.speech()` runs once per mode, in this autoclosure: the
        // voice starts loading its model here.
        _voice = StateObject(wrappedValue: VoiceModeController(
            recorder: recorder,
            speech: VoiceChoice.speech(),
            send: { [weak store] text in store?.sendSpoken(text) },
            earcon: { [earcons = Earcons()] in earcons.play($0) }
        ))
    }

    var body: some View {
        // The button is centred on the screen (Mike, 2026-10-21) and
        // everything else hangs off it, so the caption, a notice or the voice
        // hint appearing never moves the target his thumb is aiming for.
        bigButton
            .overlay(alignment: .top) {
                Text(caption)
                    .font(.title2.weight(.semibold))
                    .multilineTextAlignment(.center)
                    .containerRelativeFrame(.horizontal) { width, _ in width - 48 }
                    // About 2em of the caption's font between it and the
                    // button (Mike, 2026-10-21).
                    .alignmentGuide(.top) { d in d[.bottom] + captionGap }
                    .accessibilityHidden(true)
            }
            .overlay(alignment: .bottom) {
                VStack(spacing: 12) {
                    if let notice = audioError ?? recorder.errorMessage {
                        Text(notice)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                .multilineTextAlignment(.center)
                .containerRelativeFrame(.horizontal) { width, _ in width - 48 }
                .alignmentGuide(.bottom) { d in d[.top] - 24 }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .overlay(alignment: .topLeading) {
                Button {
                    close()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 20, weight: .semibold))
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("Leave voice mode")
                .padding(.horizontal, 12)
            }
            .background(Color(.systemBackground).ignoresSafeArea())
        .onAppear(perform: enter)
        .onDisappear(perform: leave)
        .onReceive(store.$messages) { voice.messagesChanged($0) }
        .onReceive(store.$pendingPermission) { voice.approvalChanged(pending: $0 != nil) }
        .onChange(of: scenePhase) { _, phase in
            // Nothing keeps speaking or listening in the background yet
            // (no `audio` background mode), so stop cleanly rather than be
            // cut off mid-word by the system.
            if phase == .background { voice.stopAll() }
        }
        .onReceive(NotificationCenter.default.publisher(for: AVAudioSession.interruptionNotification)) { note in
            // A call, Siri, navigation: whatever took the audio, stop and
            // wait for a press, rather than inherit what the framework does.
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            if raw == AVAudioSession.InterruptionType.began.rawValue { voice.stopAll() }
        }
    }

    private var bigButton: some View {
        Button {
            voice.press()
        } label: {
            ZStack {
                RoundedRectangle(cornerRadius: 28, style: .continuous)
                    .fill(buttonFill)
                RoundedRectangle(cornerRadius: 28, style: .continuous)
                    .strokeBorder(Color.accentColor, lineWidth: voice.phase == .idle ? 3 : 0)
                VStack(spacing: 20) {
                    Image(systemName: icon)
                        .font(.system(size: 48, weight: .regular))
                        .symbolEffect(.pulse, isActive: voice.phase == .waiting || voice.phase == .transcribing)
                    if voice.phase == .recording {
                        LevelMeterView(levelMeter: recorder.levelMeter, color: .white)
                            .frame(height: 32)
                            .padding(.horizontal, 20)
                    }
                }
                .foregroundStyle(voice.phase == .idle ? Color.accentColor : .white)
            }
            // Half the first build's size (Mike, 2026-10-21: "too big"):
            // half the screen's width, and 130–210pt tall rather than
            // 260–420. Still a big target, no longer the whole screen.
            .containerRelativeFrame(.horizontal) { width, _ in width * 0.5 }
            .frame(minHeight: 130, maxHeight: 210)
        }
        .buttonStyle(.plain)
        .disabled(voice.phase == .transcribing)
        .accessibilityLabel(accessibilityLabel)
    }

    private var caption: String {
        switch voice.phase {
        case .idle: "Tap to talk"
        case .recording: "Listening — tap to send"
        case .transcribing: "Transcribing…"
        case .waiting: "Marlow is thinking — tap to interrupt"
        case .speaking: "Tap to interrupt"
        }
    }

    private var icon: String {
        switch voice.phase {
        case .idle: "mic"
        case .recording: "arrow.up"
        case .transcribing: "ellipsis"
        case .waiting: "ellipsis"
        case .speaking: "speaker.wave.2"
        }
    }

    private var buttonFill: Color {
        switch voice.phase {
        case .idle: Color.accentColor.opacity(0.08)
        case .recording: .red
        case .transcribing, .waiting, .speaking: .accentColor
        }
    }

    private var accessibilityLabel: String {
        switch voice.phase {
        case .idle: "Talk to Marlow"
        case .recording: "Send"
        case .transcribing: "Transcribing"
        case .waiting, .speaking: "Interrupt and talk"
        }
    }

    private func enter() {
        Self.owner = token
        do {
            try VoiceAudioSession.activate()
        } catch {
            audioError = "Couldn't start audio."
        }
        // A car-mode screen: it stays on while the mode is open.
        ScreenAwake.hold("voiceMode", true)
        store.onReplyProgress = { [weak voice] id, text, marks in
            voice?.replyProgress(id, text: text, marks: marks)
        }
    }

    private func leave() {
        voice.stopAll()
        guard Self.owner == token else { return }
        Self.owner = nil
        store.onReplyProgress = nil
        ScreenAwake.hold("voiceMode", false)
        VoiceAudioSession.deactivate()
    }

    private func close() {
        voice.stopAll()
        onClose()
    }
}
