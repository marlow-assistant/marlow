# The native iPhone app

Marlow's iPhone app isn't on the App Store. You build it yourself and install
it on your own phone from a Mac. If you'd rather not, the web app does
everything the native app does from the iPhone home screen, notifications
included ([`deploy/INSTALL.md`](../deploy/INSTALL.md), step 5).

## What you need

- **A Mac with a recent Xcode** (16 or later), from the Mac App Store.
- **An iPhone on iOS 18 or later**, with a cable to connect it the first time.
- **A paid Apple Developer Program membership** ($99 a year, at
  [developer.apple.com/programs](https://developer.apple.com/programs/)).
  A free Apple ID isn't enough: it can't sign an app that receives push
  notifications, and the box needs a push key from your account to send them.
  Without push, Marlow could never tell you when it has something for you.
- **A Marlow box already installed** ([`deploy/INSTALL.md`](../deploy/INSTALL.md)).

## 1. Make the app yours

The bundle identifiers are under the project author's Apple team, which yours
can't use. Pick a reverse-DNS prefix of your own, such as `com.yourname`, and
replace it throughout `ios/`, from the repository root:

```bash
grep -rl --exclude=README.md com.mikemason ios | xargs sed -i '' 's/com\.mikemason/com.yourname/g'
```

That covers the project, the entitlements (the app group and keychain groups
the notification extension shares with the app) and the code that names them.
The two must match, which is why it's all of them at once.

Then tell Xcode your team. Find your Team ID under **Membership details** at
[developer.apple.com/account](https://developer.apple.com/account), and put it
in `ios/Marlow/Config/Signing.local.xcconfig`, which git ignores:

```
DEVELOPMENT_TEAM = ABCDE12345
```

## 2. Build it to your phone

1. Open `ios/Marlow/Marlow.xcodeproj` in Xcode.
2. Connect the iPhone and trust the Mac when it asks. On the phone, turn on
   **Settings → Privacy & Security → Developer Mode** and let it restart.
3. Pick your iPhone as the run destination, choose the **Marlow** scheme and
   press Run. Xcode registers the identifiers and capabilities under your
   team the first time.

The app opens on its pairing screen. Pair it as in step 5 of
[`deploy/INSTALL.md`](../deploy/INSTALL.md) (**Scan pairing code**).

## 3. Let the box send notifications

The box sends iPhone notifications through Apple's push service, signed with a
key from your developer account. Chat works without it; notifications don't.

1. At [developer.apple.com/account](https://developer.apple.com/account), under
   **Keys**, create a key with **Apple Push Notifications service (APNs)**
   enabled. Download the `.p8` file (Apple only lets you download it once) and
   note its **Key ID**.
2. Copy it to the box (`scp` works), then move it into Marlow's key folder,
   readable only by `marlow`:

   ```bash
   sudo install -d -o marlow -g marlow -m 700 ~marlow/.marlow/apns
   sudo install -o marlow -g marlow -m 600 AuthKey_KEYID.p8 ~marlow/.marlow/apns/
   ```

   Marlow's sandbox keeps Claude out of that folder.
3. Tell the server where it is, in a settings file that the installer and
   updates leave alone:

   ```bash
   sudo systemctl edit marlow-server --drop-in=20-local
   ```

   ```ini
   [Service]
   Environment=MARLOW_APNS_KEY_PATH=/home/marlow/.marlow/apns/AuthKey_KEYID.p8
   Environment=MARLOW_APNS_KEY_ID=KEYID
   Environment=MARLOW_APNS_TEAM_ID=ABCDE12345
   Environment=MARLOW_APNS_TOPIC=com.yourname.Marlow
   ```

   The topic is the app's bundle identifier from step 1. Then
   `sudo systemctl restart marlow-server`.

An app built from Xcode uses Apple's development push service, which is the
server's default, so there's nothing more to set.

## Updating

Commit your identifier change from step 1 once (`git commit -am "My
identifiers"`), so a later pull can carry it along. Then, to update:
`git pull --rebase` in your clone, and Run again from Xcode. A build signed
with a paid membership lasts a year before it has to be rebuilt.

## Working on the app

`Marlow.xcodeproj` is generated from `project.yml` by
[XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).
After adding or removing a source file, run `xcodegen generate` in
`ios/Marlow`, or the new file isn't compiled.
