#!/usr/bin/env bash
# Installs Marlow on a fresh Ubuntu 24.04 box you control. Run as root:
#
#   sudo ./deploy/install.sh --user-name "Priya" --timezone Europe/London \
#       --email priya@example.com
#
# Idempotent: every step checks before it acts, so the answer to any stop --
# the repository isn't readable yet, Claude is not logged in yet -- is to do
# the one thing it asks and run the same command again. Run from a terminal, it
# waits at those two points instead of stopping, and stops only if the wait
# didn't fix it (a friend's install, 2026-10-25: copying the login command out
# and re-running the installer was the clumsiest part). It never overwrites a
# seeded identity (the assistant's diverged memory) or a working checkout.
#
# What it sets up is deploy/README.md §1-§7 and §11 for a single box, minus the
# optional pieces (the identity push, the off-host dead-man's switch, the
# egress allowlist). After install, updates are deploy/update.sh as usual.
#
# Before running it:
#   - Tailscale is installed and `tailscale up` has joined this box to your
#     tailnet, with MagicDNS and HTTPS certificates enabled in the admin console
#     (DNS page). The phone reaches Marlow only over the tailnet.
#   - You have a Claude subscription to log in with, once, when it asks.
#
# A SECOND INSTANCE on the same box (docs/tasks/marlow-for-taylor.md) is the
# same command with --instance: its own Linux user, never in the marlow group,
# with its own Claude login and connectors, on its own port.
#
#   sudo ./deploy/install.sh --instance taylor --public-port 8444 \
#       --user-name "Taylor" --timezone America/Edmonton --email you@example.com
#
# It runs marlow-server@<user> (update.sh renders the template), stops once for
# that user's own Claude login, and leaves the first instance alone.
set -euo pipefail

REPO_URL="https://github.com/marlow-assistant/marlow.git"
REF=main
USER_NAME=""
TIMEZONE=""
EMAIL=""
INSTANCE=""
PUBLIC_PORT=""
FUNNEL_FLAG=()

usage() {
    cat >&2 <<'EOF'
usage: sudo deploy/install.sh --user-name NAME --email ADDRESS [--timezone ZONE]
                              [--repo URL] [--ref BRANCH]
                              [--instance USER --public-port PORT] [--funnel]

  --user-name   what Marlow calls you to start with ("Priya"); it checks on first chat
  --email       your address, given to the browser push services (Apple's, Google's,
                Mozilla's) as the contact for this box's notifications; asked for
                if left off
  --timezone    your home IANA zone (default: this box's zone, unless that is UTC)
  --repo        where to clone the software from (default: Marlow's public GitHub repo)
  --ref         branch to install (default: main)
  --instance    a second instance's Linux user (its own Claude login, its own port)
  --public-port that instance's tailnet port (not 8443, which the first one has)
  --funnel      reach this box from the internet through Tailscale Funnel, so a phone
                needs no Tailscale (deploy/INSTALL.md, "Funnel"); off by default
EOF
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user-name) USER_NAME="${2:-}"; shift 2 || usage ;;
        --timezone) TIMEZONE="${2:-}"; shift 2 || usage ;;
        --email) EMAIL="${2:-}"; shift 2 || usage ;;
        --repo) REPO_URL="${2:-}"; shift 2 || usage ;;
        --ref) REF="${2:-}"; shift 2 || usage ;;
        --instance) INSTANCE="${2:-}"; shift 2 || usage ;;
        --public-port) PUBLIC_PORT="${2:-}"; shift 2 || usage ;;
        --funnel) FUNNEL_FLAG=(--funnel); shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# sudo -u keeps the caller's working directory, and marlow can't enter the admin
# user's home (750 on Ubuntu 24.04), so git and pip fail there with "Permission
# denied". The first friend's install hit it running from ~/marlow-install
# (2026-10-02). Nothing below uses a relative path; lib.sh is already sourced.
cd /

die() { echo "install.sh: $*" >&2; exit 1; }
step() { echo; echo "== $* =="; }

