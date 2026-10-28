import AudioToolbox
import PhotosUI
import SwiftUI
import UIKit
import UserNotifications

/// List + text field. Send -> POST /v1/message, then poll GET /v1/turn/{id}
/// until terminal. Two banners surface state Mike currently only finds out
/// about by noticing an absence: reauth_needed and queue_paused.
///
/// **The transcript rows are eager (`VStack`), deliberately, and the viewport
/// is measured from a `.background` rather than a wrapping `GeometryReader`.**
/// Both are the fix for a cold-launch bug that had survived two earlier
/// attempts, and both look like things worth "tidying" if you don't know why
/// they're here, so: on device (iPhone 14 Pro, 205 messages, 2026-09-06) the
/// app launched to a black screen for several seconds and painted the entire
/// transcript the instant Mike scrolled. Instrumenting the real device — the
/// simulator never reproduced it, at 120 or 2500 messages — counted how many
/// rows `LazyVStack` had actually realized:
///
///   - wrapped in a GeometryReader, LazyVStack rows:  0 realized, 8s, black
///   - GeometryReader removed, LazyVStack rows:       5 realized, stuck at top
///   - GeometryReader removed, eager rows:          205 realized, correct
///
/// LazyVStack measured a single screenful, concluded from its own truncated
/// height that there was nothing further to scroll to, and never asked again —
/// self-reinforcing, which is why it never recovered on its own but recovered
/// instantly from one gesture. The GeometryReader was contributing: as a
/// container it proposes an unstable size during first layout, and removing it
/// alone moved the count from 0 to 5.
///
/// The cost of eager rows is real and is Mike's accepted trade (2026-09-06):
/// every rendered message builds at launch. Rendering the *whole* transcript
/// that way was a visible blank on cold launch by 2026-09-25, so the rows are
/// now a window — roughly 48 hours eagerly, older behind "Show earlier
/// messages" (`TranscriptWindow`, docs/tasks/ios-transcript-scaling.md). Do not
/// restore `LazyVStack` to "fix" the scaling without reading it and
/// re-testing on a real device; the simulator will tell you it's fine.
struct ChatView: View {
    @EnvironmentObject private var session: SessionStore
    @StateObject private var store: ChatStore
    @StateObject private var dictation = DictationController()
    @State private var draft = ""
    @State private var showingSettings = false
    @State private var showingMarlow = false
    @State private var showingIdeas = false
    /// The "Marlow was updated" card (docs/tasks/whats-new.md), raised by
    /// `presentWhatsNewIfDue()` rather than a tap.
    @State private var showingWhatsNew = false
    /// Voice mode (docs/tasks/voice-mode-hands-free.md): the big-button screen
    /// over the chat.
    @State private var showingVoiceMode = false
    /// Lives as long as the chat screen, unlike the Marlow sheet's stores: the
    /// lightbulb's dot and the spotlight card show without the panel open.
    @StateObject private var ideasStore: IdeasStore
    /// Same lifetime as `ideasStore`, for the same reason: the card shows
    /// without Settings open.
    @StateObject private var whatsNew: WhatsNewStore
    @State private var isFarFromBottom = false
    /// The transcript viewport's height, measured from the background rather
    /// than by wrapping the transcript in a `GeometryReader` — see the
    /// `.background` below.
    @State private var viewportHeight: CGFloat = 0
    @State private var scrollToBottomRequest = 0
    @FocusState private var draftFocused: Bool
    @Environment(\.scenePhase) private var scenePhase

    /// Already-downscaled JPEG bytes waiting to go out with the next send, in
    /// attach order, at most `ChatMessage.maxImages` — appended to by the photo
    /// picker, camera and paste, cleared by `submitDraft()` in the same
    /// run-loop hop as `draft` (docs/tasks/ios-image-input.md trap #3:
    /// captured and cleared together, or a send could carry the text without
    /// the images or vice versa).
    @State private var pendingImages: [Data] = []
    @State private var selectedPhotoItems: [PhotosPickerItem] = []
    /// Presents the photo library. A separate flag from `selectedPhotoItems`
    /// because the picker is no longer its own button: `PhotosPicker` as a
    /// label does not survive being put inside a `Menu` (the menu wants to own
    /// the tap), so the menu item raises this and the
    /// `.photosPicker(isPresented:)` modifier on the composer presents it.
    @State private var showingPhotoPicker = false
    /// Presents the camera, raised from the same menu for the same reason.
    @State private var showingCamera = false
    /// Set when an assistant message completes while the transcript is
    /// scrolled away from the tail — drives the dot on the jump button
    /// (docs/tasks/ios-chat-ux-gaps.md item 14). View state, not store state:
    /// it is a fact about where *this* viewport is looking, and it dies with
    /// the view.
    @State private var hasUnseenArrival = false
    /// The exchange being followed after a local send (`ReplyFollow`), or
    /// nil. Written only when following starts and ends — never per frame —
    /// because it decides which rows measure themselves at all: the row
    /// geometry readers exist only while this is set.
    @State private var followedExchange: FollowedExchange?
    /// The per-frame half of following, in a class on purpose: writing a
    /// reference type's property does not invalidate the body, where a
    /// `@State` write on every scroll frame redraws the whole transcript (the
    /// `TranscriptBottomKey` lesson, below).
    @State private var followTracker = FollowTracker()
    /// The height of the readable transcript: the scroll view's own frame,
    /// which the header's `.safeAreaInset` has already shortened. Not
    /// `viewportHeight`, which includes the strip under the header.
    ///
    /// Measured on the simulator (2026-09-29): the `"transcript"` coordinate
    /// space starts at the header's bottom edge, not at the top of the
    /// screen — a row at y 0 there sits just under the header (global y
    /// 162 = 62 status bar + 100 header), and `scrollTo(_, anchor: .bottom)`
    /// lands a row's bottom at exactly this height. So the readable region
    /// in that space is `0...readableHeight`.
    @State private var readableHeight: CGFloat = 0

    init(deviceToken: String, location: LocationProvider) {
        _store = StateObject(wrappedValue: ChatStore(
            deviceToken: deviceToken,
            currentLocation: { [weak location] in location?.currentLocation() }
        ))
        _ideasStore = StateObject(wrappedValue: IdeasStore(deviceToken: deviceToken))
        _whatsNew = StateObject(wrappedValue: WhatsNewStore(deviceToken: deviceToken))
    }

    #if DEBUG
    /// For `UIFixture` only — a store backed by a scripted server.
    init(store: @autoclosure @escaping () -> ChatStore) {
        _store = StateObject(wrappedValue: store())
        _ideasStore = StateObject(wrappedValue: UIFixture.makeIdeasStore())
        _whatsNew = StateObject(wrappedValue: UIFixture.makeWhatsNewStore())
    }
    #endif

