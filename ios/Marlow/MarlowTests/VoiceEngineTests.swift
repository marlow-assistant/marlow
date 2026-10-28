import AVFoundation
import Foundation
import Testing
@testable import Marlow

// Voice mode's Phonon voice (docs/tasks/voice-mode-tts-engines.md). What a
// test can reach: the stored voice, the bundled manifest, the download's
// staging and resume, and the fall-back to Apple's voice when the model won't
// load. What it can't — how it sounds and how it behaves on the Neural Engine
// — is the device checklist in the task doc, and `PhononSoakTests`.

private func scratchDefaults() -> UserDefaults {
    let name = "voice-engine-tests-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: name)!
    defaults.removePersistentDomain(forName: name)
    return defaults
}

/// A value set from one task and read from another.
private final class LockedBox<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: T
    init(_ value: T) { self.value = value }
    func get() -> T { lock.withLock { value } }
    func set(_ new: T) { lock.withLock { value = new } }
}

private func scratchFolder() -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("voice-models-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

@Suite("Voice preference")
struct VoicePreferenceTests {
    @Test func noChoiceMeansTheFirstVoice() {
        #expect(VoicePreference.voice(in: scratchDefaults()) == NeuralVoice.phonon[0])
    }

    @Test func theVoiceRoundTrips() {
        let defaults = scratchDefaults()
        VoicePreference.setVoice(NeuralVoice.phonon[4], in: defaults)
        #expect(VoicePreference.voice(in: defaults) == NeuralVoice.phonon[4])
    }

    @Test func aVoiceThisBuildDoesntOfferFallsBackToTheFirst() {
        // Say a voice from a dropped engine, or one a later build removed.
        let defaults = scratchDefaults()
        defaults.set("charles", forKey: VoicePreference.voiceKey)
        #expect(VoicePreference.voice(in: defaults) == NeuralVoice.phonon[0])
    }

    @Test func aPhononVoicePickedBeforeTheSimplificationSurvives() {
        // "voice.phonon" was Phonon's key when every engine had its own.
        let defaults = scratchDefaults()
        defaults.set("en_us_Zoey", forKey: "voice.phonon")
        #expect(VoicePreference.voice(in: defaults).id == "en_us_Zoey")
    }
}

@Suite("Voice model manifest")
struct VoiceModelManifestTests {
    let manifests = VoiceModelManifest.load(from: Bundle(for: ChatStore.self))

    @Test func phononIsInTheBundle() throws {
        // A manifest that isn't copied into the app leaves nothing to download.
        let phonon = try #require(manifests["phonon"])
        #expect(phonon.totalBytes > 100_000_000)
        #expect(Set(manifests.keys) == ["phonon"])
    }

    @MainActor
    @Test func theSizeIsWholeMegabytes() {
        let store = VoiceModelStore(root: scratchFolder(), manifests: manifests)
        #expect(store.downloadSize(of: .phonon) == "153 MB")
    }

    @Test func everyFileFluidAudioNeedsIsListed() throws {
        // What FluidAudio's `PocketTtsSmallDownloader` checks for before it
        // calls a pack complete.
        let pack = Set(try #require(manifests["phonon"]).files.map(\.path))
        for name in ["pocket_state.mlmodelc/coremldata.bin", "pocket_state.mlmodelc/model.mil",
                     "mimi_decoder.mlmodelc/coremldata.bin", "mimi_decoder.mlmodelc/model.mil",
                     "arch.json", "pocket_state.json",
                     "constants_bin/tokenizer.model", "constants_bin/text_embed_table.bin"] {
            #expect(pack.contains(name), "\(name)")
        }
    }

    /// Each offered voice is a file in the pack: a KV snapshot in
    /// `constants_bin/` or conditioning rows in `voices/`.
    @Test func everyOfferedVoiceIsDownloaded() throws {
        let pack = Set(try #require(manifests["phonon"]).files.map(\.path))
        for voice in NeuralVoice.phonon {
            let file = "\(voice.id).safetensors"
            #expect(pack.contains("constants_bin/\(file)") || pack.contains("voices/\(file)"),
                    "\(voice.id) isn't in the pack")
        }
    }

    @Test func everyFileIsPinnedAndStaysInItsFolder() {
        for (engine, manifest) in manifests {
            for file in manifest.files {
                #expect(VoiceModelManifest.isSafe(file.path), "\(engine): \(file.path)")
                #expect(file.url.scheme == "https" && file.url.host == "huggingface.co", "\(file.url)")
                // A branch name would let the repo change under us.
                #expect(!file.url.path.contains("/resolve/main/"), "\(file.url)")
                #expect(file.size > 0)
            }
        }
    }

    @Test func pathsThatLeaveTheFolderAreRefused() {
        #expect(!VoiceModelManifest.isSafe("../escape"))
        #expect(!VoiceModelManifest.isSafe("a/../../b"))
        #expect(!VoiceModelManifest.isSafe("/etc/passwd"))
        #expect(!VoiceModelManifest.isSafe(""))
        #expect(!VoiceModelManifest.isSafe("a//b"))
        #expect(VoiceModelManifest.isSafe("voices/en_gb_Elodie-Rose.safetensors"))
    }
}

@MainActor
@Suite("Voice model downloads")
struct VoiceModelStoreTests {
    /// Serves bytes of the manifest's size, counting what was fetched.
    final class FakeServer: @unchecked Sendable {
        private let lock = NSLock()
        private var _fetched: [String] = []
        /// Called from several fetches at once.
        var fetched: [String] {
            get { lock.withLock { _fetched } }
            set { lock.withLock { _fetched = newValue } }
        }
        var failOn: String?
        var wrongSizeFor: String?

        func fetch(_ url: URL, progress: @escaping @Sendable (Int64) -> Void) async throws -> URL {
            let name = url.lastPathComponent
            if name == failOn {
                // After the others have landed: files are fetched a few at a
                // time, and a resume test needs to know which got in first.
                try await Task.sleep(for: .milliseconds(100))
                throw URLError(.networkConnectionLost)
            }
            lock.withLock { _fetched.append(name) }
            let size = Int(url.query?.split(separator: "=").last ?? "0") ?? 0
            let out = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try Data(count: name == wrongSizeFor ? size + 1 : size).write(to: out)
            progress(Int64(size))
            return out
        }
    }

    static func manifest(_ names: [String: Int64]) -> [String: VoiceModelManifest] {
        let files = names.sorted { $0.key < $1.key }.map { name, size in
            VoiceModelManifest.File(
                url: URL(string: "https://huggingface.co/x/resolve/abc/\(name)?size=\(size)")!,
                path: name, size: size)
        }
        return ["phonon": VoiceModelManifest(files: files)]
    }

    func waitUntilSettled(_ store: VoiceModelStore) async {
        for _ in 0..<500 {
            switch store.state(of: .phonon) {
            case .downloading, .preparing: try? await Task.sleep(for: .milliseconds(10))
            default: return
            }
        }
    }

    @Test func nothingIsReadyBeforeADownload() {
        let store = VoiceModelStore(root: scratchFolder(), manifests: Self.manifest(["arch.json": 10]))
        #expect(store.state(of: .phonon) == .absent)
        #expect(store.directory(for: .phonon) == nil)
        #expect(store.state(of: .apple) == .ready, "Apple's voice needs no download")
    }

    @Test func aFinishedDownloadIsReadyAndHasEveryFile() async throws {
        let server = FakeServer()
        let store = VoiceModelStore(
            root: scratchFolder(), manifests: Self.manifest(["arch.json": 10, "voices/a.safetensors": 5]),
            fetch: server.fetch)
        store.download(.phonon)
        await waitUntilSettled(store)
        #expect(store.state(of: .phonon) == .ready)
        let folder = try #require(store.directory(for: .phonon))
        #expect(FileManager.default.fileExists(atPath: folder.appendingPathComponent("arch.json").path))
        #expect(FileManager.default.fileExists(atPath: folder.appendingPathComponent("voices/a.safetensors").path))
    }

    @Test func aFinishedDownloadIsPreparedBeforeItIsReady() async throws {
        // The Neural Engine compile happens here, with "Preparing" showing,
        // not on the first sample or reply.
        let gate = AsyncStream<Void>.makeStream()
        let prepared = LockedBox<URL?>(nil)
        let store = VoiceModelStore(
            root: scratchFolder(), manifests: Self.manifest(["a.bin": 10]), fetch: FakeServer().fetch,
            prepare: { folder in
                prepared.set(folder)
                for await _ in gate.stream { break }
            })
        store.download(.phonon)
        for _ in 0..<500 where store.state(of: .phonon) != .preparing {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(store.state(of: .phonon) == .preparing)
        #expect(prepared.get() == store.directory(for: .phonon), "it prepares the finished folder")
        gate.continuation.yield()
        await waitUntilSettled(store)
        #expect(store.state(of: .phonon) == .ready)
    }

    @Test func aDownloadCutShortIsNotReadyAndResumesWhereItStopped() async {
        let root = scratchFolder()
        let server = FakeServer()
        server.failOn = "b.bin"
        let manifests = Self.manifest(["a.bin": 10, "b.bin": 5])
        let store = VoiceModelStore(root: root, manifests: manifests, fetch: server.fetch)
        store.download(.phonon)
        await waitUntilSettled(store)
        guard case .failed = store.state(of: .phonon) else {
            Issue.record("expected a failure, got \(store.state(of: .phonon))")
            return
        }
        #expect(store.directory(for: .phonon) == nil, "a partial folder must never load")

        // A new store (the app relaunched) sees nothing ready, and the retry
        // fetches only what's missing.
        server.failOn = nil
        server.fetched = []
        let relaunched = VoiceModelStore(root: root, manifests: manifests, fetch: server.fetch)
        #expect(relaunched.state(of: .phonon) == .absent)
        relaunched.download(.phonon)
        await waitUntilSettled(relaunched)
        #expect(relaunched.state(of: .phonon) == .ready)
        #expect(server.fetched == ["b.bin"])
    }

    @Test func aFileOfTheWrongSizeFailsTheDownload() async {
        let server = FakeServer()
        server.wrongSizeFor = "a.bin"
        let store = VoiceModelStore(
            root: scratchFolder(), manifests: Self.manifest(["a.bin": 10]), fetch: server.fetch)
        store.download(.phonon)
        await waitUntilSettled(store)
        #expect(store.state(of: .phonon) == .failed("A model file didn't match its expected size."))
        #expect(store.directory(for: .phonon) == nil)
    }

    @Test func aBadPathInTheManifestIsNeverWritten() async {
        let root = scratchFolder()
        let server = FakeServer()
        let store = VoiceModelStore(root: root, manifests: Self.manifest(["../outside": 3]), fetch: server.fetch)
        store.download(.phonon)
        await waitUntilSettled(store)
        #expect(store.state(of: .phonon) == .failed("Bad file path in the voice manifest."))
        #expect(server.fetched.isEmpty)
        #expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent("outside").path))
    }

    @Test func untilPhononIsDownloadedTheVoiceIsApples() async {
        let store = VoiceModelStore(
            root: scratchFolder(), manifests: Self.manifest(["a.bin": 10]), fetch: FakeServer().fetch)
        #expect(VoiceChoice.speech(models: store) is SystemSpeech)
        store.download(.phonon)
        await waitUntilSettled(store)
        #expect(VoiceChoice.speech(models: store) is NeuralSpeech)
    }

    @Test func deleteRemovesTheModel() async {
        let root = scratchFolder()
        let store = VoiceModelStore(
            root: root, manifests: Self.manifest(["a.bin": 10]), fetch: FakeServer().fetch)
        store.download(.phonon)
        await waitUntilSettled(store)
        store.delete(.phonon)
        #expect(store.state(of: .phonon) == .absent)
        #expect(store.directory(for: .phonon) == nil)
        #expect((try? FileManager.default.contentsOfDirectory(atPath: root.path))?.isEmpty == true)
    }

    @Test func aChangedFileListIsANewDownload() async throws {
        let root = scratchFolder()
        let old = VoiceModelStore(root: root, manifests: Self.manifest(["a.bin": 10]), fetch: FakeServer().fetch)
        old.download(.phonon)
        await waitUntilSettled(old)
        let oldFolder = try #require(old.directory(for: .phonon))

        // A later build adds a file (say, another voice) with the model
        // itself unchanged: the old folder lacks it, so it mustn't count as
        // ready, and it's removed once the new one is in.
        let new = VoiceModelStore(
            root: root, manifests: Self.manifest(["a.bin": 10, "voices/new.safetensors": 4]),
            fetch: FakeServer().fetch)
        #expect(new.state(of: .phonon) == .absent)
        new.download(.phonon)
        await waitUntilSettled(new)
        let newFolder = try #require(new.directory(for: .phonon))
        #expect(newFolder != oldFolder)
        #expect(FileManager.default.fileExists(atPath: newFolder.appendingPathComponent("voices/new.safetensors").path))
        #expect((try? FileManager.default.contentsOfDirectory(atPath: root.path)) == [newFolder.lastPathComponent])
    }

    @Test func retiredEnginesDownloadsAreRemovedWhenTheStoreStarts() async throws {
        // Kokoro and Pocket TTS, downloaded when they were engines: hundreds
        // of megabytes no build would otherwise touch again.
        let root = scratchFolder()
        let first = VoiceModelStore(root: root, manifests: Self.manifest(["a.bin": 10]), fetch: FakeServer().fetch)
        first.download(.phonon)
        await waitUntilSettled(first)
        let phonon = try #require(first.directory(for: .phonon)).lastPathComponent
        for retired in ["kokoro-0a1b2c", "pocket-3d4e5f", "pocket-3d4e5f.partial"] {
            try FileManager.default.createDirectory(
                at: root.appendingPathComponent(retired), withIntermediateDirectories: true)
        }

        let relaunched = VoiceModelStore(root: root, manifests: Self.manifest(["a.bin": 10]))
        #expect(relaunched.state(of: .phonon) == .ready)
        // Removed in the background, off the main thread.
        func left() -> [String]? { try? FileManager.default.contentsOfDirectory(atPath: root.path) }
        for _ in 0..<200 where left() != [phonon] {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(left() == [phonon])
    }
}

/// The real model, when a run points at it: a simulator test can read the
/// Mac's disk, so `TEST_RUNNER_MARLOW_VOICE_MODELS=<folder with phonon/>` on
/// the xcodebuild line runs Phonon for real. Skipped otherwise — the model is
/// 153 MB and not in the repo. In the simulator Core ML runs on the Mac's CPU
/// and GPU, so this says nothing about the Neural Engine.
@Suite("Phonon synthesis (real model)")
struct PhononModelTests {
    static let root = ProcessInfo.processInfo.environment["MARLOW_VOICE_MODELS"].map { URL(fileURLWithPath: $0) }
    static let sentence = "Your next meeting is at three with the design team, and the afternoon is clear after that."
    static var phonon: URL { root!.appendingPathComponent("phonon") }

    @Test(.enabled(if: root != nil), arguments: [NeuralVoice.phonon[0], NeuralVoice.phonon.last!])
    func speaksASentence(voice: NeuralVoice) throws {
        let synth = PhononSynth(directory: Self.phonon, voice: voice.id)
        let loadStart = Date()
        synth.load()
        let loaded = Date().timeIntervalSince(loadStart)
        let start = Date()
        guard case .audio(let samples, let rate) = synth.generate(Self.sentence) else {
            Issue.record("Phonon didn't load")
            return
        }
        let took = Date().timeIntervalSince(start)
        let seconds = Double(samples.count) / Double(rate)
        print("NEURAL phonon/\(voice.id): load \(loaded)s, synth \(took)s for \(seconds)s of audio, RTF \(took / seconds)")
        #expect(rate == 24_000)
        // About 5s spoken; silence or a runaway would be far outside this.
        #expect(seconds > 3 && seconds < 10)
        let peak = samples.map(abs).max() ?? 0
        #expect(peak > 0.05 && peak <= 1.0, "peak \(peak)")
        // Kept beside the model, to be listened to or transcribed.
        let wav = Self.root!.appendingPathComponent("sample-phonon-\(voice.id).wav")
        try Self.wav(samples, rate: rate).write(to: wav)
    }

    /// The whole path voice mode uses: synthesis, the audio engine, and the
    /// finished callback the controller counts on — twice in a row, then a
    /// stop mid-sentence that must not report the cut-off one finished.
    @MainActor
    @Test(.enabled(if: root != nil))
    func playsThroughAndReportsEachSentenceFinished() async throws {
        try AVAudioSession.sharedInstance().setCategory(.playback)
        let fallback = FakeSpeech()
        let speech = NeuralSpeech(directory: Self.phonon, voice: NeuralVoice.phonon[0], fallback: fallback)
        var finished = 0
        speech.onUtteranceFinished = { finished += 1 }
        let start = Date()
        speech.speak("One moment.")
        speech.speak("Here it is.")
        while finished < 2, Date().timeIntervalSince(start) < 30 {
            try await Task.sleep(for: .milliseconds(50))
        }
        #expect(finished == 2)
        #expect(fallback.spoken.isEmpty, "the model loaded, so nothing should fall back")
        print("NEURAL phonon played two sentences in \(Date().timeIntervalSince(start))s")

        speech.speak(Self.sentence)
        try await Task.sleep(for: .seconds(2.5))
        speech.stopSpeaking()
        try await Task.sleep(for: .seconds(4))
        #expect(finished == 2, "a sentence cut off by a stop is not 'finished'")
    }

    static func wav(_ samples: [Float], rate: Int) -> Data {
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        let bytes = samples.count * 2
        data.append(contentsOf: Array("RIFF".utf8)); append(UInt32(36 + bytes))
        data.append(contentsOf: Array("WAVEfmt ".utf8)); append(UInt32(16)); append(UInt16(1)); append(UInt16(1))
        append(UInt32(rate)); append(UInt32(rate * 2)); append(UInt16(2)); append(UInt16(16))
        data.append(contentsOf: Array("data".utf8)); append(UInt32(bytes))
        for sample in samples { append(Int16(max(-1, min(1, sample)) * Float(Int16.max))) }
        return data
    }
}

@MainActor
@Suite("Neural speech")
struct NeuralSpeechTests {
    @Test func aModelThatWontLoadFallsBackToTheSystemVoice() async throws {
        // An empty folder: the model can't load, so every sentence must still
        // be spoken — by the fallback — and still be reported finished, or
        // voice mode would sit in "speaking" for ever.
        let fallback = FakeSpeech()
        let speech = NeuralSpeech(directory: scratchFolder(), voice: NeuralVoice.phonon[0], fallback: fallback)
        var finished = 0
        speech.onUtteranceFinished = { finished += 1 }
        speech.speak("First.")
        for _ in 0..<300 where fallback.spoken.isEmpty {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(fallback.spoken == ["First."])
        speech.speak("Second.")
        #expect(fallback.spoken == ["First.", "Second."], "once fallen back, it stays on the fallback")
        fallback.finishOne()
        fallback.finishOne()
        #expect(finished == 2)
    }

    @Test func aStopReachesTheFallback() {
        let fallback = FakeSpeech()
        let speech = NeuralSpeech(directory: scratchFolder(), voice: NeuralVoice.phonon[0], fallback: fallback)
        speech.stopSpeaking()
        #expect(fallback.stops == 1)
    }
}

@Suite("Phonon spelling")
struct PhononSpellingTests {
    @Test func britishMeasuresAreSpelledTheAmericanWay() {
        #expect(PhononSpelling.american("About 4 centimetres, and gusts to 67 kilometres an hour.")
                == "About 4 centimeters, and gusts to 67 kilometers an hour.")
        #expect(PhononSpelling.american("A metre, 2 millimetres, a litre, 500 millilitres.")
                == "A meter, 2 millimeters, a liter, 500 milliliters.")
        #expect(PhononSpelling.american("The Centre, the theatre and fibre.")
                == "The Center, the theater and fiber.")
        #expect(PhononSpelling.american("METRES") == "METERS")
    }

    @Test func otherWordsAreLeftAlone() {
        let text = "Entre nous, the centred acre is mediocre; a metric litany, sincere."
        #expect(PhononSpelling.american(text) == text)
    }
}

/// Phonon on the phone, many sentences in a row: FluidAudio's Core ML Kokoro
/// segfaults in Apple's libBNNS on iOS 26.6 after 8–20 synthesis calls
/// (FluidAudio #844), and Phonon also runs a Core ML model on the CPU.
/// Synthesis only, no playback, with the model the app on that phone has
/// already downloaded. Run on a device with `TEST_RUNNER_MARLOW_SOAK=<calls>`
/// (docs/tasks/voice-mode-tts-engines.md); skipped otherwise. A pass is not
/// proof — the reported crash came and went by the day — but a crash here
/// settles it. 300 passed on Mike's iPhone 14 Pro, iOS 26.6, 2026-10-21.
@Suite("Phonon soak (device)")
struct PhononSoakTests {
    static let calls = ProcessInfo.processInfo.environment["MARLOW_SOAK"].flatMap(Int.init)

    static let sentences = [
        "Your next meeting is at three with the design team.",
        "After that the afternoon's clear, so there's time for a run before dinner.",
        "I moved the dentist to Thursday at ten, and told them you'd be five minutes late.",
        "Three emails need a reply today: one from Sarah about the budget, one from the school, and one from your accountant.",
        "Yes.",
        "The forecast says rain from about four, so take a coat.",
        "I couldn't find a flight under four hundred dollars, but the train gets you there by noon for eighty-five.",
        "Done.",
        "Here's the short version: the contract is fine, except clause seven, which lets them end it with a week's notice.",
        "Traffic on the bridge is heavy; leaving now, you'd arrive around twenty past six.",
    ]

    @Test(.enabled(if: calls != nil), .timeLimit(.minutes(30)))
    func manySentencesInARow() async throws {
        let directory = try #require(
            await MainActor.run { VoiceModelStore.shared.directory(for: .phonon) },
            "Download Phonon in the app on this phone first")
        let voice = NeuralVoice.phonon[0]
        let calls = Self.calls!
        let result = await Task.detached { () -> (done: Int, audio: Double, took: Double) in
            let synth = PhononSynth(directory: directory, voice: voice.id)
            let loadStart = Date()
            synth.load()
            print("SOAK load \(Date().timeIntervalSince(loadStart))s")
            var audio = 0.0, took = 0.0
            for i in 0..<calls {
                let sentence = Self.sentences[i % Self.sentences.count]
                let start = Date()
                guard case .audio(let samples, let rate) = synth.generate(sentence), !samples.isEmpty else {
                    print("SOAK call \(i + 1) produced no audio")
                    return (i, audio, took)
                }
                let seconds = Double(samples.count) / Double(rate)
                let elapsed = Date().timeIntervalSince(start)
                audio += seconds
                took += elapsed
                if (i + 1) % 25 == 0 || i < 3 {
                    print("SOAK \(i + 1)/\(calls): \(String(format: "%.2f", elapsed))s for \(String(format: "%.2f", seconds))s, running RTF \(String(format: "%.3f", took / audio))")
                }
            }
            return (calls, audio, took)
        }.value
        print("SOAK done \(result.done)/\(calls) calls, \(Int(result.audio))s of audio in \(Int(result.took))s")
        #expect(result.done == calls)
    }
}