# --- validate everything before touching the box -----------------------------
# The name lands in USER.md, and used to land in a systemd drop-in as well
# (Environment="..."), where `"`, `\` and `%` are syntax. A plain name needs
# none of them, so refuse them rather than escape them.
[[ -n "$USER_NAME" ]] || { echo "--user-name is required" >&2; usage; }
if ! [[ "$USER_NAME" =~ ^[[:alpha:]][[:alpha:]\ .\'-]{0,39}$ ]]; then
    die "--user-name: letters, spaces, . ' - only, up to 40 characters (got: $USER_NAME)"
fi
if [[ -z "$TIMEZONE" ]]; then
    TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    [[ -n "$TIMEZONE" ]] || die "could not read this box's timezone; pass --timezone"
    # Cloud and VM images ship in UTC whatever their owner's zone, and the
    # starter schedules are seeded in this zone: a morning briefing at 08:00
    # UTC is midnight in Vancouver. Refused, not warned -- a warning scrolls
    # away under apt's output. Someone who really lives in UTC says so.
    case "$TIMEZONE" in
        UTC|UCT|GMT|Zulu|Universal|Etc/*)
            die "this box's timezone is $TIMEZONE, which is usually a server default rather than yours.
    Pass your own, e.g. --timezone America/Vancouver (or --timezone UTC if that really is it)." ;;
    esac
fi
if ! python3 -c 'import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])' "$TIMEZONE" 2>/dev/null \
    || [[ "$TIMEZONE" != */* && "$TIMEZONE" != UTC ]]; then
    die "--timezone must be an IANA name like Europe/London (got: $TIMEZONE)"
fi
# Web push names a contact for whoever runs the sending server (RFC 8292's
# `sub`). Apple's service refuses a placeholder: Safari notifications got 403
# with mailto:marlow@localhost until a real address was set (Mike's box,
# 2026-10-25). It is the box owner's own address, never a default someone
# else's pushes would carry, so it is asked for. It lands in a systemd
# drop-in, so the name's rule again: refuse anything not plain.
if [[ -z "$EMAIL" && -t 0 && -t 1 ]]; then
    echo "Marlow's notifications go through Apple's, Google's or Mozilla's push service,"
    echo "which asks for a contact address for this box. It is not shown to anyone else."
    read -r -p "Your email address: " EMAIL || exit 3
    # Every STOPPED below says "run the same command again": make it carry this.
    echo "   (when running this again, add: --email $EMAIL)"
fi
[[ -n "$EMAIL" ]] || die "--email is required: the push services' contact for this box's notifications"
if ! [[ "$EMAIL" =~ ^[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] || (( ${#EMAIL} > 254 )); then
    die "--email must be a plain address like you@example.com (got: $EMAIL)"
fi
# Same rule, and same reason, as update.sh's --branch: a ref that starts with
# `-` reaches git as an option however it is quoted.
[[ "$REF" =~ ^[A-Za-z0-9._/][A-Za-z0-9._/-]*$ ]] || die "--ref is not a plain branch name: $REF"
[[ "$REPO_URL" =~ ^https://[A-Za-z0-9._:/@~-]+$ ]] || die "--repo must be an https:// URL"
# These land in a systemd drop-in and a cron line, so the same rule as the
# name: refuse anything that isn't plain rather than escape it.
if [[ -n "$INSTANCE" ]]; then
    [[ "$INSTANCE" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "--instance must be a plain Linux user name (got: $INSTANCE)"
    [[ "$INSTANCE" != marlow ]] || die "--instance marlow is the first instance; leave --instance off for it"
    [[ "$PUBLIC_PORT" =~ ^[1-9][0-9]{3,4}$ ]] && (( PUBLIC_PORT >= 1024 && PUBLIC_PORT <= 65535 )) \
        || die "--instance needs --public-port, 1024-65535 (got: ${PUBLIC_PORT:-none})"
    [[ "$PUBLIC_PORT" != 8443 ]] || die "--public-port 8443 is the first instance's"
elif [[ -n "$PUBLIC_PORT" ]]; then
    die "--public-port is for --instance; the first instance is on 8443"
fi

[[ "$(id -u)" -eq 0 ]] || die "run with sudo -- it creates a user and installs system units"

MARLOW_USER="${INSTANCE:-marlow}"
HOME_DIR="/home/$MARLOW_USER"
if [[ -n "$INSTANCE" ]]; then
    UNIT="marlow-server@$MARLOW_USER"
    CERT_TIMER="marlow-cert-renew@$MARLOW_USER.timer"
    WATCHDOG_CRON="/etc/cron.d/marlow-watchdog-$MARLOW_USER"
else
    UNIT=marlow-server
    CERT_TIMER=marlow-cert-renew.timer
    PUBLIC_PORT=8443
fi
REPO_DIR="$HOME_DIR/marlow-server"
VENV="$HOME_DIR/.venvs/marlow-server"
CLAUDE_BIN="$HOME_DIR/.local/bin/claude"

# Runs a fixed command as marlow. Arguments go through as argv, never through a
# shell string, so nothing typed on the command line is ever re-parsed.
as_marlow() { sudo -u "$MARLOW_USER" -H "$@"; }

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
        echo "WARNING: this is ${PRETTY_NAME:-an unknown OS}; Marlow is only tested on Ubuntu 24.04." >&2
    fi
fi

# --- 1. tailnet ----------------------------------------------------------------
step "checking tailscale"
command -v tailscale >/dev/null \
    || die "tailscale is not installed. Install it and join your tailnet first:
    curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
TAILNET_NAME="$(tailnet_name)"
[[ -n "$TAILNET_NAME" ]] \
    || die "this box has no tailnet name. Run 'sudo tailscale up', and turn on MagicDNS in the admin console."
echo "   this box is $TAILNET_NAME"

# --- 2. packages -----------------------------------------------------------------
step "installing packages"
export DEBIAN_FRONTEND=noninteractive
PACKAGES=(git python3-venv python3-pip bubblewrap curl cron sudo gpg qrencode)
if [[ -n "$INSTANCE" ]]; then
    # A second instance shares this box with a running one: install only what
    # is missing, since `apt-get install` of a present package can upgrade it
    # (bubblewrap under the first instance's turns, for one).
    MISSING=()
    for pkg in "${PACKAGES[@]}"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
    done
    if [[ ${#MISSING[@]} -gt 0 ]]; then
        apt-get update -q
        apt-get install -y -q "${MISSING[@]}"
    else
        echo "   all present"
    fi
else
    apt-get update -q
    apt-get install -y -q "${PACKAGES[@]}"
fi

# Node builds the web client (update.sh -> build-web.sh, which refuses anything
# older). Ubuntu 24.04's own nodejs is 18, so this adds NodeSource's signed apt
# repo for 22 -- their documented manual setup, not their piped script -- and
# leaves a new-enough node alone, however it got there.
NODE_FLOOR="20.19.0"  # keep in step with build-web.sh's NODE_FLOOR
node_ok() {
    command -v node >/dev/null 2>&1 || return 1
    local have; have="$(node --version)"; have="${have#v}"
    [[ "$(printf '%s\n%s\n' "$NODE_FLOOR" "$have" | sort -V | head -n1)" == "$NODE_FLOOR" ]]
}
step "installing node (for the web client)"
if node_ok; then
    echo "   node $(node --version) is new enough"
else
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list
    apt-get update -q
    apt-get install -y -q nodejs
    node_ok || die "node $(node --version 2>/dev/null || echo missing) is older than $NODE_FLOOR after install"
    echo "   node $(node --version)"
fi

# --- 3. the marlow user --------------------------------------------------------
step "creating the $MARLOW_USER user"
if id "$MARLOW_USER" >/dev/null 2>&1; then
    echo "   exists"
else
    # A real shell, not nologin: `claude` needs one interactive login (step 7).
    # marlow never gets sudo -- nothing here or in update.sh grants it.
    useradd --system --create-home --shell /bin/bash "$MARLOW_USER"
fi
chmod 750 "$HOME_DIR"
# A second instance must not be able to read the first's home (750, group
# marlow): that is where its mail, memory and device tokens live.
# Captured, then matched, not piped into grep: under pipefail that can fail on SIGPIPE.
INSTANCE_GROUPS=" $(id -nG "$MARLOW_USER") "
if [[ -n "$INSTANCE" && "$INSTANCE_GROUPS" == *" marlow "* ]]; then
    die "$MARLOW_USER is in the marlow group, which can read the first instance's files.
    Remove it first: sudo gpasswd -d $MARLOW_USER marlow"
fi
if ! as_marlow git config --global user.name >/dev/null; then
    as_marlow git config --global user.name "Marlow"
    as_marlow git config --global user.email "marlow@$TAILNET_NAME"
fi

# --- 4. the software -----------------------------------------------------------
step "getting the software"
if [[ -d "$REPO_DIR/.git" ]]; then
    echo "   $REPO_DIR already cloned"
else
    # A public repo over HTTPS needs no credential. A wrong URL there makes
    # GitHub ask for one, and git would stop at a Username prompt that reads
    # as the installer's own question.
    as_marlow env GIT_TERMINAL_PROMPT=0 git clone --branch "$REF" "$REPO_URL" "$REPO_DIR"
fi

step "installing Python dependencies"
[[ -x "$VENV/bin/python" ]] || as_marlow python3 -m venv "$VENV"
as_marlow "$VENV/bin/pip" install -q -r "$REPO_DIR/server/requirements.txt"

step "installing the Claude CLI (as $MARLOW_USER)"
if [[ -x "$CLAUDE_BIN" ]]; then
    echo "   present: $(as_marlow "$CLAUDE_BIN" --version 2>/dev/null || echo unknown version)"
else
    # The vendor's installer, run as the unprivileged user into ~/.local. The
    # service pins it there (MARLOW_CLAUDE_BIN, DISABLE_AUTOUPDATER).
    as_marlow bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi

# --- 5. confinement (deploy/README.md §11) ----------------------------------------
step "installing the bubblewrap AppArmor profile"
# Box-wide, and the first instance's turns depend on it: a second instance
# leaves an installed one alone and only checks it works for its own user.
if [[ -n "$INSTANCE" && -f /etc/apparmor.d/bwrap ]]; then
    echo "   already installed; leaving it to the first instance's install"
else
    install -m 644 "$REPO_DIR/deploy/apparmor-bwrap" /etc/apparmor.d/bwrap
    apparmor_parser -r /etc/apparmor.d/bwrap
fi
# The confinement is on by default and every turn fails without it, loudly.
# Better to find out here than on the first message.
as_marlow bwrap --dev-bind / / -- /bin/true \
    || die "bubblewrap cannot create a namespace as $MARLOW_USER -- see deploy/README.md §11"

# --- 6. runtime dirs and identity ------------------------------------------------
step "creating runtime directories"
# Created AS marlow, not by root with -o: root making directories under a tree
# marlow owns follows a symlink planted there and chowns its target (update.sh's
# as-marlow note records the /security-review finding, 2026-08-30).
as_marlow install -d -m 700 "$HOME_DIR/.marlow" "$HOME_DIR/.marlow/data" "$HOME_DIR/.marlow/logs"

step "installing speech to text (the web client's mic and voice mode)"
# The same script update.sh runs; never fails the install (deploy/install-voice.sh).
as_marlow env MARLOW_VOICE="${MARLOW_VOICE:-1}" bash "$REPO_DIR/deploy/install-voice.sh" \
    "$REPO_DIR" "$VENV" "$HOME_DIR/.marlow/data" || true

step "seeding the starter identity"
# A box from before the working dir's rename (deploy/rename-working-dir.sh)
# still has it at ~/claude-assistant, and update.sh, which runs after this,
# moves it. Seeding here too would leave two identities, and the move refuses.
if as_marlow test -e "$HOME_DIR/marlow-identity/SOUL.md" \
    || as_marlow test -e "$HOME_DIR/claude-assistant/SOUL.md"; then
    echo "   already seeded -- leaving the assistant's own files alone"
else
    as_marlow "$REPO_DIR/deploy/seed-identity.sh" --template \
        --user-name "$USER_NAME" --timezone "$TIMEZONE"
fi

# No systemd drop-in for the name. Earlier installs wrote MARLOW_USER_NAME to
# marlow-server.service.d/10-install.conf for the per-turn context block, and a
# rerun with a corrected name then split it from USER.md. The block no longer
# names anyone and USER.md is the only home (install-review-leftovers.md item
# 1). A drop-in left by an older install is inert.

# --- 7. Claude login -------------------------------------------------------------
# A second instance logs in to ITS user's own Claude account, never the first
# instance's: each user has its own ~/.claude, and the connectors (Gmail,
# Calendar) come with the account that logs in here.
if [[ -n "$INSTANCE" ]]; then
    WHOSE="$MARLOW_USER's own Claude account, not yours"
    WHO_SIGNS_IN="Send them the login link it prints; they sign in and send
back the code it shows, which you paste in."
else
    WHOSE="your Claude account"
    WHO_SIGNS_IN="Follow the login link it prints."
fi
step "checking the Claude login"
CLAUDE_LOGIN_CMD="sudo -iu $MARLOW_USER bash -c 'mkdir -p ~/.claude-login && cd ~/.claude-login && ~/.local/bin/claude'"
CLAUDE_LOGIN_STEPS="Pick a colour theme (any), choose to log in with your Claude subscription,
and sign in. $WHO_SIGNS_IN Then it asks whether to trust
the folder: choose \"No, exit\" (the folder is empty, Marlow does not run there,
and you stay logged in). If it doesn't ask, type /exit."
if ! as_marlow test -s "$HOME_DIR/.claude/.credentials.json"; then
    if [[ -t 0 && -t 1 ]]; then
        cat <<EOF

Marlow needs to log in to $WHOSE, once. Starting Claude now.

$CLAUDE_LOGIN_STEPS

EOF
        read -r -p "Press Enter to start Claude: " _ || exit 3
        bash -c "$CLAUDE_LOGIN_CMD" || true
    fi
    if ! as_marlow test -s "$HOME_DIR/.claude/.credentials.json"; then
        cat <<EOF

STOPPED: Marlow needs to log in to $WHOSE, once.

    $CLAUDE_LOGIN_CMD

$CLAUDE_LOGIN_STEPS Then run the same
install command again -- it picks up from here.
EOF
        exit 3
    fi
    echo "   logged in"
fi
# The exact call shape server/invoke.py uses, stdin closed included. If this
# fails, nothing after it can work.
# Compared whole, not searched: an error like "OAuth token has expired"
# contains "ok" as a substring, and a substring match passes it.
REPLY="$(as_marlow bash -c 'cd ~ && "$0" -p "reply with the single word: ok" </dev/null' "$CLAUDE_BIN" 2>&1 || true)"
NORMALIZED="$(printf '%s' "$REPLY" | tr -d '[:space:][:punct:]' | tr '[:upper:]' '[:lower:]')"
if [[ "$NORMALIZED" != ok ]]; then
    die "the Claude CLI did not answer as $MARLOW_USER. It said:
$REPLY"
fi
echo "   claude -p answers"

# --- 8. TLS (deploy/README.md §8b) ---------------------------------------------
step "issuing the tailnet TLS certificate"
# Run from the checkout this once, by the admin running the install. The
# timer runs update.sh's root-owned copy (/opt/marlow/bin, or the instance's
# /opt/marlow/instances/<user>/bin) from then on.
# A second instance gets its own copy of the same node cert, in its own home
# (it can't read the first's); its marlow-cert-renew@ timer, which update.sh
# installs, keeps that copy fresh.
MARLOW_TAILNET_NAME="$TAILNET_NAME" MARLOW_TLS_OWNER="$MARLOW_USER" \
    MARLOW_TLS_DIR="$HOME_DIR/.marlow/tls" MARLOW_SYSTEMD_UNIT="$UNIT" \
    "$REPO_DIR/deploy/renew-cert.sh" \
    || die "tailscale could not issue a certificate. Turn on HTTPS certificates
    in the Tailscale admin console (DNS page), then run this again."
if [[ -z "$INSTANCE" ]]; then
    install -o root -g root -m 644 "$REPO_DIR/deploy/marlow-cert-renew.service" /etc/systemd/system/
    install -o root -g root -m 644 "$REPO_DIR/deploy/marlow-cert-renew.timer" /etc/systemd/system/
fi

# --- 9. the service -------------------------------------------------------------
# update.sh does the rest, so an install and an update can never disagree about
# it: the root-owned copies of everything root runs (cron entrypoint, cert
# renewal, watchdog), the permission policy, the units, restart, and the health
# check.
# The unit's own settings, in its drop-in, written before update.sh first
# starts it. Rewritten on every run from these arguments, so hand-added
# settings (a model, say) go in a 20-local.conf beside them.
step "writing $UNIT's settings"
DROPIN_DIR="/etc/systemd/system/$UNIT.service.d"
install -d -o root -g root -m 755 "$DROPIN_DIR"
cat > "$DROPIN_DIR/15-push-contact.conf" <<EOF
# Written by deploy/install.sh --email; rerun it to change this.
[Service]
Environment=MARLOW_WEBPUSH_SUBJECT=mailto:$EMAIL
EOF
chmod 644 "$DROPIN_DIR/15-push-contact.conf"
if [[ -n "$INSTANCE" ]]; then
    cat > "$DROPIN_DIR/10-instance.conf" <<EOF
# Written by deploy/install.sh --instance; rerun it to change these.
[Service]
Environment=MARLOW_PUBLIC_PORT=$PUBLIC_PORT
Environment=MARLOW_CRON_ENTRYPOINT=/opt/marlow/instances/$MARLOW_USER/cron/entrypoint.py
EOF
    chmod 644 "$DROPIN_DIR/10-instance.conf"
fi
step "installing and starting $UNIT"
MARLOW_USER="$MARLOW_USER" "$REPO_DIR/deploy/update.sh" --branch "$REF" ${FUNNEL_FLAG[@]+"${FUNNEL_FLAG[@]}"}
systemctl enable "$UNIT" "$CERT_TIMER"
systemctl start "$CERT_TIMER"

step "installing the watchdog"
# Root runs it, so it runs update.sh's root-owned copy -- never marlow's
# checkout, which marlow can write (the same rule as cron/entrypoint.py, §6a).
if [[ -n "$INSTANCE" ]]; then
    # Values validated above: a plain user name, so nothing here is syntax.
    cat > "$WATCHDOG_CRON" <<EOF
# Written by deploy/install.sh --instance: restart $UNIT if it stops answering.
*/2 * * * * root MARLOW_USER=$MARLOW_USER MARLOW_SYSTEMD_UNIT=$UNIT /opt/marlow/instances/$MARLOW_USER/bin/watchdog.sh >> /var/log/marlow-watchdog-$MARLOW_USER.log 2>&1
EOF
else
    cat > /etc/cron.d/marlow-watchdog <<'EOF'
# Written by deploy/install.sh: restart marlow-server if it stops answering.
*/2 * * * * root /opt/marlow/bin/watchdog.sh >> /var/log/marlow-watchdog.log 2>&1
EOF
fi

# --- done ---------------------------------------------------------------------
step "done"
cat <<EOF
Marlow is running at https://$TAILNET_NAME:$PUBLIC_PORT for $USER_NAME ($TIMEZONE).

Next: put the device you'll use on your tailnet (not needed with --funnel), then
pair it with the link below: scan the QR with an Android phone's camera, open the
link in Chrome or Firefox, or paste it into the Marlow iPhone app (TestFlight).
For another device later:
    sudo -u $MARLOW_USER env MARLOW_PUBLIC_PORT=$PUBLIC_PORT $REPO_DIR/deploy/pair-device.sh
EOF
# As marlow: it needs no privilege, and root running a file from marlow's
# checkout is what /opt/marlow/bin exists to avoid (§6a).
as_marlow env MARLOW_TAILNET_NAME="$TAILNET_NAME" MARLOW_PUBLIC_PORT="$PUBLIC_PORT" "$REPO_DIR/deploy/pair-device.sh" || true