    var body: some View {
        VStack(spacing: 0) {
            // One strip, not five banners (docs/tasks/ios-chat-ux-gaps.md
            // item 13). What used to be here was five independent `if`s, any
            // number of which could be true at once — so a three-second
            // dictation error got the same full-width permanent-looking bar as
            // a server that has stopped accepting work, and a bad moment could
            // eat four rows off the top of the transcript at once.
            //
            // The split that fixes it is persistent vs transient, not
            // important vs unimportant. A persistent state is a fact about the
            // server that is still true a minute from now and that Mike has to
            // go *do* something about (SSH in and reauthorize); it belongs at
            // the top, it outranks the others, and it is never dismissible —
            // reauth in particular must not be closable into invisibility,
            // which is why nothing here has a close button. A transient notice
            // is the echo of an action he just took, it expires on its own, and
            // it has moved to sit beside the control that caused it: the
            // permission notice to the foot of the transcript (with
            // FetchIndicator, for the same reason given there — the bottom is
            // where you are already looking), the dictation error to just above
            // the mic.
            StatusStrip(statuses: activeStatuses, usageText: usageHighText)

            ScrollViewReader { proxy in
                // Computed once per body evaluation, not per row: `ForEach`
                // still keys identity on the message's own stable `id`
                // (never the array position, which would misbehave the day
                // a message is ever inserted mid-transcript rather than only
                // appended — see ios-chat-ux-gaps.md §7), but finding "does
                // this message start a new time gap" no longer needs a full
                // `Array(store.messages.enumerated())` copy on every publish.
                let timestampIds = messageIdsNeedingTimestamp
                let failedReplies = failedReplyByUserMessage
                ZStack(alignment: .bottomTrailing) {
                    ScrollView {
                        // The measuring sentinel is a sibling of the row
                        // stack, not its last child — a distinction that
                        // mattered while the rows were lazy: scrolling up far
                        // enough to want the jump button was exactly what
                        // evicted the bottom row, which withdrew the
                        // preference, which reset it to `defaultValue` (0), so
                        // the button was hidden by the very gesture meant to
                        // summon it (found on device, 2026-08-23). The rows
                        // are eager now and cannot be evicted, but the sentinel
                        // stays out here: it costs nothing and it is what makes
                        // the measurement independent of how the rows are
                        // built, rather than correct only by luck.
                        VStack(spacing: 0) {
                            // Eager, NOT `LazyVStack` — see the type comment
                            // on `ChatView` and ios-transcript-scaling.md.
                            VStack(alignment: .leading, spacing: 12) {
                                if store.hasEarlierMessages {
                                    EarlierMessagesButton {
                                        revealEarlierMessages(proxy: proxy)
                                    }
                                }
                                // The window, not the whole transcript: every
                                // row here is built before the first frame, so
                                // how many there are IS the cold-launch pause
                                // (see `TranscriptWindow`).
                                ForEach(store.visibleMessages, id: \.id) { message in
                                    if timestampIds.contains(message.id) {
                                        // Measured with the row when followed:
                                        // `scrollTo(id)` targets this whole
                                        // ForEach element, divider included,
                                        // so a pin to `.top` lands the divider
                                        // there, not the bubble. Measuring the
                                        // bubble alone let the last follow step
                                        // carry the divider under the header
                                        // and the pin then moved the view back
                                        // down ~30pt (simulator, 2026-09-29).
                                        TimestampDivider(date: message.createdAt)
                                            .modifier(FollowFrameReporter(
                                                id: message.id,
                                                isActive: followedExchange?.contains(message.id) == true
                                            ))
                                    }
                                    MessageRow(
                                        message: message, onRetry: { store.retry($0) },
                                        loadImage: { store.loadedImage(filename: $0) },
                                        failedReplyId: failedReplies[message.id],
                                        isAttachedFailure: message.clientMsgId
                                            .flatMap(UUID.init(uuidString:))
                                            .map { failedReplies[$0] == message.id } ?? false,
                                        reportsFrame: followedExchange?.contains(message.id) == true
                                    ).id(message.id)
                                }
                            }
                            // Arrivals only: see `MessageRow.arrivalAnimation`.
                            // Keyed on the LAST id so revealing earlier rows
                            // (which moves the first) and the cold load (no
                            // change at all) insert without it.
                            .animation(MessageRow.arrivalAnimation, value: store.messages.last?.id)
                            .padding()

                            Color.clear
                                .frame(height: 1)
                                .background {
                                    GeometryReader { bottom in
                                        Color.clear.preference(
                                            key: TranscriptBottomKey.self,
                                            value: bottom.frame(in: .named("transcript")).maxY
                                        )
                                    }
                                }
                        }
                    }
                    // Places the first rendered frame at the bottom outright,
                    // so nothing has to scroll there afterwards — see the
                    // `onAppear` below for why that mattered.
                    .modifier(InitialBottomScrollAnchor())
                    .coordinateSpace(name: "transcript")
                    .onPreferenceChange(TranscriptBottomKey.self) { bottomY in
                        // Compare before writing. `bottomY` is measured in the
                        // transcript's own coordinate space, so it changes on
                        // *every frame of every scroll* — and a SwiftUI
                        // `@State` setter doesn't dedupe, so assigning `false`
                        // over `false` still invalidated the whole ChatView
                        // body: the ScrollView, the ForEach and every row,
                        // sixty-plus times a second while a finger was down.
                        // Only a threshold *crossing* is news.
                        //
                        // Honest history: this was written believing the churn
                        // explained the blank-transcript bug. It did not — the
                        // device instrumentation showed the rows were never
                        // built at all (see the type comment above). The
                        // dedupe is worth keeping on its own merits; it just
                        // isn't the fix it was thought to be, and the next
                        // person should not read it as one.
                        //
                        // Nothing to compare against until the background
                        // reader has reported. `viewportHeight` starts at 0,
                        // and deciding against 0 makes the threshold
                        // `max(160, 0)` — which a full transcript clears
                        // instantly, so the jump-to-bottom button would appear
                        // at launch, sitting there until a scroll produced the
                        // next preference change to correct it. A hidden
                        // button is the right answer while the answer is
                        // unknown.
                        guard viewportHeight > 0 else { return }
                        let far = bottomY > viewportHeight + max(160, viewportHeight / 3)
                        if far != isFarFromBottom {
                            isFarFromBottom = far
                            // Scrolling back down to the tail clears the mark
                            // as surely as tapping the button does — the
                            // arrival has been seen either way, and leaving a
                            // dot on a button that is about to disappear would
                            // strand the flag set for the next time the user
                            // scrolls up.
                            if !far { hasUnseenArrival = false }
                        }
                    }
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { h in
                        if h != readableHeight { readableHeight = h }
                    }
                    .onPreferenceChange(FollowedRowFramesKey.self) { frames in
                        followStreamingReply(frames: frames, proxy: proxy)
                    }
                    // A finger on the transcript ends following for this
                    // turn: auto-scroll must never fight one. Scroll phase,
                    // not a gesture, where the OS has it — a drag gesture
                    // added to a ScrollView competes with its own.
                    .modifier(TouchEndsFollowing(isFollowing: followedExchange != nil) {
                        followedExchange = nil
                    })
                    // The standard affordance (Messages, Discord, Slack
                    // all do this): dragging the transcript lets the
                    // keyboard follow the gesture down and dismiss
                    // interactively, rather than it sitting there forever
                    // once the text field has been tapped once.
                    // `.interactively` (not `.immediately`) is what makes
                    // it follow the drag instead of vanishing the moment
                    // scrolling starts — a small flick shouldn't punt the
                    // keyboard away when the intent was just to glance up
                    // at older messages.
                    .scrollDismissesKeyboard(.interactively)
                    // An overlay, not a row: it is not a message, is never
                    // persisted, and must not be something Copy or a scroll
                    // anchor can find. A stand-in for a real first-run
                    // onboarding, which is wanted eventually (Mike,
                    // 2026-09-28); a blank screen read as broken.
                    .overlay {
                        if store.messages.isEmpty && !store.hasEarlierMessages {
                            EmptyChatGreeting(loading: store.awaitingFirstFetch)
                        }
                    }
                    // An inset, not a sibling in the VStack: rows scroll up
                    // UNDER the header and fade out through its gradient,
                    // while at rest the first row still starts below it.
                    // Attached to the ScrollView itself so the jump button's
                    // ZStack and the viewport measurement behind it keep the
                    // geometry they had (see the type comment before moving
                    // either).
                    .safeAreaInset(edge: .top, spacing: 0) {
                        PresenceHeader(
                            presence: ChatPresence.of(store.messages),
                            ideasHaveNews: ideasStore.showsDot,
                            onSettings: { showingSettings = true },
                            onAvatar: { showingMarlow = true },
                            onIdeas: { showingIdeas = true }
                        )
                    }

                    if isFarFromBottom, let last = store.messages.last {
                        Button {
                            // Unanimated, deliberately, and repeated a
                            // run-loop turn later. A tap that lands while
                            // the transcript is still gliding loses to the
                            // momentum: the scroll view finishes its
                            // deceleration and an animated scrollTo is
                            // simply discarded, so the button looks dead —
                            // it only worked once the scroll had settled
                            // (found on device, 2026-08-23). Setting the
                            // offset outright is what cancels deceleration,
                            // and animating is what stops it doing so. The
                            // deferred repeat covers a first call swallowed
                            // mid-glide and is a no-op when it wasn't.
                            hasUnseenArrival = false
                            proxy.scrollTo(last.id, anchor: .bottom)
                            DispatchQueue.main.async {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
                        } label: {
                            Image(systemName: "arrow.down")
                                .font(.headline.bold())
                                .frame(width: 44, height: 44)
                                // Same material and hairline as the gear in
                                // `PresenceHeader` (`FloatingIconButton`), so
                                // the two floating controls read as one family.
                                .background(.regularMaterial, in: Circle())
                                .overlay(Circle().stroke(Color.primary.opacity(0.08), lineWidth: 0.5))
                                .shadow(radius: 3, y: 1)
                                // A dot, not a count
                                // (docs/tasks/ios-chat-ux-gaps.md item 14): the
                                // client has no settled answer for whether an
                                // ingested cron turn, a retry, and two
                                // assistant rows from one turn are one arrival
                                // or four, and a number is a promise to have
                                // one. A dot says the only thing that is
                                // certainly true.
                                //
                                // Overlaid outside the `.background` circle so
                                // the material doesn't tint it, and drawn with
                                // a background-coloured ring so it reads as a
                                // badge rather than a smudge on the shadow.
                                .overlay(alignment: .topTrailing) {
                                    if hasUnseenArrival {
                                        Circle()
                                            .fill(Color.accentColor)
                                            .frame(width: 10, height: 10)
                                            .overlay(Circle().stroke(Color(.systemBackground), lineWidth: 2))
                                            .offset(x: -2, y: 2)
                                    }
                                }
                        }
                        // Deliberately NOT tied to the app-icon badge
                        // (`APNsRegistrar.marksChatUnread`). That one means
                        // "something arrived while you were away" and survives
                        // until the app opens; this one means "something
                        // arrived while you were scrolled up" and dies the
                        // moment you look. Same word, different lifetimes —
                        // sharing state between them would make one of the two
                        // wrong.
                        .accessibilityLabel(
                            hasUnseenArrival
                            ? "New message. Jump to newest message"
                            : "Jump to newest message"
                        )
                        .padding(12)
                        }
                }
                // The stand-in for the approval card while dictating
                // (`permissionCardRequest`). An overlay, not a row, so it
                // never moves the transcript; it floats just above the
                // composer, where the recording is.
                .overlay(alignment: .bottom) {
                    if let request = permissionPillRequest {
                        PermissionPendingPill(
                            request: request,
                            onTap: { dictation.stop() },
                            onExpire: { store.expirePermissionSheet(requestId: request.requestId) }
                        )
                        .padding(.bottom, 10)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                }
                .animation(.snappy(duration: 0.25), value: permissionPillRequest?.requestId)
                // The viewport height, measured from the BACKGROUND, not by wrapping the
                // transcript in a `GeometryReader`. A GeometryReader as a
                // container hands its child an unstable size proposal during
                // the first layout pass, and on device that was enough to stop
                // the rows being built at all (see the type comment above).
                // Reading the size from behind the view leaves the
                // transcript's own layout path alone.
                //
                // `onAppear`/`onChange` rather than a preference: a preference
                // set inside `.background` does not propagate out to the
                // backed view, so the height read back as 0 — which silently
                // pinned the jump-button threshold to its floor.
                .background {
                    GeometryReader { g in
                        Color.clear
                            .onAppear {
                                if g.size.height != viewportHeight { viewportHeight = g.size.height }
                            }
                            .onChange(of: g.size.height) { _, h in
                                if h != viewportHeight { viewportHeight = h }
                            }
                    }
                }
                .onChange(of: store.messages.count) { _, _ in
                    if let last = store.messages.last, !isFarFromBottom {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
                .onChange(of: scrollToBottomRequest) { _, _ in
                    // An accepted local send is different from an incoming
                    // message: even while reading older history, the sender
                    // needs immediate confirmation that their new message and
                    // its status placeholder entered the transcript.
                    guard let last = store.messages.last else { return }
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    // Then keep the reply in view as it streams — this path
                    // only, never an incoming turn, which keeps the
                    // `!isFarFromBottom` rule above (`ReplyFollow`).
                    let messages = store.messages
                    if messages.count >= 2, last.role == .assistant,
                       messages[messages.count - 2].role == .user {
                        followTracker = FollowTracker()
                        followedExchange = FollowedExchange(
                            userId: messages[messages.count - 2].id, replyId: last.id
                        )
                    }
                }
                // A turn that finishes, errors or is merged away ends
                // following — as does anything landing after it. A run-loop
                // turn later, so the layout that shows the final text still
                // gets one follow decision: the finished reply can be longer
                // than the typing had reached.
                .onChange(of: followedReplyIsStreaming) { _, streaming in
                    guard !streaming else { return }
                    DispatchQueue.main.async { followedExchange = nil }
                }
                // Cold open lands at the top of a restored transcript, because
                // the count never changed — nothing was appended, the messages
                // were decoded from disk. Tapping a "Marlow has something for
                // you" notification is exactly that path, so the one moment the
                // newest message matters most is the one it wasn't shown
                // (found on device, 2026-08-07). Unanimated: this is where the
                // view should already have been, not a movement to watch.
                .onAppear {
                    // Only where `InitialBottomScrollAnchor` is a no-op. Where
                    // it isn't, this call is not merely redundant, it is the
                    // bug: scrolling to a row is exactly what the anchor
                    // exists to avoid having to do.
                    guard #unavailable(iOS 18.0) else { return }
                    guard let last = store.messages.last else { return }
                    // Deferred a run-loop turn so the layout pass triggered
                    // by this same appearance finishes before the scroll is
                    // asked for. That deferral used to be load-bearing and
                    // unreliable, because the rows were lazy and might not
                    // exist yet; with eager rows there is a real position to
                    // scroll to by the time this runs.
                    DispatchQueue.main.async {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }

            // The foot of the conversation, NOT the banner stack at the top.
            // Two reasons. Mike's (2026-09-07): a word like "Checking" is too
            // big a thing to say for what is happening, and the place you are
            // already staring at after thumbing a notification is the bottom
            // of the transcript, where the message you were promised should
            // appear. And ios-chat-ux-gaps.md item 13's: those banners give
            // temporary state the same permanent-looking treatment as a server
            // that has stopped, and there are already five of them.
            //
            // Outside the ScrollView rather than appended after the last row.
            // Inside, `scrollTo(last.id, anchor: .bottom)` — which runs on
            // launch, on every arrival, and on the jump button — would park
            // the last MESSAGE against the bottom edge and push the indicator
            // out of sight, hiding it in exactly the moment it exists for.
            FetchIndicator(activity: store.connectionActivity)

            // The permission notice, demoted from the banner stack
            // (docs/tasks/ios-chat-ux-gaps.md item 13). It is the echo of a
            // tap Mike just made on the permission sheet — "Allowed", "Too
            // late" — and `ChatStore.showPermissionNotice` already clears it
            // after three seconds. Three seconds is not a banner. It sits here
            // for the same reason FetchIndicator does: the sheet dismissing
            // leaves him looking at the bottom of the transcript, and that is
            // where the answer to "did that land?" should appear.
            if let notice = store.permissionNotice {
                TransientNotice(text: notice)
            }

            if !pendingImages.isEmpty {
                // One 64x64 thumbnail per staged image, three across, so six
                // are two rows (CMP-5). A single row of six at 44pt left a
                // remove control too small to hit with confidence on a phone
                // (Mike, 2026-10-24); three across leaves room for both.
                LazyVGrid(
                    columns: Array(repeating: GridItem(.fixed(64), spacing: 18), count: 3),
                    alignment: .leading, spacing: 18
                ) {
                        ForEach(Array(pendingImages.enumerated()), id: \.offset) { index, data in
                            // A staged image that will not decode still counts
                            // toward the cap and is still sent, so it still
                            // gets a tile and a remove control: a gray one.
                            Group {
                                if let preview = UIImage(data: data) {
                                    Image(uiImage: preview)
                                        .resizable()
                                        .scaledToFill()
                                } else {
                                    Color.gray.opacity(0.25)
                                }
                            }
                            .frame(width: 64, height: 64)
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                            .overlay(alignment: .topTrailing) {
                                Button {
                                    // By content, not the captured index: a
                                    // second tap before the re-render would
                                    // otherwise remove whichever image has
                                    // slid into this slot.
                                    if let at = pendingImages.firstIndex(of: data) {
                                        pendingImages.remove(at: at)
                                    }
                                } label: {
                                    // A 36pt target centred on the corner, as
                                    // on the web: a 44pt one over the tile
                                    // covered most of it, so touching a photo
                                    // removed it. The grid's 18pt spacing keeps
                                    // it off the next tile.
                                    Image(systemName: "xmark.circle.fill")
                                        .font(.system(size: 26))
                                        .symbolRenderingMode(.palette)
                                        .foregroundStyle(.white, .black.opacity(0.65))
                                        .frame(width: 36, height: 36)
                                        .contentShape(Rectangle())
                                }
                                .offset(x: 18, y: -18)
                                .accessibilityLabel("Remove image \(index + 1) of \(pendingImages.count)")
                            }
                        }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 18)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            // The spotlight: the one idea Marlow thinks is worth interrupting
            // for (docs/tasks/ideas-panel.md). Between the transcript and the
            // composer, never a row in the transcript, so it is out of the
            // context and gone the moment it is answered. Not while dictating,
            // for the reason on `permissionCardRequest`: a card appearing must
            // not move the recording controls under a thumb.
            if let spotlight = ideasStore.spotlight, !dictation.isRecording {
                SpotlightCard(
                    idea: spotlight,
                    onOpen: { showingIdeas = true },
                    onDoIt: { doIt(spotlight) },
                    onNotNow: { Task { await ideasStore.dismiss(spotlight) } }
                )
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            // The dictation error, demoted from the banner stack
            // (docs/tasks/ios-chat-ux-gaps.md item 13) and moved next to the
            // control it is about. Every string that reaches
            // `dictation.errorMessage` is the result of pressing the mic or
            // pressing send — mic permission refused, recognition failed — so
            // it reads as a caption on the button, and putting it at the top of
            // the screen made the user find the connection between a bar up
            // there and a tap down here.
            if let dictationError = dictation.errorMessage {
                TransientNotice(text: dictationError)
            }

            // spacing 0, because each control below carries its own 34x44 tap
            // target — the transparent margin inside those frames *is* the gap
            // now. Leaving the old 8pt on top of it stacked two spacings and
            // left the composer full of holes (Mike, on device, 2026-08-23).
            // 34 wide rather than a square 44: the row's height is free, its
            // width is not, and 34 keeps the four controls near the pitch they
            // had before the tap targets went in.
            //
            // One rounded field holding every control, rather than a bordered
            // text field with icons around it (2026-09-28 polish): the row
            // reads as one thing to type into, and the divider that used to
            // fence it off from the transcript is gone.
            HStack(alignment: .bottom, spacing: 0) {
                // One attachment button, not two
                // (docs/tasks/ios-chat-ux-gaps.md item 11). Photo Library and
                // Paste used to sit here as separate icons, which gave four
                // controls equal visual weight around a text field and made
                // the row read as a toolbar with a field wedged into it.
                //
                // 44 wide here where the others are 34: that comment below
                // about width being the scarce dimension is still true, which
                // is exactly why collapsing two 34s into one 44 is affordable
                // — it hands the text field back 24pt *and* gives the one
                // remaining attachment control the full square tap target item
                // 11 asks for. Camera (ios-image-input.md step 2) is the
                // third item; that is what the menu is for.
                // Hidden while dictating: attaching mid-recording makes no
                // sense, and the meter gets the room (Mike, 2026-10-17).
                if !dictation.isRecording {
                    Menu {
                        if CameraPicker.isAvailable {
                            Button {
                                if hasRoomForAnotherImage() { showingCamera = true }
                            } label: {
                                Label("Take Photo", systemImage: "camera")
                            }
                            .accessibilityLabel("Take a photo")
                            .accessibilityHint("Attaches a photo from the camera")
                        }
                        Button {
                            if hasRoomForAnotherImage() { showingPhotoPicker = true }
                        } label: {
                            Label("Photo Library", systemImage: "photo.on.rectangle")
                        }
                        .accessibilityLabel("Choose a photo")
                        .accessibilityHint("Attaches an image from the photo library")
                        // Still reads the pasteboard only when tapped, never
                        // reactively — the original reasoning survives the move: a
                        // `UIPasteboard.general.hasImages` check would have to be
                        // polled to notice a pasteboard change, and it is not worth
                        // it to grey out an item that is a harmless no-op when
                        // empty.
                        Button {
                            if hasRoomForAnotherImage(),
                               let image = UIPasteboard.general.image,
                               let downscaled = ImageDownscaler.downscaledJPEG(from: image) {
                                appendPendingImages([downscaled])
                            }
                        } label: {
                            Label("Paste Image", systemImage: "doc.on.clipboard")
                        }
                        .accessibilityLabel("Paste image")
                        .accessibilityHint("Attaches an image from the clipboard")
                    } label: {
                        Image(systemName: "plus")
                            .font(.system(size: 20, weight: .medium))
                            .foregroundStyle(.secondary)
                            .frame(width: 44, height: 44)
                    }
                    .accessibilityLabel("Add attachment")
                    .accessibilityHint(CameraPicker.isAvailable
                        ? "Attach an image from the camera, the photo library or the clipboard"
                        : "Attach an image from the photo library or the clipboard")
                }
                // Swaps in for the text field while dictating — the Claude
                // iOS app does the same thing: replace the thing you'd type
                // into with a live "yes, I hear you" indicator, since a tap
                // to start gives no other feedback that the mic caught
                // anything until the transcript lands on send.
                if dictation.isRecording {
                    LevelMeterView(levelMeter: dictation.levelMeter)
                        .frame(height: 36)
                        .padding(.vertical, 4)
                        // Air before the stop button so it reads as its own
                        // control, not the meter's last bar.
                        .padding(.trailing, 10)
                        // The + is hidden while recording; keep the bars off
                        // the pill's rounded edge.
                        .padding(.leading, 12)
                } else if dictation.isTranscribing {
                    // Recognition now runs over the whole recording *after*
                    // Mike stops (DictationController, "record first,
                    // transcribe once"), so there is a real gap here where
                    // nothing has landed yet. Without this the field snaps
                    // back to empty and it reads as the mic having failed.
                    HStack(spacing: 8) {
                        ProgressView()
                        Text("Transcribing…").foregroundStyle(.secondary)
                    }
                    .frame(height: 36, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 4)
                } else {
                    // Plain, inside the pill: the pill is the border now.
                    // Vertical padding centres one line against the 44pt
                    // controls and lets further lines grow the pill upward.
                    TextField("Message Marlow", text: $draft, axis: .vertical)
                        .textFieldStyle(.plain)
                        .lineLimit(1...5)
                        .focused($draftFocused)
                        .padding(.vertical, 11)
                }
                // Push-to-dictate (docs/tasks/voice-mode.md): tap to start,
                // tap again to stop. Which button ends it decides where the
                // transcript goes — stop means "let me look at it," send means
                // "send it" (see DictationController.Endpoint). While recording
                // the mic becomes a stop square: a filled mic read as "still
                // listening, tap to talk" and Taylor didn't find the way to end
                // it (2026-10-17). The thin ring says "button" next to the
                // meter bars without competing with the filled send arrow.
                Button {
                    dictation.toggle { text, endpoint in
                        receiveDictation(text, endpoint: endpoint)
                    }
                } label: {
                    Image(systemName: dictation.isRecording ? "stop.circle" : "mic")
                        .font(.system(size: dictation.isRecording ? 28 : 20, weight: dictation.isRecording ? .light : .regular))
                        .foregroundStyle(dictation.isRecording ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(.secondary))
                        .frame(width: dictation.isRecording ? 40 : 34, height: 44)
                }
                .accessibilityLabel(dictation.isRecording ? "Stop dictation" : "Start dictation")
                .accessibilityHint(
                    dictation.isRecording
                    ? "Stops recording and places the transcript in the message field"
                    : "Starts recording a dictated message"
                )
                .disabled(dictation.isTranscribing)
                // Shown only when pressing it would do something, rather than
                // sitting there greyed out: an idle composer is just the field
                // and the mic, and the arrow arriving is itself the cue that
                // the message is ready. The condition is exactly the old
                // `.disabled` one inverted, so nothing that could be sent
                // before is unsendable now.
                if canSend {
                    Button {
                        // While dictating there is nothing in `draft` to submit —
                        // the field is swapped out for the level meter. Send means
                        // "finish and send," which is Mike's ask (2026-08-16) and
                        // the reason this button is no longer inert mid-dictation.
                        if dictation.isRecording {
                            dictation.stopAndSend()
                        } else {
                            submitDraft()
                        }
                    } label: {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 30))
                            .foregroundStyle(Color.accentColor)
                            .frame(width: 40, height: 44)
                    }
                    .accessibilityLabel(dictation.isRecording ? "Stop dictation and send" : "Send message")
                    .transition(.scale(scale: 0.5).combined(with: .opacity))
                } else if !dictation.isTranscribing {
                    // Voice mode lives in send's slot whenever there is nothing
                    // to send (Mike, 2026-10-20) — the same pattern as the
                    // send arrow appearing only when it has a job.
                    Button {
                        draftFocused = false
                        showingVoiceMode = true
                    } label: {
                        Image(systemName: "waveform.circle.fill")
                            .font(.system(size: 30))
                            .foregroundStyle(Color.accentColor)
                            .frame(width: 40, height: 44)
                    }
                    .accessibilityLabel("Voice mode")
                    .accessibilityHint("Talk to Marlow and hear the replies read aloud")
                    .transition(.scale(scale: 0.5).combined(with: .opacity))
                }
            }
            .animation(.snappy(duration: 0.2), value: canSend)
            .padding(.horizontal, 4)
            .background(
                Color(.secondarySystemBackground),
                in: RoundedRectangle(cornerRadius: 22, style: .continuous)
            )
            .padding(.horizontal, 12)
            .padding(.top, 6)
            .padding(.bottom, 8)
            // Presented from the composer row rather than from the menu item,
            // for the reason on `showingPhotoPicker`. Still `PhotosPicker`
            // rather than `PHPickerViewController`
            // (docs/tasks/ios-image-input.md step 1) — no Info.plist
            // usage-description string needed either way.
            .photosPicker(
                isPresented: $showingPhotoPicker, selection: $selectedPhotoItems,
                maxSelectionCount: max(1, ChatMessage.maxImages - pendingImages.count), matching: .images
            )
            .onChange(of: selectedPhotoItems) { _, items in
                guard !items.isEmpty else { return }
                Task {
                    // In pick order. A pick that fails to load or decode is
                    // dropped on its own and the rest still attach, but it is
                    // said out loud, as on the web: one missing thumbnail in
                    // six is easy to miss, and the caption may count them.
                    var loaded: [Data] = []
                    for item in items {
                        if let data = try? await item.loadTransferable(type: Data.self),
                           let downscaled = ImageDownscaler.downscaledJPEG(from: data) {
                            loaded.append(downscaled)
                        }
                    }
                    appendPendingImages(loaded)
                    let failed = items.count - loaded.count
                    if failed > 0 {
                        store.showPermissionNotice(
                            failed == 1 ? "Couldn't read one of those photos." : "Couldn't read \(failed) of those photos."
                        )
                    }
                    selectedPhotoItems = []
                }
            }
            // Full screen, not a sheet: the capture UI is built to own the
            // whole display, and a sheet leaves the shutter under the grabber.
            .fullScreenCover(isPresented: $showingCamera) {
                CameraPicker(
                    onCapture: { image in
                        // Off the main thread: a camera image is always the
                        // full sensor (12-48MP), unlike a pasted screenshot,
                        // and redrawing it would hitch the cover's dismissal.
                        Task.detached(priority: .userInitiated) {
                            let downscaled = ImageDownscaler.downscaledJPEG(from: image)
                            await MainActor.run {
                                if let downscaled { appendPendingImages([downscaled]) }
                            }
                        }
                    },
                    onFinish: { showingCamera = false }
                )
                .ignoresSafeArea()
            }
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                clearUnreadBadge()
                store.handleDidBecomeActive()
                Task { await ideasStore.refresh() }
                Task { await pullPreferences() }
                // A box can be updated while the app sits in the background.
                // The present covers news that waited behind something else.
                Task { await whatsNew.refresh() }
                presentWhatsNewIfDue()
                // The initial `.active` never reaches `onChange`; launch is
                // covered by the `.task` below.
                Self.ensureDictationAssets()
            } else if newPhase == .background {
                // True backgrounding only — not `.inactive`, which also
                // fires for transient interruptions (Control Center, a
                // system alert) that don't mean the user left the app and
                // shouldn't cut off an in-progress dictation.
                dictation.stop()
            }
        }
        .task { Self.ensureDictationAssets() }
        // Launch fetch; later foregrounds go through `onChange(of: scenePhase)`.
        .task { await ideasStore.refresh() }
        .task { await pullPreferences() }
        .task { await whatsNew.refresh() }
        .onChange(of: whatsNew.pending) { _, _ in presentWhatsNewIfDue() }
        .animation(.snappy(duration: 0.25), value: ideasStore.spotlight?.id)
        // Collapsed "Not now" rows are for the panel's open lifetime only.
        .onChange(of: showingIdeas) { _, showing in
            if !showing { ideasStore.panelClosed() }
        }
        .onChange(of: store.completedAssistantTurns) { _, completed in
            guard !completed.isEmpty else { return }
            // Drain unconditionally, before the scene-phase check: a turn that
            // completed while the app was backgrounded had its sound played by
            // APNs, and leaving it queued would sound it a second time on the
            // next completion.
            let turnIds = store.takeCompletedAssistantTurns()
            guard scenePhase == .active else { return }
            var needsForegroundSound = false
            // No short-circuit: every id has to be consumed, or a suppression
            // marker outlives the turn it belongs to.
            for turnId in turnIds where !TurnReadySoundTracker.shared.consume(turnId) {
                needsForegroundSound = true
            }
            // Marked BEFORE the scene-phase guard below, deliberately. A turn
            // that completes while the app is away is announced by the
            // app-icon badge — and `clearUnreadBadge()` wipes that the instant
            // Mike opens the app, which is also the instant he is looking at
            // wherever he left the transcript. Put the mark behind the guard
            // and the one arrival he was *most* likely to miss is the only one
            // with no indicator at all. `isFarFromBottom` holds its last value
            // across backgrounding, which is exactly the question being asked:
            // was he scrolled up when he left.
            if isFarFromBottom { hasUnseenArrival = true }
            // Not in voice mode: the reply finishes being written long before
            // it finishes being spoken, so the chime landed mid-sentence.
            if needsForegroundSound && !showingVoiceMode {
                ChatSoundPlayer.playReceived()
                // Inside the same suppression check as the sound, not beside
                // it. A turn APNs already announced got the system's own
                // notification haptic when the banner arrived; buzzing again
                // here would be the second of two taps for one event.
                ChatHaptics.replyFinished()
            }
        }
        // The reply beginning to write itself — Mike's ask, 2026-09-11: the
        // wait between send and any visible answer is the whole of Marlow's
        // thinking time, and on a phone that lives on silent nothing marked its
        // end, because `ChatSoundPlayer` is suppressed by the ringer switch and
        // there was never a sound for this moment anyway. See `ChatHaptics` for
        // why this fires unconditionally rather than "only when silent."
        //
        // Drained like `completedAssistantTurns` above and for the identical
        // reason — the poll loop republishes several times a second, and a
        // queue that is read without being emptied buzzes on every tick.
        .onChange(of: store.startedAssistantReplies) { _, started in
            guard !started.isEmpty else { return }
            let ids = store.takeStartedAssistantReplies()
            // Voice mode has its own cues (earcons, the button).
            guard scenePhase == .active, !ids.isEmpty, !showingVoiceMode else { return }
            // One tap however many replies began in the same publish. The
            // server runs one turn at a time, so replies normally start one by
            // one, but a drain that fired per id would turn any future change
            // there into a burst of haptics, and "it started" is not a
            // countable event the way an arrival is.
            ChatHaptics.replyStarted()
        }
        .onAppear {
            // Only when the app is genuinely in front. A `turn_ready` push
            // carries `content-available`, so iOS can launch a terminated app
            // straight into the background to handle it — and SwiftUI runs
            // `onAppear` for that background scene just the same. Unguarded,
            // this cleared the badge APNs had set moments earlier, from a
            // launch the user never saw: the banner arrived and the icon
            // stayed clean (Mike, on device, 2026-08-23).
            guard scenePhase == .active else { return }
            clearUnreadBadge()
        }
        // A new approval request taps the phone. Keyed on the request id, so
        // the poll loop republishing the same pending request every couple of
        // seconds does not buzz again; `condition` skips the "cleared" edge.
        // Fires whether the card or the dictation pill is what shows it — the
        // point is that something needs attention.
        .sensoryFeedback(.warning, trigger: store.pendingPermission?.requestId) { _, new in new != nil }
        // The card's sound, as well as its buzz. Voice mode plays its own,
        // and none while dictating: the card isn't shown then (the pill is),
        // and the chime would land in the recording.
        .onChange(of: store.pendingPermission?.requestId) { _, new in
            if new != nil && !showingVoiceMode && !dictation.isRecording { ChatSoundPlayer.playApproval() }
        }
        // No navigation bar: `PresenceHeader` floats over the transcript and
        // carries everything the bar did — the face and the gear (the
        // "Sonnet/Low" diagnostic Mike asked for on 2026-08-27 lived here as
        // a chip until 2026-10-12; it is in Settings now). Hidden rather than
        // restyled because a bar reserves its height whether or not anything
        // scrolls under it, and the point of floating is that the transcript
        // gets that height back.
        .toolbar(.hidden, for: .navigationBar)
        // One `.sheet`, not two independent ones: SwiftUI only ever presents
        // one sheet at a time from a given view, and stacking a second
        // `.sheet(isPresented:)` for Settings alongside the permission
        // sheet let Settings silently block an incoming permission request
        // until dismissed — up to the server's ~55s auto-deny timeout, on a
        // modal that's meant to be un-swipeable and unmissable
        // (docs/tasks/ios-permission-round-trip-ui.md §4).
        //
        // The approval is no longer one of the sheets (2026-10-02: a card
        // over the conversation, `PermissionOverlay`, below), but the lesson
        // stands: nothing presented here may sit on top of it. So
        // `activeSheet` is nil while a request is pending, which makes SwiftUI
        // dismiss a Settings/Marlow sheet that is open, and the card sits in
        // ChatView's own view tree where no sheet can cover it. The flags are
        // left alone for that dismissal — the setter below only clears them
        // when the user dismissed — so answering the request brings Settings
        // back underneath, rather than dumping the user out to the transcript.
        .sheet(item: Binding(get: { activeSheet }, set: {
            if $0 == nil && store.pendingPermission == nil {
                showingSettings = false
                showingMarlow = false
                showingIdeas = false
                // OK and a swipe both land here.
                // Not on the approval card's dismissal, which skips this
                // block: the card comes back once the request is answered.
                if showingWhatsNew {
                    whatsNew.acknowledge()
                    showingWhatsNew = false
                }
            }
        })) { sheet in
            switch sheet {
            case .settings:
                SettingsView(store: store, whatsNew: whatsNew, stopDictation: { dictation.stop() })
            case .marlow:
                MarlowView(marlow: makeMarlowStore(), activity: makeActivityStore(), usage: makeUsageStore())
            case .ideas:
                IdeasPanel(store: ideasStore, onDoIt: doIt)
            case .whatsNew:
                WhatsNewSheet(store: whatsNew)
            }
        }
        // Above everything in ChatView, the floating `PresenceHeader`
        // included, and an overlay rather than a row so the transcript's
        // layout is untouched: no scroll jump when a request arrives or is
        // answered. Not while dictating — see `permissionPillRequest`.
        // Under the approval card, never over it: a request that arrives
        // while Mike is talking has to show, with its tap, on this screen.
        .overlay {
            if showingVoiceMode {
                VoiceModeView(store: store) { showingVoiceMode = false }
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: showingVoiceMode)
        .overlay {
            PermissionOverlay(
                request: permissionCardRequest,
                onDecision: { decision in await store.answerPermission(decision: decision) },
                onExpire: { store.expirePermissionSheet(requestId: $0.requestId) }
            )
        }
    }

    /// Sends what's in the text field and clears it.
    ///
    /// The focus drop and the run-loop hop are both load-bearing, and both are
    /// about *keyboard* dictation — the mic on Apple's own keyboard, not our
    /// push-to-dictate button, which never touches `draft` at all. While a
    /// keyboard dictation session is live the text field holds text that
    /// hasn't been committed to the binding yet, and the session commits it a
    /// beat *after* anything we write. Clearing `draft` directly from here
    /// therefore got undone: the turn sent, then the dictated text reappeared
    /// in the field (found on device, 2026-08-11, sending mid-dictation
    /// without stopping the keyboard mic first).
    ///
    /// Dropping focus ends that session and forces the commit; the hop lets
    /// the commit land in `draft` before it's read. Ordered this way the
    /// final dictated words get *sent* rather than deleted — the reverse
    /// (clear again on the next turn) would have silently eaten them.
    private func submitDraft() {
        draftFocused = false
        DispatchQueue.main.async {
            let text = draft
            // Captured and cleared in the same hop as `draft`
            // (docs/tasks/ios-image-input.md trap #3) — an attached image
            // must ride the same run-loop hop as the text it was cleared
            // alongside, or a send could carry one without the other.
            let images = pendingImages
            draft = ""
            pendingImages = []
            if store.send(text: text, images: images) {
                ChatSoundPlayer.playSent()
                ChatHaptics.sent()
                scrollToBottomRequest += 1
            } else {
                // `send` refused (nothing sendable once a photo failed to
                // save). Put what was staged back rather than swallowing it.
                draft = text
                pendingImages = images
            }
        }
    }

    /// Do it on an idea (panel card or spotlight): closes the panel, drops the
    /// card, and sends `Do it` (quoting the idea) through the same path as the composer so
    /// it is Mike's bubble and polls like any other turn. The plan does not ride
    /// in the text: `idea_id` lets the server tell Marlow which idea, and what
    /// it offered. If the send is refused the card is still gone, as it would be
    /// after any optimistic tap; it returns on the next launch.
    private func doIt(_ idea: Idea) {
        showingIdeas = false
        ideasStore.accept(idea)
        if store.send(text: IdeaFormat.doItMessage, ideaId: idea.id, quote: IdeaQuote(idea)) {
            ChatSoundPlayer.playSent()
            ChatHaptics.sent()
            scrollToBottomRequest += 1
        }
    }

    /// Routes a finished dictation to whichever endpoint ended it.
    ///
    /// `.review` appends rather than replaces: the draft can already hold
    /// typed text ("remind me to" + a dictated sentence), and a second
    /// dictation should extend the first rather than delete it.

    /// The persistent server states currently true, in precedence order.
    ///
    /// `ChatStatus.allCases` is declared in precedence order, so filtering it
    /// yields an already-ranked list and there is no second place where the
    /// ranking is written down to drift out of step with the first.
    private var activeStatuses: [ChatStatus] {
        ChatStatus.allCases.filter { status in
            switch status {
            case .reauthNeeded: return store.reauthNeeded
            case .queuePaused: return store.queuePaused
            // Suppressed by the hard failure rather than merely outranked by
            // it: once reauthorization is actually needed, "expires soon" is no
            // longer a warning about the future, it is a stale description of
            // the past. `ChatStore` already clears this on the same health
            // poll; the guard is here too because a strip that can show both
            // must never be able to show them as two separate live facts.
            case .reauthPrewarn: return store.reauthPrewarn && !store.reauthNeeded
            // Recomputed against now on every body evaluation, so a window
            // that has reset clears the row on the next redraw, not only at
            // the next fetch.
            case .usageHigh: return usageHighText != nil
            }
        }
    }

    /// The words of the `usageHigh` row, nil when no window is at the
    /// threshold. The facts come from `ChatStore.usageReport`; the 90% and the
    /// sentence are `UsageFormat`'s.
    private var usageHighText: String? {
        UsageFormat.highUsage(store.usageReport).map(UsageFormat.highUsageText)
    }

    /// Whether the send button is shown.
    ///
    /// An attached photo with no caption must still be sendable
    /// (`/code-review medium` finding: this used to gate on draft text alone,
    /// so picking a photo and never typing anything left no way to send it at
    /// all).
    ///
    /// The empty-draft half has to be skipped while dictating: the draft is
    /// *always* empty then, so keeping it would hide the button at exactly the
    /// moment it has a job — finish and send.
    private var canSend: Bool {
        if dictation.isTranscribing { return false }
        if dictation.isRecording { return true }
        return !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !pendingImages.isEmpty
    }

    /// Whether another image can be staged; says so when it cannot. The cap is
    /// `ChatMessage.maxImages`, the server's own limit (a 7th would be a 400
    /// after the upload).
    private func hasRoomForAnotherImage() -> Bool {
        guard pendingImages.count >= ChatMessage.maxImages else { return true }
        store.showPermissionNotice("You can attach up to \(ChatMessage.maxImages) photos to a message.")
        return false
    }

    /// Appends, never replaces, up to the cap; anything past it is dropped with
    /// the same notice. The picker's own `maxSelectionCount` already stops that
    /// in the common case; this covers the camera finishing late and a stale count.
    private func appendPendingImages(_ images: [Data]) {
        let room = max(0, ChatMessage.maxImages - pendingImages.count)
        pendingImages.append(contentsOf: images.prefix(room))
        if images.count > room {
            store.showPermissionNotice("You can attach up to \(ChatMessage.maxImages) photos to a message.")
        }
    }

    private func receiveDictation(_ text: String, endpoint: DictationController.Endpoint) {
        switch endpoint {
        case .review:
            appendToDraft(text)
            draftFocused = true
        case .send:
            // Send means "send what I have staged" — all of it. The dictation,
            // any photo, and any text already typed in the field, captured and
            // cleared in one go so a send can't carry some of it and abandon
            // the rest (docs/tasks/ios-image-input.md trap #3). Leaving the
            // draft behind was the subtler half: nothing clears it, so it
            // silently prepends to the *next* message, one turn later and
            // impossible to attribute.
            let outgoing = joined(draft, text)
            let images = pendingImages
            draft = ""
            pendingImages = []
            if store.send(text: outgoing, images: images) {
                ChatSoundPlayer.playSent()
                ChatHaptics.sent()
                scrollToBottomRequest += 1
            } else {
                // `send` refused. Fall back to the review path rather than
                // dropping it — a minute of speech must never vanish.
                // Restores the combined text, which is what he'd want to retry.
                draft = outgoing
                pendingImages = images
            }
        }
    }

    /// Adds dictated text to whatever is already in the field rather than
    /// replacing it. Typing "remind me to" and then dictating the rest is a
    /// real flow, and so is dictating twice; either one losing the earlier half
    /// would be the same silent-loss bug this whole change exists to end.
    private func appendToDraft(_ text: String) {
        draft = joined(draft, text)
    }

    private func joined(_ existing: String, _ addition: String) -> String {
        existing.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? addition
            : existing + " " + addition
    }

    /// IDs of messages that should show a timestamp divider above them — the
    /// first message, and any message landing more than 30 minutes after the
    /// one before it. One O(n) pass, read once per body evaluation rather
    /// than recomputed per row.
    private var messageIdsNeedingTimestamp: Set<UUID> {
        var result: Set<UUID> = []
        var previous: ChatMessage?
        for message in store.visibleMessages {
            if previous == nil || message.createdAt.timeIntervalSince(previous!.createdAt) > 30 * 60 {
                result.insert(message.id)
            }
            previous = message
        }
        return result
    }

    /// Mike's message id -> the failed reply placeholder it pairs with, for
    /// the Messages-style failed send (Mike, 2026-09-28, on device): the
    /// retry arrow beside HIS bubble and the reason under it, rather than a
    /// Retry button down on the placeholder row near the bottom of the screen.
    /// Only pairs with both halves in the window; a placeholder whose message
    /// has scrolled out keeps its own inline Retry, so retry is never lost.
    private var failedReplyByUserMessage: [UUID: UUID] {
        Self.failedReplies(in: store.visibleMessages)
    }

    static func failedReplies(in messages: some Collection<ChatMessage>) -> [UUID: UUID] {
        let visible = Set(messages.lazy.filter { $0.role == .user }.map(\.id))
        var result: [UUID: UUID] = [:]
        for message in messages where message.role == .assistant && message.status == .error {
            if let userId = message.clientMsgId.flatMap(UUID.init(uuidString:)), visible.contains(userId) {
                result[userId] = message.id
            }
        }
        return result
    }

    /// Keeps the reader where they were. Rows inserted above leave the
    /// scroll offset measured from the top unchanged, so without this the
    /// newly revealed rows would slide in under the finger and the message
    /// just read would be pushed a screen or more down. Unanimated and a
    /// run-loop turn later, for the same reasons as the jump button: this is
    /// where the view already was, and the rows must exist before there is a
    /// position to scroll to.
    private func revealEarlierMessages(proxy: ScrollViewProxy) {
        let anchor = store.visibleMessages.first?.id
        store.showEarlierMessages()
        guard let anchor else { return }
        DispatchQueue.main.async {
            proxy.scrollTo(anchor, anchor: .top)
        }
    }

    /// True while the followed reply is still the newest row and still in
    /// flight. `.stalled` counts: the turn is still running, only polled
    /// more slowly.
    private var followedReplyIsStreaming: Bool {
        guard let exchange = followedExchange,
              let last = store.messages.last, last.id == exchange.replyId
        else { return false }
        switch last.status {
        case .sending, .running, .stalled: return true
        case .done, .error: return false
        }
    }

    /// Carries out `ReplyFollow` for a frame of row geometry. Runs on every
    /// scroll frame while following, so it writes no `@State` except to end
    /// following, which happens once.
    ///
    /// **Only growth is news.** A scroll moves both rows together and leaves
    /// the span from the message's top to the reply's bottom unchanged, so a
    /// frame whose span (and viewport) matches the last one is skipped. That
    /// is what keeps this from cutting short the send's own animated scroll,
    /// and from reissuing a `scrollTo` it has already made.
    ///
    /// The scrolls are unanimated: each is a line's worth of growth, typed
    /// in, and setting the offset outright is what lands — an animated
    /// `scrollTo` is discarded during deceleration (see the jump button), and
    /// would restart itself on every token.
    private func followStreamingReply(frames: [UUID: CGRect], proxy: ScrollViewProxy) {
        guard let exchange = followedExchange,
              let user = frames[exchange.userId], let reply = frames[exchange.replyId],
              let replyMessage = store.messages.last, replyMessage.id == exchange.replyId
        else { return }
        let geometry = ReplyFollow.Geometry(
            userTop: user.minY, userHeight: user.height,
            replyTop: reply.minY, replyBottom: reply.maxY,
            visibleTop: 0, visibleBottom: readableHeight
        )
        // Called first, and always, so the baseline keeps moving.
        let grew = followTracker.isNews(geometry)
        // Nothing to follow until Marlow has written something. Before that,
        // the only changes are the send's own arrival — row heights
        // interpolating under `MessageRow.arrivalAnimation` read as growth,
        // and following them cut the animated send-scroll short with a snap
        // (simulator, 2026-09-29). The send's scroll already shows both rows.
        let hasWords = !(replyMessage.streamedText ?? "").isEmpty
            || replyMessage.status == .done || replyMessage.status == .error
        guard grew, hasWords else { return }
        let action = ReplyFollow.action(for: geometry)
        switch action {
        case .hold: break
        case .followReply: proxy.scrollTo(exchange.replyId, anchor: .bottom)
        case .pinUser: proxy.scrollTo(exchange.userId, anchor: .top)
        case .pinReply: proxy.scrollTo(exchange.replyId, anchor: .top)
        }
        if action.endsFollowing { followedExchange = nil }
    }

    /// Gets iOS 26's SpeechTranscriber model onto the phone before the first
    /// dictation needs it. The OS owns the download; until it lands, dictation
    /// uses the old recognizer (`AppleEngineSelector`) rather than waiting.
    private static func ensureDictationAssets() {
        guard #available(iOS 26, *) else { return }
        Task.detached(priority: .utility) { await SpeechTranscriberAssets.ensureInstalled() }
    }

    private func clearUnreadBadge() {
        Task {
            try? await UNUserNotificationCenter.current().setBadgeCount(0)
        }
    }

    private enum ActiveSheet: Identifiable {
        case settings
        case marlow
        case ideas
        case whatsNew

        var id: String {
            switch self {
            case .settings: return "settings"
            case .marlow: return "marlow"
            case .ideas: return "ideas"
            case .whatsNew: return "whatsNew"
            }
        }
    }

    /// Raises the card if there is news and nothing else is in the way. Never
    /// over another sheet, voice mode or a recording: the card is not urgent,
    /// and it waits for the next foreground rather than interrupting.
    ///
    /// Synchronous and called from `onChange`, not at the end of the fetch:
    /// the first version ran these checks inside the `.task` after the await,
    /// and on Mike's phone (2026-10-23) the card never came up at launch
    /// although the store had news and Settings listed it. Either the view
    /// copy the task captured still held the launch-time `scenePhase`, or the
    /// initial `.active` reached `onChange` too and its refresh bailed on
    /// `isFetching`; driven from `pending` changing, neither can happen. No
    /// `scenePhase` check either: a card raised while the scene is still
    /// becoming active is simply there when it appears.
    private func presentWhatsNewIfDue() {
        guard !whatsNew.pending.isEmpty, !showingWhatsNew,
              !showingSettings, !showingMarlow, !showingIdeas, !showingVoiceMode,
              !dictation.isRecording
        else { return }
        showingWhatsNew = true
    }

    /// Nil while a permission request is pending, for the reason on the
    /// `.sheet` above: the sheet gets out of the way of the approval card and
    /// returns when it is answered. The Marlow sheet (activity, schedule,
    /// memory, You, Soul; ux-lane.md §3a) is in the same single sheet, so it
    /// cannot block a request either.
    private var activeSheet: ActiveSheet? {
        if store.pendingPermission != nil { return nil }
        if showingSettings { return .settings }
        if showingMarlow { return .marlow }
        if showingIdeas { return .ideas }
        if showingWhatsNew { return .whatsNew }
        return nil
    }

    /// The request to show as a card: the pending one, unless Mike is
    /// mid-dictation. A card landing on a recording used to stop it (reported
    /// 2026-10-02: "popped up mid-dictation and cancelled it halfway"), so
    /// while the recorder is running the request waits behind
    /// `permissionPillRequest` instead, and the card appears the moment the
    /// recording ends by any route. Derived from the recorder's own state
    /// rather than latched, so there is no flag to fall out of step with it.
    private var permissionCardRequest: PermissionRequestInfo? {
        dictation.isRecording ? nil : store.pendingPermission
    }

    /// The small tappable stand-in for the card while recording.
    private var permissionPillRequest: PermissionRequestInfo? {
        dictation.isRecording ? store.pendingPermission : nil
    }

    /// Takes up the face and Thinking level picked on another client. A
    /// changed face brings the home-screen icon along now: there is no Done
    /// on this path.
    private func pullPreferences() async {
        #if DEBUG
        // A fixture carries its own `-marlowFace`; the box must not replace it.
        if UIFixture.state != nil { return }
        #endif
        guard let deviceToken = session.deviceToken else { return }
        if await PreferenceSync.pull(api: APIClient(), deviceToken: deviceToken).contains(.face) {
            await AppIconSync.activated()
        }
    }

    private func makeActivityStore() -> ActivityStore {
        #if DEBUG
        if UIFixture.state != nil {
            return ActivityStore(api: UIFixture.ActivityFixture(), deviceToken: "fixture")
        }
        #endif
        return ActivityStore(deviceToken: session.deviceToken ?? "")
    }

    private func makeUsageStore() -> UsageStore {
        #if DEBUG
        if UIFixture.state != nil {
            return UsageStore(api: UIFixture.UsageFixture(), deviceToken: "fixture")
        }
        #endif
        return UsageStore(deviceToken: session.deviceToken ?? "")
    }

    private func makeMarlowStore() -> MarlowStore {
        #if DEBUG
        if UIFixture.state != nil {
            return MarlowStore(api: UIFixture.MarlowFixture(), deviceToken: "fixture")
        }
        #endif
        return MarlowStore(deviceToken: session.deviceToken ?? "")
    }
}

/// The idea a "Do it" answers, inside Mike's bubble above his words: a thin
/// bar, then emoji and title, smaller and secondary. `.secondary` over the
/// accent-tinted bubble stays legible in light and dark; the bar is the label
/// colour at low opacity, not the accent, which would vanish into the tint.
private struct IdeaQuoteBlock: View {
    let quote: IdeaQuote

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(Color.primary.opacity(0.35))
                .frame(width: 3)
            Text("\(quote.emoji) \(quote.title)")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .lineLimit(3)
                .multilineTextAlignment(.leading)
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Quoting: \(quote.title)")
    }
}

/// Sits above the oldest rendered row while older rows exist. A tap rather
/// than load-on-scroll-to-top: loading on scroll needs to know when the top
/// row appeared, and row-visibility signals are exactly what proved
/// unreliable on device here (the `ChatView` type comment).
private struct EarlierMessagesButton: View {
    let action: () -> Void

    var body: some View {
        Button("Show earlier messages", action: action)
            .font(.footnote)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 4)
    }
}

/// Renders the transcript's first frame already at the bottom, instead of
/// laying it out at the top and then scrolling to it.
///
/// **This is not what fixed the cold-launch black screen** — it was added
/// believing it would, and the device run that carried it was still black. The
/// cause was `LazyVStack` never building rows at all; see the `ChatView` type
/// comment. What this does is narrower and still worth having: with no
/// `scrollTo` on the launch path there is no row-position dependency there at
/// all, which is one less thing to be wrong.
///
/// **`for: .initialOffset` is load-bearing, and is why this needs iOS 18.**
/// The iOS 17 `defaultScrollAnchor(_:)` has no role parameter and applies to
/// every role at once — including `.sizeChanges`, which would re-anchor the
/// transcript to the bottom every time a streaming reply grows a line. That
/// silently defeats the `!isFarFromBottom` guard on `messages.count` and the
/// whole "stay put while reading older history" behaviour, so taking the
/// unscoped modifier as a stand-in on 17 would trade this bug for a worse one.
/// iOS 17 keeps the deferred `scrollTo` instead.
private struct InitialBottomScrollAnchor: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.defaultScrollAnchor(.bottom, for: .initialOffset)
        } else {
            content
        }
    }
}

/// Mike's message and the reply placeholder a local send created.
private struct FollowedExchange: Equatable {
    let userId: UUID
    let replyId: UUID

