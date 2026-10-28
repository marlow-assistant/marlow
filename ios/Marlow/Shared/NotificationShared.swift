import Foundation
import Security

/// Constants and helpers compiled into BOTH the app and the notification
/// service extension (docs/tasks/ios-notification-open-and-content-swap.md).
///
/// An extension is a separate executable: it shares no code, no container and
/// no Keychain item with the app unless arranged explicitly, which is what
/// this file and the two entitlements files are for.
enum NotificationShared {
    static let appGroup = "group.com.mikemason.Marlow"

    /// `$(AppIdentifierPrefix)` is substituted in the entitlements plist but
    /// never in Swift, so both targets' Info.plists carry it (project.yml) and
    /// this reads it back. Until 2026-10-26 the team ID was written out here;
    /// it left the repo for the gitignored `Signing.local.xcconfig`. The
    /// string must match the entitlement exactly, or the extension's Keychain
    /// read fails.
    static let keychainAccessGroup =
        (Bundle.main.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String ?? "")
        + "com.mikemason.Marlow.shared"

    /// Comfortably under Apple's ~30s extension budget, so a stalled request
    /// still leaves room to record a diagnostic and hand the original content
    /// back ourselves. Being killed by the system leaves no trace; timing out
    /// on our own terms leaves a log line saying so.
    static let requestTimeout: TimeInterval = 10

    /// A banner is not a transcript. iOS truncates the collapsed line anyway
    /// and expands on long-press, so this only has to stop a morning briefing
    /// from becoming a wall of text on the lock screen.
    static let maxBodyCharacters = 300

    /// Plain, whitespace-collapsed, length-capped reply text.
    ///
    /// Newlines go because a notification body renders them as real line
    /// breaks, and a markdown reply's blank lines turn one message into a
    /// full-screen banner. The markup goes because a notification body is not
    /// rendered markdown and never will be — Mike's lock screen on 2026-09-09
    /// showed a morning briefing with its `**` and `#` intact. The app's
    /// bubble renders the same text properly; only the banner needs this.
    static func alertBody(from resultText: String) -> String? {
        let collapsed = plainText(from: resultText)
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
        guard !collapsed.isEmpty else { return nil }
        guard collapsed.count > maxBodyCharacters else { return collapsed }
        return collapsed.prefix(maxBodyCharacters).trimmingCharacters(in: .whitespaces) + "\u{2026}"
    }

    /// Markdown with the markup taken off, for display somewhere that cannot
    /// render it.
    ///
    /// **Deliberately not a markdown parser.** It handles the constructs
    /// Marlow's replies actually contain and leaves everything else exactly
    /// as written, because the failure modes are not symmetric: a stray `**`
    /// on a banner is the cosmetic bug being fixed here, while mangling the
    /// text — eating the `_` out of `turn_ready`, or the `*` out of `2 * 3` —
    /// would misreport what Marlow said. Every rule below is one that cannot
    /// fire on prose that was never markup.
    static func plainText(from markdown: String) -> String {
        var lines: [String] = []
        var inFence = false

        for rawLine in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            var line = rawLine.drop { $0 == " " || $0 == "\t" }

            // A fence marker is never content. What it wraps is, so the lines
            // between the markers pass through untouched — emphasis rules
            // must not fire inside code.
            if line.hasPrefix("```") || line.hasPrefix("~~~") {
                inFence.toggle()
                continue
            }
            if inFence {
                lines.append(String(rawLine))
                continue
            }

            // `> ### **Heading**` is one line, so the prefixes come off in a
            // loop rather than one pass.
            var bullet = ""
            var isRule = false
            while true {
                // Checked each pass, and BEFORE the bullet rule below: the
                // spaced spelling `- - -` is a thematic break, but its first
                // two characters are also exactly a list marker, so a bullet
                // rule that ran first ate it down to a lone `-` and put a
                // bullet in front of it.
                let bare = line.filter { !$0.isWhitespace }
                if bare.count >= 3, let first = bare.first, "-*_".contains(first),
                   bare.allSatisfy({ $0 == first }) {
                    isRule = true
                    break
                }
                if line.hasPrefix(">") {
                    line = line.dropFirst().drop { $0 == " " || $0 == "\t" }
                    continue
                }
                let hashes = line.prefix { $0 == "#" }
                if (1...6).contains(hashes.count),
                   line.dropFirst(hashes.count).first.map({ $0 == " " }) ?? true {
                    line = line.dropFirst(hashes.count).drop { $0 == " " }
                    continue
                }
                // A bullet becomes a real one instead of vanishing: run
                // together on a single banner line, "the first the second"
                // reads as one sentence and loses what the list was saying.
                if let marker = line.first, marker == "-" || marker == "*" || marker == "+",
                   line.dropFirst().first == " " {
                    line = line.dropFirst().drop { $0 == " " }
                    bullet = "\u{2022} "
                    continue
                }
                break
            }
            // A rule carries no words, and once the newlines collapse it
            // would land as stray punctuation mid-sentence.
            if isRule { continue }

            lines.append(bullet + stripInline(String(line)))
        }

