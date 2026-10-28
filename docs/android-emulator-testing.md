# Trying the web client on Android without an Android phone

The web client's target is Android Chrome, installed as a PWA
(`docs/tasks/portable-web-client.md`). Desktop Chrome's device mode gets the
layout roughly right but none of the behaviour that broke on iOS: the soft
keyboard, flings, install, push with the screen off, voice typing. The Android
Emulator runs real Android Chrome on a Mac and covers almost all of it. Written
2026-10-02, before anyone had run it; Android Studio menu names move between
releases, so treat the paths below as directions, not exact clicks.

## One-time setup (about 30 minutes, mostly downloads)

1. **Install Android Studio** from developer.android.com/studio (the `mac_arm`
   build on Apple silicon). Accept the default SDK and emulator components.
2. **Create a virtual device:** Device Manager → Create → Pixel 7 → a recent
   system image whose *Target* says **Google Play** (not "Google APIs").
   Play matters twice: Chrome's push goes through Google's servers, and
   Tailscale comes from the Play Store.
3. **Start it, sign in to a Google account, update Chrome** from the Play
   Store.
4. **Join the tailnet:** install Tailscale from the Play Store and log in. The
   emulator becomes a device on the tailnet (remove it in the admin console
   when done). `https://marlow.<tailnet>.ts.net:8443/` should now load in
   its Chrome, with a valid certificate.
5. **Microphone, for voice typing:** in the emulator's Extended Controls →
   Microphone, turn on "Virtual microphone uses host audio input". Gboard's
   mic then hears the Mac's.

## Pair and install

1. On the box: `~marlow/marlow-server/deploy/pair-device.sh "Android emulator"`.
2. In the emulator's Chrome, open the link it prints (the `#pair=` part is the
   invite; one use, ten minutes) and tap Pair.
3. Chrome menu → **Install app** (or "Add to home screen"). Open Marlow from
   the home screen: it should run full-screen, with no address bar.
4. The chat shows a "Turn on notifications" card under the header until push
   is on. Tap **Turn on** and allow the Android prompt. Nothing is pushed until
   this is done. Settings (the gear) has the same control.

## What to check

These are the rules the contract marks `[device]`, plus the "only Mike can
check" list from the web client's PR description (2026-10-02). Desktop Playwright can't answer any of them.

- **Keyboard (WEB-3, CMP-8):** scroll to the bottom, tap the composer. The
  newest message must stay just above the keyboard as it opens and closes, not
  jump or hide behind it. Type five or more lines: the field grows to five,
  then scrolls.
- **Voice typing (CMP-8):** dictate with Gboard's mic, then send. The words
  must not reappear in the field after sending (iOS had exactly this bug).
- **Follow and fling (SCR-11, SCR-12, SCR-14):** send something long
  (a request for a long answer). While it streams, flick the transcript up:
  the fling must not be cancelled or snapped back. Tap the ↓ button during a
  fling; it must not be lost.
- **Push with the screen off (Doze):** lock the emulator (power button in the
  side bar), then force Doze from a Mac terminal:

  ```sh
  ~/Library/Android/sdk/platform-tools/adb shell dumpsys deviceidle force-idle
  ```

  Send Marlow a message from the desktop and wait for the reply to land there.
  The emulator's screen stays dark: a notification does not turn the screen on.
  Wake it with the power button, then pull down the notification shade. Pass:
  a Marlow notification with the reply's real text, stamped with the time the
  reply finished, not the time you woke the device. Fail: no notification, or
  one that appears only after the wake. Undo with `... deviceidle unforce`.
  On waking, the foot of the chat may show a spinner for a moment while the
  tailnet comes back; it must not flash red "Can't reach Marlow" (STA-8).
- **Approval:** trigger a tool prompt from the desktop and confirm the
  emulator gets the "Marlow needs your OK" notification, and that tapping it
  opens the prompt.
- **Badge:** with Marlow closed, a reply should put a dot on its home-screen
  icon (launcher-dependent); opening Marlow clears it.

## Debugging

Desktop Chrome → `chrome://inspect/#devices` lists the emulator's tabs and the
installed app; **inspect** gives full DevTools (console, network, elements) on
the emulator's page.

## Results so far (Pixel 10 emulator, 2026-10-02)

- **Works:** pairing, install, voice typing (the field clears after send),
  approval, the in-chat notifications prompt, push with the app closed and the
  screen on (the real reply text, from the service worker's own fetch), and the
  badge.
- **The lock screen shows "Contents hidden".** That is the emulator's
  lock-screen privacy setting acting on the notification's public version, not
  a bug. A notification does not turn the screen on either.
- **Forced Doze held the push until wake, without losing it.** At the moment the
  box sent (Mike's iPhone buzzed), logcat showed only
  `AlarmManager: setExactAndAllowWhileIdle [name: FcmRetry ...]`: Play
  services saw the message and scheduled a retry instead of delivering it, so
  Chrome never ran. The server sends replies with `Urgency: high`. The same
  emulator was crash-looping its NFC service every 3 seconds, so this is not
  yet evidence about real phones. **Open:** a real phone left idle for 20–30
  minutes. If that also holds pushes until wake, look at how the push service
  treats `Urgency` for web pushes.
- **Diagnostics that worked:** `adb shell dumpsys notification --noredact |
  grep -i "turn-"` lists Marlow's notifications (tag `turn-<turn id>`) without
  waking the device; `adb shell dumpsys deviceidle get deep` confirms Doze;
  `adb logcat -v time` captured during the test shows the FCM side.
- **Streaming that looks absent** is not the client: the box's
  `usage.jsonl` showed the first reply text arriving 0.3–0.4s before the turn
  finished, so the final text replaces it almost at once (STR-5, same on iOS).

## What the emulator still can't tell you

- **Manufacturer battery killers.** Samsung and others delay or drop push in
  ways stock Android doesn't. Only real phones show this.
- **Real fling feel and a mid-range phone's speed.** The emulator runs on the
  Mac's CPU.
- **The real Gboard on a real keyboard size.** Close, not identical.

That is what the friends' phones are for.