    func contains(_ id: UUID) -> Bool { id == userId || id == replyId }
}

/// Remembers the last geometry `ChatView.followStreamingReply` acted on, so a
/// frame that is only a scroll can be told apart from one where the reply
/// grew. A class, for the reason on `ChatView.followTracker`.
private final class FollowTracker {
    private var last: (span: CGFloat, visibleTop: CGFloat, visibleBottom: CGFloat)?

    /// The first measurement is recorded, not acted on: it lands while the
    /// send's animated scroll to the new rows is still under way, and that
    /// scroll already shows them.
    func isNews(_ g: ReplyFollow.Geometry) -> Bool {
        let now = (span: g.replyBottom - g.userTop, visibleTop: g.visibleTop, visibleBottom: g.visibleBottom)
        defer { last = now }
        guard let last else { return false }
        return abs(now.span - last.span) > ReplyFollow.tolerance
            || now.visibleTop != last.visibleTop
            || now.visibleBottom != last.visibleBottom
    }
}

/// Frames of the followed exchange's two rows, in the `"transcript"` space.
/// Only those two rows report, and only while following. A row and its
/// timestamp divider report under the same id and are unioned, because
/// that pair is what `scrollTo` moves.
private enum FollowedRowFramesKey: PreferenceKey {
    static var defaultValue: [UUID: CGRect] = [:]

