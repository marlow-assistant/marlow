#if DEBUG
import Foundation
import os

/// A paired-looking chat with no server, for screenshotting the UI in the
/// simulator: `-uiFixture empty|idle|working|streaming|interrupted|failed|follow|approval|approvalBash|ideas|markdown` as a launch
/// argument (`xcrun simctl launch <device> com.mikemason.Marlow -uiFixture
/// working`). Debug builds only.
///
/// Why it exists: the chat screen only renders once paired, and pairing needs
/// the box. Before this, every look at a UI change went through Mike's phone.
/// The phone is still the real test for anything touching scroll or layout —
/// the simulator has lied about the transcript before (`ChatView`'s type
/// comment) — but "what does the header say while a tool runs" does not need a
/// device, and should not cost Mike a round trip.
///
/// The transcript is written to a temporary directory, never the app's real
/// one, so running a fixture cannot touch a paired install's history. Content
/// is generic on purpose: screenshots may end up in a PR.
///
/// **Only the chat's store is fake.** The session, Keychain and preferences
/// are the install's real ones, which is why `SettingsView` disables its whole
/// form under a fixture: its Unpair would sign a paired phone out for real
/// (`/code-review medium` finding, 2026-09-28). Anything new that writes real
/// state from a screen reachable here needs the same guard.
///
/// `follow` is the one fixture that moves: a long scrollback where a send
/// succeeds and its reply streams in over about fifteen seconds, then
/// finishes — for watching `ReplyFollow` chase a growing reply. The
/// simulator can show *where* the view goes; how it feels under a thumb is
/// still a phone question.
///
/// `approval` and `approvalBash` are `working` plus one pending tool-approval
/// request, held for ~55s from launch (or until answered), so the approval
/// card can be screenshotted without a server. `approval` is an Edit with a
/// diff preview and an "allow similar" scope; `approvalBash` is a Bash command
/// with a Marlow-says quotation and no preview.
///
/// `ideas` is `idle` plus a spotlight card above the composer and a lightbulb
/// with its dot; the panel behind it holds a handful of ideas, one nearing its
/// `until`. A "Do it" sends and starts a turn that never finishes. The other
/// states have no ideas, so their screenshots carry no card.
///
/// `markdown` is `idle` plus a reply exercising the block renderer: a GFM
/// table with alignment, inline formatting and cells too long for the bubble.
///
/// `whatsNew` is `idle` plus the "Marlow was updated" card, as on a device
/// that has never shown it: one scripted server entry and this build's own.
enum UIFixture {
    enum State: String {
        case empty, idle, working, streaming, interrupted, failed, follow, approval, approvalBash, ideas, markdown, whatsNew
    }

    /// Read from the launch argument, which UserDefaults exposes in its
    /// volatile argument domain — nothing is written to disk.
    static var state: State? {
        UserDefaults.standard.string(forKey: "uiFixture").flatMap(State.init(rawValue:))
    }

    static let turnId = "fixture-turn"

