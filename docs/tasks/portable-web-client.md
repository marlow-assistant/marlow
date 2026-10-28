# Portable web client: Android friends and Mike's desktop

**Status:** shipped 2026-10-02, live on the box.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Phases 1–5 are done. Checked in desktop Firefox against the box and on a Pixel 10
emulator (`docs/android-emulator-testing.md`). Still open, on a real Android
phone: push through Doze, the soft keyboard, and flinging during a stream. Three
review findings are parked in `web-client-review-leftovers.md`.

Mike's calls (2026-10-01):
- **Web, not native.** One client for Android friends, Mike's desktop, and any
  other browser.
- **Framework:** TypeScript, Vite and React. Mike had no preference; React is
  what Claude writes most reliably.
- **Mike dogfoods it as his desktop client.** It is not a friends-only build.
- **The box builds it:** `update.sh` runs the build, and no build output is
  committed. The CLI already needs Node there. Committed build output is
  generated noise in every client PR, and it invites a source change that
  forgets to rebuild.
- **Supported browsers for v1: Chrome and Firefox** (Mike, later the same
  day). The real goal is an installable-feeling Android app; Chrome on
  Android is the target.
- **Safari is supported too** (Mike, 2026-10-25), and on an iPhone the
  home-screen web app is the least-friction way in: less polished than the
  native app, but fully featured. Two Safari rules shape it. It has no
  focused-tab exemption for silent pushes, which `mustShowEveryPush` handles.
  Its push service refuses the placeholder VAPID subject (every send got 403
  on Mike's phone until a real `mailto:` was set), so `install.sh --email`
  sets one. Not supported, and not worked around:
  - Edge on Windows: its push service (`notify.windows.com`) is not in the
    allowlist, so its notifications can't be turned on. Chat works.
- iOS stays the reference client. Where this doc and the iOS app disagree on
  behaviour, the app wins.

The build was handed to one session end to end, with phases: 1 serve and pair,
2 chat to the UX contract (`docs/chat-ux-contract.md`), 3 installable PWA and
Web Push, 4 ship path (`update.sh` builds, a pairing-code script, README and
START), 5 read-only activity and identity views. Out of scope: dictation
(keyboard voice typing is the baseline), Capacitor, any iOS change.

## Serving

`public_app` serves `web/dist` at `/` with an SPA fallback, never shadowing
`/v1/*` or `/health`. A missing `dist` gives a plain 404 with a hint and the
server still starts. Every static response carries `Content-Security-Policy:
default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self';
img-src 'self' blob: data:; connect-src 'self' https://huggingface.co
https://*.hf.co; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`, plus `nosniff` and `Referrer-Policy: no-referrer`.
Hashed assets cache long; `index.html` and the service worker are `no-cache`.
Same origin throughout, so no CORS is configured and the client never knows a
server URL.

The token lives in IndexedDB so the service worker can read it too; a 401 on
any call returns the client to pairing.

## Security (non-negotiable)

The device token can answer `/v1/permission/{id}`, so any script running in
the page can approve tool use. Marlow's replies quote email and web pages,
which makes them attacker-influenced input, and rendered markdown is the path
for an injection. Therefore:
- **Markdown never becomes raw HTML.** Parse it to a tree and build React
  elements. No `dangerouslySetInnerHTML` anywhere in `web/`; a test greps the
  source for it. Raw HTML in the markdown renders as text.
- **Links:** only `http:`, `https:` and `mailto:` become anchors, with
  `rel="noopener noreferrer"` and `target="_blank"`. Anything else renders as
  text. Images in markdown are not loaded, since they could be tracking
  pixels; show the alt text.
- **The CSP above**, with no inline scripts and no third-party origins.
- The `#markdown` dev tag includes a raw `<img onerror>` and a `javascript:`
  link; a Playwright test asserts that neither does anything.

## Web Push

Each box has its own VAPID keypair and sends to the browser's push service
directly: no relay, no Firebase project, and Mike's APNs key is not used.
- Keys are generated on first use in `settings.data_dir` (in `SECRET_PATHS`,
  so the agent can't read it), mode 0600, never logged.
- A subscription's endpoint is validated: `https:` only and a host under a
  known push service, so a token can't turn the server into a request
  forwarder. A 404 or 410 from the push service drops it.
- The payload is `{type, ref}`, plus `alert_body` for `notify` only. For
  `turn_ready` the service worker fetches `/v1/turn/{ref}` with the stored
  token for the body, the same design as iOS's `NotificationService`; if that
  fails it shows a generic notification, never an empty one. Web Push encrypts
  payloads end to end (RFC 8291). Sends are fire-and-forget, off the work
  queue.
- `pywebpush` is the one new server dependency. The egress allowlist gained
  the push hosts: `fcm.googleapis.com` (Chrome, Edge, Android),
  `updates.push.services.mozilla.com` (Firefox), `web.push.apple.com`
  (Safari).

## Decisions in the behaviour

- `docs/chat-ux-contract.md` is the specification; where it says a rule is a
  SwiftUI workaround, it wasn't ported. Rules tagged `[derived]` are reasoning,
  not findings, and were checked in the browser.
- Tool approval with two devices: match iOS and add nothing smarter, but add
  the Web Push `permission_request` notification, the only path that reaches an
  idle desktop.
- Every message is recorded with `source: "ios"` whichever client sent it, and
  the client never branches on `source`; changing it would change how turns
  run. The rename to `chat` is `rename-ios-source.md`.
- Display wording is the client's job (facts on the server, wording on the
  phone), so the formatting helpers and their tests were ported rather than
  adding display strings to the server.
- `update.sh` builds into a temporary directory and renames it into place, so a
  failed build fails the update loudly and never leaves a stale or half-written
  `dist`.

## What only Mike can check (open)

- Desktop dogfooding against the real box over the tailnet. HTTPS comes from
  the box's tailnet certificate, so the service worker and push work there and
  not over the dev server's HTTP.
- Install as a PWA on Android Chrome, push with the screen off (Doze), the
  soft keyboard with `interactive-widget=resizes-content`, and voice typing.
- The two-device case: a reply started on the phone streams on the desktop,
  and the reverse.
- `update.sh` on the box, then on the Android user's VPS.
- The box's Node version isn't recorded here: check `node --version` there.
  The build floor is Node 20.

`scripts/dev-server.sh` needs the server's Python. A worktree has no `.venv`, so
the script falls back to the system `python3` without the dependencies, and the
e2e `webServer` fails to start; link the main checkout's `.venv` in.

## Why web, and why a PWA first

The first friends who want Marlow are on Android. A native Compose port would
work, but every UX change would then have to be built and checked twice, and
Mike's attention for checking is the scarce resource, not the code. One web
client covers Android, Mike's desktop, and any other machine with a browser.

**PWA first, Capacitor only if needed.** On Android Chrome an installed PWA
covers everything the native bridge was going to do:

| Need | iOS today | PWA |
|---|---|---|
| Push, with the content swap | APNs plus `NotificationService` (an extension) | Web Push. The service worker fetches `/v1/turn` and shows the banner |
| Dictation | `SpeechTranscriber`, whole-file and on-device | The keyboard's own voice typing, with no code from us. The alpha's baseline |
| Haptics and sounds | `ChatHaptics`, `AudioToolbox` | `navigator.vibrate` (Android only) and Web Audio |
| Photos, camera, paste | `PhotosUI`, `ImageDownscaler` | `<input type=file accept=image/*>`, clipboard image paste, canvas downscale |
| Token storage | Keychain | IndexedDB. See Security |
| Unread badge | `APNsRegistrar.marksChatUnread` | Badging API, where supported |

Capacitor wraps the same build later, so starting with a PWA costs nothing to
reverse. Switch to Capacitor only when one of these happens:
- a friend finds keyboard dictation clearly worse than Mike's iOS dictation;
- Web Push proves unreliable under Android Doze.

**Update 2026-10-22:** dictation no longer waits on Capacitor. The web client
has its own mic and voice mode, transcribed on the box by Parakeet, with no
native bridge (`voice-mode-android.md`; merged 2026-10-23).

The speech bridge is the risky piece, not the big one. Android's
`SpeechRecognizer` streams and ends the session on pauses, and its quality
varies by phone maker. iOS dictation is good because of the whole-file design,
so a bridge would not get that quality for free.

**Self-hosting makes Web Push easy.** Each friend's box holds its own VAPID
keypair and sends pushes directly. That needs no relay, no Firebase project,
and no use of Mike's APNs key. iOS push for self-hosters still needs the relay
in `friends-alpha.md` item 8. Android and desktop do not.

**Rejected:** native Compose, React Native and Flutter, because each is a
second UI to dogfood. Kotlin/Compose Multiplatform gives no cheap browser
client.

## Why the port is mostly `ChatStore`, not `ChatView`

Measured 2026-10-01, the two hold about 3,600 Swift lines, roughly 1,500 of
them comments that record device findings:
- **`ChatStore`** (send with dedupe, retry and backoff; the polling policy; the
  transcript sync and merge; windowing; status precedence) is the logic to
  port faithfully, with its tests.
- **`ChatView`.** Much of its scroll code works around SwiftUI (an eager
  `VStack`, measuring the viewport from a background), and the browser needs
  none of that. `ReplyFollow` is already pure geometry and ports almost line
  for line.

What's hard on the web, and not on iOS:
- the Android soft keyboard (`interactive-widget=resizes-content`,
  `visualViewport`);
- not cancelling a fling with programmatic scrolling;
- re-rendering only the streaming bubble, not the whole list, on each 50ms
  tick.

**Don't oversell Playwright.** Gaps items 8–10 were each found after a green
suite, by holding the phone. Desktop Chromium is not an Android browser with
the keyboard up. Mike's desktop use and the friends' phones are the real
verification.
