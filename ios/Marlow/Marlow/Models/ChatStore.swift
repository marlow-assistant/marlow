import Foundation
import UIKit

/// Timing for the send/poll state machine. Hoisted out of `ChatStore` as
/// injectable values so tests can exercise the fast->slow phase handoff and the
/// attempt-cap paths without waiting real minutes for them. Production uses
/// `.default`, which carries the same numbers the constants used to.
struct PollingPolicy {
    /// Burst phase: the first few seconds of a turn, where everything worth
    /// showing happens — CLI startup, the first tool call, the first text
    /// (measured at 3.2s on the box). At the 2s fast cadence a tool call
    /// starting at t=2.1s is invisible until t=4s; here it lands within half a
    /// second of the server knowing about it.
    ///
    /// **This cannot flood a slow link.** `pollLoop` is sequential — sleep,
    /// await the response, sleep again — so the real cadence is
    /// `interval + round trip` and the interval is a CEILING on the rate, not
    /// a floor. On good wifi (~30ms) this polls every ~0.53s; on bad cell
    /// (~1.5s) it self-throttles to ~2s, exactly today's behaviour, with no
    /// adaptive logic to get wrong. A slow network can never be polled faster
    /// than it can answer.
    ///
    /// Bounded by WALL CLOCK rather than an attempt count, and that is the
    /// part that matters on a bad link: 12 attempts at 500ms stretches to 24
    /// seconds of burst when each round trip costs 1.5s, which is the one way
    /// this could have made a slow network worse. A deadline ends when it
    /// should regardless of link speed. `burstAttempts` is a second bound for
    /// the pathological case, never the binding one on a healthy link.
    var burstInterval: UInt64 = 500_000_000 // 500ms
    var burstDuration: Duration = .seconds(6)
    var burstAttempts: Int = 16

    /// Fast phase: covers a normal turn.
    var fastInterval: UInt64 = 2_000_000_000 // 2s
    var fastAttempts: Int = 150 // ~5 minutes

    /// Slow background phase: a tool-heavy turn that finishes late.
    var slowInterval: UInt64 = 30_000_000_000 // 30s
    var slowAttempts: Int = 120 // ~another hour

    var healthInterval: UInt64 = 15_000_000_000 // 15s

    /// Least time between `/v1/usage` reads in the health loop, for the
    /// >= 90% status row. Plan usage only moves when Marlow runs, so the
    /// 15s health tick would be 20 reads for every change worth seeing.
    var usageInterval: TimeInterval = 300 // 5 min
    var activityDwell: UInt64 = 700_000_000 // 700ms

    /// How long a foreground fetch may run before the transcript says so.
    /// Tapping "Marlow has something for you" on a poor connection used to
    /// land on a chat with nothing new in it and no sign anything was
    /// happening — the fetch (`/v1/pending` then, the transcript sync now) sets
    /// no timeout of its own, so it inherits URLSession's 60s and the screen
    /// can sit blank for a minute (Mike, 2026-09-07). The delay exists so a healthy link never flashes the
    /// indicator: a round trip on wifi is ~30ms, two orders of magnitude
    /// inside this, and it is for the case where waiting is the experience.
    var connectionNoticeDelay: UInt64 = 600_000_000 // 600ms

    /// Typing-out pace for the streamed reply (docs/tasks/streaming-reply-text.md).
    /// The server's text arrives in whole slabs — one poll interval of
    /// generation, ~430 characters at the rate measured on the box — so
    /// rendering each response as it lands would step a paragraph at a time.
    /// The tick reveals a slice instead, sized to drain whatever is pending
    /// within one poll interval, so smoothing is never the reason text is late:
    /// a faster model simply types faster.
    var partialTextTick: UInt64 = 50_000_000 // 50ms
    /// Past this much unshown text, stop typing and jump. A phone coming back
    /// from a tunnel with the whole answer buffered should show the answer,
    /// not perform it.
    var partialTextJumpThreshold: Int = 2_000

    /// Retrying the initial `POST /v1/message` before giving up and marking
    /// the send `.error`. A dropped connection here is the most likely
    /// network failure of all — it's the very first round trip of a turn,
    /// and on a flaky connection (a tunnel, a dead zone) the odds of it
    /// landing on exactly the moment signal drops are as good as any other.
    /// Doubling backoff capped at `sendMaxInterval`, budgeted for a few
    /// minutes total — long enough to ride out most gaps without the message
    /// ever needing to look "failed."
    ///
    /// The cap came down from 30s to 8s on 2026-08-27, once
    /// `sendAttemptTimeout` below put a real bound under each attempt. There
    /// is no contention argument for backing off here — one phone, one box,
    /// and a handful of sends in flight at most — so this schedule
    /// only stretches a fixed attempt count over a useful wall clock. What it
    /// costs is latency *after* recovery: what's being retried is one boolean
    /// (is there a path yet), so a 30s sleep meant signal could return a
    /// second in and the message still sit dead for 29 more — which is the
    /// "hung" this task was opened for. At 8s the worst case is ~3.5min
    /// (8 × 20s of request + ~52s of sleeps) rather than ~5min, and dead air
    /// after recovery is bounded at 8s. `sendMaxAttempts` is deliberately NOT
    /// part of that trade — see `docs/tasks/ios-chat-ux-gaps.md` item 1.
    ///
    /// `sendBackoff` shifts by an already-incremented `attempt`, so the first
    /// retry waits 2 × `sendBaseInterval` (4s) — the base is never itself a
    /// delay the schedule produces.
    var sendMaxAttempts: Int = 8
    var sendBaseInterval: UInt64 = 2_000_000_000 // 2s
    var sendMaxInterval: UInt64 = 8_000_000_000 // 8s

    /// Per-attempt inactivity timeout and wall-clock deadline for
    /// `POST /v1/message`. The route only waits for the turn to be queued, so
    /// Mike chose 20s on 2026-08-27 as already well beyond a healthy round
    /// trip while still failing visibly on a poor connection. One flat bound
    /// covers text and the app's downscaled images alike.
    var sendAttemptTimeout: TimeInterval = 20

    static let `default` = PollingPolicy()
}

/// Per-message cursor and bounded display backlog for the server's cumulative
/// activity array. Labels, rather than raw server events, are queued so
/// consecutive events with the same user-facing copy coalesce before pacing.
struct ActivityPlaybackState {
    private(set) var pendingLabels: [String] = []
    private var lastQueuedLabel: String?
    private var previousWindow: [TurnActivity] = []

    mutating func ingest(_ activities: [TurnActivity]) {
        // The server keeps only its latest 64 events. Find the longest overlap
        // between our previous tail and its current prefix so a full window
        // that slides by one still yields that one new event. A count cursor
        // alone would freeze forever once both arrays stayed length 64.
        let maximumOverlap = min(previousWindow.count, activities.count)
        var overlap = maximumOverlap
        while overlap > 0 {
            if previousWindow.suffix(overlap).elementsEqual(activities.prefix(overlap)) {
                break
            }
            overlap -= 1
        }

        let newActivities = activities.dropFirst(overlap)
        previousWindow = activities

        for activity in newActivities {
            let label = activity.label
            if label != lastQueuedLabel {
                pendingLabels.append(label)
                lastQueuedLabel = label
            }
        }
        // A phone returning after a long gap should catch up to the recent
        // shape of the turn, not spend seconds narrating its distant past.
        if pendingLabels.count > 3 {
            pendingLabels = Array(pendingLabels.suffix(3))
        }
    }

    mutating func nextLabel() -> String? {
        guard !pendingLabels.isEmpty else { return nil }
        return pendingLabels.removeFirst()
    }

}

/// Paces the server's cumulative reply text out as typing.
///
/// Note what is NOT here, in contrast to `ActivityPlaybackState` above: no
/// cursor, no overlap search, no dedupe. The server sends a growing string and
/// the whole thing every time, so "what is new" is just "what is longer" —
/// which is why a missed poll costs nothing and there is no resync path to get
/// wrong. Activities need that machinery because they are a capped *window*;
/// text deliberately is not.
struct PartialTextPlaybackState {
    /// Everything the server has sent. Only ever grows: a shorter payload
    /// means the turn was coalesced or the server restarted, and rewinding a
    /// bubble the user is mid-sentence in is worse than showing stale text
    /// that the terminal `resultText` is about to replace anyway.
    private(set) var target: String = ""
    private(set) var revealed: String = ""
    /// Characters revealed per tick, recomputed each time a slab lands so the
    /// backlog drains in about the time the next one takes to arrive. Held
    /// rather than derived per tick on purpose: recomputing from the shrinking
    /// backlog decays geometrically and never actually finishes, which leaves
    /// the tail of every reply arriving late — the exact latency tax smoothing
    /// is supposed to avoid.
    private var stride: Int = 1
    /// Offsets into `target` where a tool call interrupted the reply, straight
    /// off the server. Assigned only when the list is at least as long as what
    /// we hold: the server evicts its oldest marks past a cap, and losing a
    /// boundary would silently re-fuse two thoughts.
    private(set) var marks: [Int] = []
    /// Set when this state attached to a turn that was ALREADY producing text
    /// — the relaunch path, where `reconcilePendingMessages` wipes the
    /// streamed text off the message and `startPoll` then re-ingests the
    /// server's whole cumulative string into a state that has revealed
    /// nothing. Typing that out is a REPLAY of a reply generated minutes or
    /// hours ago, which is what Mike saw on 2026-09-05: open the app on an
    /// unread message and watch a finished turn type itself.
    ///
    /// The rule is "type only what arrived while we were watching" — so the
    /// first slab a late joiner receives is revealed whole and everything
    /// after it paces normally. Deliberately NOT keyed on "is this the first
    /// ingest", which is also true of a live turn's first text and would
    /// flatten the effect this whole feature exists for.
    private var catchingUp: Bool

    init(joinedLate: Bool = false) {
        catchingUp = joinedLate
    }

    var backlog: Int { max(0, target.count - revealed.count) }

    /// The marks the user can actually see yet — the ones inside what has been
    /// revealed. A mark past the typing cursor has nothing to split.
    var revealedMarks: [Int] {
        let shown = revealed.unicodeScalars.count
        return marks.filter { $0 > 0 && $0 < shown }
    }

    /// `ticksPerPoll` is how many playback ticks fit in one poll interval. It
    /// is the only pacing input, and there is deliberately no characters-per-
    /// second constant to tune: a faster model produces bigger slabs and the
    /// text simply types faster.
    mutating func ingest(_ text: String, marks: [Int] = [], ticksPerPoll: Int) {
        if marks.count >= self.marks.count { self.marks = marks }
        guard text.count > target.count else { return }
        target = text
        if catchingUp {
            // Everything on the next tick, rather than assigning `revealed`
            // here: `advance()` is the single path that publishes to the
            // bubble, and a state that arrived already drained would never
            // start its player at all (`startPartialTextPlaybackIfNeeded`
            // guards on a non-empty backlog), leaving the caught-up text
            // held and unshown.
            catchingUp = false
            stride = backlog
            return
        }
        stride = max(1, Int((Double(backlog) / Double(max(1, ticksPerPoll))).rounded(.up)))
    }