    static func reduce(value: inout [UUID: CGRect], nextValue: () -> [UUID: CGRect]) {
        value.merge(nextValue()) { $0.union($1) }
    }
}

/// Reports a view's frame for following, while `isActive`. Inactive, it
/// adds no reader at all.
private struct FollowFrameReporter: ViewModifier {
    let id: UUID
    let isActive: Bool

    func body(content: Content) -> some View {
        content.background {
            if isActive {
                GeometryReader { g in
                    Color.clear.preference(
                        key: FollowedRowFramesKey.self,
                        value: [id: g.frame(in: .named("transcript"))]
                    )
                }
            }
        }
    }
}

/// Ends following when a finger lands on the transcript. On iOS 18 that is
/// the scroll phase — `.tracking` is a touch before any movement, which is
/// exactly "stop" — and costs nothing when not following. iOS 17 has no
/// scroll phase, so it falls back to a simultaneous drag, which only notices
/// once the finger moves.
private struct TouchEndsFollowing: ViewModifier {
    let isFollowing: Bool
    let stop: () -> Void

    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.onScrollPhaseChange { _, phase in
                guard isFollowing else { return }
                if phase == .tracking || phase == .interacting { stop() }
            }
        } else {
            content.simultaneousGesture(
                DragGesture().onChanged { _ in if isFollowing { stop() } },
                including: isFollowing ? .all : .subviews
            )
        }
    }
}