    /// A day and a half of generic activity for the pane behind the avatar.
    /// Times are relative to launch so "Today" and "Yesterday" are real.
    struct ActivityFixture: ActivityAPI {
        func activity(limit: Int, before: Double?, deviceToken: String) async throws -> ActivityPage {
            let now = Date().timeIntervalSince1970
            let hour = 3600.0
            let entries = [
                ActivityEntry(
                    turnId: "f1", source: "chat", label: nil,
                    startedAt: now - 0.5 * hour - 20, finishedAt: now - 0.5 * hour,
                    tools: [
                        ActivityToolCall(name: "WebSearch", target: "flights to Halifax in May", at: nil),
                        ActivityToolCall(name: "Read", target: "memory/travel.md", at: nil),
                        ActivityToolCall(name: "Edit", target: "memory/travel.md", at: nil),
                    ],
                    approvals: [ActivityApproval(tool: "Edit", decision: "allow")],
                    files: ["memory/travel.md"]
                ),
                ActivityEntry(
                    turnId: "f2", source: "cron", label: "morning",
                    startedAt: now - 5 * hour - 14, finishedAt: now - 5 * hour,
                    tools: [
                        ActivityToolCall(name: "mcp__claude_ai_Gmail__search_threads", target: nil, at: nil),
                        ActivityToolCall(name: "mcp__claude_ai_Google_Calendar__list_events", target: nil, at: nil),
                        ActivityToolCall(name: "Read", target: "memory/projects.md", at: nil),
                    ]
                ),
                ActivityEntry(
                    turnId: "f3", source: "cron", label: "consolidate",
                    startedAt: now - 11 * hour - 95, finishedAt: now - 11 * hour,
                    tools: [
                        ActivityToolCall(name: "Read", target: "memory/MEMORY.md", at: nil),
                        ActivityToolCall(name: "Read", target: "memory/people.md", at: nil),
                        ActivityToolCall(name: "Read", target: "memory/people.md", at: nil),
                        ActivityToolCall(name: "Edit", target: "memory/people.md", at: nil),
                    ],
                    files: ["memory/MEMORY.md", "memory/people.md"],
                    tier: "plumbing"
                ),
                ActivityEntry(
                    turnId: "f3b", source: "cron", label: "ideas",
                    startedAt: now - 11.5 * hour - 60, finishedAt: now - 11.5 * hour,
                    tools: [ActivityToolCall(name: "Read", target: "ideas/ideas.json", at: nil)],
                    tier: "plumbing", hasOutput: true
                ),
                ActivityEntry(
                    turnId: "f4", source: "cron", label: "physio-nudge",
                    startedAt: now - 30 * hour - 4, finishedAt: now - 30 * hour
                ),
                ActivityEntry(
                    turnId: "f5", source: "chat", label: nil,
                    startedAt: now - 32 * hour - 40, finishedAt: now - 32 * hour, status: "error",
                    tools: [ActivityToolCall(name: "Bash", target: nil, at: nil)],
                    denials: ["Bash"]
                ),
            ]
            return ActivityPage(entries: before == nil ? entries : [], nextBefore: nil)
        }

        func activityOutput(turnId: String, deviceToken: String) async throws -> ActivityOutput {
            ActivityOutput(text: "Added one idea about the spring trip; retired two stale ones.")
        }
    }

    /// A mid-day reading for the Usage panel: the 5-hour window high enough to
    /// show the yellow bar, a conversation part-way to its tidy-up. Times are
    /// relative to launch so "resets 8:59 PM" and "Started 6:00 AM" hold.
    struct UsageFixture: UsageAPI {
        func usage(deviceToken: String) async throws -> UsageReport {
            let now = Date().timeIntervalSince1970
            let hour = 3600.0
            return UsageReport(
                plan: UsagePlan(
                    observedAt: now - 0.2 * hour,
                    windows: [
                        "five_hour": UsageWindow(utilization: 0.93, resetsAt: now + 1.5 * hour),
                        "seven_day": UsageWindow(utilization: 0.41, resetsAt: now + 52 * hour),
                    ]
                ),
                conversation: UsageConversation(
                    contextTokens: 61_000, observedAt: now - 0.1 * hour, compactionThresholdTokens: 110_000,
                    startedAt: now - 9 * hour, compactions: 1
                ),
                marlow: [
                    "five_hour": UsageBuckets(
                        chat: UsageBucket(turns: 12, weightedTokens: 90_000),
                        scheduled: UsageBucket(turns: 6, weightedTokens: 12_000)
                    ),
                    "seven_day": UsageBuckets(
                        chat: UsageBucket(turns: 60, weightedTokens: 400_000),
                        scheduled: UsageBucket(turns: 70, weightedTokens: 380_000),
                        compaction: UsageBucket(turns: 3, weightedTokens: 30_000)
                    ),
                ],
                subscriptionType: "pro"
            )
        }
    }

