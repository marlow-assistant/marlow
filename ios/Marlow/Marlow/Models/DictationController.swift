import AVFoundation
import Foundation
import os
import Speech

/// Rolling window of recent mic input levels (0...1), oldest first — drives
/// the "something is listening" bar indicator (Claude iOS app has the same
/// affordance). Not tied to transcription accuracy, just mic activity, so it
/// updates even during silence (all zeros). That distinction is the point:
/// when the phone is paired to the van's Bluetooth and Mike is standing
/// outside it, flat bars are how he learns the mic can't hear him.
///
/// Split out from `DictationController` itself rather than published there:
/// `ChatView.body` reads `dictation.errorMessage`/`dictation.isRecording`
/// directly, so a `@Published` on `DictationController` invalidates the
/// whole view (including its O(n) message-timestamp scan) on every level
/// update. Only the meter view observes this object, so a level update only
/// re-renders the meter.
@MainActor
final class LevelMeter: ObservableObject {
    @Published private(set) var levels: [Float] = Array(repeating: 0, count: 24)

    func reset() {
        levels = Array(repeating: 0, count: levels.count)
    }

    func push(_ level: Float) {
        levels.removeFirst()
        levels.append(level)
    }
}

/// Push-to-dictate: tap to start, record to a file, transcribe it when Mike
/// stops, hand the text to the caller — see docs/tasks/voice-mode.md. Speech
/// stays on-device unless the user explicitly allows Apple's servers.
/// The full hands-free mode (auto-engage, spoken replies) is a separate, later
/// thing (docs/tasks/voice-mode-hands-free.md) and shares none of this state.
///
/// **Record first, transcribe once (2026-08-18).** This class used to stream
/// audio into a live `SFSpeechAudioBufferRecognitionRequest` and assemble the
/// transcript from partial results. It could not be made to stop losing text.
/// The recognizer ends its own task at utterance boundaries and at an internal
/// audio-duration limit, and it does so without a reliable way to distinguish
/// "this utterance is done" from "this dictation is done" — so the code grew a
/// committed/pending split, a restart path, a generation filter, a restart
/// ceiling, and a first-character heuristic guessing at where one utterance
/// ended and the next began. Every one of those was a seam, and every bug was
/// at a seam. On 2026-08-18 a minute of dictation arrived with only its last
/// sentence intact, on the branch that had already fixed that same symptom
/// twice.
///
/// The audio has no seams. Recording it to a file and running recognition over
/// the whole file exactly once deletes the entire class of bug: there are no
/// fragments, so there is nothing to stitch and nothing to guess. The one thing
/// streaming bought — live partial text on screen — was never actually built,
/// so it cost us everything and returned nothing.
///
/// **Two ways to end a dictation, and they mean different things**
/// (Mike's call, 2026-08-16). The mic button means "I'm done, let me look at
/// it" and routes the transcript to the text field for editing; the send
/// button means "I'm done, send it" and skips the review. The original design
/// had only the second, unreviewable, which is what made a mis-transcription
/// unrecoverable and a truncated one invisible.
@MainActor
final class DictationController: NSObject, ObservableObject {
    /// What should happen to the transcript once it lands.
    enum Endpoint {
        /// Into the text field, for Mike to read and edit before sending.
        case review
        /// Straight out as a turn, no review.
        case send
    }

    @Published private(set) var isRecording = false
    /// True from the moment recording stops until the transcript lands. Unlike
    /// the streaming design, there is now a real gap here — recognition runs
    /// over the whole recording after the fact — and a UI that didn't show it
    /// would look like the mic button did nothing.
    @Published private(set) var isTranscribing = false
    @Published var errorMessage: String?
    let levelMeter = LevelMeter()

    /// Dictation telemetry. Deliberately at `.notice` and above, never
    /// `.info`: `.info` is memory-only, so it is gone by the time anyone goes
    /// looking, which is exactly what happened when we tried to diagnose the
    /// 2026-08-18 failure after the fact. These lines persist to disk and come
    /// back out of `log collect`.
    ///
    /// Never logs transcript *text* — durations, byte counts, character
    /// counts and confidence only, so a lost dictation leaves evidence without
    /// putting what was said into the system log.
    private static let log = Logger(subsystem: "com.marlow.app", category: "dictation")