private enum TranscriptBottomKey: PreferenceKey {
    static var defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}

private enum ChatSoundPlayer {
    // Undocumented system-sound mappings: 1004 is the sent-message tone and
    // 1007 is a received-message tone on current iOS. Apple does not promise
    // these IDs are stable, so an OS update could change or remove them; that
    // trade is intentional for now rather than bundling copies of system
    // sounds. Audio Services honors the silent switch and does not take over
    // dictation's audio session.
    private static let sent: SystemSoundID = 1004
    private static let received: SystemSoundID = 1007

    static func playSent() { AudioServicesPlaySystemSound(sent) }
    static func playReceived() { AudioServicesPlaySystemSound(received) }

    /// Voice mode's "needs your OK" tink-tink, so the one sound means an
    /// approval card everywhere (Mike, 2026-10-21: "it will train me").
    /// Registered from the synthesised WAV once, through Audio Services for
    /// the same silent-switch and session reasons as above.
    @MainActor static func playApproval() {
        if approval == nil {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("approval-chime.wav")
            var id: SystemSoundID = 0
            if (try? Earcons.wav(for: .approval).write(to: url)) != nil,
               AudioServicesCreateSystemSoundID(url as CFURL, &id) == kAudioServicesNoError {
                approval = id
            }
        }
        if let approval { AudioServicesPlaySystemSound(approval) }
    }

