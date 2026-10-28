import Foundation

/// Recording behind a seam, so the state machine can be tested without a mic.
/// `DictationController` is the production one.
@MainActor
protocol VoiceRecorder: AnyObject {
    /// Starts listening. `onResult` fires exactly once per `begin` that isn't
    /// cancelled: the transcript, or nil when nothing came of it.
    func begin(onResult: @escaping (String?) -> Void)
    /// Stops listening and transcribes; the result goes to `begin`'s callback.
    func finish()
    /// Stops listening and reports nothing.
    func cancel()
}

extension DictationController: VoiceRecorder {}

/// Voice mode's one button and what it does in each state
/// (docs/tasks/voice-mode-hands-free.md, Mike 2026-10-20). Press to talk,
/// press again to send; Marlow's reply is read out sentence by sentence as it
/// streams; pressing while Marlow is working or speaking stops the speech and
/// starts listening — "stop whatever Marlow is doing out loud and listen to
/// me." The turn itself is never cancelled (`claude -p` can't be): it finishes,
/// and its whole text is in the chat.
///
/// Never endpoints on silence. The press is the only end of a turn, because
/// Mike pauses mid-thought and a truncated turn can't be taken back.
@MainActor
final class VoiceModeController: ObservableObject {
    enum Phase: Equatable {
        case idle
        case recording
        case transcribing
        /// Sent; nothing to speak yet.
        case waiting
        case speaking
    }

    @Published private(set) var phase: Phase = .idle

    private let recorder: VoiceRecorder
    private let speech: SpeechOutput
    private let send: (String) -> UUID?
    private let earcon: (Earcons.Kind) -> Void
    private let firstWorkingTick: Duration
    private let workingTickInterval: Duration
    private let approvalReminderInterval: Duration

    /// The reply being followed. Only one: an interrupted reply is dropped
    /// from here, so its later polls are ignored rather than spoken.
    private(set) var reply: UUID?
    private var chunker = SentenceChunker()
    private var fences = CodeFenceFilter()
    private var replyFinished = false
    private var queued = 0
    private var workingTask: Task<Void, Never>?
    /// An approval card is up: Marlow is waiting on them, not working.
    private var awaitingApproval = false

    init(
        recorder: VoiceRecorder,
        speech: SpeechOutput,
        send: @escaping (String) -> UUID?,
        earcon: @escaping (Earcons.Kind) -> Void = { _ in },
        firstWorkingTick: Duration = .milliseconds(800),
        workingTickInterval: Duration = .seconds(3),
        approvalReminderInterval: Duration = .seconds(10)
    ) {
        self.recorder = recorder
        self.speech = speech
        self.send = send
        self.earcon = earcon
        self.firstWorkingTick = firstWorkingTick
        self.workingTickInterval = workingTickInterval
        self.approvalReminderInterval = approvalReminderInterval
        speech.onUtteranceFinished = { [weak self] in self?.utteranceFinished() }
    }

    // MARK: - The button

    func press() {
        switch phase {
        case .idle:
            listen()
        case .recording:
            phase = .transcribing
            recorder.finish()
        case .transcribing:
            // Inert: the transcript is a second away and a new recording
            // would race it.
            break
        case .waiting, .speaking:
            silenceReply()
            listen()
        }
    }

    /// Leaving the mode, or anything that takes the audio away (a call,
    /// backgrounding): stop talking, stop listening, send nothing.
    func stopAll() {
        silenceReply()
        if phase == .recording || phase == .transcribing { recorder.cancel() }
        phase = .idle
    }

    private func listen() {
        phase = .recording
        earcon(.listening)
        recorder.begin { [weak self] text in self?.transcribed(text) }
    }

    private func transcribed(_ text: String?) {
        // Exited or cancelled in the meantime.
        guard phase == .recording || phase == .transcribing else { return }
        guard let text, let id = send(text) else {
            phase = .idle
            return
        }
        reply = id
        chunker = SentenceChunker()
        fences = CodeFenceFilter()
        replyFinished = false
        queued = 0
        phase = .waiting
        earcon(.sent)
        startWorkingTicks()
    }

    // MARK: - The reply

    /// The raw streamed text for a running reply (`ChatStore.onReplyProgress`).
    func replyProgress(_ id: UUID, text: String, marks: [Int]) {
        guard id == reply, !replyFinished else { return }
        speak(chunker.ingest(text, marks: marks))
    }