    /// The next thing to display, or nil when there is nothing left to reveal.
    mutating func advance(jumpThreshold: Int) -> String? {
        let pending = backlog
        guard pending > 0 else { return nil }
        let step = pending >= jumpThreshold ? pending : stride
        revealed = String(target.prefix(revealed.count + step))
        return revealed
    }

    /// Splits streamed text at the server's tool-call offsets, dropping empty
    /// pieces. Without this the 11% of turns that go text -> tool -> text
    /// render as "I'll check your calendar.You have three meetings" — two
    /// separate thoughts fused into one sentence.
    ///
    /// Offsets are counted in UNICODE SCALARS, not Characters, because that is
    /// what produced them: the server measures `len(partial_text)` in Python,
    /// which counts code points. Splitting a Swift String by `Character` would
    /// drift from the server's arithmetic on the first emoji or accented
    /// letter and put the break in the wrong place.
    static func segments(of text: String, splitAt marks: [Int]) -> [String] {
        let scalars = Array(text.unicodeScalars)
        let cuts = Set(marks.filter { $0 > 0 && $0 < scalars.count }).sorted()
        guard !cuts.isEmpty else { return text.isEmpty ? [] : [text] }

        var pieces: [String] = []
        var start = 0
        for cut in cuts + [scalars.count] {
            let piece = String(String.UnicodeScalarView(scalars[start..<cut]))
            if !piece.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                pieces.append(piece)
            }
            start = cut
        }
        return pieces
    }
}

/// Whether the app is currently getting through to the server, for the one
/// case where the user has no other way to tell: a fetch this device did not
/// ask for. A send has its own spinner and its own error bubble; a
/// notification tap has nothing — the transcript simply sits there looking
/// finished while the transcript sync is still in flight.
enum ConnectionActivity: Equatable {
    /// Reaching the server is not currently interesting to look at.
    case idle
    /// A fetch the user is implicitly waiting on has been running longer than
    /// `connectionNoticeDelay`.
    case checking
    /// The last completed pass did not get through. Sticky on purpose: it
    /// stays up across the quiet gap between passes, because "we tried and
    /// failed 8 seconds ago" is exactly what the blank screen fails to say.
    case unreachable
}


/// Drives the chat screen: send -> POST /v1/message, poll GET /v1/turn/{id}
/// until terminal, persist the transcript locally, and poll /health for the
/// reauth/paused banners. One instance per paired session.
///
/// Every network-driving Task is tracked in `activeTasks` and checks
/// `Task.isCancelled` at each suspension point, so `cancelActiveWork()` (called
/// from the Unpair action, and from `deinit` as a backstop) reliably stops
/// outbound calls rather than leaving them running against a token the UI has
/// already discarded.
@MainActor
final class ChatStore: ObservableObject {
    @Published private(set) var messages: [ChatMessage] = []
    /// The oldest `createdAt` `ChatView` builds rows for; `nil` renders the
    /// whole transcript. Set once at launch and only ever moved earlier — see
    /// `TranscriptWindow` for why the rows are windowed at all. A sent message
    /// is newer than it by construction; a synced row can be older (a cron
    /// reply that arrived days late, phone off), so `syncTranscript` lowers the
    /// floor to meet it — an arrival someone was notified about must not land
    /// above a "show earlier" button.
    @Published private(set) var transcriptFloor: Date?
    /// The server has rows older than the oldest this phone holds: the last
    /// bootstrap or earlier-page fetch said `has_more`. What keeps the "Show
    /// earlier" button up once the local rows run out. Persisted, so a cold
    /// launch does not forget there is more.
    @Published private(set) var hasOlderOnServer = false

    /// The rows `ChatView` renders. `messages` stays the whole transcript for
    /// everything else — persistence, reconcile, the jump-to-latest button.
    var visibleMessages: ArraySlice<ChatMessage> {
        messages[TranscriptWindow.startIndex(of: messages, floor: transcriptFloor)...]
    }

    var hasEarlierMessages: Bool {
        TranscriptWindow.startIndex(of: messages, floor: transcriptFloor) > 0 || hasOlderOnServer
    }

    /// Reveals the 48 hours before the oldest row on screen (at least
    /// `TranscriptWindow.minimumCount` rows), not the whole history — the
    /// history only grows, and one tap costing the full launch-time pause this
    /// window exists to avoid would be the same bug moved up the screen.
    ///
    /// Once the window already starts at the first message this phone holds,
    /// the next page comes from the server (`fetchEarlierPage`). That path is
    /// asynchronous, so unlike the local reveal it lands after this returns.
    func showEarlierMessages() {
        let start = TranscriptWindow.startIndex(of: messages, floor: transcriptFloor)
        guard start > 0 else {
            fetchEarlierPage()
            return
        }
        transcriptFloor = TranscriptWindow.floor(
            for: messages, before: start, reference: messages[start].createdAt
        )
    }
    @Published var reauthNeeded = false
    /// True while the session still works but is inside the server's
    /// pre-warn window. Kept separate from `reauthNeeded` because the ask
    /// is different: this one is "do it today", not "everything has
    /// stopped". `/health` has reported it since the beginning; nothing
    /// ever showed it, so the only warning Mike got was after the fact.
    @Published var reauthPrewarn = false
    @Published var queuePaused = false
    /// The latest `/v1/usage` reading, kept only to drive the >= 90% status
    /// row. Held as the report, not as a verdict: whether a window is high
    /// depends on `now` (a reset clears it), so the view asks `UsageFormat`
    /// at draw time. A failed fetch leaves the previous reading in place.
    @Published private(set) var usageReport: UsageReport?
    /// The model the server reports it is running (`/health`). Display-only —
    /// nil until the first health poll lands, and on a server predating the
    /// field, in which case Settings shows a dash rather than guessing.
    @Published private(set) var serverModel: String?
    /// `branch@sha` for the code the SERVER PROCESS is running, once a health
    /// poll has landed. Surfaced in Settings because the box can now be put on
    /// a feature branch for testing (`deploy/update.sh --branch`), which makes
    /// "what is actually running over there" a question with a non-obvious
    /// answer — and one worth being able to check from the phone.
    @Published private(set) var serverBuild: String?

    /// Drives the indicator at the foot of the conversation. Only the
    /// health/sync loop writes it — the send path deliberately does not,
    /// because a send already reports itself through its own message bubble
    /// and two things narrating one network is noise.
    @Published private(set) var connectionActivity: ConnectionActivity = .idle
    /// Fires the `.checking` notice once a foreground pass has been slow
    /// enough to be worth mentioning. Held so the pass can cancel it on the
    /// happy path before it ever shows.
    private var connectionNoticeTask: Task<Void, Never>?

    /// The tool-permission request the sheet should show, if any. A
    /// permission request can only exist while a turn is RUNNING (the
    /// PreToolUse hook fires mid-invocation), so this is populated from the
    /// same poll loop that already checks `getTurn` — no separate timer, no
    /// push dependency, at most `fastInterval` (2s) of latency
    /// (docs/tasks/ios-permission-round-trip-ui.md §4).
    @Published private(set) var pendingPermission: PermissionRequestInfo?

    /// Set after answering a request the server had already resolved
    /// (timed out, or answered from another device). Transient — clears
    /// itself a few seconds after being set.
    @Published private(set) var permissionNotice: String?

    /// Turn ids of assistant messages that have just reached `.done`, queued
    /// for ChatView to drain (it plays the receive sound for them).
    ///
    /// An event queue rather than something the view derives from `messages`:
    /// "which assistant messages are done" is only answerable by walking the
    /// whole transcript, and the poll loop republishes several times a second
    /// for the length of a turn, so deriving it costs an O(transcript) pass
    /// and set comparison per tick on a transcript that only ever grows.
    /// Publishing the transition itself is O(1) and says what actually
    /// happened — a turn *completed* — instead of restating a steady state.
    @Published private(set) var completedAssistantTurns: [String] = []

    /// Placeholder ids whose reply has just begun *writing itself* — the
    /// moment `streamedText` goes from nothing to its first characters —
    /// queued for ChatView to drain (it fires the reply-started haptic).
    ///
    /// A separate queue from `completedAssistantTurns` and keyed differently
    /// on purpose: that one carries server turn ids because its whole job is
    /// to be matched against the ids APNs already sounded, and this one has no
    /// such counterpart — a push only ever announces a *finished* turn, so
    /// there is nothing to suppress against and the placeholder's own id is
    /// the thing actually available at the moment the text lands.
    ///
    /// Mike's ask (2026-09-11) was for feedback that "Marlow is responding",
    /// and this is the earliest instant that is true and observable. It is
    /// deliberately not "the first activity label" — a turn can sit on
    /// "Searching…" for a while and still be some way from saying anything.
    @Published private(set) var startedAssistantReplies: [UUID] = []

    /// Ids already announced by `startedAssistantReplies`, so one reply buzzes
    /// once however many times its text is republished.
    ///
    /// Never pruned, including by `clearTurnProgress`, and that is the
    /// conservative direction rather than an oversight: pruning at terminal
    /// would re-arm the latch for any stray late update that put text back on
    /// a finished message, which is a double buzz — while keeping the id costs
    /// 16 bytes per turn for one app session, and can only ever *under*-fire.
    private var announcedReplyStarts: Set<UUID> = []

    /// The highest server `seq` this cache holds; the cursor `syncTranscript`
    /// asks past. `nil` means nothing has been fetched yet — a fresh install or
    /// an unreadable cache — and the next sync bootstraps from the newest page.
    private var latestSeq: Int? {
        didSet { if (latestSeq == nil) != awaitingFirstFetch { awaitingFirstFetch = latestSeq == nil } }
    }
    /// True until the first transcript fetch lands. An empty transcript is
    /// then "not loaded yet", not "nothing here": on the build that dropped the
    /// local history, the greeting flashed before the server's rows arrived
    /// (Mike, 2026-10-01), so the view says a fetch is under way instead.
    @Published private(set) var awaitingFirstFetch = true
    /// One sync at a time. Everything is on the main actor, so a flag is enough.
    private var syncInFlight = false
    /// Where `fetchEarlierPage` keeps its task in `activeTasks`, so it is
    /// cancelled and awaited like every other piece of network work.
    private let earlierFetchKey = UUID()

    private let client: ChatAPI
    private let deviceToken: String
    private let persistURL: URL
    private let attachmentsDirectoryURL: URL
    private let policy: PollingPolicy
    /// Backing store for the chosen thinking effort. See `init`.
    private let preferences: UserDefaults
    /// Read at send time for the `location` the message carries. Nil result =
    /// attach nothing (see `LocationProvider.currentLocation`).
    private let currentLocation: @MainActor () -> OutgoingLocation?

