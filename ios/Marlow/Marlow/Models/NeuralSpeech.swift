import AVFoundation
import FluidAudio
import Foundation
import os

/// Voice mode's voice: Phonon, run on this iPhone by `PhononSynth`
/// (docs/tasks/voice-mode-tts-engines.md).
///
/// Sentences are synthesised one at a time on a serial queue, so the next is
/// being made while the last plays. Each plays through one `AVAudioEngine`
/// in voice mode's own audio session.
///
/// If the model won't load or a sentence fails, every sentence from then on
/// goes to `fallback` (Apple's voice): in a car, the wrong voice is better
/// than silence.
@MainActor
final class NeuralSpeech: SpeechOutput {
    var onUtteranceFinished: (() -> Void)?

    private let synth: PhononSynth
    private let fallback: SpeechOutput
    private let queue = DispatchQueue(label: "com.marlow.voice.synth", qos: .userInitiated)
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var format: AVAudioFormat?
    /// Bumped by every stop. Work and playback callbacks carry the value they
    /// started with, and anything stale is dropped — same job as
    /// `SystemSpeech.live`.
    private var generation = 0 {
        didSet { current.withLock { [generation] in $0 = generation } }
    }
    /// `generation`, readable from the synthesis queue.
    private let current = OSAllocatedUnfairLock(initialState: 0)
    /// Buffers scheduled and not yet reported finished. A route change can
    /// stop the engine and flush them without their completions; each is
    /// then reported finished, or voice mode would wait in "speaking" for
    /// sentences that will never play (`/code-review high`, 2026-10-21).
    private var live: Set<Int> = []
    private var nextBuffer = 0
    private var usingFallback = false
    private var configObserver: NSObjectProtocol?
    private static let log = Logger(subsystem: "com.marlow.app", category: "voice")

    init(directory: URL, voice: NeuralVoice, fallback: SpeechOutput) {
        self.synth = PhononSynth(directory: directory, voice: voice.id)
        self.fallback = fallback
        fallback.onUtteranceFinished = { [weak self] in self?.onUtteranceFinished?() }
        engine.attach(player)
        // Loading takes a second or two; start now so it's done by the time
        // the first reply arrives.
        let synth = self.synth
        queue.async { synth.load() }
        // A route change (AirPods in or out, the HFP switch) stops the engine;
        // it's restarted on the next sentence. Whatever was queued is lost.
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.engineReset() }
        }
    }

    deinit {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        engine.stop()
    }

    func speak(_ text: String) {
        if usingFallback { return fallback.speak(text) }
        let started = generation
        let synth = self.synth
        let current = self.current
        queue.async { [weak self] in
            // Cut off before its turn came: don't spend a second making it.
            guard current.withLock({ $0 }) == started else { return }
            let result = synth.generate(text)
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.synthesised(result, text: text, generation: started) }
            }
        }
    }

    func stopSpeaking() {
        generation += 1
        live.removeAll()
        player.stop()
        fallback.stopSpeaking()
    }

    private func engineReset() {
        format = nil
        let lost = live.count
        live.removeAll()
        player.stop()
        for _ in 0..<lost { onUtteranceFinished?() }
    }

    /// A buffer's end, once: a flushed buffer's late completion is dropped.
    private func played(_ id: Int) {
        guard live.remove(id) != nil else { return }
        onUtteranceFinished?()
    }

    private func synthesised(_ result: PhononSynth.Result, text: String, generation started: Int) {
        guard started == generation else { return }
        switch result {
        case .unavailable:
            // The model didn't load. Everything from here on, this sentence
            // included, is Apple's.
            Self.log.error("neural voice unavailable, using the system voice")
            usingFallback = true
            fallback.speak(text)
        case .audio(let samples, let sampleRate):
            guard !samples.isEmpty, let buffer = buffer(samples, sampleRate: sampleRate), startEngine(sampleRate) else {
                onUtteranceFinished?()
                return
            }
            let id = nextBuffer
            nextBuffer += 1
            live.insert(id)
            player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { self?.played(id) }
                }
            }
            if !player.isPlaying { player.play() }
        }
    }

    private func buffer(_ samples: [Float], sampleRate: Int) -> AVAudioPCMBuffer? {
        guard let format = AVAudioFormat(standardFormatWithSampleRate: Double(sampleRate), channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count))
        else { return nil }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { source in
            buffer.floatChannelData![0].update(from: source.baseAddress!, count: samples.count)
        }
        return buffer
    }

    /// Connects the player at the model's rate (the mixer converts to the
    /// route's) and starts the engine if it isn't running.
    private func startEngine(_ sampleRate: Int) -> Bool {
        if format?.sampleRate != Double(sampleRate) || !engine.isRunning {
            let format = AVAudioFormat(standardFormatWithSampleRate: Double(sampleRate), channels: 1)
            engine.connect(player, to: engine.mainMixerNode, format: format)
            self.format = format
        }
        guard !engine.isRunning else { return true }
        do {
            try engine.start()
            return true
        } catch {
            Self.log.error("voice audio engine didn't start: \(error.localizedDescription, privacy: .public)")
            return false
        }
    }
}


