import CryptoKit
import Foundation
import os

/// The files a neural engine needs, from the bundled `VoiceModels.json`
/// (written by `scripts/voice_model_manifest.py`). Each file is pinned to a
/// Hugging Face commit and has a known size, so the app never asks what to
/// fetch, and a changed repo fails the download rather than changing the
/// voice.
struct VoiceModelManifest: Decodable, Equatable {
    struct File: Decodable, Equatable {
        let url: URL
        /// Where it goes, relative to the engine's folder.
        let path: String
        let size: Int64
    }

    let files: [File]

    var totalBytes: Int64 { files.reduce(0) { $0 + $1.size } }

    /// Names the engine's folder: any change to the file list — a new
    /// voice recording, a bumped model — is a different folder, so a build
    /// never loads a set that's missing what it expects. The repo revision
    /// alone missed a file added from a second repo (`/code-review high`).
    var fingerprint: String {
        let lines = files.map { "\($0.url.absoluteString) \($0.path) \($0.size)\n" }.joined()
        return SHA256.hash(data: Data(lines.utf8)).prefix(6).map { String(format: "%02x", $0) }.joined()
    }

    static func load(from bundle: Bundle = .main) -> [String: VoiceModelManifest] {
        guard let url = bundle.url(forResource: "VoiceModels", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let manifests = try? JSONDecoder().decode([String: VoiceModelManifest].self, from: data)
        else { return [:] }
        return manifests
    }

    /// A path that stays inside the engine's folder. The manifest is ours and
    /// bundled, but a `..` would still write anywhere in the container.
    static func isSafe(_ path: String) -> Bool {
        !path.isEmpty && !path.hasPrefix("/")
            && !path.split(separator: "/", omittingEmptySubsequences: false)
                .contains { $0 == ".." || $0 == "." || $0.isEmpty }
    }
}

/// Downloads, keeps and deletes the neural voices' models
/// (docs/tasks/voice-mode-tts-engines.md). One shared store, so Settings
/// and voice mode see the same state.
///
/// A download goes to `<engine>-<fingerprint>.partial` and is renamed into
/// place only when every file is there at its manifest size: a folder
/// without the suffix is always complete. Files already in the partial
/// folder at the right size are kept, so a download cut short by the app
/// going to the background resumes at the file it was on. The folder name
/// carries the manifest's fingerprint, so a build with a different file
/// list never loads an old set.
@MainActor
final class VoiceModelStore: ObservableObject {
    enum State: Equatable {
        case absent
        /// 0...1 of the bytes.
        case downloading(Double)
        case ready
        case failed(String)
        /// Downloaded, and iOS is compiling the model for the Neural Engine.
        case preparing
    }

    static let shared = VoiceModelStore()

    @Published private(set) var states: [VoiceEngine: State] = [:]

    /// Fetches one file to a local URL (a temporary file the caller moves),
    /// reporting bytes received so far.
    typealias Fetch = (URL, @escaping @Sendable (Int64) -> Void) async throws -> URL
    /// Readies a finished download for use, given its folder.
    typealias Prepare = (URL) async -> Void

    private let root: URL
    private let manifests: [String: VoiceModelManifest]
    private let fetch: Fetch
    private let prepare: Prepare
    private var tasks: [VoiceEngine: Task<Void, Never>] = [:]
    private static let log = Logger(subsystem: "com.marlow.app", category: "voice")

    init(
        root: URL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("VoiceModels", isDirectory: true),
        manifests: [String: VoiceModelManifest] = VoiceModelManifest.load(),
        fetch: @escaping Fetch = VoiceModelStore.urlSessionFetch,
        prepare: @escaping Prepare = VoiceModelStore.compileForNeuralEngine
    ) {
        self.root = root
        self.manifests = manifests
        self.fetch = fetch
        self.prepare = prepare
        for engine in VoiceEngine.allCases where engine != .apple {
            states[engine] = isComplete(engine) ? .ready : .absent
        }
        // Off the main thread: the store is made on the first visit to
        // Settings or voice mode, and Kokoro's folder alone is ~360 files
        // (`/code-review high`, 2026-10-21).
        let root = self.root
        Task.detached(priority: .utility) { Self.removeRetiredEngines(in: root) }
    }

    /// Kokoro and Pocket TTS were engines for a day (2026-10-21), and their
    /// downloads, 384 MB and up, would otherwise sit on the phone for good:
    /// remove every folder that isn't a current engine's.
    nonisolated private static func removeRetiredEngines(in root: URL) {
        let current = VoiceEngine.allCases.filter { $0 != .apple }.map { "\($0.rawValue)-" }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        for name in names where !current.contains(where: name.hasPrefix) {
            try? FileManager.default.removeItem(at: root.appendingPathComponent(name))
        }
    }