        return lines.joined(separator: "\n")
    }

    /// Link syntax, inline code and emphasis, within one line.
    private static func stripInline(_ line: String) -> String {
        let characters = Array(line)
        var out: [Character] = []
        /// Emphasis openers written out provisionally, as offsets into `out`.
        var pending: [(delimiter: Character, length: Int, at: Int)] = []
        /// Offsets into `out` that a closer later proved were markup.
        var drop: Set<Int> = []
        var i = 0

        while i < characters.count {
            let character = characters[i]

            // `[text](url)` and `![alt](url)` keep the human half. Only when
            // the whole shape is present — a bare `[TODO]` is left alone.
            if character == "[" || (character == "!" && i + 1 < characters.count
                                    && characters[i + 1] == "[") {
                let open = character == "!" ? i + 1 : i
                if let close = matchingBracket(characters, from: open),
                   close + 1 < characters.count, characters[close + 1] == "(",
                   let paren = matchingParen(characters, from: close + 1) {
                    out += Array(stripInline(String(characters[(open + 1)..<close])))
                    i = paren + 1
                    continue
                }
            }

            // Backticks wrap code; the code itself is the content, and it
            // passes through with no emphasis rules applied to it.
            if character == "`" {
                let run = runLength(characters, from: i, of: "`")
                if let close = index(ofRun: run, of: "`", in: characters, from: i + run) {
                    out += Array(characters[(i + run)..<close])
                    i = close + run
                    continue
                }
            }

            if character == "*" || character == "_" {
                let run = runLength(characters, from: i, of: character)
                let before = i > 0 ? characters[i - 1] : " "
                let after = i + run < characters.count ? characters[i + run] : " "
                // CommonMark's flanking rules, cut down to the part that
                // protects prose: an opener needs non-space after it, a closer
                // needs non-space before it. That alone spares `2 * 3`. `_`
                // additionally may not start or end inside a word, which is
                // what spares `turn_ready` and `snake_case_names`.
                let canOpen = !after.isWhitespace
                    && (character == "*" || !(before.isLetter || before.isNumber))
                let canClose = !before.isWhitespace
                    && (character == "*" || !(after.isLetter || after.isNumber))

                if canClose,
                   let match = pending.lastIndex(where: {
                       $0.delimiter == character && $0.length == run
                   }) {
                    let opener = pending[match]
                    drop.formUnion(opener.at..<(opener.at + opener.length))
                    // Anything opened after this one never closed, so it was
                    // literal text and stays exactly where it was written.
                    pending.removeSubrange(match...)
                    i += run
                    continue
                }
                if canOpen {
                    pending.append((character, run, out.count))
                    out += Array(repeating: character, count: run)
                    i += run
                    continue
                }
            }

            out.append(character)
            i += 1
        }

        // Openers still pending were never closed; they keep their characters.
        return String(out.enumerated().compactMap { drop.contains($0.offset) ? nil : $0.element })
    }

    // MARK: - Small character-array helpers

    private static func runLength(_ characters: [Character], from: Int, of character: Character)
        -> Int {
        var length = 0
        while from + length < characters.count, characters[from + length] == character {
            length += 1
        }
        return length
    }

    private static func index(
        ofRun run: Int, of character: Character, in characters: [Character], from: Int
    ) -> Int? {
        var i = from
        while i < characters.count {
            if characters[i] == character, runLength(characters, from: i, of: character) == run {
                return i
            }
            i += 1
        }
        return nil
    }

    /// The `)` closing the `(` at `from`. By DEPTH, not the first one found:
    /// a URL is allowed to contain parentheses, and taking the first `)` left
    /// the tail of `…/Foo_(bar))` on the banner as a stray bracket.
    private static func matchingParen(_ characters: [Character], from: Int) -> Int? {
        matchingDelimiter(characters, from: from, open: "(", close: ")")
    }

    /// The `]` closing the `[` at `from`, respecting nesting.
    private static func matchingBracket(_ characters: [Character], from: Int) -> Int? {
        matchingDelimiter(characters, from: from, open: "[", close: "]")
    }

    private static func matchingDelimiter(
        _ characters: [Character], from: Int, open: Character, close: Character
    ) -> Int? {
        var depth = 0
        var i = from
        while i < characters.count {
            if characters[i] == open { depth += 1 }
            if characters[i] == close {
                depth -= 1
                if depth == 0 { return i }
            }
            i += 1
        }
        return nil
    }
}