    /// Ideas for `-uiFixture ideas`, none for any other state. Dates are
    /// relative to launch so the "until" marker and the hiding hold. The
    /// responses are accepted and forgotten: this is for looking at.
    struct IdeasFixture: IdeasAPI {
        func ideas(deviceToken: String) async throws -> [Idea] {
            guard UIFixture.state == .ideas else { return [] }
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.dateFormat = "yyyy-MM-dd"
            func day(_ offset: Int) -> String {
                formatter.string(from: Calendar.current.date(byAdding: .day, value: offset, to: Date())!)
            }
            return [
                Idea(
                    id: "dinner-invites", section: "now", emoji: "\u{1F37D}\u{FE0F}",
                    title: "I can draft the dinner invites and rank the dates",
                    plan: "Three of your friends replied about Saturday. I'll draft the invite for the two dates that work for most, and put the draft in front of you before anything is sent.",
                    category: "People", source: "Email from Sam, Tuesday", until: day(1), added: day(0), spotlight: true
                ),
                Idea(
                    id: "insurance-quote", section: "now", emoji: "\u{1F697}",
                    title: "Give me the renewal notice and I'll find a cheaper quote",
                    plan: "I'll compare it against a couple of other insurers and write up what differs. Nothing is bought or booked.",
                    category: "Money", source: "Renewal notice in your inbox", until: day(9), added: day(-1)
                ),
                Idea(
                    id: "expired", section: "now", emoji: "\u{1F4C5}",
                    title: "This one is past its date and should not show",
                    plan: "Hidden by the phone without waiting for the nightly run.",
                    category: "Home & Admin", source: "Calendar", until: day(-1), added: day(-3)
                ),
                Idea(
                    id: "trip-plan", section: "standing", emoji: "\u{1F5FA}\u{FE0F}",
                    title: "I can plan a weekend away around your calendar",
                    plan: "Tell me roughly where and how far. I'll find the free weekends, sketch two itineraries, and leave booking to you.",
                    category: "Travel", added: day(-6)
                ),
                Idea(
                    id: "weekly-digest", section: "standing", emoji: "\u{1F4DD}",
                    title: "I can write you a Friday summary of the week",
                    plan: "What got done, what is waiting on someone else, and what is next. About a page, in your inbox draft folder.",
                    category: "Projects", added: day(-6)
                ),
            ]
        }

        func dismissIdea(id: String, reason: String?, deviceToken: String) async throws {}
    }

    /// The chat screen's ideas store under a fixture. Its own defaults suite,
    /// cleared at launch, so "last opened" never touches the real install's and
    /// the dot is lit on every launch of `ideas`.
    @MainActor
    static func makeIdeasStore() -> IdeasStore {
        let defaults = UserDefaults(suiteName: "ui-fixture-ideas") ?? .standard
        defaults.removeObject(forKey: IdeasStore.lastOpenedKey)
        return IdeasStore(api: IdeasFixture(), deviceToken: "fixture", defaults: defaults)
    }

    struct ChangesFixture: ChangesAPI {
        func changes(deviceToken: String) async throws -> ChangesResponse {
            ChangesResponse(changes: [ChangeEntry(
                id: 1_000_001, date: "2026-10-23", part: "server", category: "access",
                text: "Marlow can now read your Google Drive when you ask it to. It asks first, the same way as for email."
            )])
        }
    }

    /// The update card under `whatsNew`: the fixture's server entry plus this
    /// build's bundled ones, as on a device that has never shown it. Every
    /// other state gets an empty store, so their screenshots carry no card.
    @MainActor
    static func makeWhatsNewStore() -> WhatsNewStore {
        let defaults = UserDefaults(suiteName: "ui-fixture-whats-new") ?? .standard
        defaults.removeObject(forKey: WhatsNewStore.seenKey)
        defaults.removeObject(forKey: WhatsNewStore.mutedKey)
        guard state == .whatsNew else {
            struct NoChanges: ChangesAPI {
                func changes(deviceToken: String) async throws -> ChangesResponse { ChangesResponse(changes: []) }
            }
            return WhatsNewStore(api: NoChanges(), deviceToken: "fixture", defaults: defaults, bundled: [])
        }
        return WhatsNewStore(api: ChangesFixture(), deviceToken: "fixture", defaults: defaults)
    }

    /// Marlow's files and schedules for the sheet behind the avatar. Times are
    /// relative to launch so "Next: tomorrow 8:00" and "Changed 3 days ago" hold.
    struct MarlowFixture: MarlowAPI {
        private static let day = 86400.0