    /// The reply's terminal state: its final text, or nil when it failed or
    /// vanished (merged into a later turn, lost to a server restart).
    func replySettled(_ id: UUID, text: String?) {
        guard id == reply, !replyFinished else { return }
        replyFinished = true
        guard let text else {
            stopWorkingTicks()
            speech.stopSpeaking()
            queued = 0
            earcon(.failed)
            reply = nil
            if phase == .waiting || phase == .speaking { phase = .idle }
            return
        }
        speak(chunker.finish(text))
        finishIfDrained()
    }

    /// Reads a reply's terminal state off the chat's messages, which every
    /// path that settles a reply goes through (poll, prefetch, transcript).
    func messagesChanged(_ messages: [ChatMessage]) {
        guard let id = reply, !replyFinished else { return }
        guard let message = messages.first(where: { $0.id == id }) else {
            replySettled(id, text: nil)
            return
        }
        switch message.status {
        case .done: replySettled(id, text: message.text)
        case .error: replySettled(id, text: nil)
        default: break
        }
    }

    private func speak(_ sentences: [String]) {
        let spoken = sentences.map { SpeechText.plain(fences.filter($0)) }.filter { !$0.isEmpty }
        guard !spoken.isEmpty else { return }
        if phase == .waiting {
            stopWorkingTicks()
            phase = .speaking
        }
        for sentence in spoken {
            queued += 1
            speech.speak(sentence)
        }
    }

    private func utteranceFinished() {
        queued = max(0, queued - 1)
        finishIfDrained()
        // Said everything so far, but Marlow is still going: back to waiting,
        // with the purr. A spoken reply now opens with "Checking your notes."
        // before its tools run, and without this the tools, or an approval
        // card, were silence (2026-10-21).
        if queued == 0, !replyFinished, phase == .speaking {
            phase = .waiting
            startWorkingTicks()
        }
    }

    private func finishIfDrained() {
        guard replyFinished, queued == 0, phase == .waiting || phase == .speaking else { return }
        stopWorkingTicks()
        reply = nil
        phase = .idle
    }

    /// Stops the current reply being spoken, now and for its remaining polls.
    private func silenceReply() {
        stopWorkingTicks()
        speech.stopSpeaking()
        queued = 0
        reply = nil
        replyFinished = false
    }

    // MARK: - "Still working"

    /// A quiet tick while Marlow works and nothing is being said: 3–5s before
    /// the first word is normal (the spike, 2026-10-20), and in a car silence
    /// that long reads as broken. The first comes just after the "sent"
    /// tone, then every 3 s: waiting 4 s, then 3 s, for the first was "way
    /// too long" (Mike, 2026-10-21), since the countdown only starts once the
    /// speech has been transcribed.
    ///
    /// While an approval card is up, the tick is the approval chime instead,
    /// every 10 s: a purr says "Marlow is busy", which is wrong when Marlow
    /// is waiting on them.
    private func startWorkingTicks() {
        stopWorkingTicks()
        let (kind, first, interval) = awaitingApproval
            ? (Earcons.Kind.approval, approvalReminderInterval, approvalReminderInterval)
            : (Earcons.Kind.working, firstWorkingTick, workingTickInterval)
        workingTask = Task { [weak self] in
            var wait = first
            while true {
                try? await Task.sleep(for: wait)
                wait = interval
                guard let self, !Task.isCancelled, self.phase == .waiting else { return }
                self.earcon(kind)
            }
        }
    }

    // MARK: - Approvals

    /// The approval card appeared or went. It shows over the voice screen,
    /// but in a car nobody is looking at it: Mike approved one by chance,
    /// having heard only purrs (2026-10-21). A chime says "look at the
    /// phone"; once answered, the purr resumes until Marlow speaks.
    func approvalChanged(pending: Bool) {
        guard pending != awaitingApproval else { return }
        awaitingApproval = pending
        // A chime while recording would land in the recording.
        if pending, phase != .recording, phase != .transcribing { earcon(.approval) }
        if phase == .waiting { startWorkingTicks() }
    }

    private func stopWorkingTicks() {
        workingTask?.cancel()
        workingTask = nil
    }
}
