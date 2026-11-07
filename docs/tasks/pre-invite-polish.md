# Pre-invite polish pass

**Status:** scoped 2026-11-06. Settings and text size are done (Mike, 2026-11-06:
the Android user is happy with text size); the rest is not started.

The goal is what a friend notices in their first week. Friends use the web
app, on Android or added to an iPhone home screen (`friends-alpha.md`;
TestFlight is parked), so **web comes first and the native app must not lead
the list**. The both-clients rule still holds for each item below.

## In the pass, in order

1. **Walk a friend's first day.** Reset the trial VM, pair the web app from an
   iPhone home screen and from Android, and go through onboarding and the
   first morning brief. Onboarding hasn't been seen with a real new user
   (`onboarding-v2.md`). Anything a friend would notice joins this list;
   anything else is parked.
2. **Settings overhaul** (`settings-overhaul.md`): done 2026-11-06.
3. **Unsent draft survives a kill** (`ios-chat-ux-gaps.md` #20), both clients.
   The installed web app gets evicted too, and a lost message is the worst
   first impression on this list.
4. **Long replies read from the top** (#22), both clients. The morning brief
   is the first long thing a friend gets every day, and the jump button lands
   at its end.
5. **What's New fade** (#21), both clients. Small; do it with item 4.
6. **Re-pairing to a new box** (`repair-against-a-new-box.md`), native only.
   Not something a friend hits, but it bites Mike whenever he tests against
   the trial VM (Mike, 2026-11-06).
7. **Android back closes the top layer** (`android-back-gesture.md`), web only.
   Today the gesture leaves the app from anywhere.

## Cut from the pass

Each is still worth doing, just not before an invite:

- **LuxTTS strip** (`dependency-licenses.md`): only native builds carry it,
  and none goes to anyone while TestFlight is parked. It stays a blocker for
  TestFlight or any handed-out build.
- **`ideas-seen-sync.md`**: needs two clients on one account, which a friend
  on one phone doesn't have.
- **#19, the icon strip:** native only, an optical quibble.
- **#3, scroll on clearing a request:** unconfirmed on device. Item 1 will show
  whether web does it.

## To decide

- **Who does the walkthrough.** A builder session can drive the web app, but
  only a real phone shows the home-screen app and web push.

## Done when

Items 2 to 7 are on `main` and on all three boxes, each on both clients or
with a stated reason why not, and the walkthrough has been repeated with
nothing new a friend would notice.