        private static let texts: [String: (kind: IdentityKind, text: String)] = [
            "SOUL.md": (.soul, """
            ---
            name: soul
            ---
            # Soul

            Marlow is a calm, direct assistant. It leads with the answer and keeps replies short unless asked for more.

            ## Values

            - Be honest about what it doesn't know.
            - Ask before doing anything that can't be undone.
            - Treat the user's time as the scarce thing.

            Marlow doesn't flatter. When something is a bad idea it says so once, kindly, and then helps anyway.
            """),
            "USER.md": (.user, """
            # About the user

            Works in software and prefers plain answers over surveys of options.

            ## Routines

            1. Mornings are for planning; afternoons for building.
            2. Physio exercises every evening.

            Lives in Toronto and travels to Alberta a few times a year.
            """),
            "CLAUDE.md": (.instructions, """
            # How Marlow works

            Marlow reads this file at the start of every conversation.

            ## Tools

            - Reads and writes its own memory files.
            - Checks Gmail and Calendar for the morning briefing.

            ## Rules

            Never send a message on the user's behalf without asking first.
            """),
            "memory/travel.md": (.memory, """
            ---
            description: Trips, bookings and preferences for flights and hotels
            ---
            # Travel

            Prefers aisle seats and morning departures.

            - Halifax in May: flights still to be booked.
            - Alberta trip in June: hotel confirmed.
            """),
            "memory/projects.md": (.memory, "# Projects\n\nA short list of what is in flight, with the next step for each."),
            "memory/people.md": (.memory, "# People\n\nFamily, friends and colleagues, and what to remember about each."),
            "memory/marlow-features.md": (.memory, "# Marlow features\n\nIdeas for what the assistant could do next."),
            "memory/health.md": (.memory, "# Health\n\nPhysio plan and appointments."),
            "cron/morning.md": (.job, "# Morning briefing\n\nCheck Gmail and Calendar, then write a short summary of the day ahead."),
            "cron/consolidate.md": (.job, "# Nightly tidy\n\nMerge duplicate memory entries and fix anything out of date."),
            "cron/alberta-weekly.md": (.job, "# Alberta weekly\n\nA short roundup of the week's local news."),
            "cron/physio-nudge.md": (.job, "# Physio nudge\n\nRemind the user to do their exercises."),
            "cron/weekly-digest.md": (.job, "# Weekly digest\n\nSummarise the week: what shipped, what is open, what is next."),
            "cron/launch-followup.md": (.job, "# Launch follow-up\n\nCheck whether the launch was announced and report back."),
        ]

        func identity(deviceToken: String) async throws -> IdentityIndex {
            let now = Date().timeIntervalSince1970
            let days: [String: Double] = [
                "SOUL.md": 20, "USER.md": 6, "CLAUDE.md": 12,
                "memory/travel.md": 0.5, "memory/projects.md": 3, "memory/people.md": 9,
                "memory/marlow-features.md": 1, "memory/health.md": 15,
            ]
            let descriptions: [String: String] = [
                "memory/travel.md": "Trips, bookings and preferences for flights and hotels",
                "memory/projects.md": "What is in flight right now, and the next step for each project",
                "memory/people.md": "Family, friends and colleagues, and what to remember about each",
                "memory/marlow-features.md": "Ideas for what the assistant could do next",
            ]
            let ordered = ["SOUL.md", "USER.md", "CLAUDE.md", "memory/travel.md", "memory/projects.md",
                           "memory/people.md", "memory/marlow-features.md", "memory/health.md"]
            let files = ordered.map { id in
                IdentityFileInfo(
                    id: id, kind: Self.texts[id]!.kind,
                    modifiedAt: now - (days[id] ?? 1) * Self.day,
                    size: Self.texts[id]!.text.utf8.count, description: descriptions[id]
                )
            }
            // Next fires come from the calendar rather than hand arithmetic, so
            // the fixture never shows "Mondays at 8:03 · Next: tomorrow" on a
            // Tuesday, which reads as a bug in the real formatting.
            let calendar = Calendar.current
            func nextFire(weekday: Int? = nil, hour: Int, minute: Int) -> Double {
                var match = DateComponents(hour: hour, minute: minute)
                match.weekday = weekday
                let date = calendar.nextDate(after: Date(), matching: match, matchingPolicy: .nextTime)!
                return date.timeIntervalSince1970
            }
            func job(
                _ id: String, _ cron: String, _ recurrence: Recurrence,
                weekday: Int? = nil, hour: Int, minute: Int
            ) -> ScheduleEntry {
                ScheduleEntry(
                    id: id, cron: cron, at: nil, recurrence: recurrence,
                    nextFireAt: nextFire(weekday: weekday, hour: hour, minute: minute),
                    notify: nil, prompt: nil, job: id, jobFile: "cron/\(id).md"
                )
            }
            // One-offs sit N days out at 8:15, with `at` written from that
            // same date so the row agrees with itself.
            let atFormatter = DateFormatter()
            atFormatter.locale = Locale(identifier: "en_US_POSIX")
            atFormatter.dateFormat = "yyyy-MM-dd'T'HH:mmZ"
            func once(_ id: String, daysOut: Int, notify: String?, job: Bool) -> ScheduleEntry {
                let date = calendar.date(
                    bySettingHour: 8, minute: 15, second: 0,
                    of: calendar.date(byAdding: .day, value: daysOut, to: Date())!
                )!
                return ScheduleEntry(
                    id: id, cron: nil, at: atFormatter.string(from: date), recurrence: .once,
                    nextFireAt: date.timeIntervalSince1970,
                    notify: notify, prompt: nil, job: job ? id : nil, jobFile: job ? "cron/\(id).md" : nil
                )
            }
            let entries = [
                job("consolidate", "0 1 * * *", .daily(times: ["01:00"]), hour: 1, minute: 0),
                job("morning", "0 8 * * *", .daily(times: ["08:00"]), hour: 8, minute: 0),
                job("alberta-weekly", "3 8 * * 1", .weekly(weekdays: [1], times: ["08:03"]),
                    weekday: 2, hour: 8, minute: 3),
                job("weekly-digest", "0 18 * * 0", .weekly(weekdays: [0], times: ["18:00"]),
                    weekday: 1, hour: 18, minute: 0),
                job("physio-nudge", "57 20 * * *", .daily(times: ["20:57"]), hour: 20, minute: 57),
                once("launch-followup", daysOut: 4, notify: nil, job: true),
                once("starlink-reminder", daysOut: 20, notify: "Downgrade Starlink to paused mode today.", job: false),
                once("trip-mode-trial", daysOut: 2, notify: "Trip mode trial — cancel it, or sign up properly.", job: false),
            ]
            return IdentityIndex(
                files: files,
                schedules: ScheduleIndex(timezone: "America/Toronto", rejected: nil, entries: entries)
            )
        }