/// The speech voice mode and the Settings sample read with: Phonon once it's
/// downloaded, Apple's until then. Voice mode itself opens only once Phonon
/// is downloaded (`VoiceModeView`), so Apple's is the fallback here and in
/// `NeuralSpeech`, never a choice: Mike found it "basically unusable"
/// (2026-10-21).
@MainActor
enum VoiceChoice {
    static func speech(models: VoiceModelStore = .shared) -> SpeechOutput {
        guard let directory = models.directory(for: .phonon) else { return SystemSpeech() }
        return NeuralSpeech(directory: directory, voice: VoicePreference.voice(), fallback: SystemSpeech())
    }
}

/// British "-re" spellings as Phonon expects them. Marlow writes Canadian
/// English for Mike, so a spoken weather reply said "4 centimetres" and "67
/// kilometres an hour". Phonon, trained on American text, read those oddly,
/// and the "-er" spelling sounded right to Mike (2026-10-21). The words are
/// said the same either way, so only the audio changes; the chat keeps
/// Marlow's spelling. Swapping r and e in place keeps each letter's case.
/// "-red" ("centred") is left alone: it would come out "centerd".
enum PhononSpelling {
    static func american(_ text: String) -> String {
        text.replacingOccurrences(
            of: #"(?i)\b(\w*(?:met|lit|cent|theat|fib))(r)(e)(s?)\b"#,
            with: "$1$3$2$4",
            options: .regularExpression
        )
    }
}

/// Phonon 31m (Gradium, Apache-2.0) on FluidAudio's Core ML host: the FlowLM
/// on the Neural Engine, the Mimi decoder in fp32 on the CPU. Used only on
/// `NeuralSpeech`'s serial queue; both calls block it.
///
/// Mike picked it over Kokoro (sherpa-onnx, CPU) and Kyutai's Pocket TTS by
/// ear, 2026-10-21, accepting the risk of the iOS 26.6 Core ML crash
/// FluidAudio documents for its Kokoro. 300 sentences in a row ran clean on
/// his iPhone 14 Pro (`PhononSoakTests`).
///
/// The directory is the pack exactly as `VoiceModels.json` lays it out, so
/// FluidAudio loads it and never downloads anything itself.
final class PhononSynth: @unchecked Sendable {
    enum Result {
        case audio([Float], sampleRate: Int)
        case unavailable
    }

    private let manager: PocketTtsSmallManager
    private let directory: URL
    private let voice: String
    private var loaded: Bool?
    private static let log = Logger(subsystem: "com.marlow.app", category: "voice")

    init(directory: URL, voice: String) {
        manager = PocketTtsSmallManager(model: .phonon31m, defaultVoice: voice)
        self.directory = directory
        self.voice = voice
    }

    /// Loads the model, once; `generate` loads it too if this hasn't run.
    func load() {
        guard loaded == nil else { return }
        let manager = self.manager, directory = self.directory
        // The first load on a phone compiles for the Neural Engine, which
        // FluidAudio measured at 10–150 s; later loads took 4 s on Mike's
        // phone.
        loaded = Self.wait { try await manager.load(from: directory) } != nil
    }

    func generate(_ text: String) -> Result {
        load()
        guard loaded == true else { return .unavailable }
        let manager = self.manager, voice = self.voice
        // A thrown error is Apple's voice from here on, not a silent
        // "finished" sentence: a Core ML failure tends to repeat on every
        // call (`/code-review high`, 2026-10-21).
        let text = PhononSpelling.american(text)
        guard let samples = Self.wait({ try await manager.synthesizeSamples(text: text, voice: voice) })
        else { return .unavailable }
        return .audio(samples, sampleRate: PocketTtsConstants.audioSampleRate)
    }

    /// Runs FluidAudio's async API to completion from the synthesis queue.
    /// It blocks a GCD thread, not one of Swift's cooperative ones, so the
    /// manager's actor can't be starved by it. Nil, and logged, on an error.
    private static func wait<T>(_ work: @escaping () async throws -> T) -> T? {
        let box = Box<T>()
        let done = DispatchSemaphore(value: 0)
        Task.detached {
            do {
                box.value = try await work()
            } catch {
                log.error("phonon voice: \(error.localizedDescription, privacy: .public)")
            }
            done.signal()
        }
        done.wait()
        return box.value
    }

    private final class Box<T>: @unchecked Sendable { var value: T? }
}