    /// Loads the model once, straight after the download, so iOS compiles it
    /// for the Neural Engine then, with "Preparing" on screen, rather than on
    /// the first sample or reply. FluidAudio measured that compile at
    /// 10–150 s; Core ML caches the result, and later loads took 4 s on
    /// Mike's phone. Runs on a GCD thread because `PhononSynth` blocks.
    nonisolated static func compileForNeuralEngine(_ directory: URL) async {
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            DispatchQueue.global(qos: .userInitiated).async {
                PhononSynth(directory: directory, voice: NeuralVoice.phonon[0].id).load()
                done.resume()
            }
        }
    }

    /// The engine's download size in whole megabytes ("153 MB"): the
    /// formatter's "153.4 MB" was precision nobody needs (Mike, 2026-10-21).
    func downloadSize(of engine: VoiceEngine) -> String {
        let megabytes = Double(manifest(for: engine)?.totalBytes ?? 0) / 1_000_000
        return "\(Int(megabytes.rounded())) MB"
    }

    func manifest(for engine: VoiceEngine) -> VoiceModelManifest? { manifests[engine.rawValue] }

    func state(of engine: VoiceEngine) -> State {
        engine == .apple ? .ready : states[engine] ?? .absent
    }

    /// The engine's folder, when its download is complete.
    func directory(for engine: VoiceEngine) -> URL? {
        isComplete(engine) ? folder(for: engine) : nil
    }

    func download(_ engine: VoiceEngine) {
        guard engine != .apple, tasks[engine] == nil, let manifest = manifest(for: engine) else { return }
        states[engine] = .downloading(0)
        // A locked phone suspends the app and the download with it, and the
        // big model files are one request each; keep the screen on until
        // it's done. Hundreds of megabytes is a couple of minutes on Wi-Fi.
        ScreenAwake.hold("download.\(engine.rawValue)", true)
        tasks[engine] = Task { [weak self] in
            await self?.run(engine, manifest: manifest)
        }
    }

    func cancel(_ engine: VoiceEngine) {
        tasks[engine]?.cancel()
    }

    func delete(_ engine: VoiceEngine) {
        tasks[engine]?.cancel()
        // Every fingerprint's folder, finished or not: nothing else uses them.
        let prefix = "\(engine.rawValue)-"
        let names = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        for name in names where name.hasPrefix(prefix) {
            try? FileManager.default.removeItem(at: root.appendingPathComponent(name))
        }
        states[engine] = .absent
    }

    // MARK: - Downloading