        func identityFile(id: String, deviceToken: String) async throws -> IdentityFile {
            guard let entry = Self.texts[id] else { throw URLError(.fileDoesNotExist) }
            return IdentityFile(
                id: id, kind: entry.kind, modifiedAt: Date().timeIntervalSince1970 - 2 * Self.day,
                text: entry.text, truncated: false
            )
        }
    }

    @MainActor
    static func makeStore(state: State) -> ChatStore {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ui-fixture-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let persistURL = directory.appendingPathComponent("transcript.json")
        let seeded = ChatStore.TranscriptCache(
            latestSeq: nil, hasOlderOnServer: nil, messages: seedMessages(state: state)
        )
        if let data = try? JSONEncoder().encode(seeded) {
            try? data.write(to: persistURL)
        }
        return ChatStore(
            deviceToken: "fixture-token",
            client: FixtureChatAPI(state: state),
            persistURL: persistURL,
            attachmentsDirectoryURL: directory.appendingPathComponent("attachments", isDirectory: true),
            preferences: UserDefaults(suiteName: "ui-fixture") ?? .standard,
            inbox: { [:] }
        )
    }

    private static func seedMessages(state: State) -> [ChatMessage] {
        guard state != .empty else { return [] }
        let now = Date()
        func at(_ minutesAgo: Double) -> Date { now.addingTimeInterval(-minutesAgo * 60) }
        if state == .follow {
            // Enough scrollback that a send lands with history above it.
            return (0..<24).map { i in
                ChatMessage(
                    id: UUID(), role: i.isMultiple(of: 2) ? .user : .assistant,
                    text: i.isMultiple(of: 2)
                        ? "Earlier question number \(i / 2 + 1)?"
                        : "An earlier answer, long enough to wrap onto a second line so the scrollback has some height to it.",
                    createdAt: at(Double(60 - i)), turnId: i.isMultiple(of: 2) ? nil : "h\(i)", status: .done
                )
            }
        }
        var messages = [
            ChatMessage(
                id: UUID(), role: .assistant, text: """
                **Morning.** Nothing on the calendar before noon.

                - Two emails worth a look, both replies you owe
                - Rain from about 3pm, so the walk is better early
                - The library hold is ready for pickup
                """,
                createdAt: at(190), turnId: "t1", status: .done
            ),
            ChatMessage(
                id: UUID(), role: .user, text: "Which two emails?",
                createdAt: at(12), turnId: nil, status: .done
            ),
            ChatMessage(
                id: UUID(), role: .assistant, text: """
                The contractor asking to move Thursday's visit, and the renewal notice for the \
                car insurance — it auto-renews on the 14th unless you say otherwise.
                """,
                createdAt: at(11.5), turnId: "t2", status: .done
            ),
        ]
        if state == .ideas {
            // A Do it already sent: Mike quoting the idea, "Do it" under it.
            messages.append(ChatMessage(
                id: UUID(), role: .user, text: IdeaFormat.doItMessage, createdAt: at(4), turnId: nil, status: .done,
                quote: IdeaQuote(
                    ideaId: "library-hold", emoji: "\u{1F4DA}",
                    title: "I can renew your library hold before it lapses and tell you when the new copy is ready for pickup"
                )
            ))
            messages.append(ChatMessage(
                id: UUID(), role: .assistant, text: "Renewed. The new due date is the 29th.",
                createdAt: at(3.5), turnId: "t3", status: .done
            ))
        }
        if state == .markdown {
            messages.append(ChatMessage(
                id: UUID(), role: .user, text: "Compare the three insurance quotes?",
                createdAt: at(4), turnId: nil, status: .done
            ))
            messages.append(ChatMessage(
                id: UUID(), role: .assistant, text: """
                Here's how the three quotes compare:

                | Insurer | Annual | Deductible | Notes |
                |:--------|-------:|:----------:|-------|
                | **Current** | $1,240 | $500 | Auto-renews on the 14th unless you say otherwise |
                | Harbour Mutual | $1,085 | $1,000 | *Cheapest*, but the deductible doubles |
                | Northline | $1,130 | $500 | Same cover as now; quote valid until `Oct 30` |

                Northline is the like-for-like saving, about **$110** a year.
                """,
                createdAt: at(3.5), turnId: "t3", status: .done
            ))
        }
        guard state != .idle, state != .ideas, state != .markdown, state != .whatsNew else { return messages }
        let userMessage = ChatMessage(
            id: UUID(), role: .user, text: "Can you find a cheaper quote for the insurance?",
            createdAt: at(0.2), turnId: nil, status: .done
        )
        messages.append(userMessage)
        // `failed` is terminal, so launch reconciliation leaves it alone and
        // nothing polls: the Messages-style failed send, held for a screenshot.
        let failed = state == .failed
        messages.append(ChatMessage(
            id: UUID(), role: .assistant,
            text: failed ? "Anthropic's API timed out — try again." : "",
            createdAt: at(0.2), turnId: turnId,
            status: failed ? .error : .running, clientMsgId: userMessage.id.uuidString
        ))
        return messages
    }
}