    /// Generous cap (minutes, not seconds) so a forgotten-open mic doesn't
    /// run forever. Surfaced via `errorMessage`, not silent, if a real
    /// dictation ever actually hits it.
    private let maxDuration: TimeInterval = 180

    private let recognizer = SFSpeechRecognizer()
    private let audioEngine = AVAudioEngine()
    private let pipeline: DictationTranscriptionPipeline

    /// Who owns the audio session. Dictation sets up `.record` for each
    /// recording and hands it back after. Voice mode
    /// (docs/tasks/voice-mode-hands-free.md) holds one `.playAndRecord`
    /// session open for the whole mode, so recording joins it and leaves it
    /// up: tearing it down would cut off the reply about to be spoken.
    enum SessionRole {
        case dictation
        case voiceMode
    }

    private let sessionRole: SessionRole

    /// `apple` is injectable for tests; production picks SpeechTranscriber or
    /// the old recognizer per dictation.
    init(apple: AppleTranscribing? = nil, sessionRole: SessionRole = .dictation) {
        self.pipeline = DictationTranscriptionPipeline(
            apple: apple ?? SelectingAppleTranscriber(recognizer: recognizer)
        )
        self.sessionRole = sessionRole
        super.init()
    }

    /// The file being recorded into, behind a lock because two threads reach
    /// for it: the main actor creates and releases it, and the audio engine's
    /// real-time tap writes to it ~43x/sec. `uncheckedState` because
    /// `AVAudioFile` isn't `Sendable`; the lock is what makes the access safe,
    /// and the tap is removed before the reference is dropped.
    private let recordingFile = OSAllocatedUnfairLock<AVAudioFile?>(uncheckedState: nil)
    private var recordingURL: URL?
    private var recordingStartedAt: Date?

    private var capTask: Task<Void, Never>?
    private var errorClearToken: UUID?
    private var onFinished: ((String, Endpoint) -> Void)?

    /// Where this dictation's transcript is headed, set by whichever control
    /// ended it. Defaults to `.review` so every path that ends a dictation
    /// *without* Mike pressing send — the length cap, backgrounding, a
    /// recognizer failure — puts the text somewhere he can see it rather than
    /// firing off a turn he never asked for.
    private var endpoint: Endpoint = .review

    /// Written from the audio engine's real-time thread (the tap callback),
    /// read from `levelPollTask` on the main actor — a lock instead of a
    /// per-buffer `Task` hop, since hopping ~43 times/sec just to publish one
    /// Float was both wasted allocation and, worse, a race: a hop already
    /// queued before `stop()` tore things down could still land afterward
    /// and push a stale value into `levelMeter`. Polling instead means
    /// nothing writes to `levelMeter` except this task, whose lifetime is
    /// cancelled synchronously alongside the rest of teardown.
    private let rawLevel = OSAllocatedUnfairLock(initialState: Float(0))
    private var levelPollTask: Task<Void, Never>?

    /// Guards the window between a tap and `isRecording` actually flipping
    /// true — `start()` awaits two permission round-trips before it gets
    /// there, and without this a second tap in that window re-enters
    /// `start()` and installs a second tap on the same audio bus, which
    /// crashes.
    private var isStarting = false

    /// The mic button: starts a dictation, or ends one for review. Inert while
    /// a transcription is already running, so a second tap can't start a
    /// recording that would race the first one's result.
    func toggle(onFinished: @escaping (String, Endpoint) -> Void) {
        if isRecording {
            stop(endpoint: .review)
        } else if !isStarting && !isTranscribing {
            isStarting = true
            Task { [weak self] in
                await self?.start(onFinished: onFinished)
                self?.isStarting = false
            }
        }
    }

    /// The send button, pressed while dictating: end it and send without a
    /// review step. Does nothing when no dictation is running, so the caller
    /// can route on `isRecording` without a second check racing it.
    func stopAndSend() {
        stop(endpoint: .send)
    }