    @MainActor private static var approval: SystemSoundID?
}

/// Scrolling volume bars while dictating — the only feedback that the mic is
/// actually picking anything up, since there's no transcript preview.
/// `levels` is a fixed-size rolling window (oldest first); each bar height
/// tracks one slot, so newly-arriving levels visibly scroll in from the
/// trailing edge.
struct LevelMeterView: View {
    @ObservedObject var levelMeter: LevelMeter
    /// Voice mode draws it inside the lit button, where accent-on-accent
    /// would vanish.
    var color: Color = .accentColor

    var body: some View {
        GeometryReader { geometry in
            HStack(alignment: .center, spacing: 3) {
                ForEach(Array(levelMeter.levels.enumerated()), id: \.offset) { _, level in
                    Capsule()
                        .fill(color)
                        .frame(height: barHeight(for: level, in: geometry.size.height))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .animation(.linear(duration: 0.05), value: levelMeter.levels)
    }

    private func barHeight(for level: Float, in maxHeight: CGFloat) -> CGFloat {
        // A floor so silence still reads as "a bar," not a gap.
        let minHeight: CGFloat = 3
        return max(minHeight, CGFloat(level) * maxHeight)
    }
}

/// A spinner at the foot of the conversation while the app is fetching a turn
/// the user did not ask for — the morning briefing, a reminder, anything
/// arriving behind a "Marlow has something for you" push.
///
/// The gap it fills: tapping that notification on a poor connection opened a
/// transcript with nothing new in it and no sign anything was in flight, for
/// as long as the fetch (`getPending` then, the transcript sync now) took to
/// answer — up to URLSession's 60s default,
/// since it passes no timeout of its own (Mike, 2026-09-07).
///
/// A failed pass keeps the row and swaps in a line of text, rather than
/// vanishing. A spinner that simply disappears having shown nothing is the
/// blank screen this exists to fix, arrived at more slowly.
private struct FetchIndicator: View {
    let activity: ConnectionActivity

    var body: some View {
        switch activity {
        case .idle:
            EmptyView()
        case .checking:
            row {
                ProgressView()
                    .controlSize(.mini)
            }
            .accessibilityLabel("Checking for new messages")
        case .unreachable:
            row {
                Image(systemName: "wifi.slash")
                Text("Can't reach Marlow")
            }
            .accessibilityLabel("Can't reach Marlow. Still trying.")
        }
    }

    private func row(@ViewBuilder _ content: () -> some View) -> some View {
        HStack(spacing: 6) {
            content()
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity)
        .padding(.top, 4)
        .padding(.bottom, 2)
        .accessibilityElement(children: .ignore)
    }
}

/// A persistent server state worth a permanent strip at the top of the chat.
///
/// **Declaration order is precedence order** (highest first) and is the one
/// place it is written down — `ChatView.activeStatuses` filters `allCases` and
/// inherits it. The order is Mike's, from
/// docs/tasks/ios-chat-ux-gaps.md item 13, and it ranks by what he has to *do*
/// rather than by severity in the abstract: reauthorization is work he must go
/// and perform before anything else works at all, a paused queue means turns
/// are being accepted and not run, and the pre-warn is a heads-up about a
/// deadline that has not arrived.
///
/// Everything here is a fact about the server that stays true until the server
/// changes. Anything that expires on a timer is a `TransientNotice` instead and
/// belongs next to whatever caused it, not up here.
private enum ChatStatus: Int, CaseIterable, Identifiable {
    case reauthNeeded
    case queuePaused
    case reauthPrewarn
    /// Last on purpose: running out of plan is a hard stop, but the warning
    /// is a nudge about a window, below everything that is broken now. The
    /// only usage text that appears in the chat at all (Mike, 2026-10-04);
    /// the rest lives in the sheet.
    case usageHigh

    var id: Int { rawValue }

    /// `usageText` is the one case whose words depend on data, so it is
    /// passed in rather than held here, which keeps this enum payload-free
    /// (`CaseIterable` is what ranks the strip).
    func text(usageText: String?) -> String {
        switch self {
        case .reauthNeeded: return "Marlow needs reauthorization — SSH in and run `claude`."
        case .queuePaused: return "Marlow is paused."
        case .reauthPrewarn: return "Marlow's Claude session expires soon — SSH in and run `claude`."
        case .usageHigh: return usageText ?? ""
        }
    }

    var color: Color {
        switch self {
        case .reauthNeeded: return .red
        case .queuePaused, .reauthPrewarn: return .orange
        case .usageHigh: return .yellow
        }
    }

    /// White on yellow is unreadable; the other rows keep their white.
    var foreground: Color {
        self == .usageHigh ? .black : .white
    }
}

/// At most one persistent status row, with the rest reachable behind a tap.
///
/// The old code stacked an independent `Banner` per state, so a bad moment —
/// paused *and* pre-warned, or reauth arriving on top of a pause — spent three
/// or four full-width rows of the transcript saying things that are mostly one
/// piece of news. This shows the highest-precedence one and a "+N" control, and
/// expands in place when tapped.
///
/// **Nothing here can be dismissed**, only collapsed, and that is the rule item
/// 13 names explicitly: the top state is routinely the one that means Marlow
/// cannot work at all until Mike goes and fixes it, and a strip he can swipe
/// away is a strip that is gone the next time he needs it. Collapsing hides a
/// *lower*-ranked state behind a visible count; it never hides the fact that
/// something is wrong.
private struct StatusStrip: View {
    let statuses: [ChatStatus]
    let usageText: String?
    @State private var expanded = false

    var body: some View {
        // The `Group` is load-bearing, and the version without it was wrong.
        // With the `.onChange` attached to the VStack *inside* the `if`, the
        // one transition that most needs to reset `expanded` — the last status
        // clearing, so `statuses.first` goes nil — removes the observer in the
        // same update that should have fired it. `expanded` then stays true
        // with nothing under it, and the next unrelated state to arrive shows
        // up pre-expanded. Hanging the observer on a wrapper that outlives the
        // content is what makes the reset unconditional.
        Group {
            if let top = statuses.first {
                VStack(spacing: 0) {
                    Banner(
                        text: top.text(usageText: usageText), color: top.color, foreground: top.foreground,
                        trailing: extraCount > 0 ? disclosure(foreground: top.foreground) : nil
                    )
                    if expanded {
                        ForEach(statuses.dropFirst()) { status in
                            Banner(text: status.text(usageText: usageText), color: status.color, foreground: status.foreground)
                        }
                    }
                }
            }
        }
        // Keyed on the statuses, not on `expanded` alone: there is nothing to
        // expand *into* below two, so anything that drops the count collapses
        // the strip rather than leaving a chevron pointing at nothing.
        .onChange(of: statuses) { _, new in
            if new.count < 2 { expanded = false }
        }
    }

    private var extraCount: Int { max(0, statuses.count - 1) }

    private func disclosure(foreground: Color) -> some View {
        Button {
            expanded.toggle()
        } label: {
            HStack(spacing: 2) {
                Text("+\(extraCount)")
                Image(systemName: expanded ? "chevron.up" : "chevron.down")
            }
            .font(.footnote.weight(.semibold))
            .foregroundStyle(foreground)
            // The white-on-colour hit area has no edge of its own, so it needs
            // a real one to be tappable at the size VoiceOver and a thumb both
            // expect.
            .padding(.horizontal, 8)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .accessibilityLabel(
            expanded
            ? "Hide \(extraCount) more status \(extraCount == 1 ? "message" : "messages")"
            : "Show \(extraCount) more status \(extraCount == 1 ? "message" : "messages")"
        )
    }
}

/// A short-lived line of feedback about something the user just did, shown
/// beside the control that caused it rather than in the status strip at the
/// top (docs/tasks/ios-chat-ux-gaps.md item 13).
///
/// Visually quiet on purpose — no fill, no full-width colour bar. The whole
/// point of the split is that "you allowed that tool" and "Marlow needs
/// reauthorization" should not look like the same class of thing, and giving
/// this one the same slab treatment in a different colour would have kept the
/// problem and only repainted it.
private struct TransientNotice: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.horizontal)
            .padding(.vertical, 4)
    }
}

private struct Banner<Trailing: View>: View {
    let text: String
    let color: Color
    var foreground: Color = .white
    /// The "+N" disclosure, on the top row of a multi-status strip only.
    /// Generic rather than `AnyView` so the common no-accessory case costs no
    /// type erasure, and defaulted via the `EmptyView` extension below so call
    /// sites that never wanted one don't have to say so.
    let trailing: Trailing?