/// Answers `ChatStore`'s calls with a turn frozen in one state. It never
/// finishes the turn: a screenshot wants the state held, not played out —
/// except under `follow`, whose whole point is a reply that grows.
private struct FixtureChatAPI: ChatAPI {
    let state: UIFixture.State

    private static let streamed = "I'll compare it against a couple of other insurers. "

    /// When each `follow` send was accepted, by turn id. Main-actor state:
    /// `ChatStore` calls in from there.
    @MainActor private static var followStarts: [String: Date] = [:]

    private static let followParagraph = """
        Here is a paragraph of a longer reply, arriving a few words at a time the way \
        a real turn does, so the transcript has something growing to keep up with.
        """
    private static let followParagraphs = 12
    private static let followDuration: TimeInterval = 15

    @MainActor
    private func followTurn(_ turnId: String) -> Turn {
        let started = Self.followStarts[turnId] ?? Date()
        let progress = min(1, Date().timeIntervalSince(started) / Self.followDuration)
        let full = Array(repeating: Self.followParagraph, count: Self.followParagraphs)
            .joined(separator: "\n\n")
        let done = progress >= 1
        return Turn(
            turnId: turnId, status: done ? "done" : "running", source: "chat",
            resultText: done ? full : nil, hadPermissionDenial: false, error: nil,
            createdAt: started.timeIntervalSince1970,
            activities: [TurnActivity(kind: "text", tool: nil, at: 1)],
            partialText: done ? "" : String(full.prefix(Int(Double(full.count) * progress)))
        )
    }