    private var healthTask: Task<Void, Never>?
    private var activeTasks: [UUID: Task<Void, Never>] = [:]
    private var activityPlaybackStates: [UUID: ActivityPlaybackState] = [:]
    private var activityPlaybackTasks: [UUID: Task<Void, Never>] = [:]
    private var activityPlaybackTokens: [UUID: UUID] = [:]
    private var partialTextStates: [UUID: PartialTextPlaybackState] = [:]
    private var partialTextTasks: [UUID: Task<Void, Never>] = [:]
    private var partialTextTokens: [UUID: UUID] = [:]
    /// Placeholders whose turn was already underway when this process
    /// attached to it — set by `reconcilePendingMessages()`, read once when
    /// the first slab of streamed text builds a playback state. See
    /// `PartialTextPlaybackState.catchingUp` for why the distinction has to
    /// be carried from there to here rather than inferred at ingest time.
    private var resumedPlaceholders: Set<UUID> = []

    /// Reads the notification extension's prefetched replies. See `inbox:` in
    /// `init` for why this is injected rather than called directly.
    private let inbox: () -> [String: Data]

    init(
        deviceToken: String,
        client: ChatAPI = APIClient(),
        persistURL: URL = ChatStore.makePersistURL(),
        attachmentsDirectoryURL: URL = ChatStore.makeAttachmentsDirectoryURL(),
        policy: PollingPolicy = .default,
        // Injected for the same reason `client` and `persistURL` are: the
        // effort read on the send path would otherwise reach the process-wide
        // store, which no test can touch without mutating the preference the
        // running app is using (`/code-review medium` finding).
        preferences: UserDefaults = .standard,
        // Injected for the same reason as the two above. Reading the real
        // inbox needs a provisioned App Group container, which no test host
        // has — so without this the prefetch path would silently be a no-op
        // in every test and report green for having done nothing.
        inbox: @escaping () -> [String: Data] = TurnInbox.load,
        // Injected so no test or fixture can reach Core Location; the default
        // attaches nothing, which is also what the server expects from a
        // client that does not share its location.
        currentLocation: @escaping @MainActor () -> OutgoingLocation? = { nil }
    ) {
        self.deviceToken = deviceToken
        self.client = client
        self.persistURL = persistURL
        self.attachmentsDirectoryURL = attachmentsDirectoryURL
        self.policy = policy
        self.preferences = preferences
        self.inbox = inbox
        self.currentLocation = currentLocation
        load()
        // Before the ingest below, from the transcript as loaded: the floor has
        // to exist before anything arrives that might need to lower it. A
        // week-old reply with 30 newer rows landed above the window when this
        // was computed after the ingest (`/code-review medium` finding).
        transcriptFloor = TranscriptWindow.floor(
            for: messages, before: messages.count, reference: Date()
        )
        // Before `reconcilePendingMessages`, and that order is the whole
        // point: a placeholder this resolves is one that never starts a poll.
        ingestPrefetchedTurns()
        reconcilePendingMessages()
        pruneUnreferencedAttachments()
        retryMissingAttachments()
        startHealthPolling()
    }

    deinit {
        healthTask?.cancel()
        connectionNoticeTask?.cancel()
        for (_, task) in activeTasks { task.cancel() }
        for (_, task) in activityPlaybackTasks { task.cancel() }
        for (_, task) in partialTextTasks { task.cancel() }
    }

    /// Stops all outstanding network work without touching the persisted
    /// transcript. Call before signing out — a discarded ChatView otherwise
    /// leaves its ChatStore running in the background for up to
    /// `fastAttempts * fastInterval`, still using the token the user just
    /// asked to revoke.
    ///
    /// Entries clear themselves from `activeTasks` as the cancelled tasks
    /// unwind, so the dictionary stays an accurate picture of what is still
    /// in flight (which is what `awaitPendingWork()` reads).
    func cancelActiveWork() {
        healthTask?.cancel()
        healthTask = nil
        connectionNoticeTask?.cancel()
        connectionNoticeTask = nil
        connectionActivity = .idle
        for (_, task) in activeTasks { task.cancel() }
        // Both dictionaries, not just the tasks: a playback queue that has
        // drained removes its own task and leaves the label and window cursor
        // behind, so keying off `activityPlaybackTasks` alone would clear a
        // message caught mid-drain and skip the identical one caught 300ms
        // later.
        // `resumedPlaceholders` is in the union for the same reason the two
        // state dictionaries are: a turn resumed at launch that never produced
        // streamed text has no entry in any of the other four, so keying off
        // those alone left its id in the set for the life of the store.
        for id in Set(activityPlaybackTasks.keys)
            .union(activityPlaybackStates.keys)
            .union(partialTextTasks.keys)
            .union(partialTextStates.keys)
            .union(resumedPlaceholders) {
            clearTurnProgress(for: id)
        }
    }

    /// `images` are already-downscaled JPEG bytes, in attach order, at most
    /// `ChatMessage.maxImages` (docs/tasks/ios-image-input.md — downscaling
    /// happens on the device, before this is ever called; this method just
    /// stores and sends what it's handed). Each is copied into
    /// `attachmentsDirectoryURL` as `<clientMsgId>-<index>.jpg` so `retry(_:)` and
    /// `reconcilePendingMessages()` can recover the same bytes later by the
    /// same handle they already use to recover the text.
    ///
    /// Text may be empty when an image is attached (`/code-review medium`
    /// finding: the photo/paste buttons were otherwise unreachable, since
    /// nothing could ever satisfy both "picked an image" and "typed a
    /// caption"). At least one of the two must be present.
    @discardableResult
    func send(text: String, images: [Data] = [], ideaId: String? = nil, quote: IdeaQuote? = nil) -> Bool {
        startSend(text: text, images: images, ideaId: ideaId, quote: quote) != nil
    }

    /// Voice mode's send (docs/tasks/voice-mode-hands-free.md): marked spoken
    /// so Marlow knows the reply will be read aloud, and returns the reply's
    /// placeholder id so the speaker can follow that one reply.
    func sendSpoken(_ text: String) -> UUID? {
        startSend(text: text, spoken: true)
    }

    /// Text the poll loop has received for a running reply, cumulative, with
    /// the server's tool-call offsets — the raw stream, ahead of the bubble's
    /// typing-out. Voice mode speaks from this; terminal states it reads off
    /// `messages`, which every path that settles a reply goes through.
    var onReplyProgress: ((_ placeholderId: UUID, _ text: String, _ marks: [Int]) -> Void)?

    /// The reply placeholder's id, or nil when there was nothing to send.
    private func startSend(
        text: String, images: [Data] = [], ideaId: String? = nil, quote: IdeaQuote? = nil, spoken: Bool = false
    ) -> UUID? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty || !images.isEmpty else { return nil }

