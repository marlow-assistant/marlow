import AVFoundation
import os
import UIKit

/// The one audio session voice mode holds open from entry to exit
/// (docs/tasks/voice-mode-hands-free.md). `.playAndRecord` for its whole life,
/// not a switch per press: on AirPods that means the HFP call route for the
/// replies too, which Mike heard on device on 2026-10-20 and called
/// acceptable — and it buys no route change between talking and listening,
/// plus a live mic route if barge-in by voice is ever built.
enum VoiceAudioSession {
    static func activate() throws {
        let session = AVAudioSession.sharedInstance()
        // `.defaultToSpeaker`, or with no headphones the replies come out of
        // the earpiece, which nobody holds to their ear in a car.
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try session.setActive(true)
        // The start/stop haptics are muted while a record-capable session is
        // live otherwise (same reason as DictationController's).
        try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)
    }

    static func deactivate() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

/// Keeps the screen on while anything needs it: voice mode, and a voice
/// model download. Each holds its own reason, so one finishing can't turn
/// the idle timer back on under the other — a download ending mid-drive
/// would otherwise let the phone lock and stop voice mode
/// (`/code-review high`, 2026-10-21).
@MainActor
enum ScreenAwake {
    private static var reasons: Set<String> = []

    static func hold(_ reason: String, _ on: Bool) {
        if on { reasons.insert(reason) } else { reasons.remove(reason) }
        UIApplication.shared.isIdleTimerDisabled = !reasons.isEmpty
    }
}

/// Speaking, behind a seam so `VoiceModeController` can be tested without a
/// synthesizer.
@MainActor
protocol SpeechOutput: AnyObject {
    /// Once per utterance that played to its end. Not called for one cut off
    /// by `stopSpeaking()`.
    var onUtteranceFinished: (() -> Void)? { get set }
    func speak(_ text: String)
    func stopSpeaking()
}

@MainActor
final class SystemSpeech: NSObject, SpeechOutput, AVSpeechSynthesizerDelegate {
    var onUtteranceFinished: (() -> Void)?

    private let synth = AVSpeechSynthesizer()
    /// Utterances queued since the last stop. A delegate callback for one not
    /// in here belongs to speech already cut off and is dropped, so a stop
    /// can't be followed by a stale "finished" that ends the next reply early.
    private var live: Set<ObjectIdentifier> = []

    override init() {
        super.init()
        synth.delegate = self
        // Voice mode owns the session (`VoiceAudioSession`); the synthesizer
        // must not reconfigure it under the recorder.
        synth.usesApplicationAudioSession = true
    }

    func speak(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = Self.voice
        live.insert(ObjectIdentifier(utterance))
        synth.speak(utterance)
    }

    func stopSpeaking() {
        live.removeAll()
        synth.stopSpeaking(at: .immediate)
    }

    /// Best installed English voice: premium, then enhanced, then default,
    /// preferring the phone's own English. Siri voices are not offered to
    /// apps at all — Mike downloaded one first and got the fallback
    /// ("the world's worst TTS"). Only Phonon's fallback now, so the screen
    /// no longer points at a Premium voice to download.
    static let voice: AVSpeechSynthesisVoice? = {
        let preferred = Locale.preferredLanguages.first ?? "en-US"
        let english = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix("en") }
        return english.max { a, b in
            (a.quality.rawValue, a.language == preferred ? 1 : 0)
                < (b.quality.rawValue, b.language == preferred ? 1 : 0)
        } ?? AVSpeechSynthesisVoice(language: preferred)
    }()

    // Delegate calls can arrive off the main actor; `DispatchQueue.main.async`
    // keeps them in order, which a `Task` hop per call would not.
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        DispatchQueue.main.async {
            MainActor.assumeIsolated {
                guard self.live.remove(id) != nil else { return }
                self.onUtteranceFinished?()
            }
        }
    }
}

/// Short tones for what the screen can't say when nobody is looking at it —
/// "listening", "sent", "still working", "that failed". `START.md` gap #4
/// rejected spoken filler ("one moment!"); a tone isn't Marlow speaking, so it
/// stays honest. Synthesised, so there are no audio assets to ship.
@MainActor
final class Earcons {
    enum Kind { case listening, sent, working, approval, failed }

    private var players: [Kind: AVAudioPlayer] = [:]
    private static let log = Logger(subsystem: "com.marlow.app", category: "voice")