    func getTurn(_ turnId: String, deviceToken: String) async throws -> Turn {
        if state == .follow { return await followTurn(turnId) }
        let search = TurnActivity(kind: "tool_start", tool: "WebSearch", at: 1)
        let writing = TurnActivity(kind: "text", tool: nil, at: 2)
        let (activities, text): ([TurnActivity], String) = switch state {
        case .empty, .idle, .working, .failed, .follow, .approval, .approvalBash, .ideas, .markdown, .whatsNew: ([search], "")
        case .streaming: ([writing], Self.streamed)
        case .interrupted: ([writing, TurnActivity(kind: "tool_start", tool: "WebSearch", at: 3)], Self.streamed)
        }
        return Turn(
            turnId: turnId, status: "running", source: "chat", resultText: nil,
            hadPermissionDenial: false, error: nil, createdAt: Date().timeIntervalSince1970,
            activities: activities, partialText: text,
            partialTextMarks: state == .interrupted ? [Self.streamed.unicodeScalars.count] : []
        )
    }

    /// Nothing observed, so no fixture state grows a yellow status row it
    /// never had; the Usage panel's own fixture carries the high reading.
    func usage(deviceToken: String) async throws -> UsageReport {
        UsageReport()
    }

    func health(deviceToken: String) async throws -> HealthResponse {
        HealthResponse(
            status: "ok", authOk: true, reauthPrewarn: false, queueDepth: 0,
            queuePaused: false, oldestPendingPermissionAge: nil, model: "sonnet"
        )
    }

    /// An empty server: the fixture's conversation is seeded into the cache as
    /// local-only messages, so there is nothing for a sync to add.
    func transcript(after: Int?, before: Int?, limit: Int, deviceToken: String) async throws -> TranscriptPage {
        TranscriptPage(rows: [], latestSeq: 0, hasMore: false)
    }

    func transcriptAttachment(id: String, deviceToken: String) async throws -> Data {
        throw URLError(.fileDoesNotExist)
    }

    /// Fixed at first use, which is the first permissions poll, a moment after
    /// launch — so the request has its full ~55s on screen.
    private static let approvalExpiry = Date().addingTimeInterval(55)
    private static let approvalAnswered = OSAllocatedUnfairLock(initialState: false)

    /// The one pending request for `approval` / `approvalBash`, returned on
    /// every poll until answered, as the real server does.
    private func approvalRequest() -> PermissionRequestInfo? {
        guard !Self.approvalAnswered.withLock({ $0 }) else { return nil }
        let expires = Self.approvalExpiry.timeIntervalSince1970
        let json: String
        switch state {
        case .approval:
            json = """
            {"request_id": "fixture-req", "tool": "Edit", "tool_input": {
              "file_path": "/home/marlow/marlow-identity/memory/USER.md",
              "old_string": "- Drinks: tea",
              "new_string": "- Drinks: tea\\n- Likes unicorns"},
             "created_at": \(expires - 55), "expires_at": \(expires),
             "summary": {"title": "Edit memory/USER.md",
               "preview": [{"op": "add", "text": "- Likes unicorns"}],
               "marlow_says": null, "similar": {"label": "edits to memory/USER.md"}}}
            """
        case .approvalBash:
            json = """
            {"request_id": "fixture-req", "tool": "Bash", "tool_input": {
              "command": "find /home/marlow/marlow-identity/memory -maxdepth 1 -name \\"*.md\\" -newer /home/marlow/marlow-identity/CLAUDE.md",
              "description": "List memory files changed since CLAUDE.md"},
             "created_at": \(expires - 55), "expires_at": \(expires),
             "summary": {"title": "Find files in memory", "preview": [],
               "marlow_says": "List memory files changed since CLAUDE.md",
               "similar": {"label": "find commands"}}}
            """
        default:
            return nil
        }
        return try? JSONDecoder().decode(PermissionRequestInfo.self, from: Data(json.utf8))
    }

    func permissions(deviceToken: String) async throws -> PermissionsResponse {
        PermissionsResponse(permissionRequests: approvalRequest().map { [$0] } ?? [])
    }

    func sendMessage(
        text: String, clientMsgId: String?, imagesBase64: [String],
        effort: ThinkingEffort, location: OutgoingLocation?, ideaId: String?, spoken: Bool,
        timeout: TimeInterval?, deviceToken: String
    ) async throws -> MessageResponse {
        guard state == .follow || state == .ideas else { throw URLError(.notConnectedToInternet) }
        let turnId = "fixture-follow-\(UUID().uuidString)"
        await MainActor.run { Self.followStarts[turnId] = Date() }
        return MessageResponse(turnId: turnId)
    }

    func answerPermission(requestId: String, decision: String, deviceToken: String) async throws -> AnswerPermissionResponse {
        Self.approvalAnswered.withLock { $0 = true }
        return AnswerPermissionResponse(accepted: true)
    }
}
#endif