// MARK: - Shared credential

/// A COPY of the paired device token, in a Keychain access group both targets
/// can reach.
///
/// **Deliberately a copy, not a migration** (Mike's call, 2026-09-08). Moving
/// `KeychainStore`'s item into a shared group is the tidier design — one
/// token, not two — but that item is what "paired" means, and getting the
/// access group wrong un-pairs the phone. The cost of the copy is a second
/// live bearer token on the device, kept in step by `SessionStore` and
/// cleared on sign-out; the cost of the migration going wrong is a phone that
/// has to be re-paired. Weighed once, with the copy chosen; revisit only with
/// an upgrade test that proves the move on a device that is already paired.
///
/// So `KeychainStore`'s item is read for its value and never written, moved
/// or deleted by this file.
enum SharedCredential {
    /// Still says `spike` because this exact string is the address of an item
    /// already sitting in the Keychain on Mike's phone. Renaming it would
    /// orphan that item — unreachable, unclearable by `clear()`, and holding a
    /// live token. The name is cosmetic; the orphan would not be.
    private static let service = "com.mikemason.Marlow.spike.deviceToken"
    private static let account = "device_token"

    private static var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: NotificationShared.keychainAccessGroup,
        ]
    }

    /// App-side. Idempotent; safe to call on every launch.
    static func mirror(_ token: String) {
        SecItemDelete(baseQuery as CFDictionary)
        var attributes = baseQuery
        attributes[kSecValueData as String] = Data(token.utf8)
        // AfterFirstUnlock, not WhenUnlocked: the extension's whole job
        // happens on a LOCKED phone, where a WhenUnlocked item is unreadable.
        // ThisDeviceOnly for the same backup-exfiltration reason as
        // `KeychainStore`.
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(attributes as CFDictionary, nil)
        guard status != errSecSuccess else { return }
        // Recorded, not swallowed. If Keychain Sharing was not provisioned
        // onto the App ID — the likeliest first-device-build failure, and one
        // the task doc warns about — this returns errSecMissingEntitlement and
        // no copy is ever written. The extension would then report
        // `no_credential`, which also means "the app has never been launched".
        // Without this line the two are indistinguishable, and they have
        // opposite fixes.
        NotificationDiagnostics.record(outcome: "mirror_failed_\(status)", milliseconds: 0)
    }

    /// Extension-side, read-only.
    static func load() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// Called on unpair. The copy outliving the credential it mirrors would
    /// leave a live bearer token on a phone the user believes they signed out
    /// of — the one way this arrangement is worse than a migration.
    static func clear() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}