    /// Voice mode's recorder: no text field to fall back on, so the caller
    /// always hears back — the transcript, or nil when nothing came of it
    /// (permission refused, no speech, a failure, or an ending Mike didn't
    /// press: the length cap and backgrounding end as `.review`, and voice
    /// mode sends nothing it wasn't told to). The banner says why, as for
    /// dictation.
    func begin(onResult: @escaping (String?) -> Void) {
        guard !isRecording, !isStarting, !isTranscribing else {
            onResult(nil)
            return
        }
        onNothing = { onResult(nil) }
        isStarting = true
        Task { [weak self] in
            guard let self else { return }
            await self.start { text, endpoint in
                onResult(endpoint == .send ? text : nil)
            }
            self.isStarting = false
            if self.abandonStart {
                // `cancel()` landed while permissions were being asked: the
                // mode is gone, so the mic it just opened must close again.
                self.abandonStart = false
                self.cancel()
                return
            }
            if !self.isRecording { self.reportNothing() }
        }
    }

    /// Voice mode's second press: stop, transcribe, and hand the text to
    /// `begin`'s callback for sending.
    func finish() {
        stop(endpoint: .send)
    }

    /// Leaving voice mode mid-recording: stop listening and drop whatever was
    /// said. Nothing is sent and nothing is reported.
    func cancel() {
        abandonStart = isStarting
        onNothing = nil
        onFinished = nil
        stop(endpoint: .review)
    }

    private var abandonStart = false

    /// `begin`'s "nothing came of it" path; nil for a plain dictation.
    private var onNothing: (() -> Void)?

    private func reportNothing() {
        let report = onNothing
        onNothing = nil
        report?()
    }

    private func start(onFinished: @escaping (String, Endpoint) -> Void) async {
        guard let recognizer else {
            showError("Dictation isn't available on this device.")
            return
        }
        guard recognizer.supportsOnDeviceRecognition else {
            showError("On-device dictation isn't available on this device.")
            return
        }
        guard await requestSpeechAuthorization() else {
            showError("Speech recognition isn't authorized — check Settings.")
            return
        }
        guard await requestMicPermission() else {
            showError("Microphone access isn't authorized — check Settings.")
            return
        }

        let session = AVAudioSession.sharedInstance()
        do {
            switch sessionRole {
            case .dictation:
                try session.setCategory(.record, mode: .measurement, options: .duckOthers)
                try session.setActive(true, options: .notifyOthersOnDeactivation)
            case .voiceMode:
                // Idempotent: the mode activated it on entry; this re-asserts
                // it after anything (a call, Siri) that took it away. Not
                // `.measurement`, so AGC is on and the level meter reads
                // fuller than in dictation — it's only an "I hear you".
                try VoiceAudioSession.activate()
            }
        } catch {
            showError("Couldn't start the microphone.")
            return
        }
        // Without this the start haptic below is swallowed: iOS mutes haptics
        // and system sounds while a record session is live. `try?` because a
        // missing tap is not a reason to refuse to dictate.
        try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)

        self.onFinished = onFinished
        endpoint = .review
        levelMeter.reset()