    var body: some View {
        HStack(spacing: 8) {
            Text(text)
                .font(.footnote.weight(.medium))
                .foregroundStyle(foreground)
                .frame(maxWidth: .infinity, alignment: .leading)
                // The padding lives on the TEXT, not on the row. Putting it on
                // the row meant conditioning it away to 0 whenever a 44pt
                // disclosure was setting the height — which is fine for the
                // one-line English strings, and wrong the moment Dynamic Type
                // wraps the reauth message to three lines: they then sat
                // flush against the red edge, top and bottom. This way the
                // text always breathes and the disclosure's own minHeight
                // still sets the floor.
                .padding(.vertical, 8)
            if let trailing { trailing }
        }
        .padding(.horizontal, 8)
        .background(color)
    }
}

extension Banner where Trailing == EmptyView {
    init(text: String, color: Color, foreground: Color = .white) {
        self.init(text: text, color: color, foreground: foreground, trailing: nil)
    }
}

/// Shown above a message when it lands more than 30 minutes after the one
/// before it — a burst of replies a few minutes apart doesn't need one
/// between each, but a message opened cold the next morning should say when
/// it actually arrived rather than just falling at the top of the list.
private struct TimestampDivider: View {
    let date: Date

    var body: some View {
        Text(Self.label(for: date))
            .font(.caption2)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity)
            .padding(.top, 4)
    }

    private static func label(for date: Date) -> String {
        let calendar = Calendar.current
        let time = date.formatted(.dateTime.hour().minute())
        if calendar.isDateInToday(date) {
            return time
        }
        if calendar.isDateInYesterday(date) {
            return "Yesterday, \(time)"
        }
        if let daysAgo = calendar.dateComponents([.day], from: date, to: .now).day, daysAgo < 7 {
            return "\(date.formatted(.dateTime.weekday(.wide))), \(time)"
        }
        return "\(date.formatted(.dateTime.month(.abbreviated).day())), \(time)"
    }
}

private struct MessageRow: View {
    let message: ChatMessage
    let onRetry: (UUID) -> Void
    let loadImage: (String) -> UIImage?
    /// On Mike's bubble: the failed reply to retry from the arrow beside it.
    var failedReplyId: UUID? = nil
    /// On the failed reply: its bubble is on screen and carries the arrow,
    /// so this row is only the reason, right-aligned under that bubble.
    var isAttachedFailure: Bool = false
    /// Only the two rows of an exchange `ChatView` is following after a send
    /// measure themselves; every other row stays reader-free.
    var reportsFrame = false

    /// Retrying only makes sense for a turn that originated on this device —
    /// `clientMsgId` is how `retry(_:)` finds the original text to resend.
    /// An ingested cron turn that errored has none, and there is nothing to
    /// resend it *as*. The only call site is already inside the `.error`
    /// branch below, so that half of the condition doesn't need repeating
    /// here.
    private var canRetry: Bool {
        message.clientMsgId != nil
    }

    /// Every non-terminal state gets a spinner, `.stalled` included: the app is
    /// still polling in all three, and `.stalled` is the one where that is
    /// least obvious — after a relaunch resumes it, a static line of text is
    /// indistinguishable from an abandoned message.
    /// The streamed reply, broken at the offsets where a tool call interrupted
    /// it. One piece for the 89% of turns that write a single block; more only
    /// when the model went back to a tool mid-answer, where running the two
    /// halves together would read as one garbled sentence.
    private var streamedSegments: [String] {
        guard let streamed = message.streamedText, !streamed.isEmpty else { return [] }
        return PartialTextPlaybackState.segments(
            of: streamed, splitAt: message.streamedTextMarks ?? []
        )
    }

