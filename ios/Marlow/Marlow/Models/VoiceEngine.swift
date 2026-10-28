import Foundation

/// The voices that can read replies in voice mode
/// (docs/tasks/voice-mode-tts-engines.md). Phonon, a neural voice run on this
/// iPhone once its model is downloaded, is the one voice mode uses. Apple's
/// is only the fallback: Mike found it "ropy" even at Premium quality, then
/// "basically unusable" (2026-10-21). Kokoro and Pocket TTS were tried and
/// dropped by ear the same day.
enum VoiceEngine: String, CaseIterable {
    case apple
    case phonon
}

/// One of Phonon's voices: a `.safetensors` in its pack.
struct NeuralVoice: Identifiable, Hashable {
    let id: String
    let name: String
    var accent: Accent?

    enum Accent: String {
        case american = "American"
        case british = "British"
    }

    /// Phonon's English catalogue voices, British first, and the checkpoint's
    /// own default (the manifest script's `PHONON_VOICES`).
    static let phonon: [NeuralVoice] =
        ["Edwin", "Hugh", "Spencer", "Toby", "Alfie", "Teddy",
         "Bryony", "Maeve", "Imogen", "Pippa", "Lottie", "Freya", "Elodie-Rose"]
            .map { NeuralVoice(id: "en_gb_\($0)", name: $0, accent: .british) }
        + ["Preston", "Trevor", "Russell", "Miles", "Micah", "Easton",
           "Delia", "Hadley", "Everly", "Harper", "Juniper", "Zoey"]
            .map { NeuralVoice(id: "en_us_\($0)", name: $0, accent: .american) }
        + [NeuralVoice(id: "default", name: "Phonon's default")]
}

/// The stored voice. One write path, like `LocationPreference`.
enum VoicePreference {
    /// Kept from when each engine had its own choice, so a voice picked then
    /// survives.
    static let voiceKey = "voice.phonon"

    /// The chosen voice, or Phonon's first.
    static func voice(in defaults: UserDefaults = .standard) -> NeuralVoice {
        let id = defaults.string(forKey: voiceKey)
        return NeuralVoice.phonon.first { $0.id == id } ?? NeuralVoice.phonon[0]
    }

    static func setVoice(_ voice: NeuralVoice, in defaults: UserDefaults = .standard) {
        defaults.set(voice.id, forKey: voiceKey)
    }
}