        let inputNode = audioEngine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        // Written before the engine starts, not after: anything captured while
        // the file is nil is discarded outright, and with the engine already
        // running that is the first fraction of a second of speech — usually
        // the first word.
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("dictation-\(UUID().uuidString).caf")
        do {
            let file = try AVAudioFile(forWriting: url, settings: format.settings)
            recordingFile.withLock { $0 = file }
            recordingURL = url
        } catch {
            Self.log.error("dictation couldn't open its recording file: \(error.localizedDescription, privacy: .public)")
            releaseAudioSession()
            self.onFinished = nil
            showError("Couldn't start the microphone.")
            return
        }

        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            // Runs on the audio engine's real-time thread. The reference is
            // taken under the lock and written outside it: the critical
            // section is one reference read, and `stop()` removes this tap
            // *before* dropping the file, so no callback can be in flight
            // against a file that has been closed.
            let file = self?.recordingFile.withLock { $0 }
            try? file?.write(from: buffer)
            guard let self else { return }
            let level = Self.level(of: buffer)
            self.rawLevel.withLock { $0 = level }
        }

        levelPollTask = Task { [weak self] in
            while true {
                try? await Task.sleep(for: .milliseconds(50))
                guard let self, !Task.isCancelled else { return }
                self.levelMeter.push(self.rawLevel.withLock { $0 })
            }
        }

        audioEngine.prepare()
        do {
            try audioEngine.start()
        } catch {
            inputNode.removeTap(onBus: 0)
            levelPollTask?.cancel()
            levelPollTask = nil
            recordingFile.withLock { $0 = nil }
            discardRecording()
            releaseAudioSession()
            self.onFinished = nil
            showError("Couldn't start the microphone.")
            return
        }

        isRecording = true
        recordingStartedAt = Date()
        Self.log.notice("dictation started")
        ChatHaptics.dictationStarted()

        capTask = Task { [weak self, maxDuration] in
            try? await Task.sleep(for: .seconds(maxDuration))
            guard let self, !Task.isCancelled else { return }
            self.showError("Stopped — that clip hit the length cap.")
            self.stop(endpoint: .review)
        }
    }

    func stop(endpoint: Endpoint = .review) {
        guard isRecording else { return }
        self.endpoint = endpoint
        capTask?.cancel()
        capTask = nil

        stopCapturingAudio()
        // Dropping the last reference closes the file and flushes it, which
        // has to happen before anything tries to read it back.
        recordingFile.withLock { $0 = nil }
        releaseAudioSession()
        isRecording = false
        ChatHaptics.dictationStopped()

        let seconds = recordingStartedAt.map { Date().timeIntervalSince($0) } ?? 0
        recordingStartedAt = nil
        guard let url = recordingURL else {
            Self.log.error("dictation stopped with no recording to transcribe")
            finish(with: .nothingHeard)
            return
        }
        let bytes = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? nil
        Self.log.notice(
            "dictation stopped: \(seconds, format: .fixed(precision: 1))s recorded, \(bytes ?? -1) bytes"
        )

        isTranscribing = true
        Task { [weak self] in
            await self?.transcribe(url, recordedSeconds: seconds)
        }
    }

    /// Runs the pipeline over the whole recording, once — which Apple engine
    /// is `AppleEngineSelector`'s, the old recognizer's request and its
    /// why-comments live in `AppleFileRecognizer`. This keeps the telemetry
    /// and the file's deletion.
    private func transcribe(_ url: URL, recordedSeconds: TimeInterval) async {
        defer {
            isTranscribing = false
            discardRecording()
        }
        let resolution = await pipeline.run(url: url, recordedSeconds: recordedSeconds)
        logResolution(resolution, recordedSeconds: recordedSeconds)
        finish(with: resolution.outcome)
    }

    /// Telemetry for one resolved dictation. Content-free: counts, durations,
    /// confidence and case names, never text.
    private func logResolution(
        _ resolution: DictationResolution,
        recordedSeconds: TimeInterval
    ) {
        let engine = resolution.engine.rawValue
        let apple = resolution.transcription
        let elapsed = resolution.elapsedSeconds
        if let failure = apple.failure {
            // Domain/code are the evidence for *why* a dictation died —
            // notably whether long clips are hitting the recognizer's own
            // audio-duration limit.
            Self.log.error(
                """
                dictation failed to transcribe (\(engine, privacy: .public)): \
                \(failure.domain, privacy: .public) \(failure.code) \
                after \(recordedSeconds, format: .fixed(precision: 1))s of audio, \
                \(elapsed, format: .fixed(precision: 1))s elapsed, \
                \(apple.assembledText.count) chars salvaged
                """
            )
        } else {
            let utterances = apple.restarts + 1
            // The numbers that decide whether the recognizer's own duration
            // limit is truncating long clips: how much audio went in against
            // how much text came out. If a 90s recording keeps yielding about
            // 60s worth of words, that is the cap, and the answer is to split
            // the file — a deterministic operation on audio we still hold,
            // unlike the stream-stitching this design replaced.
            // `assembled` against `final` is the measurement that matters
            // now: the gap between them is text the recognizer dropped by
            // endpointing, and would have been lost. Equal counts with zero
            // restarts means it never endpointed and the assembler was a
            // no-op — which is what a short, pause-free clip looks like.
            Self.log.notice(
                """
                dictation transcribed (\(engine, privacy: .public)): \
                \(recordedSeconds, format: .fixed(precision: 1))s audio \
                -> \(apple.assembledText.trimmed.count) chars assembled from \(utterances) utterance(s), \
                final result alone was \(apple.finalCharacterCount), \
                \(apple.confidences.count) segments, \
                avg confidence \(DictationOutcome.averageConfidence(apple.confidences) ?? -1, format: .fixed(precision: 2)), \
                took \(elapsed, format: .fixed(precision: 1))s
                """
            )
        }
    }

    /// Hands the transcript to whoever started the dictation, or explains why
    /// it didn't.
    private func finish(with outcome: DictationOutcome) {
        guard let onFinished else { return }
        self.onFinished = nil
        switch outcome {
        case .send(let text):
            onNothing = nil
            onFinished(text, endpoint)
        case .nothingHeard:
            // A finish with nothing transcribed. A tap that produces no send
            // with no explanation reads as the button not having worked.
            Self.log.notice("dictation discarded: empty transcript")
            showError("No speech heard — try again.")
            reportNothing()
        case .noise:
            Self.log.notice("dictation discarded as noise")
            showError("No speech heard — try again.")
            reportNothing()
        }
    }

    /// Deletes the recording. Dictated audio is the most sensitive thing this
    /// app touches, so it lives on disk only as long as it takes to transcribe
    /// it — every path out of `transcribe` comes through here. (A developer
    /// corpus capture was the one exception, 2026-09-01 to 09-30; removed with
    /// the engine decision, recoverable at tag
    /// `dictation-corpus-capture-2026-09-30`.)
    private func discardRecording() {
        guard let url = recordingURL else { return }
        recordingURL = nil
        try? FileManager.default.removeItem(at: url)
    }

    /// Stops the microphone feeding the recording file. Once this returns, the
    /// tap can no longer call `write` — which is what makes it safe to drop
    /// the file reference and close it.
    private func stopCapturingAudio() {
        levelPollTask?.cancel()
        levelPollTask = nil
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        levelMeter.reset()
    }

    /// Hands the audio session back. Split from `stopCapturingAudio()` so the
    /// ordering in `stop()` stays explicit: capture stops, the file closes,
    /// and only then does the session go away.
    private func releaseAudioSession() {
        guard sessionRole == .dictation else { return }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// RMS of the buffer's first channel, mapped from dB (not linear) onto
    /// 0...1 — a linear scale either clips or flatlines depending on mic
    /// distance, and `.measurement` mode's disabled AGC (`start()`, session
    /// setup above) means real speech sits at meaningfully lower raw
    /// amplitude than `.record`'s default mode would give it, which a fixed
    /// linear multiplier can't track across that whole range. -65dB/-35dB
    /// brackets normal speaking volume at arm's length in `.measurement`
    /// mode — measured on-device (2026-08-11): silence/ambient sat around
    /// -70dB, normal talking peaked around -38 to -40dB. Loud/close speech
    /// clips to 1, silence/ambient floor to 0.
    private static func level(of buffer: AVAudioPCMBuffer) -> Float {
        guard let channelData = buffer.floatChannelData?[0] else { return 0 }
        let frameLength = Int(buffer.frameLength)
        guard frameLength > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<frameLength {
            let sample = channelData[i]
            sum += sample * sample
        }
        let rms = sqrt(sum / Float(frameLength))
        guard rms > 0 else { return 0 }
        let decibels = 20 * log10(rms)
        let minDecibels: Float = -65
        let maxDecibels: Float = -35
        let normalized = (decibels - minDecibels) / (maxDecibels - minDecibels)
        return max(0, min(normalized, 1))
    }

    private func requestSpeechAuthorization() async -> Bool {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }

    private func requestMicPermission() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    /// Self-clearing, same pattern as `ChatStore.showPermissionNotice` — a
    /// token rather than a text comparison, so a second error in quick
    /// succession doesn't get wiped early by the first one's timer.
    private func showError(_ text: String) {
        let token = UUID()
        errorClearToken = token
        errorMessage = text
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            guard let self, self.errorClearToken == token else { return }
            self.errorMessage = nil
        }
    }
}
