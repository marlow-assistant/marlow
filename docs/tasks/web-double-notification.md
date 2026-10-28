# Task — doubled alert on Mike's Mac

**Status:** closed 2026-10-21. It was cause 1, not the web client: the box
was still pushing APNs to the stale "iPad" pairing (the Mac's Designed-for-iPad
copy), and the Mac kept toasting it after the app was deleted. Cause 2 is a
real mismatch in the code but was not what Mike heard (window focused the whole
time); parked as item 4 of `web-client-review-leftovers.md`. A change that
tried cause 2 (one attended rule for page sound and the push toast) was closed
unmerged.

**What to know next time.**
- `PairingStore` reads `device_tokens.json` once at startup and rewrites the
  whole file on every save. Edit it only with the service stopped, or restart
  after; otherwise the running server keeps pushing to the entry you removed
  and its next save puts it back.
- Telling the sources apart: "Time Sensitive" and "Marlow has something for
  you." with the reply text not swapped in are APNs (`push.py`). The web
  service worker can set neither.
- Deleting the app from the Mac did not stop the toasts. A Designed-for-iPad
  install from Xcode is a read-only `nullfs` mount of
  `DerivedData/.../.XCInstall/.../Wrapper`; `rm` on the mount fails, and
  deleting the `.XCInstall` folder cleared it. Not confirmed why the Mac kept
  displaying pushes for a deleted app.
- Run on "My Mac (Designed for iPad)" only on purpose, and unpair it after.

**Symptom.** When Marlow asks for a tool approval (and possibly when a reply
arrives), Mike's Mac shows a macOS toast titled "Marlow" *and* the web app
plays its in-page tink-tink. He uses the web client installed as a Chrome app
(`~/Applications/Chrome Apps.localized/Marlow.app`, bundle id
`com.google.Chrome.app.<app-id>`). System Settings →
Notifications shows one "Marlow" entry (Badges, Sounds, Desktop, Time
Sensitive); "Google Chrome" is off.

Two candidate causes. Find out which before changing code; it can be both.

1. **A stray iPhone build on the Mac.** An iPhone build of Marlow ("Designed
   for iPad", from Xcode, 2026-09-28) runs from
   `/private/var/folders/.../Wrapper/Marlow.app`, and its
   `MarlowNotificationService` extension was running. The box sends every
   push to every paired device (`server/push.py`), so if that copy was ever
   paired it gets APNs pushes and shows them as toasts. Hint: the one
   "Marlow" entry offers Time Sensitive, which the iOS app has
   (`Marlow.entitlements`, `push.py`'s `interruption-level`) and a Chrome web
   app probably doesn't. Check the box's paired devices
   (`server/pairing.py`, `device_tokens.json`; needs Mike's sudo) and
   whether the toast has a small Chrome badge. Fix: remove the copy and its
   pairing; no code.
2. **The web client's own mismatch.** The service worker shows no toast only
   when a window is focused *and* visible (`web/src/sw.ts`,
   `web/src/lib/pushNotification.ts` `planFor`). The page plays sounds when
   visible (`chatStore.ts` `isVisible`, `feedback.ts`; `PermissionCard.tsx`
   plays `feedbackApproval` on mount). A visible but unfocused window gets
   both. Fix: one rule for both, so exactly one of sound or toast fires, for
   replies and approvals. Test it, and watch the test fail with the fix
   reverted.