    func play(_ kind: Kind) {
        if players[kind] == nil {
            players[kind] = try? AVAudioPlayer(data: Self.wav(for: kind))
        }
        guard let player = players[kind] else {
            Self.log.error("earcon couldn't be built")
            return
        }
        player.currentTime = 0
        player.play()
    }

    private static let rate = 44_100.0

    /// (frequency Hz, seconds) notes, played in order.
    private static func notes(for kind: Kind) -> ([(Double, Double)], Float) {
        switch kind {
        case .listening: ([(660, 0.07), (880, 0.09)], 0.35)
        case .sent: ([(880, 0.07), (660, 0.09)], 0.3)
        case .working, .approval: ([], 0)
        case .failed: ([(330, 0.12), (262, 0.18)], 0.35)
        }
    }

    /// -1...1 samples for the cue, each note with a short fade so it doesn't
    /// click.
    static func samples(for kind: Kind) -> [Float] {
        if kind == .working { return purr() }
        if kind == .approval { return tinkTink() }
        let (notes, volume) = notes(for: kind)
        var samples: [Float] = []
        for (frequency, seconds) in notes {
            let count = Int(rate * seconds)
            let fade = Int(rate * 0.01)
            for i in 0..<count {
                let envelope = Float(min(1, Double(min(i, count - i)) / Double(fade)))
                samples.append(Float(sin(2 * .pi * frequency * Double(i) / rate)) * volume * envelope)
            }
        }
        return samples
    }

    /// "Still working": a low tone that swells and fades over 0.6 s while
    /// its volume flutters. A single 60 ms blip read as a glitch, not as
    /// something happening; a wibble (pitch wobbling around 560 Hz, then
    /// 420 Hz) sounded like "a Star Trek theremin". Mike picked this one from
    /// six rendered options, 2026-10-21: the flutter is in the volume, not
    /// the pitch.
    private static func purr() -> [Float] {
        let seconds = 0.6
        let count = Int(rate * seconds)
        return (0..<count).map { i in
            let t = Double(i) / rate
            // A smooth swell and fade, so it never clicks or startles.
            let swell = pow(sin(.pi * t / seconds), 2)
            // The purr: the volume dips and recovers 11 times a second.
            let flutter = 0.65 + 0.35 * sin(2 * .pi * 11 * t)
            // A quiet octave above makes 300 Hz rounder than a bare sine.
            let phase = 2 * .pi * 300 * t
            let tone = sin(phase) + 0.35 * sin(2 * phase)
            return Float(tone / 1.35 * swell * flutter) * 0.18
        }
    }

    /// "Look at the phone, it needs your OK": two taps on a glass, high and
    /// short, so it stands out from the purr and from Marlow's voice. Also
    /// played on the chat screen, so one sound means "approval needed"
    /// everywhere. Mike picked it from six rendered options, 2026-10-21.
    private static func tinkTink() -> [Float] {
        // A glass's overtones aren't whole multiples of the note, which is
        // what makes it a "tink" and not a beep.
        func tap(_ t: Double, length: Double, decay: Double) -> Double {
            guard t >= 0, t < length else { return 0 }
            let attack = min(t / 0.005, 1), tail = min(1, (length - t) / 0.02)
            let f = 1568.0
            let tone = sin(2 * .pi * f * t) + 0.3 * sin(2 * .pi * f * 2.76 * t)
                + 0.1 * sin(2 * .pi * f * 5.4 * t)
            return tone * attack * exp(-t / decay) * tail
        }
        let count = Int(rate * 0.59)
        return (0..<count).map { i in
            let t = Double(i) / rate
            let both = tap(t, length: 0.35, decay: 0.07) + tap(t - 0.14, length: 0.45, decay: 0.09)
            return Float(both * 0.315)
        }
    }

    /// 16-bit mono PCM in a WAV container.
    static func wav(for kind: Kind) -> Data {
        let samples = samples(for: kind).map { Int16($0 * Float(Int16.max)) }
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        let bytes = samples.count * 2
        data.append(contentsOf: Array("RIFF".utf8)); append(UInt32(36 + bytes))
        data.append(contentsOf: Array("WAVEfmt ".utf8)); append(UInt32(16)); append(UInt16(1)); append(UInt16(1))
        append(UInt32(rate)); append(UInt32(rate) * 2); append(UInt16(2)); append(UInt16(16))
        data.append(contentsOf: Array("data".utf8)); append(UInt32(bytes))
        for sample in samples { append(sample) }
        return data
    }
}