    private func run(_ engine: VoiceEngine, manifest: VoiceModelManifest) async {
        defer {
            tasks[engine] = nil
            progress[engine] = nil
            ScreenAwake.hold("download.\(engine.rawValue)", false)
        }
        let partial = folder(for: engine).appendingPathExtension("partial")
        do {
            try prepareRoot()
            try FileManager.default.createDirectory(at: partial, withIntermediateDirectories: true)
            guard manifest.files.allSatisfy({ VoiceModelManifest.isSafe($0.path) }) else {
                throw DownloadError.message("Bad file path in the voice manifest.")
            }
            var pending: [VoiceModelManifest.File] = []
            var progress = Progress(total: max(manifest.totalBytes, 1))
            for file in manifest.files {
                if Self.size(of: partial.appendingPathComponent(file.path)) == file.size {
                    progress.done += file.size
                } else {
                    pending.append(file)
                }
            }
            self.progress[engine] = progress
            report(engine)
            let fetch = self.fetch
            // A few at a time: Kokoro's espeak-ng data is ~350 small files,
            // and one by one each paid a Hugging Face redirect round trip —
            // 1% a minute in the simulator before the big file even started.
            try await withThrowingTaskGroup(of: (VoiceModelManifest.File, URL).self) { group in
                var queue = pending[...]
                var active = 0
                while true {
                    while active < Self.parallelFetches, let file = queue.popFirst() {
                        self.progress[engine]?.inFlight[file.path] = 0
                        group.addTask {
                            let fetched = try await fetch(file.url) { [weak self] received in
                                Task { @MainActor in self?.received(received, of: file.path, for: engine) }
                            }
                            return (file, fetched)
                        }
                        active += 1
                    }
                    guard let (file, fetched) = try await group.next() else { break }
                    active -= 1
                    self.progress[engine]?.inFlight[file.path] = nil
                    guard Self.size(of: fetched) == file.size else {
                        try? FileManager.default.removeItem(at: fetched)
                        throw DownloadError.message("A model file didn't match its expected size.")
                    }
                    let target = partial.appendingPathComponent(file.path)
                    try FileManager.default.createDirectory(
                        at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
                    try? FileManager.default.removeItem(at: target)
                    try FileManager.default.moveItem(at: fetched, to: target)
                    self.progress[engine]?.done += file.size
                    report(engine)
                    try Task.checkCancellation()
                }
            }
            let final = folder(for: engine)
            try? FileManager.default.removeItem(at: final)
            try FileManager.default.moveItem(at: partial, to: final)
            removeOtherRevisions(of: engine)
            states[engine] = .preparing
            await prepare(final)
            states[engine] = .ready
        } catch is CancellationError {
            states[engine] = .absent
        } catch let error as URLError where error.code == .cancelled {
            states[engine] = .absent
        } catch {
            Self.log.error("voice model download failed: \(error.localizedDescription, privacy: .public)")
            states[engine] = .failed(Self.describe(error))
        }
    }

    /// Bytes of a download so far: whole files, plus each file in flight.
    private struct Progress {
        let total: Int64
        var done: Int64 = 0
        var inFlight: [String: Int64] = [:]
    }

    private var progress: [VoiceEngine: Progress] = [:]
    static let parallelFetches = 4

    private func received(_ bytes: Int64, of path: String, for engine: VoiceEngine) {
        // A late tick for a file that's already finished would count it twice.
        guard progress[engine]?.inFlight[path] != nil else { return }
        progress[engine]?.inFlight[path] = bytes
        report(engine)
    }

    private func report(_ engine: VoiceEngine) {
        guard let progress = progress[engine], case .downloading = states[engine] else { return }
        let bytes = progress.done + progress.inFlight.values.reduce(0, +)
        states[engine] = .downloading(min(1, Double(bytes) / Double(progress.total)))
    }

    private enum DownloadError: Error { case message(String) }

    private static func describe(_ error: Error) -> String {
        if case DownloadError.message(let text) = error { return text }
        if let error = error as? URLError {
            switch error.code {
            case .notConnectedToInternet, .networkConnectionLost: return "The connection dropped. Tap Download to carry on."
            case .timedOut: return "The download timed out. Tap Download to carry on."
            default: break
            }
        }
        if (error as NSError).code == NSFileWriteOutOfSpaceError { return "Not enough free space on this iPhone." }
        return "The download failed. Tap Download to try again."
    }

    // MARK: - Folders

    private func folder(for engine: VoiceEngine) -> URL {
        let fingerprint = manifest(for: engine)?.fingerprint ?? "none"
        return root.appendingPathComponent("\(engine.rawValue)-\(fingerprint)", isDirectory: true)
    }

    private func isComplete(_ engine: VoiceEngine) -> Bool {
        guard engine != .apple, manifest(for: engine) != nil else { return false }
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: folder(for: engine).path, isDirectory: &isDirectory)
            && isDirectory.boolValue
    }

    /// Hundreds of megabytes the app can download again: kept out of iCloud
    /// and device backups.
    private func prepareRoot() throws {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var url = root
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
    }

    private func removeOtherRevisions(of engine: VoiceEngine) {
        let keep = folder(for: engine).lastPathComponent
        let names = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        for name in names where name.hasPrefix("\(engine.rawValue)-") && name != keep {
            try? FileManager.default.removeItem(at: root.appendingPathComponent(name))
        }
    }

    private static func size(of url: URL) -> Int64? {
        (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber)?.int64Value
    }

    // MARK: - Fetching

    /// A plain `URLSession` download. Progress is read off the task a few
    /// times a second rather than through a delegate.
    nonisolated static func urlSessionFetch(_ url: URL, progress: @escaping @Sendable (Int64) -> Void) async throws -> URL {
        // The task, or that the fetch was cancelled before it existed. Without
        // the flag, a Cancel landing just before a file started let the
        // whole file download anyway (`/code-review high`, 2026-10-21).
        let box = OSAllocatedUnfairLock<(task: URLSessionDownloadTask?, cancelled: Bool)>(
            uncheckedState: (nil, false))
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<URL, Error>) in
                let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
                let task = URLSession.shared.downloadTask(with: url) { location, response, error in
                    timer.cancel()
                    if let error { return continuation.resume(throwing: error) }
                    guard let location, let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                        return continuation.resume(throwing: URLError(.badServerResponse))
                    }
                    // The system deletes `location` when this returns.
                    let kept = FileManager.default.temporaryDirectory
                        .appendingPathComponent(UUID().uuidString)
                    do {
                        try FileManager.default.moveItem(at: location, to: kept)
                        continuation.resume(returning: kept)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
                timer.schedule(deadline: .now() + 0.25, repeating: 0.25)
                timer.setEventHandler { [weak task] in
                    if let task { progress(task.countOfBytesReceived) }
                }
                timer.resume()
                let cancelled = box.withLockUnchecked { state in
                    state.task = task
                    return state.cancelled
                }
                // Resumed and then cancelled, so the completion still runs
                // and the continuation is resumed exactly once.
                task.resume()
                if cancelled { task.cancel() }
            }
        } onCancel: {
            let task = box.withLockUnchecked { state in
                state.cancelled = true
                return state.task
            }
            task?.cancel()
        }
    }
}