// MARK: - Diagnostics

/// One record per extension invocation.
///
/// State, timing and error CATEGORY only — never the bearer token, never the
/// fetched text. That is a rule, not an accident of the current fields: this
/// file is written by a process running on a locked phone into a container
/// the app can read, and a notification body is the one piece of message
/// content the design otherwise keeps off disk outside the transcript.
struct NotificationEvent: Codable, Identifiable, Equatable {
    /// Stored, not derived from the timestamp: it is also the filename, and
    /// two runs recording in the same millisecond must not collide.
    let id: String
    let at: Double
    /// A fixed vocabulary, not free text — see `NotificationService`.
    let outcome: String
    let milliseconds: Int
    /// Length of the body we displayed, as evidence the fetch produced
    /// something. Never the body.
    let bodyCharacters: Int?
}

/// Why a notification did or did not show real text. Read from the Mac with
/// `scripts/pull-notification-events.sh`, not shown in the app (2026-09-30:
/// diagnostics are pulled on demand, the Settings screen is for settings).
///
/// Kept after the spike it was built for, because an extension is otherwise
/// invisible: it runs on a locked phone, in another process, with no console
/// attached, and its failures all look identical from the outside — the
/// generic banner. `network_error` (no route), `no_credential` (Keychain not
/// shared) and `http_401` (token rejected) are three different problems
/// wearing the same face, and this is the only thing that tells them apart.
///
/// A file per run rather than one appended array: iOS can run several service
/// extension instances at once when pushes arrive together — a cron burst is
/// exactly that — and a read-modify-write of a shared array silently drops
/// whichever run finished first, making a delivered notification look like one
/// that never fired.
enum NotificationDiagnostics {
    /// Enough history to see a pattern without the directory growing forever.
    static let maxEvents = 50

    /// Under `Library/` because that is all `devicectl` can see of an app
    /// group container (checked 2026-09-30: a folder at the container root is
    /// neither listed nor copyable), and the pull script is the only reader.
    private static var directoryURL: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: NotificationShared.appGroup)?
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("notification-events", isDirectory: true)
    }

    static func record(outcome: String, milliseconds: Int, bodyCharacters: Int? = nil) {
        guard let directoryURL else { return }
        let event = NotificationEvent(
            id: UUID().uuidString,
            at: Date().timeIntervalSince1970,
            outcome: outcome,
            milliseconds: milliseconds,
            bodyCharacters: bodyCharacters
        )
        guard let data = try? JSONEncoder().encode(event) else { return }
        try? FileManager.default.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
        )
        // Readable by the extension on a locked phone for the same reason the
        // mirrored credential is.
        try? data.write(
            to: directoryURL.appendingPathComponent("\(event.id).json"),
            options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        )
        prune()
    }

    /// Best-effort and deliberately unsynchronised: two instances pruning at
    /// once can only both delete an already-old event, which is what pruning
    /// is for. Nothing here can drop a run that has not yet been read.
    private static func prune() {
        let events = load()
        guard events.count > maxEvents, let directoryURL else { return }
        for event in events.sorted(by: { $0.at < $1.at }).prefix(events.count - maxEvents) {
            try? FileManager.default.removeItem(
                at: directoryURL.appendingPathComponent("\(event.id).json")
            )
        }
    }

    private static func load() -> [NotificationEvent] {
        guard let directoryURL,
              let names = try? FileManager.default.contentsOfDirectory(atPath: directoryURL.path)
        else { return [] }
        return names.compactMap { name in
            guard let data = try? Data(contentsOf: directoryURL.appendingPathComponent(name)) else {
                return nil
            }
            return try? JSONDecoder().decode(NotificationEvent.self, from: data)
        }
    }
}