        var userMessage = ChatMessage(
            id: UUID(), role: .user, text: trimmed, createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil, ideaId: ideaId, quote: quote
        )
        let clientMsgId = userMessage.id.uuidString
        // Sent and persisted-for-retry must agree, or a retry (`resendable()`
        // reads the files back by `imageFilenames`) silently drops an image the
        // first attempt carried (`/code-review medium` finding). If ANY write
        // fails, send text-only on *this* attempt too rather than have the
        // two diverge, and say why: a caption with only some of its images is
        // the "reply about a picture Marlow never saw" failure (CMP-4/CMP-6).
        var imagesToSend = images
        var attachmentWriteFailed = false
        if !images.isEmpty {
            var filenames: [String] = []
            for (index, data) in images.enumerated() {
                guard let filename = saveAttachment(data, clientMsgId: clientMsgId, index: index) else {
                    filenames = []
                    break
                }
                filenames.append(filename)
            }
            if filenames.count == images.count {
                userMessage.imageFilenames = filenames
            } else {
                // Files written before the failing one are orphans; the
                // launch-time prune removes anything no message references.
                imagesToSend = []
                attachmentWriteFailed = true
            }
        }
        guard !trimmed.isEmpty || !imagesToSend.isEmpty else { return nil }
        // Only claim "sending without it" once we know the send is actually
        // going ahead (`/code-review medium` finding: with a blank caption,
        // a failed write used to trip this notice on the way to the guard
        // above rejecting the send outright — claiming a send that never
        // happened).
        if attachmentWriteFailed {
            showPermissionNotice(
                images.count == 1
                    ? "Couldn't save the photo — sending your message without it."
                    : "Couldn't save the photos — sending your message without them."
            )
        }
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil, status: .sending,
            clientMsgId: clientMsgId
        )
        messages.append(userMessage)
        messages.append(placeholder)
        persist()

        startSubmit(
            text: trimmed, clientMsgId: clientMsgId, placeholderId: placeholder.id,
            images: imagesToSend, ideaId: ideaId, spoken: spoken
        )
        return placeholder.id
    }

    /// Test support: waits until every in-flight send/poll task has finished.
    /// The state machine runs in unstructured Tasks that `send()` and `init`
    /// kick off and never hand back, so without this a test has no way to know
    /// when the machine has settled. Not used by the app.
    ///
    /// Returns `false` if `timeout` elapses with work still in flight. It polls
    /// rather than awaiting `task.value` directly, precisely so it can hold a
    /// deadline: a task suspended in a non-cancellation-aware `await` (e.g. a
    /// `withCheckedContinuation` a test forgot to resume) never completes, and
    /// awaiting it would hang the whole `xcodebuild test` until the CI job's
    /// 30-minute timeout — burning ~300 billed minutes on a 10x-priced macOS
    /// runner and reporting a timeout instead of a diagnosable failure.
    @discardableResult
    func awaitPendingWork(timeout: Duration = .seconds(10)) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if activeTasks.isEmpty { return true }
            try? await Task.sleep(for: .milliseconds(1))
        }
        return activeTasks.isEmpty
    }

    // MARK: - Task tracking

    /// `imageFilenames`, not `images`, is how a resend (`retry()`,
    /// `reconcilePendingMessages()`) supplies its image — the read happens
    /// inside the `Task` below, not before this call, so `init()` (which
    /// calls `reconcilePendingMessages()` synchronously) never blocks app
    /// launch on `Data(contentsOf:)` for a pending image
    /// (`/code-review medium` finding). A fresh `send()` already has the
    /// bytes in memory and passes `images` directly instead.
    private func startSubmit(
        text: String, clientMsgId: String, placeholderId: UUID,
        images: [Data] = [], imageFilenames: [String] = [], ideaId: String? = nil, spoken: Bool = false
    ) {
        activeTasks[placeholderId] = Task { [weak self] in
            guard let self else { return }
            var resolvedImages = images
            if resolvedImages.isEmpty, !imageFilenames.isEmpty {
                // All or nothing: one missing file sends the text alone, never
                // the caption with some of its images (CMP-4, CMP-6).
                let loaded = imageFilenames.compactMap { self.loadAttachment(filename: $0) }
                resolvedImages = loaded.count == imageFilenames.count ? loaded : []
                if resolvedImages.isEmpty {
                    // The paired send already told the user if *its* write
                    // failed; this is the separate case of the file having
                    // gone missing by the time a later retry/relaunch reads
                    // it back — same "don't resend silently" rule applies
                    // (`/code-review medium` finding).
                    self.showPermissionNotice(
                        imageFilenames.count == 1
                            ? "Couldn't find the photo to resend — sending without it."
                            : "Couldn't find the photos to resend — sending without them."
                    )
                }
            }
            await self.submit(
                text: text, clientMsgId: clientMsgId, placeholderId: placeholderId,
                images: resolvedImages, ideaId: ideaId, spoken: spoken
            )
            self.activeTasks.removeValue(forKey: placeholderId)
        }
    }

    private func startPoll(turnId: String, placeholderId: UUID) {
        activeTasks[placeholderId] = Task { [weak self] in
            await self?.pollTurn(turnId, placeholderId: placeholderId)
            self?.activeTasks.removeValue(forKey: placeholderId)
        }
    }

    /// The POST retries on its own (below) before ever reaching this catch —
    /// it only fires once the retry budget is exhausted or the failure isn't
    /// the retryable kind, so by the time a message shows red here, it's a
    /// real stopping point, not a blip the user needed to notice.
    private func submit(
        text: String, clientMsgId: String, placeholderId: UUID, images: [Data] = [], ideaId: String? = nil,
        spoken: Bool = false
    ) async {
        var attempt = 0
        // Encoded once, before the retry loop (CMP-3).
        let imagesBase64 = images.map { $0.base64EncodedString() }
        while true {
            do {
                let response = try await sendMessageWithDeadline(
                    text: text, clientMsgId: clientMsgId,
                    imagesBase64: imagesBase64,
                    ideaId: ideaId, spoken: spoken,
                    timeout: policy.sendAttemptTimeout
                )
                if Task.isCancelled { return }
                update(placeholderId) {
                    $0.turnId = response.turnId
                    $0.status = .running
                }
                persist()
                await pollTurn(response.turnId, placeholderId: placeholderId)
                return
            } catch {
                if Task.isCancelled { return }
                attempt += 1
                guard Self.isTransientNetworkError(error), attempt < policy.sendMaxAttempts else {
                    update(placeholderId) {
                        $0.status = .error
                        $0.text = error.localizedDescription
                    }
                    persist()
                    return
                }
                // Stays `.sending` through every retry — "Sending…" is still
                // an accurate description of what's happening, and there's
                // nothing more specific worth telling the user without this
                // turning into a running commentary on the network.
                try? await Task.sleep(nanoseconds: Self.sendBackoff(attempt: attempt, policy: policy))
                // The sleep is the retry loop's other suspension point besides
                // the network call itself — cancelActiveWork() must not leave
                // this parked here only to fire one more request with a token
                // that's being revoked on the way out.
                if Task.isCancelled { return }
            }
        }
    }

    /// Races only the send request against a real wall-clock deadline.
    /// `URLRequest.timeoutInterval` is an inactivity timeout and can be reset
    /// forever by a trickling connection; a session-wide
    /// `timeoutIntervalForResource` would also shorten the deliberately long
    /// poll budget. The deadline therefore lives here, scoped to one send
    /// attempt. Its timeout is translated to `URLError.timedOut` so the
    /// existing send classifier retries it; surfacing `CancellationError`
    /// would incorrectly make the first timed-out attempt terminal.
    private func sendMessageWithDeadline(
        text: String, clientMsgId: String?,
        imagesBase64: [String], ideaId: String?, spoken: Bool, timeout: TimeInterval
    ) async throws -> MessageResponse {
        // Read per attempt rather than captured once at submit(): a retry that
        // happens after Mike changes the picker should carry what the picker is
        // currently showing him, not a level he has already moved off.
        let effort = EffortPreference.current(in: preferences)
        // Same per-attempt read, with an extra reason: a retry minutes later
        // must not carry a position that has since aged past the freshness
        // window, and nothing at all is the correct thing to send then.
        let location = currentLocation()
        return try await withThrowingTaskGroup(of: MessageResponse.self) { group in
            group.addTask { [client, deviceToken] in
                try await client.sendMessage(
                    text: text, clientMsgId: clientMsgId,
                    imagesBase64: imagesBase64, effort: effort,
                    location: location, ideaId: ideaId, spoken: spoken, timeout: timeout, deviceToken: deviceToken
                )
            }
            group.addTask {
                try await Task.sleep(for: .seconds(timeout))
                throw URLError(.timedOut)
            }
            defer { group.cancelAll() }
            guard let first = try await group.next() else {
                throw CancellationError()
            }
            return first
        }
    }

    private static func sendBackoff(attempt: Int, policy: PollingPolicy) -> UInt64 {
        min(policy.sendBaseInterval << min(attempt, 10), policy.sendMaxInterval)
    }

    /// Distinguishes a dropped connection from a rejected request. Only the
    /// former is worth retrying automatically — retrying a request the
    /// server has already told us is bad (or that we couldn't even parse the
    /// response to) just delays the same answer.
    private static func isTransientNetworkError(_ error: Error) -> Bool {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .notConnectedToInternet, .networkConnectionLost, .timedOut,
                 .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed,
                 .dataNotAllowed, .internationalRoamingOff, .callIsActive,
                 .secureConnectionFailed:
                return true
            default:
                return false
            }
        }
        if case APIError.server(let status, _) = error {
            // 0 is this client's own stand-in for "no HTTP response at all"
            // (`APIClient.send`) — a connection-level failure wearing an
            // APIError costume, not a real status code. 5xx is the server's
            // own transient trouble, not a rejection of this request.
            return status == 0 || status >= 500
        }
        return false
    }

    /// Resends a failed turn — manually, for whenever the automatic retry
    /// above already gave up, or the failure was a poll timeout rather than
    /// the send itself. Same mechanics as `reconcilePendingMessages`'
    /// `.sending` case (resend under the same `clientMsgId`, which the
    /// server dedupes on), just triggered by a tap instead of a relaunch —
    /// for when connectivity comes back before the user thinks to reopen the
    /// app. Shares `resendable(_:)` with that case rather than repeating the
    /// lookup.
    ///
    /// When the TURN failed rather than the send, the dedupe used to hand back
    /// that same ERROR turn, so this replayed the failure without running
    /// anything. The server now enqueues fresh over an errored turn
    /// (`WorkQueue.enqueue`, 2026-09-28); nothing here had to change for it.
    func retry(_ placeholderId: UUID) {
        guard
            let message = messages.first(where: { $0.id == placeholderId }),
            message.status == .error,
            let (clientMsgId, text, imageFilenames, ideaId) = resendable(message)
        else { return }
        update(placeholderId) {
            $0.status = .sending
            $0.text = ""
            // An error row from the server (a failed turn, or the restart's
            // "interrupted" row) confirmed this placeholder and gave it a
            // `seq`. A retry makes it local-only again: `TranscriptMerge` only
            // matches a reply to a placeholder with no `seq`, so one that kept
            // it would never be matched by the retry's reply row, which would
            // be inserted as a SECOND bubble while this one's own poll also
            // completed — two replies and two sounds (review, 2026-10-01).
            $0.seq = nil
            $0.answers = nil
        }
        persist()
        startSubmit(
            text: text, clientMsgId: clientMsgId, placeholderId: placeholderId,
            imageFilenames: imageFilenames, ideaId: ideaId
        )
    }

    /// The original text, `client_msg_id`, and image (if any) a
    /// `.sending`-shaped message should resend under — shared by `retry()`
    /// (a tap) and `reconcilePendingMessages()`'s `.sending` case (a
    /// relaunch), the two places that resend an unsent turn. `nil` means the
    /// paired user message is gone (interrupted before `clientMsgId` was even
    /// recorded); each caller decides what that means for itself —
    /// `reconcilePendingMessages` has a specific "interrupted" message to
    /// show, `retry` simply has nothing to retry.
    ///
    /// Returns the filename, not the bytes — `retry()` fires an unbounded
    /// time after the original `send()`, possibly across an app relaunch, so
    /// the file on disk (keyed by `clientMsgId`, per `Self.saveAttachment`)
    /// is the only copy that can still exist by then, and `startSubmit`
    /// reads it back lazily inside its own `Task` rather than here — this
    /// runs synchronously from `reconcilePendingMessages()`, itself called
    /// from `init()`, and a disk read here would block app launch
    /// (`/code-review medium` finding).
    private func resendable(
        _ message: ChatMessage
    ) -> (clientMsgId: String, text: String, imageFilenames: [String], ideaId: String?)? {
        guard
            let clientMsgId = message.clientMsgId,
            let userMessage = messages.first(where: { $0.id.uuidString == clientMsgId })
        else { return nil }
        return (clientMsgId, userMessage.text, userMessage.allImageFilenames, userMessage.ideaId)
    }

    /// Fast phase (`fastAttempts` at `fastInterval`, ~5min) covers normal
    /// turns. If that window closes without a terminal status, the message
    /// gets a visible "still checking" error rather than a silent spinner,
    /// but polling continues in a slow background phase (`slowAttempts`
    /// at `slowInterval`, ~another hour) — a tool-heavy turn that
    /// finishes late still upgrades the message to its real result instead
    /// of leaving a permanently wrong error on screen.
    ///
    /// The interim state is `.stalled`, not `.error`, and that distinction is
    /// load-bearing: `.error` is terminal to `reconcilePendingMessages`, so
    /// persisting the interim state as an error would mean a force-quit during
    /// the slow phase strands the message on disk showing "still checking in
    /// the background" while nothing checks — with the turn_id sitting right
    /// there, recoverable. `.stalled` is resumed at launch like `.running`.
    /// Only once the slow phase is also exhausted does this become a real,
    /// terminal error.
    private func pollTurn(_ turnId: String, placeholderId: UUID) async {
        // Permissions are deliberately NOT polled during the burst. A request
        // has the server's 55s `t_hook_seconds` to be answered, so arriving on
        // the fast phase's first tick a few seconds later is invisible to the
        // user — and pairing every burst poll with a second round trip is the
        // one part of this that would double request count for nothing.
        if await pollLoop(
            turnId, placeholderId: placeholderId, attempts: policy.burstAttempts,
            interval: policy.burstInterval, checkPermissionsEnabled: false,
            deadline: .now + policy.burstDuration
        ) {
            return
        }
        if Task.isCancelled { return }
        if await pollLoop(
            turnId, placeholderId: placeholderId, attempts: policy.fastAttempts, interval: policy.fastInterval,
            checkPermissionsEnabled: true
        ) {
            return
        }
        if Task.isCancelled { return }
        clearTurnProgress(for: placeholderId)
        update(placeholderId) {
            $0.status = .stalled
            $0.text = "Marlow hasn't answered yet — still checking in the background."
        }
        persist()
        if await pollLoop(
            turnId, placeholderId: placeholderId, attempts: policy.slowAttempts, interval: policy.slowInterval,
            checkPermissionsEnabled: false
        ) {
            return
        }
        if Task.isCancelled { return }
        update(placeholderId) {
            $0.status = .error
            $0.text = "Marlow never answered this one. Try sending it again."
        }
        clearTurnProgress(for: placeholderId)
        persist()
    }

    /// Runs up to `attempts` poll iterations. Returns `true` if the turn
    /// reached a terminal state (or the check was cancelled) — the caller
    /// should stop. Returns `false` if the budget was exhausted with no
    /// terminal status.
    ///
    /// `checkPermissionsEnabled` is false for the slow phase: a permission
    /// request must be answered within the server's `t_hook_seconds` (55s) or
    /// it's auto-denied, and the slow phase only starts after the 5-minute
    /// fast phase is exhausted — so by then any request tied to this turn has
    /// been dead for minutes, and every slow-phase `GET /v1/permissions` (up
    /// to 120 per stalled turn) would be a guaranteed-empty round trip.
    ///
    /// `deadline` ends the phase on the wall clock, whatever the link speed —
    /// see `PollingPolicy.burstDuration`. Nil means "run the attempts out",
    /// which is what both of the original phases do.
    private func pollLoop(
        _ turnId: String, placeholderId: UUID, attempts: Int, interval: UInt64,
        checkPermissionsEnabled: Bool, deadline: ContinuousClock.Instant? = nil
    ) async -> Bool {
        for _ in 0..<attempts {
            if Task.isCancelled { return true }
            try? await Task.sleep(nanoseconds: interval)
            if Task.isCancelled { return true }
            // Checked after the sleep, not before: a phase whose deadline has
            // passed hands over to the next one rather than firing one more
            // request on the way out.
            if let deadline, ContinuousClock.now >= deadline { return false }

            if checkPermissionsEnabled {
                await checkPermissions()
                if Task.isCancelled { return true }
            }

            do {
                let turn = try await client.getTurn(turnId, deviceToken: deviceToken)
                // The call above is a suspension point of its own: a cancel
                // landing while it was in flight must not fall through and
                // mutate a transcript the UI has already walked away from.
                if Task.isCancelled { return true }
                // A merged turn has no result of its own: the server folded
                // its text into another turn, whose reply answers both. Drop
                // the placeholder rather than show an empty bubble.
                if turn.status == "merged" {
                    clearTurnProgress(for: placeholderId)
                    messages.removeAll { $0.id == placeholderId }
                    persist()
                    return true
                }
                switch turn.status {
                case "done":
                    clearTurnProgress(for: placeholderId)
                    update(placeholderId) {
                        $0.status = .done
                        $0.text = turn.resultText ?? ""
                    }
                    completedAssistantTurns.append(turnId)
                    persist()
                    return true
                case "error":
                    clearTurnProgress(for: placeholderId)
                    update(placeholderId) {
                        $0.status = .error
                        $0.text = turn.error ?? "Something went wrong."
                    }
                    persist()
                    return true
                default:
                    // Only `.running` renders `activityLabel` — `.stalled`
                    // shows its own "still checking in the background" text
                    // instead. Without this guard the hour-long slow phase
                    // keeps restarting playback, republishing the whole
                    // transcript for labels nothing can display.
                    if statusOf(placeholderId) == .running {
                        enqueueActivities(turn.activities, for: placeholderId)
                        enqueuePartialText(
                            turn.partialText, marks: turn.partialTextMarks, for: placeholderId
                        )
                        onReplyProgress?(placeholderId, turn.partialText, turn.partialTextMarks)
                    }
                    continue // pending/running
                }
            } catch APIError.server(let status, _) where status == 404 {
                // Permanent: the server's in-memory turn is gone (e.g. a
                // restart), not a network blip — no point burning the rest
                // of the poll budget on it.
                if Task.isCancelled { return true }
                clearTurnProgress(for: placeholderId)
                update(placeholderId) {
                    $0.status = .error
                    $0.text = "Marlow lost track of this message (the server may have restarted). Try sending it again."
                }
                persist()
                return true
            } catch {
                // Already-received events are a local playback queue, not a
                // claim about what the server is doing at this instant. Keep
                // pacing them through a transient fetch failure; the next
                // cumulative response fills any gap without replaying them.
                continue // transient network hiccup - keep polling until the attempt cap
            }
        }
        return false
    }

    private func statusOf(_ id: UUID) -> ChatMessage.Status? {
        messages.first(where: { $0.id == id })?.status
    }

    private func update(_ id: UUID, _ mutate: (inout ChatMessage) -> Void) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        mutate(&messages[index])
    }

    private func enqueueActivities(_ activities: [TurnActivity], for id: UUID) {
        var state = activityPlaybackStates[id] ?? ActivityPlaybackState()
        state.ingest(activities)
        activityPlaybackStates[id] = state
        startActivityPlaybackIfNeeded(for: id)
    }

    private func startActivityPlaybackIfNeeded(for id: UUID) {
        guard activityPlaybackTasks[id] == nil,
              activityPlaybackStates[id]?.pendingLabels.isEmpty == false else { return }

        let token = UUID()
        activityPlaybackTokens[id] = token
        activityPlaybackTasks[id] = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                guard var state = self.activityPlaybackStates[id],
                      let label = state.nextLabel() else { break }
                self.activityPlaybackStates[id] = state
                self.update(id) { $0.activityLabel = label }
                do {
                    try await Task.sleep(nanoseconds: self.policy.activityDwell)
                } catch {
                    break
                }
            }

            // A cancelled, superseded task must not clear a newer player's
            // bookkeeping after a reconnect starts it.
            guard self.activityPlaybackTokens[id] == token else { return }
            self.activityPlaybackTasks[id] = nil
            self.activityPlaybackTokens[id] = nil
            self.startActivityPlaybackIfNeeded(for: id)
        }
    }

    private func enqueuePartialText(_ text: String, marks: [Int], for id: UUID) {
        guard !text.isEmpty else { return }
        var state = partialTextStates[id]
            ?? PartialTextPlaybackState(joinedLate: resumedPlaceholders.contains(id))
        state.ingest(text, marks: marks, ticksPerPoll: ticksPerPoll)
        partialTextStates[id] = state
        startPartialTextPlaybackIfNeeded(for: id)
    }

    /// Playback ticks per poll interval — the pacing budget one slab of text
    /// gets before the next one is expected.
    private var ticksPerPoll: Int {
        max(1, Int(policy.fastInterval / max(1, policy.partialTextTick)))
    }

    private func startPartialTextPlaybackIfNeeded(for id: UUID) {
        guard partialTextTasks[id] == nil,
              (partialTextStates[id]?.backlog ?? 0) > 0 else { return }

        let token = UUID()
        partialTextTokens[id] = token
        partialTextTasks[id] = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                guard var state = self.partialTextStates[id],
                      let shown = state.advance(
                          jumpThreshold: self.policy.partialTextJumpThreshold
                      ) else { break }
                let marks = state.revealedMarks
                self.partialTextStates[id] = state
                self.update(id) {
                    $0.streamedText = shown
                    $0.streamedTextMarks = marks
                }
                // Announced here, at the assignment, rather than from
                // `enqueuePartialText` where the slab arrives: the server can
                // hand over text a beat before the pacer reveals any of it, and
                // a buzz saying "Marlow is talking" above a still-blank row is
                // the same unexplained silence this is meant to end, moved
                // earlier. The first tick that actually puts characters on
                // screen is the honest moment.
                if !shown.isEmpty, self.announcedReplyStarts.insert(id).inserted {
                    self.startedAssistantReplies.append(id)
                }
                do {
                    try await Task.sleep(nanoseconds: self.policy.partialTextTick)
                } catch {
                    break
                }
            }

            // Same superseded-task guard as the activity player above: a
            // cancelled run must not clear a newer one's bookkeeping.
            guard self.partialTextTokens[id] == token else { return }
            self.partialTextTasks[id] = nil
            self.partialTextTokens[id] = nil
            self.startPartialTextPlaybackIfNeeded(for: id)
        }
    }

    /// Every caller wants the whole thing gone — the label, the queue behind
    /// it, the window cursor, and the streamed reply — so there is deliberately
    /// no "keep the cursor" variant. A reconnect re-ingests the server's
    /// cumulative array from scratch and the skip-ahead cap keeps that cheap.
    ///
    /// The streamed text goes with them, and that is the correctness rule, not
    /// tidiness: `result_text` is authoritative, so a turn must never be left
    /// rendering the approximation it accumulated. Both terminal branches in
    /// the poll loop call this before assigning `text`. Note "authoritative"
    /// is the SERVER's field, not the CLI envelope's raw `result` — on an
    /// interleaved turn the server reassembles the reply's earlier blocks into
    /// it (invoke.py `_whole_reply`), which is why this client can keep
    /// discarding what it accumulated and still show the whole answer.
    /// Mirrors `_clear_progress` on the server, for the same reason.
    private func clearTurnProgress(for id: UUID) {
        activityPlaybackTasks.removeValue(forKey: id)?.cancel()
        activityPlaybackTokens[id] = nil
        activityPlaybackStates[id] = nil
        partialTextTasks.removeValue(forKey: id)?.cancel()
        partialTextTokens[id] = nil
        partialTextStates[id] = nil
        resumedPlaceholders.remove(id)
        update(id) {
            $0.activityLabel = nil
            $0.streamedText = nil
            $0.streamedTextMarks = nil
        }
    }

    // MARK: - Reconciliation

    /// A `.sending`/`.running`/`.stalled` message on disk means the app quit
    /// (or the prior session's Task was cancelled) before that message reached
    /// a terminal state. Resume it rather than leaving a permanent spinner:
    /// `.running` and `.stalled` already have a turn_id, so resume polling
    /// directly; `.sending` never got that far, so resend — the server dedupes
    /// on `client_msg_id` (`WorkQueue.enqueue`, §3.1 idempotency), so this is
    /// safe even if the original POST actually did land.
    private func reconcilePendingMessages() {
        for message in messages {
            switch message.status {
            case .running, .stalled:
                guard let turnId = message.turnId else {
                    update(message.id) {
                        $0.status = .error
                        $0.text = "Interrupted before Marlow replied — please resend."
                        $0.activityLabel = nil
                    }
                    continue
                }
                // The label on disk is whatever this message was doing when
                // some *other* code path last called persist() mid-turn (e.g.
                // `syncTranscript`). It describes a previous launch, and if the
                // resumed turn's next poll returns an empty `activities` --
                // server restarted, or the turn is inside its first ~2.7s of
                // silence -- nothing would overwrite it. A stale label
                // outliving its turn is the one failure worse than no label
                // (docs/tasks/ios-turn-activity.md).
                // Same reasoning for the streamed reply, and it has a second
                // failure on top of staleness: playback state is rebuilt empty
                // on launch, so leaving the text on screen means the first tick
                // REWINDS the bubble from a sentence back to a few characters
                // and retypes it. Both go.
                update(message.id) {
                    $0.activityLabel = nil
                    $0.streamedText = nil
                    $0.streamedTextMarks = nil
                }
                // Clearing it is only half the fix. The next poll returns the
                // server's CUMULATIVE text, so without this the bubble types
                // the whole of an old reply back out from nothing — a
                // typewriter replay on a turn that finished long ago (Mike,
                // 2026-09-05).
                resumedPlaceholders.insert(message.id)
                startPoll(turnId: turnId, placeholderId: message.id)
            case .sending:
                guard let (clientMsgId, text, imageFilenames, ideaId) = resendable(message) else {
                    update(message.id) {
                        $0.status = .error
                        $0.text = "Interrupted before sending — please resend."
                        $0.activityLabel = nil
                    }
                    continue
                }
                // Same reason as `retry`: a resent placeholder is local-only
                // again, whatever a row once said about it.
                update(message.id) {
                    $0.seq = nil
                    $0.answers = nil
                }
                startSubmit(
                    text: text, clientMsgId: clientMsgId, placeholderId: message.id,
                    imageFilenames: imageFilenames, ideaId: ideaId
                )
            case .done, .error:
                continue
            }
        }
        persist()
    }

    // MARK: - Tool-permission round trip

    /// Bumped by every call that resolves `pendingPermission` outside of
    /// `checkPermissions` itself (an answer, an explicit expiry). Lets
    /// `checkPermissions` recognize a GET that was already in flight when one
    /// of those happened, and drop its now-stale result instead of
    /// overwriting a decision the user already acted on.
    private var permissionGeneration = 0

    /// Called alongside `getTurn` in the poll loop. A failed fetch is treated
    /// like the existing transient-network case in `pollLoop` — keep polling,
    /// don't surface an error. Publishing `nil` when the poll comes back empty
    /// is also what dismisses the sheet once the server has resolved the
    /// request on its own (timeout, or answered elsewhere). Never publishes a
    /// request whose own `expiresAt` has already passed — otherwise a sheet
    /// that renders on an already-expired request never has a `false -> true`
    /// transition to notice, and sits offering live-looking buttons.
    private func checkPermissions() async {
        let generation = permissionGeneration
        guard let response = try? await client.permissions(deviceToken: deviceToken) else { return }
        if Task.isCancelled { return }
        // The user answered (or the sheet was explicitly expired) while this
        // GET was in flight — that snapshot is stale and must not resurrect
        // a request already resolved client-side.
        guard generation == permissionGeneration else { return }
        pendingPermission = response.permissionRequests.first(where: { !$0.isExpired() })
    }

    /// Answers the currently-shown request and clears the sheet immediately
    /// rather than waiting for the next poll to notice it's gone. A network
    /// failure leaves `pendingPermission` untouched — the next poll tick
    /// either still shows it (safe to retry) or has already cleared it.
    func answerPermission(decision: String) async {
        guard let request = pendingPermission else { return }
        guard
            let response = try? await client.answerPermission(
                requestId: request.requestId, decision: decision, deviceToken: deviceToken
            )
        else { return }
        if pendingPermission?.requestId == request.requestId {
            pendingPermission = nil
            permissionGeneration += 1
        }
        if !response.accepted {
            showPermissionNotice("Marlow already moved on — that request was no longer open.")
        }
    }

    /// Called by the sheet's own countdown when it reaches zero, as a
    /// client-side backstop to the poll noticing the request is gone — the
    /// `requestId` guard avoids clearing a *newer* request that appeared in
    /// the same tick.
    func expirePermissionSheet(requestId: String) {
        if pendingPermission?.requestId == requestId {
            pendingPermission = nil
            permissionGeneration += 1
        }
    }

    /// Identifies one call's own clear-after-3s Task rather than comparing
    /// the notice text: two unrelated "too late" notices in quick succession
    /// carry the identical fixed string, and a text comparison would let the
    /// first notice's timer clear the second, fresher one early.
    private var permissionNoticeToken: UUID?

    func showPermissionNotice(_ text: String) {
        let token = UUID()
        permissionNoticeToken = token
        permissionNotice = text
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard let self, self.permissionNoticeToken == token else { return }
            self.permissionNotice = nil
        }
    }

    // MARK: - Banners

    /// Also the restart point for `handleDidBecomeActive()` below — reusing
    /// this rather than running a second, parallel fetch loop means there is
    /// only ever one health/sync call path, and only one `healthTask` to
    /// reason about.
    private func startHealthPolling() {
        healthTask = Task { [weak self] in
            // Only the FIRST pass of a run announces itself. Every run starts
            // at a moment the user is present for — launch, or
            // `handleDidBecomeActive()` after a notification tap — so that
            // pass is the one someone is waiting on. The recurring passes
            // behind it are housekeeping, and an indicator blinking every 15s
            // forever would train him to ignore the one that matters.
            var announce = true
            // Per run, so the first pass of a run always reads usage (the one
            // someone is waiting on, same as `announce`) and later passes are
            // throttled by `policy.usageInterval`.
            var lastUsageRead: Date?
            while let self, !Task.isCancelled {
                if announce { self.beginConnectionNotice() }
                announce = false
                // Shares the health tick rather than running its own timer: a
                // cron turn is announced by a push, and this is the loop that
                // is already alive whenever the chat is on screen. Worst-case
                // latency for a briefing that lands while the app is open is
                // one interval — less if `handleDidBecomeActive()` restarts
                // the loop first.
                //
                // BEFORE `refreshHealth()`: after a push wake the transcript is
                // what the user is waiting for, and `/health` is a banner. The
                // order matters because each await can take a while on a poor
                // link, and the one behind it waits.
                let transcriptReached = await self.syncTranscript()
                let healthReached = await self.refreshHealth()
                // A cancelled loop is a loop being REPLACED (only
                // `handleDidBecomeActive` and `cancelActiveWork` cancel it),
                // so its two failed calls say nothing about the network and
                // must not paint an `unreachable` state the successor would
                // have to clear. Leave the state for whoever comes next.
                if Task.isCancelled { return }
                self.settleConnectionActivity(reachable: healthReached && transcriptReached)
                // After the settle: usage is not a health signal, and the
                // reachability state must not wait on one more round trip.
                // Stamped only on success: a failed read (a flaky link at
                // launch) retries on the next 15s tick rather than leaving the
                // row dark for the whole interval (review, 2026-10-04).
                if lastUsageRead.map({ Date().timeIntervalSince($0) >= self.policy.usageInterval }) ?? true,
                   await self.refreshUsage() {
                    lastUsageRead = Date()
                }
                try? await Task.sleep(nanoseconds: self.policy.healthInterval)
            }
        }
    }

    /// Arms the `.checking` indicator, to appear only if the pass now
    /// starting is still running `connectionNoticeDelay` from now.
    ///
    /// Does not overwrite `.unreachable`: a pass that follows a failure
    /// should leave the stronger, more informative message up rather than
    /// downgrading it to "checking" and then restoring it a minute later.
    private func beginConnectionNotice() {
        connectionNoticeTask?.cancel()
        connectionNoticeTask = Task { [weak self] in
            guard let delay = self?.policy.connectionNoticeDelay else { return }
            try? await Task.sleep(nanoseconds: delay)
            guard !Task.isCancelled, let self, self.connectionActivity == .idle else { return }
            self.connectionActivity = .checking
        }
    }

    private func settleConnectionActivity(reachable: Bool) {
        connectionNoticeTask?.cancel()
        connectionNoticeTask = nil
        connectionActivity = reachable ? .idle : .unreachable
    }

    /// Whether an error means the phone never got to the server, as opposed
    /// to the server answering with something we didn't like.
    ///
    /// The distinction is the whole honesty of the indicator: "Can't reach
    /// Marlow" is a claim about the network, and a 500 out of `/health` or a
    /// body that won't decode is the opposite — proof the tailnet is fine and
    /// the box is not. Saying "can't reach" there sends Mike to look at his
    /// connection for a fault that is on the box. Only `URLError`, which is
    /// what URLSession raises when the request never completed, counts.
    private nonisolated func isTransportFailure(_ error: Error) -> Bool {
        error is URLError
    }

    /// Returns whether the call got through — the reachability signal behind
    /// the indicator. A cancelled poll reports `true`: it is not evidence
    /// about the network, and calling it a failure would be a lie the
    /// indicator tells on every sign-out.
    @discardableResult
    private func refreshHealth() async -> Bool {
        let health: HealthResponse
        do {
            health = try await client.health(deviceToken: deviceToken)
        } catch {
            return Task.isCancelled || !isTransportFailure(error)
        }
        if Task.isCancelled { return true }
        // `authOk`, not `status == "reauth_needed"`. The server reports
        // *missing or unparseable* credentials as status "error", not
        // "reauth_needed", so keying on the string left both banners dark in
        // exactly that case — while the server's auth watcher was pushing
        // "Marlow's Claude session has expired" to the lock screen. A
        // notification that leads to a screen showing nothing is worse than
        // no notification. `authOk` is false for both causes, and the ask is
        // identical for both: SSH in and run `claude`.
        reauthNeeded = !health.authOk
        // Suppressed once reauth is actually needed — the red banner says
        // the same thing more urgently, and stacking both is noise.
        reauthPrewarn = health.reauthPrewarn && !reauthNeeded
        queuePaused = health.queuePaused
        // Only overwrite on a server that actually reports it, so one poll
        // against an older server doesn't blank a name already on screen.
        if let model = health.model { serverModel = model }
        // Same only-overwrite-when-reported rule as `model` above.
        if let branch = health.gitBranch, let commit = health.gitCommit {
            serverBuild = "\(branch)@\(commit)"
        }
        return true
    }

    /// Reads the plan windows for the >= 90% status row. Any failure keeps the
    /// previous reading and says nothing: a stale row is better than a
    /// flicker, and `/v1/usage` failing is not evidence about the network, so
    /// this never touches `connectionActivity`.
    /// Returns whether a reading landed, so the caller only starts the
    /// throttle on success.
    private func refreshUsage() async -> Bool {
        guard let report = try? await client.usage(deviceToken: deviceToken), !Task.isCancelled else { return false }
        usageReport = report
        return true
    }

    /// Called from `ChatView` on a `scenePhase` transition to `.active`.
    ///
    /// Without this, a cron turn that finishes while the app is backgrounded
    /// only shows up once the health-tick loop's next iteration lands — that
    /// loop shares one `Task.sleep`, so a warm resume can land mid-sleep and
    /// wait up to a full `healthInterval` (longer still if iOS suspended the
    /// process mid-sleep). Tapping the "Marlow has something for you" push
    /// and landing on a stale transcript is exactly that window
    /// (docs/tasks/ios-notification-open-and-content-swap.md half A).
    ///
    /// Cancels and restarts `healthTask` rather than running a second task
    /// alongside it: an earlier version tracked a one-off fetch under a fixed
    /// key in `activeTasks`, but overwriting a dictionary entry doesn't
    /// cancel the `Task` it replaces, so a rapid foreground/background flap
    /// orphaned the first fetch — still running, no longer reachable from
    /// `cancelActiveWork()`, so it could keep hitting the server with a token
    /// the user had just revoked. `healthTask?.cancel()` has no such gap: the
    /// old loop iteration observes cancellation and exits (or its in-flight
    /// request is cancelled with it) before the new one starts.
    func handleDidBecomeActive() {
        // A warm resume takes this path instead of `init`, and it is the
        // common one: the app was suspended, not killed, when the push landed.
        ingestPrefetchedTurns()
        healthTask?.cancel()
        startHealthPolling()
    }

    // MARK: - Prefetched turns

    /// Render replies the notification extension already fetched, instead of
    /// asking the server for them a second time.
    ///
    /// Tapping a `turn_ready` banner resumes or relaunches the app onto a
    /// `.running` placeholder, which restarts polling — and `pollLoop` sleeps
    /// its interval *before* the first request, so the bubble stays empty for
    /// 500ms plus a round trip while the phone re-downloads text the banner
    /// had already shown it. Mike reported that wait; this removes it.
    ///
    /// The inbox is a cache and never authoritative: a missing or unreadable
    /// entry costs a poll, which is what every turn cost before this existed.
    ///
    /// Only a chat turn (`TurnSource.isChat`: `chat`, or the pre-rename `ios`) is used, to resolve its placeholder directly. A turn
    /// from any other source (a cron briefing) has no placeholder, and the
    /// transcript sync is what brings it — in server order, once, however many
    /// devices are paired — so its inbox entry is simply dropped.
    private func ingestPrefetchedTurns() {
        let entries = inbox()
        guard !entries.isEmpty else { return }

        for (turnId, data) in entries {
            guard let turn = try? JSONDecoder().decode(Turn.self, from: data),
                  turn.turnId == turnId
            else {
                // Unreadable, or claiming to be a turn it is not filed under.
                // Drop it rather than keep retrying a decode that will fail
                // identically every launch.
                TurnInbox.remove(turnId: turnId)
                continue
            }
            if !TurnSource.isChat(turn.source) {
                TurnInbox.remove(turnId: turnId)
            } else if resolvePlaceholder(with: turn) {
                TurnInbox.remove(turnId: turnId)
            }
            // Otherwise: a chat turn with no placeholder waiting on it —
            // the transcript was cleared, or this is the window before
            // `startSubmit` records the id it got back. Left in place; the
            // send path owns that message, and `prune()` bounds the
            // directory either way.
        }
    }

    /// Apply a terminal prefetched turn to a placeholder still waiting on it.
    /// Returns whether one was found.
    ///
    /// Mirrors `pollLoop`'s terminal handling rather than sharing it: that
    /// lives inside a loop that also owns activity playback and the
    /// merged-turn case, neither of which applies to a turn already over. What
    /// must NOT diverge is everything a terminal poll result does besides
    /// render — retiring the task — and `aPrefetchedTurnDoesEverythingATerminalPollWouldDo` pins that.
    @discardableResult
    private func resolvePlaceholder(with turn: Turn) -> Bool {
        guard turn.status == "done" || turn.status == "error" else { return false }
        guard let message = messages.first(where: {
            $0.turnId == turn.turnId && ($0.status == .running || $0.status == .stalled)
        }) else { return false }

        // This turn is over, so the poll asking about it has nothing left to
        // learn — and left running it reaches its own terminal branch and
        // appends `turn.turnId` to `completedAssistantTurns` a second time.
        // ChatView consumes one `TurnReadySoundTracker` marker per id, so the
        // duplicate finds none and plays a receive sound for a turn APNs
        // already sounded. Cancelling is not an optimisation.
        activeTasks.removeValue(forKey: message.id)?.cancel()

        clearTurnProgress(for: message.id)
        if turn.status == "done" {
            update(message.id) {
                $0.status = .done
                $0.text = turn.resultText ?? ""
            }
            completedAssistantTurns.append(turn.turnId)
        } else {
            update(message.id) {
                $0.status = .error
                $0.text = turn.error ?? "Something went wrong."
            }
        }
        persist()
        return true
    }

    // MARK: - Transcript sync

    /// Page size for every transcript fetch. The server clamps at 500; 200 is
    /// a screenful of history that still decodes in a blink.
    private static let transcriptPageSize = 200

    /// Bring the cache up to date with the server's transcript
    /// (docs/tasks/ios-server-transcript.md) — the morning briefing, the weekly
    /// digests, and anything another paired device sent.
    ///
    /// Before this, nothing on the phone could receive a turn it had not sent:
    /// every render path resolved a reply against a *local placeholder*
    /// created by `send()`, so a cron turn had nowhere to land. Found on the
    /// first live `morning` run: the push arrived, and opening the app showed
    /// an empty conversation. `/v1/pending` fixed that by handing each turn out
    /// once, globally, which broke the moment a second device existed; the
    /// transcript is a log with a cursor each client holds for itself.
    ///
    /// Deliberately its own call rather than a branch inside `refreshHealth()`:
    /// a `/health` failure must not also skip the sync, and vice versa. They
    /// share the tick, not the failure.
    ///
    /// Unlike the `/v1/pending` drain it replaced, this checks
    /// `Task.isCancelled` after each fetch like every other awaiting path in
    /// this class. That drain had to write down whatever it was handed because
    /// the server flipped `delivered` on the way out and the turn was gone for
    /// good if dropped; a transcript read consumes nothing, so the next sync
    /// asks past the same cursor and loses nothing.
    ///
    /// Returns whether the call got through, on the same terms as
    /// `refreshHealth()` — and this is the half that usually decides the
    /// indicator, because it is the slow one: it passes no timeout, so it runs
    /// on URLSession's 60s default.
    @discardableResult
    private func syncTranscript() async -> Bool {
        // Not a skip: `handleDidBecomeActive()` cancels the loop and starts a
        // successor in the same main-actor turn, so the old sync may still be
        // unwinding when the new one asks. Returning early there would drop
        // the very sync a notification tap is waiting for.
        while syncInFlight {
            if Task.isCancelled { return true }
            try? await Task.sleep(for: .milliseconds(2))
        }
        syncInFlight = true
        defer { syncInFlight = false }

        do {
            if latestSeq == nil {
                // Bootstrap: the newest page, which arrives newest-first.
                let page = try await client.transcript(
                    after: nil, before: nil, limit: Self.transcriptPageSize, deviceToken: deviceToken
                )
                if Task.isCancelled { return true }
                // The newest row we hold, not the page's `latest_seq`: that is
                // read a moment after the rows, and a row written in between
                // would be skipped by a cursor set past it. An empty page has
                // no rows to be skipped, so its `latest_seq` is the cursor.
                latestSeq = page.rows.map(\.seq).max() ?? page.latestSeq
                hasOlderOnServer = page.hasMore
                applyPage(page.rows.reversed(), kind: .bootstrap)
                persist()
                return true
            }

            while let cursor = latestSeq {
                let page = try await client.transcript(
                    after: cursor, before: nil, limit: Self.transcriptPageSize, deviceToken: deviceToken
                )
                if Task.isCancelled { return true }
                applyPage(page.rows, kind: .catchUp)
                // The last row's own `seq`: a page that stopped at `limit` ends
                // before `latest_seq`. No progress means stop, whatever
                // `has_more` claims, so a misbehaving server cannot spin this.
                guard let last = page.rows.map(\.seq).max(), last > cursor else { break }
                latestSeq = last
                persist()
                guard page.hasMore else { break }
            }
            return true
        } catch {
            return Task.isCancelled || !isTransportFailure(error)
        }
    }

    /// Prepends the page of rows before the oldest this phone holds. Runs when
    /// "Show earlier" is tapped with the window already at the first message.
    private func fetchEarlierPage() {
        guard hasOlderOnServer, activeTasks[earlierFetchKey] == nil,
              let oldest = messages.first?.seq else { return }
        activeTasks[earlierFetchKey] = Task { [weak self] in
            guard let self else { return }
            defer { self.activeTasks.removeValue(forKey: self.earlierFetchKey) }
            guard let page = try? await self.client.transcript(
                after: nil, before: oldest, limit: Self.transcriptPageSize,
                deviceToken: self.deviceToken
            ), !Task.isCancelled else { return }
            let previousFirst = self.messages.first?.id
            self.applyPage(page.rows.reversed(), kind: .earlier)
            self.hasOlderOnServer = page.hasMore
            // Reveal one window's worth of what just arrived, the same step a
            // local "Show earlier" takes, rather than dropping the whole page
            // into the transcript at once — the launch-time pause the window
            // exists to avoid, moved up the screen.
            if let previousFirst,
               let start = self.messages.firstIndex(where: { $0.id == previousFirst }), start > 0 {
                self.transcriptFloor = TranscriptWindow.floor(
                    for: self.messages, before: start, reference: self.messages[start].createdAt
                )
            }
            self.persist()
        }
    }

    private enum PageKind {
        /// First fetch of an empty cache: the whole page, silently.
        case bootstrap
        /// Everything past the cursor: new arrivals, announced.
        case catchUp
        /// Scrollback: older rows, silently.
        case earlier
    }

    /// One page through `TranscriptMerge`, then its effects. `announce` is false
    /// for the bootstrap, so a fresh install does not play a receive sound per
    /// row of history, and for scrollback, which is not an arrival.
    private func applyPage(_ rows: [TranscriptRow], kind: PageKind) {
        guard !rows.isEmpty else { return }
        let before = Set(messages.map(\.id))
        let (merged, effects) = TranscriptMerge.apply(rows, to: messages, announce: kind == .catchUp)
        messages = merged

        switch kind {
        case .bootstrap:
            // The window was computed at launch, from a cache that was empty;
            // without this a fresh install renders every row it just fetched.
            transcriptFloor = TranscriptWindow.floor(
                for: messages, before: messages.count, reference: Date()
            )
        case .catchUp:
            // An arrival someone was notified about must not land above a
            // "show earlier" button: a row older than the window lowers it.
            if let floor = transcriptFloor,
               let oldest = messages.filter({ !before.contains($0.id) }).map(\.createdAt).min(),
               oldest < floor {
                transcriptFloor = oldest
            }
        case .earlier:
            break
        }

        perform(effects)
        persist()
    }

    private func perform(_ effects: TranscriptMerge.Effects) {
        // First, and before the completions below are published: a placeholder
        // a row resolved must stop polling now, or its poll reaches its own
        // terminal branch and reports the same turn done a second time.
        for id in effects.cancelPolls {
            activeTasks.removeValue(forKey: id)?.cancel()
            clearTurnProgress(for: id)
        }
        for start in effects.startPolls {
            // A placeholder for a turn another device started joins it
            // mid-stream, so it must catch up rather than type the whole reply
            // out from the start (Mike, 2026-09-05 — see `resumedPlaceholders`).
            resumedPlaceholders.insert(start.messageId)
            startPoll(turnId: start.turnId, placeholderId: start.messageId)
        }
        completedAssistantTurns.append(contentsOf: effects.completedTurnIds)
        for fetch in effects.fetchAttachments {
            fetchAttachments(messageId: fetch.messageId, attachmentIds: fetch.attachmentIds)
        }
    }

    /// Downloads the server's copy of the images this phone did not send, and
    /// points the message at them. The same attachment directory as this
    /// phone's own sends — `imageFilenames` is the one thing `ChatView` reads —
    /// under the attachment ids, so a second fetch of the same row finds the
    /// files. Sequential, in row order, and each image is listed on the message
    /// as soon as it lands, so the grid fills in as they arrive; an image that
    /// fails is skipped (and tried again next launch by
    /// `retryMissingAttachments`) without costing the others.
    ///
    /// Tracked in `activeTasks` under the message's id (a user message never
    /// has a poll of its own) so `cancelActiveWork()` stops it and
    /// `awaitPendingWork()` sees it. One task per message, not per image.
    private func fetchAttachments(messageId: UUID, attachmentIds: [String]) {
        // The id becomes a filename. The server only issues uuids, but a path
        // component is not a place to trust that.
        let ids = attachmentIds.filter { UUID(uuidString: $0) != nil }
        guard activeTasks[messageId] == nil, !ids.isEmpty else { return }
        activeTasks[messageId] = Task { [weak self] in
            guard let self else { return }
            defer { self.activeTasks.removeValue(forKey: messageId) }
            for attachmentId in ids {
                if Task.isCancelled { return }
                let filename = "\(attachmentId).jpg"
                let file = self.attachmentsDirectoryURL.appendingPathComponent(filename)
                if !FileManager.default.fileExists(atPath: file.path) {
                    guard let data = try? await self.client.transcriptAttachment(
                        id: attachmentId, deviceToken: self.deviceToken
                    ), !Task.isCancelled else { continue }
                    do {
                        try FileManager.default.createDirectory(
                            at: self.attachmentsDirectoryURL, withIntermediateDirectories: true
                        )
                        try data.write(to: file, options: .atomic)
                    } catch {
                        continue
                    }
                }
                // Row order, not arrival order: a late image slots in before
                // a later one that happened to land first.
                self.update(messageId) { message in
                    let have = Set(message.allImageFilenames + [filename])
                    message.imageFilenames = message.allAttachmentIds.map { "\($0).jpg" }.filter { have.contains($0) }
                    message.imageFilename = nil
                }
                self.persist()
            }
        }
    }

    /// A row whose images never finished downloading — the app quit, or the link
    /// dropped mid-fetch — is still showing no picture, or only some. Looked
    /// for once per launch rather than on every sync tick: an attachment the
    /// server no longer has would otherwise be asked for every 15 seconds for
    /// good.
    private func retryMissingAttachments() {
        for message in messages {
            let missing = message.undownloadedAttachmentIds
            guard !missing.isEmpty else { continue }
            fetchAttachments(messageId: message.id, attachmentIds: missing)
        }
    }

    /// Hands `completedAssistantTurns` to the caller and empties it. Draining
    /// (rather than leaving the ids published) is what keeps one completion
    /// from being sounded twice, and keeps the queue from growing with the
    /// conversation.
    func takeCompletedAssistantTurns() -> [String] {
        defer { completedAssistantTurns = [] }
        return completedAssistantTurns
    }

    /// Hands `startedAssistantReplies` to the caller and empties it, for the
    /// same reason as the drain above: a queue that is read but not emptied
    /// re-fires on the next publish, and there is one of those every poll tick.
    func takeStartedAssistantReplies() -> [UUID] {
        defer { startedAssistantReplies = [] }
        return startedAssistantReplies
    }

    // MARK: - Persistence

    /// `nonisolated` so it can serve as `init`'s default argument, which is
    /// evaluated at the (not necessarily main-actor) call site.
    ///
    /// `-v2`: the file holds a cache of server rows now, not the only copy of
    /// the conversation, and an old `chat-transcript.json` is deleted rather
    /// than read (`legacyPersistURL`).
    nonisolated static func makePersistURL() -> URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("chat-transcript-v2.json")
    }

    /// The pre-server-transcript file, beside the current one. Phone-local
    /// history may be lost (Mike, 2026-10-01), so it is deleted, never migrated
    /// — its rows have no `seq`, and the server has none of them.
    private var legacyPersistURL: URL {
        persistURL.deletingLastPathComponent().appendingPathComponent("chat-transcript.json")
    }

    /// Where attached images live, keyed by `client_msg_id` plus an index
    /// (docs/tasks/ios-image-input.md §4) — the same handle `retry(_:)` and
    /// `reconcilePendingMessages()` already use to recover a `.sending`
    /// message's text, so the image is recoverable by the identical lookup.
    /// Injectable at `init`, same reasoning as `persistURL`:
    /// tests must not read or write the app's real Application Support
    /// directory.
    nonisolated static func makeAttachmentsDirectoryURL() -> URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("chat-attachments", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// Writes already-downscaled JPEG bytes and returns the filename (not the
    /// full path — `ChatMessage.imageFilename` is portable across app
    /// reinstalls/updates that move Application Support, unlike an absolute
    /// URL would be).
    /// `nil` on any write failure (disk full, sandbox denial, ...) rather than
    /// a filename that names bytes which were never actually persisted —
    /// `send()` uses the failure to keep what's sent and what's saved for
    /// retry in agreement (`/code-review medium` finding).
    private func saveAttachment(_ data: Data, clientMsgId: String, index: Int) -> String? {
        let filename = "\(clientMsgId)-\(index).jpg"
        do {
            try FileManager.default.createDirectory(
                at: attachmentsDirectoryURL, withIntermediateDirectories: true
            )
            try data.write(to: attachmentsDirectoryURL.appendingPathComponent(filename), options: .atomic)
            return filename
        } catch {
            return nil
        }
    }

    /// Deletes attachment files that no message kept at load refers to: images
    /// of messages the 30-day cutoff just dropped, and everything the old
    /// phone-local transcript left behind. Nothing else ever removes them.
    ///
    /// Off the main actor, so launch is not held up listing and deleting a
    /// directory of photos; the referenced set is computed first, here, and
    /// the task captures only values — no `self`.
    ///
    /// A file written at or after this launch is never touched. The task runs
    /// concurrently with the first send and the first attachment download, and
    /// a file they have just written is in nobody's referenced set yet.
    private func pruneUnreferencedAttachments() {
        var referenced = Set<String>()
        for message in messages {
            referenced.formUnion(message.allImageFilenames)
            // Downloads that have not finished yet will land under these names.
            for attachmentId in message.allAttachmentIds { referenced.insert("\(attachmentId).jpg") }
        }
        let directory = attachmentsDirectoryURL
        let launch = Date()
        Task.detached(priority: .utility) {
            let keys: [URLResourceKey] = [.contentModificationDateKey, .isRegularFileKey]
            guard let files = try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: keys
            ) else { return }
            for file in files where !referenced.contains(file.lastPathComponent) {
                let values = try? file.resourceValues(forKeys: Set(keys))
                guard values?.isRegularFile == true,
                      let modified = values?.contentModificationDate, modified < launch
                else { continue }
                try? FileManager.default.removeItem(at: file)
            }
        }
    }

    func loadAttachment(filename: String) -> Data? {
        try? Data(contentsOf: attachmentsDirectoryURL.appendingPathComponent(filename))
    }

    /// Decoded-image cache, keyed by filename. `ChatView`'s `MessageRow`
    /// loads a sent image inline in `body`, and `messages` is `@Published` —
    /// every poll tick while any turn is in flight re-evaluates `body` for
    /// every visible row, so an uncached decode re-read the file and
    /// re-decoded the JPEG on the main thread on every single tick, for every
    /// image on screen (`/code-review medium` finding). `NSCache` rather than
    /// a plain dictionary: it evicts under memory pressure on its own, which
    /// a growing transcript of photos would otherwise never do.
    private let imageCache = NSCache<NSString, UIImage>()

    /// Not private — `ChatView`'s `MessageRow` renders a sent image by
    /// filename and has no other handle onto this store's attachment
    /// directory or cache.
    func loadedImage(filename: String) -> UIImage? {
        let key = filename as NSString
        if let cached = imageCache.object(forKey: key) { return cached }
        guard let data = loadAttachment(filename: filename), let image = UIImage(data: data) else { return nil }
        imageCache.setObject(image, forKey: key)
        return image
    }

    /// The persisted file: the cursor, the "more on the server" flag, and the
    /// messages — server rows and this phone's own outbox together.
    struct TranscriptCache: Codable {
        var latestSeq: Int?
        /// Optional so a file written before the flag existed decodes.
        var hasOlderOnServer: Bool?
        var messages: [ChatMessage]
    }

    /// Server rows older than this are dropped on load; the server prunes at
    /// the same age (docs/tasks/server-transcript.md).
    nonisolated static let retention: TimeInterval = 30 * 24 * 60 * 60

    private func load() {
        // Always, and before reading: the old file is never read. Skipped only
        // if a caller pointed `persistURL` at that very name, so a test cannot
        // delete the file it is about to load.
        if legacyPersistURL != persistURL {
            try? FileManager.default.removeItem(at: legacyPersistURL)
        }
        guard let data = try? Data(contentsOf: persistURL),
              let cache = try? JSONDecoder().decode(TranscriptCache.self, from: data)
        else { return }
        latestSeq = cache.latestSeq
        hasOlderOnServer = cache.hasOlderOnServer ?? false
        // A message the server backs is a copy and can age out with it. One it
        // does not back (`seq == nil`) is this phone's own and is never
        // dropped here, however old: it may be an unsent message.
        let cutoff = Date().addingTimeInterval(-Self.retention)
        messages = cache.messages.filter { $0.seq == nil || $0.createdAt >= cutoff }
    }

    private func persist() {
        let cache = TranscriptCache(
            latestSeq: latestSeq, hasOlderOnServer: hasOlderOnServer, messages: messages
        )
        guard let data = try? JSONEncoder().encode(cache) else { return }
        try? data.write(to: persistURL, options: .atomic)
    }
}