    /// Non-terminal: the transcript shows progress rather than an answer.
    private var isInFlight: Bool {
        switch message.status {
        case .sending, .running, .stalled: return true
        case .done, .error: return false
        }
    }

    private var pendingLabel: String? {
        switch message.status {
        case .sending: return "Sending…"
        case .running:
            // Once the reply is typing itself, the text IS the status, and a
            // spinner saying "Thinking…" under it says strictly less than the
            // words moving above it (Mike, 2026-09-02, after watching a search
            // turn: "if the client is rendering typewriter text, there's little
            // point showing a status").
            //
            // A TOOL label is the exception and is deliberately kept: during an
            // interleaved turn the text stops mid-answer while the model goes
            // back to a tool, and that label is then the only thing on screen
            // explaining why. Silence there would recreate the stall this whole
            // feature exists to remove.
            if hasStreamedText, TurnActivity.describesUnseenWork(message.activityLabel) {
                return nil
            }
            return message.activityLabel ?? TurnActivity.thinking
        case .stalled: return message.text
        case .done, .error: return nil
        }
    }

    private var hasStreamedText: Bool { !streamedSegments.isEmpty }

    /// Both sides get a bubble again (Mike, 2026-09-28, trying it on device):
    /// The 2026-09-08 UX pass took Marlow's away for width (ios-chat-ux-gaps item 12), and
    /// the transcript read as a document rather than a conversation. This
    /// version keeps that width: Marlow's bubble can run the full column,
    /// where Mike's keeps `userGutter` on the left. It first kept a 24pt gap
    /// on Marlow's right too, but replies are mostly full-width cards, and on
    /// a phone that gap read as the cards sitting off centre (the Android user on
    /// Android, 2026-10-19); the user's gutter alone carries the "two people
    /// talking" cue. A short reply still hugs the left.
    ///
    /// Only what was *said* goes in a bubble. The working indicator, the
    /// stalled note and a failed turn's error sit under it, bare: they are
    /// the app talking, not Marlow, and a gray bubble round a spinner is the
    /// "typing" bubble `WorkingIndicator` exists to avoid.
    private var isUser: Bool { message.role == .user }
    private let userGutter: CGFloat = 40
    private let bubbleShape = RoundedRectangle(cornerRadius: 18, style: .continuous)

    /// Whether there is anything to put in a bubble yet. An assistant
    /// placeholder that has not streamed a word is just its status line, and
    /// a turn that finished with no text (`resultText ?? ""`: a tools-only
    /// turn) gets no bubble at all rather than an empty gray pill.
    private var hasSpeech: Bool {
        if isUser { return true }
        if isInFlight { return hasStreamedText }
        return message.status != .error && !message.text.isEmpty
    }

    /// A bubble arriving grows out of its speaker's bottom corner — Mike's
    /// next to the send button, Marlow's from the left — rather than
    /// appearing in one frame (Mike, 2026-09-29). Two ways in, both keyed so
    /// a cold launch and "Load earlier" stay still, since neither changes
    /// the key:
    ///  - a new row (a send, a cron turn landing): `ChatView` animates on
    ///    the transcript's last id, and this transition is on the row root;
    ///  - Marlow's first words in an existing placeholder: the bubble is
    ///    inserted when `hasSpeech` flips, animated on that flip below.
    /// Streaming text itself is not animated — it already types in.
    static let arrivalAnimation = Animation.snappy(duration: 0.25)

    private var arrival: AnyTransition {
        .asymmetric(
            insertion: .scale(scale: 0.92, anchor: isUser ? .bottomTrailing : .bottomLeading)
                .combined(with: .opacity),
            removal: .opacity
        )
    }

    var body: some View {
        if isAttachedFailure {
            attachedFailure
        } else {
            row
        }
    }

    private var row: some View {
        HStack(alignment: .center, spacing: 0) {
            if isUser { Spacer(minLength: userGutter) }
            VStack(alignment: .leading, spacing: 6) {
                if hasSpeech { bubble.transition(arrival) }
                status
            }
            .animation(Self.arrivalAnimation, value: hasSpeech)
            if let failedReplyId {
                // Beside the bubble, like a send that failed in Messages:
                // the failure belongs to this message, and the arrow is here
                // rather than at the foot of the screen (Mike, 2026-09-28).
                Button {
                    onRetry(failedReplyId)
                } label: {
                    Image(systemName: "arrow.clockwise.circle.fill")
                        .font(.title2)
                        .foregroundStyle(.red)
                }
                .buttonStyle(.borderless)
                .padding(.leading, 6)
                .accessibilityLabel("Retry")
            }
            if !isUser { Spacer(minLength: 0) }
        }
        // Inside the row, ahead of `.transition`, so the arrival transition
        // stays the outermost thing on the view `ForEach` inserts.
        .modifier(FollowFrameReporter(id: message.id, isActive: reportsFrame))
        .transition(arrival)
    }

    /// Reads as a caption on the bubble above it, the way Messages says "Not
    /// Delivered": trailing, small, and pulled up into the 12pt row gap.
    private var attachedFailure: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Spacer(minLength: userGutter)
            Image(systemName: "exclamationmark.circle.fill")
            Text(message.text)
                .multilineTextAlignment(.trailing)
        }
        .font(.footnote)
        .foregroundStyle(.red)
        .padding(.top, -8)
        .contextMenu { copyMenu }
    }

    /// The message's images: one is shown whole, as it always was; two or more
    /// are a square-cropped grid inside the same 220pt width (2 side by side,
    /// 3 across, 4 as 2x2, 5-6 as three across in two rows). Crops are the
    /// bubble's only; the model gets the full images. A server row's slots come
    /// from its attachment ids, so the grid keeps its final shape while images
    /// download and a failed one stays a visible gray tile, as on the web.
    @ViewBuilder
    private var imageGrid: some View {
        let slots: [String] = message.allAttachmentIds.isEmpty
            ? message.allImageFilenames
            : message.allAttachmentIds.map { "\($0).jpg" }
        let loaded: [UIImage?] = slots.map { loadImage($0) }
        let images: [UIImage?] = loaded
        if images.count == 1 {
            if let only = images[0] {
                Image(uiImage: only)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: 220, maxHeight: 220)
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            } else {
                // Not downloaded yet: a gray square, as on the web (120px),
                // rather than nothing and then a 220pt jump when it lands.
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color.gray.opacity(0.25))
                    .frame(width: 120, height: 120)
            }
        } else if images.count > 1 {
            let spacing: CGFloat = 4
            let columns = ChatMessage.gridColumns(forImageCount: images.count)
            let cell = (220 - spacing * CGFloat(columns - 1)) / CGFloat(columns)
            let rows: [[UIImage?]] = stride(from: 0, to: images.count, by: columns).map {
                Array(images[$0..<min($0 + columns, images.count)])
            }
            VStack(alignment: .leading, spacing: spacing) {
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    HStack(spacing: spacing) {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, image in
                            if let image {
                                Image(uiImage: image)
                                    .resizable()
                                    .scaledToFill()
                                    .frame(width: cell, height: cell)
                                    .clipped()
                            } else {
                                Rectangle()
                                    .fill(Color.gray.opacity(0.25))
                                    .frame(width: cell, height: cell)
                            }
                        }
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
    }

    private var bubble: some View {
        VStack(alignment: .leading, spacing: 8) {
            imageGrid
            if let quote = message.quote, isUser {
                IdeaQuoteBlock(quote: quote)
            }
            if isInFlight {
                // The reply as it is being written, when there is one
                // (docs/tasks/streaming-reply-text.md). Keyed off
                // `isInFlight` rather than off `pendingLabel` being
                // non-nil: the label now goes away mid-turn while the text
                // is still arriving, and hanging the streamed text off it
                // would take the answer with it.
                ForEach(Array(streamedSegments.enumerated()), id: \.offset) { _, segment in
                    MarkdownMessageView(text: segment, cached: false)
                }
            } else {
                // Rendered markdown (docs/tasks/ios-chat-ux-gaps.md §4) — Marlow
                // replies in markdown by default, so raw syntax showed on every
                // message. Fenced code stays selectable inside the renderer
                // itself; this contextMenu additionally covers whole-message
                // copy (§2) so a command buried in prose is still one long-press
                // away without hunting for the exact code span.
                MarkdownMessageView(text: message.text)
            }
        }
        // No `maxWidth: .infinity`: a short reply gets a short bubble, as in
        // any messaging app. A long one still reaches the gutter, because
        // the text wraps at the width the HStack offers it.
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        // 0.4, up from 0.15 (Mike, 2026-09-29): at 0.15 on black his bubble
        // was only just distinguishable from Marlow's gray, and "I wrote
        // this" should read at a glance. Still a tint, not solid accent —
        // the label stays the primary colour and legible in both modes.
        .background(
            isUser ? Color.accentColor.opacity(0.4) : Color(.secondarySystemBackground),
            in: bubbleShape
        )
        // The long-press lift takes the bubble's own outline instead of a
        // rectangle round it.
        .contentShape(.contextMenuPreview, bubbleShape)
        .contextMenu { copyMenu }
    }

    /// Whole-message Copy (§2), on the bubble and on a failed turn's error
    /// alike — the error string is exactly what gets pasted into a
    /// debugging session, and it lost its menu when it moved out of the
    /// bubble (`/code-review medium` finding, 2026-09-29).
    @ViewBuilder
    private var copyMenu: some View {
        if !isInFlight {
            Button {
                UIPasteboard.general.string = message.text
            } label: {
                Label("Copy", systemImage: "doc.on.doc")
            }
        }
    }

    /// An unattached failure — its user message has scrolled out of the
    /// window, or it has none (an ingested cron turn) — keeps its own
    /// inline reason and Retry, so retry is never lost.
    @ViewBuilder
    private var status: some View {
        if isInFlight {
            if message.status == .stalled {
                // Stalled keeps its words: the explanation that
                // checking continues in the background IS the
                // content, and `ChatPresence` leaves the header idle
                // for this state rather than repeat a claim nobody is
                // checking.
                HStack(spacing: 6) {
                    ProgressView()
                    Text(message.text)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .padding(.leading, 4)
            } else if let pendingLabel {
                // The words moved to the header, which does not scroll
                // away; this marks where the answer will land. Same
                // visibility rule as before — gone while text types,
                // back when a tool interrupts it.
                WorkingIndicator(label: pendingLabel)
                    .padding(.leading, 4)
            }
        } else if message.status == .error {
            // The iMessage affordance (docs/tasks/ios-chat-ux-gaps.md
            // §1) — a red exclamation, not just red text, so a failed
            // send reads as "something you can act on" at a glance
            // rather than blending into any other error copy.
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "exclamationmark.circle.fill")
                    Text(message.text)
                }
                .font(.footnote)
                .foregroundStyle(.red)
                if canRetry {
                    Button {
                        onRetry(message.id)
                    } label: {
                        Label("Retry", systemImage: "arrow.clockwise")
                            .font(.footnote.weight(.medium))
                    }
                    .buttonStyle(.borderless)
                }
            }
            .padding(.leading, 4)
            .contextMenu { copyMenu }
        }
    }
}

#Preview {
    NavigationStack {
        ChatView(deviceToken: "preview-token", location: LocationProvider())
            .environmentObject(SessionStore())
            .environmentObject(LocationProvider())
    }
}
