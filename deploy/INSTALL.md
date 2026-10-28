# Installing Marlow

Marlow is a personal assistant that lives on a Linux box you control and talks to
you through an iPhone app, or through a web app you add to your phone's home
screen (iPhone or Android) or open in a browser on your computer. It runs on **your** Claude subscription through the
Claude Code CLI, keeps its memory as plain files on your box, and can run
scheduled jobs (a morning briefing, reminders) while you're away from it.
Nothing of yours passes through anyone else's server.

**On an iPhone, the web app is the easy way in.** Add it to your home screen
from Safari and it works like an app, notifications included, with nothing to
install from a Mac. It's less polished than the native iPhone app but quite
usable, with everything working. The native app needs whoever invited you to install it from
their Mac.

This is an early, friends-only release. Expect rough edges, and tell whoever
invited you when you hit one.

## How your phone reaches Marlow

This is a key choice in setting Marlow up. The Ubuntu box always runs
[Tailscale](https://tailscale.com), a free private network between your own
devices (your *tailnet*). What you choose is how your phone gets to the box:

| Option | When | Phone needs | Who can reach the box | How to enable |
|---|---|---|---|---|
| **Tailscale on every device** | **Recommended, default.** | The Tailscale app, set up right | Only your own devices | Enabled by default |
| **Tailscale Funnel** | If Tailscale on your phone gives you trouble | Nothing | Anyone on the internet can try | `update.sh --funnel` |
| **Internet-accessible port** | A cloud server or a fixed home address | Nothing | Anyone on the internet can try | Not available yet |

**Tailscale on every device is the safest.** Marlow isn't on the internet at all; a stranger can't
even knock. The cost is the Tailscale app on every phone and computer you use
Marlow from, and a few of its settings matter (they're listed under
[Tailscale on every device](#tailscale-on-every-device) below). Almost every "Marlow can't connect" problem seen so far was one of
those settings.

**Tailscale Funnel removes the requirement for Tailscale on your phone.** The box stays on Tailscale, and
Tailscale's relay passes connections from the internet through to it. Your
phone then needs nothing installed beyond Marlow. The downside is that anyone can open a
connection to your box, and Marlow's security relies on your device's
secret token. Marlow is built for that (single-use pairing links, a way to cut
off a lost phone, limits on what a stranger can send), but it is more exposed
than Tailscale on every device.

**An internet-accessible port** would be the same as Funnel without Tailscale's relay in the way, for a box
that already has a stable public address, such as a VPS or maybe a home server configured with NAT and dynamic DNS. It isn't built yet.

Start with Tailscale on every device for maximum security, and pay attention to its configuration.
You can switch to Funnel, and back, at any time with one command, and
nothing else about your Marlow changes.

## What you need

- **An always-on Linux machine running Ubuntu 24.04.** An old laptop or mini
  PC is ideal. A small cloud VM or VPS works too. It needs about 2 GB of RAM and to
  stay awake: scheduled jobs don't run on a sleeping machine, which is why a
  gaming PC or WSL isn't recommended.
- **A Claude Pro or Max subscription.** Marlow's usage counts against it, like
  your own use of Claude Code does.
- **A Tailscale account** (free). If you don't have one, sign up at
  [tailscale.com](https://login.tailscale.com/start) before you start; signing
  in with Google, Microsoft or GitHub is fine. In its
  [admin console](https://login.tailscale.com/admin/dns), on the **DNS** page,
  turn on **MagicDNS** and **HTTPS certificates**. Marlow's address and its
  security certificate come from these, whichever option you pick.
- **A device for talking with Marlow.** The web app added to the home screen of
  an iPhone (Safari) or Android phone (Chrome or Firefox), or Chrome, Firefox or
  Safari on a computer. Or the native iPhone app, installed from a Mac by
  whoever invited you.
- Optional: in claude.ai, connect **Gmail** and **Google Calendar**
  (Settings → Connectors). Marlow uses whatever your Claude account has
  connected, and the morning briefing gets much better with them.

## Install

Allow about 15 minutes.

**Who runs what.** Log in to the box as the user you normally use, the one that
can `sudo` (on a cloud VM, the user it came with, such as `ubuntu`). Everything
below runs as that user, not as root. Don't create a user for Marlow: the
installer creates its own, called `marlow`, with no sudo, and Marlow runs as
that. You only act as `marlow` when this guide shows a command starting
`sudo -u marlow` or `sudo -iu marlow`.

**1. Join the box to your tailnet.**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

```bash
sudo tailscale up
```

It prints a login link and then waits, which is normal. Open the link in a
browser on any device and sign in to your Tailscale account; the command
finishes on its own.

**2. Get the installer.** Whoever invited you sends two files, `install.sh`
and `lib.sh`. Put both in a folder called `deploy`, and run the next step from
the folder *containing* `deploy`:

```bash
mkdir -p ~/marlow-install/deploy && cd ~/marlow-install
```

Copy the two files into `~/marlow-install/deploy/` (for example with `scp`
from your computer: `scp install.sh lib.sh you@your-box:marlow-install/deploy/`).
You don't need a GitHub account: the box gets the rest itself, with a key of
its own, in step 3.

**3. Run it.**

```bash
sudo bash ./deploy/install.sh --user-name "Your Name" --timezone Europe/London --email you@example.com
```

Use your own name (letters, spaces, `.`, `'` and `-`). Marlow asks what you'd
like to be called when you first chat, so a nickname can wait.

Use your own email address too. Notifications to a browser go through that
browser's push service (Google's, Mozilla's or Apple's), which asks every
sender for a contact address. Yours is given to them and to nobody else.
Apple's refuses to deliver without a real one.

Give your home timezone even if the box is set to yours: cloud machines
usually run on UTC, and your morning briefing is scheduled in the zone you give
here. Pick the nearest city:

| Where | `--timezone` |
|---|---|
| US Eastern (New York, Toronto) | `America/New_York` |
| US Central (Chicago) | `America/Chicago` |
| US Mountain (Denver) | `America/Denver` |
| US Pacific (Los Angeles, Vancouver) | `America/Los_Angeles` |
| UK, Ireland | `Europe/London` |
| Most of western Europe (Paris, Berlin, Madrid) | `Europe/Berlin` |
| India | `Asia/Kolkata` |
| Sydney, Melbourne | `Australia/Sydney` |

Anywhere else: `timedatectl list-timezones | grep -i yourcity` on the box.

The installer pauses once for you, for the **Claude login.** It starts Claude
for you. Pick a colour theme (any), choose to log in with your Claude
subscription, open the link it prints in a browser, and sign in. Then Claude
asks whether to trust the folder: choose **No, exit**. You're still logged in,
and the installer carries on.

If the login doesn't finish, the installer stops and says so. Run **the same command again** (up-arrow,
Enter); it picks up where it stopped. Any other stop is a problem rather than
a step: send whoever invited you the last 20 or so lines it printed. Running
the command again is always safe.

**4. Set up your phone for Tailscale.** Install Tailscale on the phone and
sign in to the same account, then check the settings under
[Tailscale on every device](#tailscale-on-every-device). (Going straight to
Funnel instead? Do [its steps](#tailscale-funnel) now, then come back to step 5.)

**5. Pair your phone or browser.** When the installer finishes, it prints a
pairing link, also as a QR code. The link works once and lasts 10 minutes.

- **Android:** scan the QR with the camera. It opens Chrome and pairs in one
  tap. Then tap **Turn on** when the chat asks about notifications.
- **iPhone, web app:** add it to the home screen first, then pair inside it.
  In Safari, open the address the installer printed (`https://…:8443`, not
  the pairing link), tap **Share** → **Add to Home Screen**, and open Marlow
  from the new icon. Paste the pairing link into it, then tap **Turn on** when
  the chat asks about notifications. iPhone only sends notifications to the
  home-screen app, which keeps its own sign-in, so a link opened in a Safari
  tab pairs only that tab.
- **iPhone, native app:** in the Marlow app, tap **Scan pairing code** and scan the QR.
  (Not the Camera app, which would open Safari.) Or copy the link and paste it
  into the app. Typing the link in by hand isn't supported.
- **A computer:** open the link in Chrome, Firefox or Safari.

Treat the link like a password for those ten minutes: whoever opens it first
becomes a paired device. For a fresh link, for this or any other device:

```bash
sudo -u marlow ~marlow/marlow-server/deploy/pair-device.sh
```

**Install it as an app** (web app only), so it gets its own icon and you never
see the address bar:

- **Chrome on Android:** the **⋮** menu at the top right → **Add to Home
  screen** → **Install** (some versions say **Install app**, or **Install and
  create shortcut**). Choose install, not a plain shortcut: the installed app
  opens without the address bar.
- **Firefox on Android:** the **⋮** menu → **Add app to Home screen**.
- **Safari on iPhone:** as in step 5. Open Marlow from the icon, never a
  Safari tab: notifications go only to the icon.
- **Chrome on a computer:** the install icon at the right end of the address
  bar, or **⋮** → **Cast, save and share** → **Install page as app**.

Open Marlow from the new icon from then on.

**Talking instead of typing (Android):** Marlow uses your keyboard's own voice
typing. If it makes a lot of mistakes, install **FUTO Keyboard** with **FUTO
Voice Input** (both free, from the Play Store) and make FUTO Keyboard your
default keyboard. Its mic button transcribes on the phone, offline, and is
much more accurate.

Marlow says hello once you're paired and asks what to call you. Over the next
few messages it shows you what it can do: it looks at your calendar and inbox
if they're connected, asks what you'd like help with, offers to set a reminder,
and asks whether 08:00 suits your morning briefing.

## Tailscale on every device

The default. Every phone or computer you use Marlow from runs the Tailscale
app, signed in to the same account as the box. In the phone's Tailscale app:

- **Use Tailscale DNS: on.** Marlow's address only exists inside your tailnet,
  so the phone has to look it up through Tailscale.
- **Exit node: None.** Marlow doesn't need one. With one set, all the phone's
  traffic goes through that machine, and video sites often slow down.
- **If one app misbehaves** with Tailscale on, exclude that app under **Split
  tunneling** rather than turning Tailscale off.
- **iPhone:** Tailscale's **VPN On Demand** keeps it connected without you
  thinking about it.

And in the [admin console's DNS page](https://login.tailscale.com/admin/dns):
if the phone loses the internet whenever Tailscale is on, **Override DNS
servers** is on without a working nameserver. Turn it off, or add one such as
1.1.1.1.

## Tailscale Funnel

Funnel gives your box's address a public route through Tailscale's relay, so
phones reach it without Tailscale. Read [the trade-off](#how-your-phone-reaches-marlow)
first: it puts Marlow on the internet.

**Turn it on:**

```bash
sudo ~marlow/marlow-server/deploy/update.sh --funnel
```

The first time, your tailnet doesn't allow Funnel yet. The command stops
before changing anything and prints a link. Pick one of these, then run the
same command again:

- **The easy way:** open the link and click **Enable Funnel**. That lets
  *every* machine on your tailnet use Funnel. That's fine if this box is the
  only machine you'd ever run a server on; nothing is exposed until a machine
  turns Funnel on itself, as this command does.
- **This box only:** in the admin console, **Access controls** → **JSON
  editor** (use Chrome; it has loaded empty in Firefox), add a node attribute
  and tag the box `tag:marlow`:

  ```json
  "nodeAttrs": [{"target": ["tag:marlow"], "attr": ["funnel"]}]
  ```

  Either way, undoing it is a hand edit in that JSON editor: the console's
  other pages don't show it.

When it succeeds, the public address can take a few minutes to start
answering. Then pair each phone again from a fresh link (step 5 of the
install). The phone no longer needs Tailscale, though one that still has it on
works too. Updates keep the setting.

**Turn it off**, back to Tailscale on every device:

```bash
sudo ~marlow/marlow-server/deploy/update.sh --no-funnel
```

Phones then need Tailscale again. Turning Funnel off doesn't take
back the permission from the first step; that stays in your tailnet's policy
until you remove it.

Funnel only serves ports 443, 8443 and 10000, so a second Marlow on the same
box can't use it. It's a Tailscale beta, with bandwidth limits that a chat
assistant stays well within.

## Internet-accessible port

Not built yet. It would suit a cloud server, or a home box with a fixed
address or dynamic DNS and a router you can forward a port on: the box would
answer on a public port directly, with its own certificate for a domain you
own, and no Tailscale on the phone. If that's your setup, tell whoever invited
you; until then, use Tailscale on every device or Funnel.

## What it sets up

- A `marlow` user that owns everything Marlow runs as. It has no sudo.
- The server as a systemd service, `marlow-server`, restarted automatically
  if it stops answering.
- A sandbox around every Claude invocation. Marlow cannot read its own keys
  or tokens. Beyond reading its own files and the web, most actions ask your
  phone first. On a scheduled run, with nobody there to answer, they are
  refused.
- Marlow's own files in `~marlow/marlow-identity`: its character (`SOUL.md`),
  rules (`CLAUDE.md`), what it knows about you (`USER.md`), `memory/`, and its
  schedule (`cron/`). All of it is versioned with git automatically, and you
  can read these files in the app. It starts with two scheduled jobs: a
  morning briefing at 08:00 and a nightly memory tidy at 01:00. Ask Marlow to
  change or add to them.

## Day to day

- **Updating:** `sudo ~marlow/marlow-server/deploy/update.sh`
- **Another device:** `sudo -u marlow ~marlow/marlow-server/deploy/pair-device.sh`,
  then pair as in step 5.
- **A lost phone:** `sudo -u marlow ~marlow/marlow-server/deploy/devices.sh`
  lists paired devices (name, when, a short fingerprint); `devices.sh revoke
  <fingerprint>` cuts one off. Unpair in the app does the same for itself.

## When something's wrong

- **Is it running?** `systemctl status marlow-server`
- **What is it doing?** `journalctl -u marlow-server -f`
- **Phone can't reach it** ("Viewing an offline copy", or
  `DNS_PROBE_FINISHED_NXDOMAIN` in Chrome) **but the box is running:** with
  Tailscale on every device, the phone is looking up Marlow's address
  somewhere other than Tailscale. In the Tailscale app, turn **Use Tailscale
  DNS** on. If that isn't it, set Android's **Private DNS** to Off or Automatic, and Chrome's
  **Use secure DNS** to your current service provider. With Funnel, check
  `tailscale funnel status` on the box, and give it a few minutes after
  turning Funnel on.
- **Claude login lapsed:** this happens periodically, and Marlow warns you
  before it does. Run
  `sudo -iu marlow bash -c 'cd ~/.claude-login && ~/.local/bin/claude'` and
  log in again.

## Leaving

`sudo systemctl disable --now marlow-server`, then delete the `marlow` user
and its home directory. Back up `~marlow/marlow-identity` first if you want
to keep what Marlow learned. Revoke its Claude login from your account
settings. If you used Funnel, remove the permission from your tailnet's
policy too.

## If you're the one inviting

1. Send `deploy/install.sh`, `deploy/lib.sh` and a link to this page.
2. iPhone users: point them to the web app on the home screen (step 5).
   The native app can only be installed from your Mac with Xcode, with
   their phone in developer mode.
