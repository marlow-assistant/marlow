import AVFoundation
import SwiftUI

/// Settings → Voice mode: Phonon's voice, its download, and a sample to hear
/// it before a drive (docs/tasks/voice-mode-tts-engines.md).
struct VoiceSettingsSection: View {
    @ObservedObject var models: VoiceModelStore = .shared
    @StateObject private var preview = VoicePreview()
    /// Read so a change redraws; written only through `VoicePreference`.
    @AppStorage(VoicePreference.voiceKey) private var voiceID = ""

    var body: some View {
        Section {
            Picker("Voice", selection: voiceSelection) {
                ForEach(NeuralVoice.phonon) { voice in
                    Text(voice.accent.map { "\(voice.name) (\($0.rawValue))" } ?? voice.name).tag(voice)
                }
            }
            downloadRow
        } header: {
            Text("Voice mode")
        } footer: {
            // Apache-2.0 asks for no credit line here; the link is where the
            // model, its makers and its licence are (Mike, 2026-10-21).
            Text("Marlow uses [Phonon](https://huggingface.co/gradium/phonon-31m-en) for high-quality speech. Voice data never leaves your phone.")
        }
        .onDisappear { preview.release() }
    }

    @ViewBuilder
    private var downloadRow: some View {
        let size = models.downloadSize(of: .phonon)
        switch models.state(of: .phonon) {
        case .absent:
            Button("Download (\(size))") { models.download(.phonon) }
        case .downloading(let fraction):
            VoiceDownloadProgress(fraction: fraction) { models.cancel(.phonon) }
        case .preparing:
            VoicePreparing()
        case .ready:
            // One row, two buttons: borderless, or a Form row takes every
            // tap as the first button's.
            HStack {
                Button {
                    preview.isPlaying ? preview.stop() : preview.play()
                } label: {
                    Label(preview.isPlaying ? "Stop" : "Play a sample",
                          systemImage: preview.isPlaying ? "stop.fill" : "play.fill")
                }
                Spacer()
                Button("Delete (\(size))", role: .destructive) {
                    preview.release()
                    models.delete(.phonon)
                }
            }
            .buttonStyle(.borderless)
        case .failed(let message):
            Text(message)
                .foregroundStyle(.secondary)
            Button("Download (\(size))") { models.download(.phonon) }
        }
    }

    private var voiceSelection: Binding<NeuralVoice> {
        Binding(
            get: {
                _ = voiceID
                return VoicePreference.voice()
            },
            set: { voice in
                preview.stop()
                VoicePreference.setVoice(voice)
            }
        )
    }
}

/// A download's progress bar, percentage and Cancel, in Settings and on voice
/// mode's download screen.
struct VoiceDownloadProgress: View {
    let fraction: Double
    let cancel: () -> Void

    var body: some View {
        HStack {
            ProgressView(value: fraction)
            Text(fraction, format: .percent.precision(.fractionLength(0)))
                .monospacedDigit()
                .foregroundStyle(.secondary)
            Button("Cancel", action: cancel)
                .buttonStyle(.borderless)
        }
    }
}

/// The model is downloaded and iOS is compiling it for the Neural Engine
/// (`VoiceModelStore.compileForNeuralEngine`), in Settings and on voice mode's
/// download screen.
struct VoicePreparing: View {
    var body: some View {
        HStack(spacing: 8) {
            ProgressView()
            Text("Preparing the voice…")
                .foregroundStyle(.secondary)
        }
    }
}

/// Plays a short sample in the chosen voice, outside voice mode.
@MainActor
final class VoicePreview: ObservableObject {
    @Published private(set) var isPlaying = false
    /// Kept between plays of the same voice: loading a model takes seconds,
    /// and comparing voices means pressing Play over and over
    /// (`/code-review high`, 2026-10-21).
    private var loaded: (voice: String, speech: SpeechOutput)?

    /// One `speak` per sentence, as voice mode does, so the first plays while
    /// the second is made. As one string, a neural voice made all of it
    /// before a sound (Mike's phone, 2026-10-21).
    static let sample = [
        "Your next meeting is at three with the design team.",
        "After that the afternoon's clear, so there's time for a run before dinner.",
    ]

    func play() {
        stop()
        let session = AVAudioSession.sharedInstance()
        // `.playback`, so the ring/silent switch doesn't mute a sample
        // someone asked to hear.
        try? session.setCategory(.playback, mode: .spokenAudio)
        try? session.setActive(true)
        let voice = VoicePreference.voice().id
        if loaded?.voice != voice {
            loaded = (voice, VoiceChoice.speech())
        }
        guard let speech = loaded?.speech else { return }
        var left = Self.sample.count
        speech.onUtteranceFinished = { [weak self] in
            left -= 1
            if left == 0 { self?.stop() }
        }
        isPlaying = true
        Self.sample.forEach(speech.speak)
    }

    func stop() {
        guard isPlaying else { return }
        loaded?.speech.stopSpeaking()
        isPlaying = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Frees the model, when Settings closes or the download is deleted.
    func release() {
        stop()
        loaded = nil
    }
}
