"""Settings loaded from env. No secrets or paths hardcoded elsewhere in server/."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# The web push contact when none is set. Apple's push service refuses it.
WEBPUSH_PLACEHOLDER_SUBJECT = "mailto:marlow@localhost"


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _default_working_dir() -> str:
    return os.environ.get("MARLOW_WORKING_DIR", "~/marlow-identity")


def _optional_env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _detect_tailscale_ip() -> str | None:
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 else None
        return ip or None
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError):
        return None


@dataclass(frozen=True)
class Settings:
    # The root-owned copy of cron/entrypoint.py that this instance's crontab
    # runs (deploy/README.md §6a). Per instance, so a second instance on the
    # box runs its own copy, from its own update, and a branch deployed to one
    # never changes the other's cron jobs (docs/tasks/marlow-for-taylor.md).
    cron_entrypoint: Path = field(
        default_factory=lambda: _env_path("MARLOW_CRON_ENTRYPOINT", "/opt/marlow/cron/entrypoint.py")
    )
    # The protected software checkout. Cron's root-owned standalone entrypoint
    # imports the server package from here, so this path must follow a deployed
    # checkout override just as the working/data/log paths do.
    server_repo: Path = field(
        default_factory=lambda: _env_path(
            "MARLOW_SERVER_REPO", str(Path(__file__).resolve().parent.parent)
        )
    )
    # The assistant's working tree — the only fs area normal tool use should touch.
    working_dir: Path = field(
        default_factory=lambda: _env_path("MARLOW_WORKING_DIR", _default_working_dir())
    )
    claude_bin: str = field(default_factory=lambda: os.environ.get("MARLOW_CLAUDE_BIN", "claude"))
    credentials_path: Path = field(
        default_factory=lambda: _env_path("MARLOW_CREDENTIALS_PATH", "~/.claude/.credentials.json")
    )

    # Server's own private state — deliberately outside the git working tree
    # (§5.3: secrets never on disk in the working tree).
    data_dir: Path = field(default_factory=lambda: _env_path("MARLOW_DATA_DIR", "~/.marlow/data"))
    log_dir: Path = field(default_factory=lambda: _env_path("MARLOW_LOG_DIR", "~/.marlow/logs"))

    # Uploaded image attachments (docs/tasks/ios-image-input.md). Deliberately
    # a sibling of data_dir/apns, NOT nested under either: data_dir is in
    # boundaries.SECRET_PATHS (hard-denied for Read, and tmpfs'd out of the
    # bwrap namespace entirely), so a "file not found" from inside there would
    # survive every permission fix. This directory is the opposite of a
    # secret — the CLI must be able to Read it.
    attachments_dir: Path = field(
        default_factory=lambda: _env_path("MARLOW_ATTACHMENTS_DIR", "~/.marlow/attachments")
    )

    # Per-invocation token/cache telemetry (docs/tasks/usage-telemetry.md).
    # Its own JSON-per-line file rather than marlow-server.log: the point is to
    # parse it later. On by default because it is the only source of real
    # cache-behaviour data and costs a line of I/O per invocation; the off
    # switch exists for tests and for anyone who wants nothing recorded at all.
    telemetry_enabled: bool = field(
        default_factory=lambda: os.environ.get("MARLOW_TELEMETRY_ENABLED", "1") != "0"
    )
    # Defaults *inside* log_dir rather than to a hardcoded ~/.marlow/logs path,
    # so setting MARLOW_LOG_DIR moves the whole logging surface as one. Getting
    # this wrong meant the test suite wrote telemetry to the real home dir
    # while every other log went to its sandbox.
    telemetry_path: Path = field(
        default_factory=lambda: _env_path(
            "MARLOW_TELEMETRY_PATH",
            os.environ.get("MARLOW_LOG_DIR", "~/.marlow/logs") + "/usage.jsonl",
        )
    )

    # Public (tailnet) API — bind to the tailscale interface, never 0.0.0.0.
    # Falls back to loopback + a loud warning if tailscale isn't detected, rather
    # than silently binding every interface (no-public-ingress is a hard constraint).
    #
    # A Funnel box (docs/tasks/funnel-ingress.md) binds loopback on purpose:
    # `tailscale funnel` forwards to 127.0.0.1 and nothing else can reach it.
    # MARLOW_FUNNEL=1 says so, so the fallback warning in app.main() can tell
    # the intended loopback from the accidental one.
    funnel: bool = field(default_factory=lambda: os.environ.get("MARLOW_FUNNEL") == "1")
    public_bind_host: str = field(
        default_factory=lambda: os.environ.get("MARLOW_PUBLIC_BIND_HOST")
        or ("127.0.0.1" if os.environ.get("MARLOW_FUNNEL") == "1" else None)
        or _detect_tailscale_ip()
        or "127.0.0.1"
    )
    public_port: int = field(default_factory=lambda: int(os.environ.get("MARLOW_PUBLIC_PORT", "8443")))

    # TLS for the public listener only (deploy/README.md §8b). A missing/unreadable
    # cert is a startup failure, not a silent plaintext downgrade — see the
    # "Decided" section of docs/marlow-v2-START.md. Local dev opts out
    # explicitly via MARLOW_ALLOW_PLAINTEXT=1; the internal loopback listener is
    # plaintext by design and has no TLS settings at all.
    tls_cert_path: Path = field(
        default_factory=lambda: _env_path("MARLOW_TLS_CERT", "~/.marlow/tls/marlow.crt")
    )
    tls_key_path: Path = field(
        default_factory=lambda: _env_path("MARLOW_TLS_KEY", "~/.marlow/tls/marlow.key")
    )
    allow_plaintext: bool = field(
        default_factory=lambda: os.environ.get("MARLOW_ALLOW_PLAINTEXT", "0") == "1"
    )

    # Internal API — the hook, cron and the deploy scripts talk to this, never
    # the tailnet. A Unix socket in an owner-only directory, not loopback TCP
    # (2026-10-16): 127.0.0.1 is reachable by every user on the box, and its
    # endpoints (pairing invites, cron turns, permission requests) carry no auth
    # of their own. With a second instance on the box, a Bash call in one
    # Marlow's turn could mint a pairing invite for the other. A 0700 directory
    # makes "can connect" mean "is this instance's user", enforced by the
    # kernel (docs/tasks/marlow-for-taylor.md). The directory is read-only
    # inside the bwrap namespace (invoke._confinement_prefix), so a turn can't
    # replace the socket with its own and answer its own permission prompts.
    internal_socket_path: Path = field(
        default_factory=lambda: _env_path("MARLOW_INTERNAL_SOCKET", "~/.marlow/run/internal.sock")
    )

    # Admin API — pairing invites, the device list, revocation. Its own socket,
    # not the internal one (review of the funnel-ingress branch, 2026-10-24):
    # a turn can connect to the internal socket, because the hook inside the
    # bwrap namespace must, so one approved Bash call could mint an invite,
    # redeem it on the public listener, and answer its own permission prompts
    # from then on. This directory is in boundaries.SECRET_PATHS, so inside
    # the namespace it is an empty tmpfs. The hook also denies Bash commands
    # that name it, but that is a string scan a script file walks past: with
    # MARLOW_BWRAP_ENABLED=0 it is the only guard, one more thing an
    # unconfined box gives up (deploy/README.md §11).
    admin_socket_path: Path = field(
        default_factory=lambda: _env_path("MARLOW_ADMIN_SOCKET", "~/.marlow/admin/admin.sock")
    )

    # Bound wait for the phone permission round-trip (gate #4). Our choice, not
    # a Claude-imposed cap — gate #1 found no sub-90s ceiling. Kept here since
    # health.py's oldest_pending_permission_age is read against it.
    t_hook_seconds: int = field(default_factory=lambda: int(os.environ.get("MARLOW_T_HOOK_SECONDS", "55")))

    # ~9-day anchored refresh-token window observed in gate #1; pre-warn this
    # many hours before refreshTokenExpiresAt lapses.
    auth_prewarn_hours: int = field(default_factory=lambda: int(os.environ.get("MARLOW_AUTH_PREWARN_HOURS", "24")))

    # How often the queue re-checks auth status while paused for reauth_needed
    # (plan §6a) — a periodic re-check rather than a tight loop. get_status()
    # is a small local file read + JSON parse, cheap enough to poll but not
    # cheap enough to spin on every event-loop tick.
    auth_recheck_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("MARLOW_AUTH_RECHECK_INTERVAL_SECONDS", "30"))
    )

    # How often the standalone auth watcher (auth_monitor.AuthWatcher) polls.
    # Unrelated to the queue's recheck above: that one is a tight-ish loop
    # waiting for a fix to land, this one is a background nag against a window
    # measured in hours, so 15 minutes is plenty and costs a file read.
    auth_watch_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("MARLOW_AUTH_WATCH_INTERVAL_SECONDS", "900"))
    )

    # Re-notify this often while still inside the pre-warn (or expired) window.
    # A single push at the top of a 24h window is one accidental swipe away
    # from being missed; 6h gives ~4 nudges across the window without becoming
    # noise Mike learns to ignore.
    auth_notify_repeat_hours: float = field(
        default_factory=lambda: float(os.environ.get("MARLOW_AUTH_NOTIFY_REPEAT_HOURS", "6"))
    )

    # Idle-triggered compaction (docs/tasks/idle-compaction.md). Kill switch,
    # on by default — once shipped, this is the mechanism that keeps a long
    # day cheap, not an experiment.
    idle_compaction_enabled: bool = field(
        default_factory=lambda: os.environ.get("MARLOW_IDLE_COMPACTION_ENABLED", "1") != "0"
    )
    # 50 minutes: just inside the CLI's one-hour prompt cache, so the summary
    # is paid only when the next message would have been a cold read anyway.
    # Was 290s, set when no cliff had been shown (gate1-results.md §8b swept
    # 240s HIT, 420s MISS, 720s HIT). Two weeks of usage.jsonl (2026-10-18)
    # then showed a clean one at exactly 3600s: resumed turns 55-60 min after
    # the last rewrote 1-6% of the prefix, turns just past 60 min rewrote
    # 12-94%. Compacting at 5 min threw away a conversation that was still
    # warm for 55 more -- a fifth of Mike's gaps -- for no saving. The CLI
    # chooses that TTL, so re-measure after a big CLI update
    # (docs/tasks/idle-compaction.md "On the idle window").
    idle_compaction_idle_seconds: float = field(
        default_factory=lambda: float(os.environ.get("MARLOW_IDLE_COMPACTION_IDLE_SECONDS", "3000"))
    )
    # ~55% of a 200K context window. A Claude Code team member is
    # cited in the doc recommending 50-60%: compacting early leaves the model
    # room to write a *thorough* summary, so earlier is better fidelity, not
    # just lower cost. The ~95% default most tools ship is called out as far
    # too late.
    idle_compaction_threshold_tokens: int = field(
        default_factory=lambda: int(os.environ.get("MARLOW_IDLE_COMPACTION_THRESHOLD_TOKENS", "110000"))
    )
    # Never compact below this many prefix tokens — summarizing costs more
    # than it reclaims below here. Matches the CLI's own reported auto-compact
    # floor (third-party-sourced, not Anthropic docs, per the doc) rather than
    # a number invented for this task.
    idle_compaction_floor_tokens: int = field(
        default_factory=lambda: int(os.environ.get("MARLOW_IDLE_COMPACTION_FLOOR_TOKENS", "50000"))
    )
    # Hysteresis: require the prefix to have grown by at least this many
    # tokens since this session's last compaction before compacting it again
    # — otherwise every idle stretch after a big session re-compacts a session
    # already as small as compaction can make it. ~25% of the default
    # threshold above. The doc says explicitly the exact fraction is a guess
    # and any defensible bound is correct; what matters is that the rule
    # exists.
    idle_compaction_growth_tokens: int = field(
        default_factory=lambda: int(os.environ.get("MARLOW_IDLE_COMPACTION_GROWTH_TOKENS", "27500"))
    )
    # How often the watcher re-checks its gates. Cheap — an in-memory
    # comparison, no file I/O, no subprocess — so this can be tighter than the
    # auth watcher's; the cost of polling often is negligible and the cost of
    # polling rarely is a longer wait past the idle window before compaction
    # fires.
    idle_compaction_poll_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("MARLOW_IDLE_COMPACTION_POLL_INTERVAL_SECONDS", "30"))
    )

    # Identity/memory injection (docs/tasks/loader-identity-injection.md). CLAUDE.md
    # is deliberately not configured here — the loader-task spike confirmed `claude
    # -p` auto-loads a cwd CLAUDE.md on its own, so injecting it again would double
    # it up in context. Paths are configurable so *what* gets loaded stays private
    # (identity content) while the loader mechanism itself (server/identity.py)
    # stays reusable open-source machinery.
    soul_path: Path = field(
        default_factory=lambda: _env_path("MARLOW_SOUL_PATH", _default_working_dir() + "/SOUL.md")
    )
    user_path: Path = field(
        default_factory=lambda: _env_path("MARLOW_USER_PATH", _default_working_dir() + "/USER.md")
    )
    memory_dir: Path = field(
        default_factory=lambda: _env_path("MARLOW_MEMORY_DIR", _default_working_dir() + "/memory")
    )
    # "inject_all": every memory/ file's full text goes into the composed
    # context. "index_only": one line per entry, bodies retrieved on demand
    # (docs/tasks/memory-index-split.md) — implemented, but the default stays
    # inject_all until the frontmatter descriptions are backfilled on the box
    # (scripts/memory_write_descriptions.py). Flipping this is the last step of
    # that task, not a separate decision.
    memory_mode: str = field(default_factory=lambda: os.environ.get("MARLOW_MEMORY_MODE", "inject_all"))
    # docs/tasks/cache-friendly-identity-injection.md: the composed identity
    # string is frozen per-session so a mid-session memory write doesn't force
    # `--append-system-prompt`'s whole cache block (and everything after it,
    # incl. history) to recreate on every subsequent turn. This bounds the
    # resulting staleness — a plausible workday session length, well beyond
    # the ~240s empirical cache-hit window (gate1-results §8b) since the
    # freeze's value (avoiding history-prefix invalidation) holds even across
    # individual cache-miss gaps within one long session.
    identity_freeze_max_age_seconds: float = field(
        default_factory=lambda: float(
            os.environ.get("MARLOW_IDENTITY_FREEZE_MAX_AGE_SECONDS", str(4 * 60 * 60))
        )
    )

    # Model/effort pin (docs/tasks/model-and-effort-config.md, piece 1). Prod
    # ran Sonnet at low when this was pinned. Medium since 2026-10-02 (Mike,
    # from the first-run dry run): a new user who finds the assistant a bit
    # dim on day one stops using it, and usefulness beats latency. The
    # clients' own defaults moved with it. The pin's point is to stop the
    # CLI's own default from being silently inherited: an unpinned CLI update
    # could move prod onto a different model or effort level with no signal
    # but latency or a bill.
    # `sonnet` is the alias, not a full version string — that pins the tier
    # while still tracking the latest Sonnet, which is the intended trade.
    # `or` rather than a plain default: os.environ.get returns "" for a var
    # that is SET BUT EMPTY, which is what `Environment=MARLOW_EFFORT=` in a
    # systemd unit produces when someone means "unset". That empty string
    # would reach argv as `--effort ""` and fail every turn from every source
    # until someone thought to read the unit file. Same guard as
    # public_bind_host above.
    model: str = field(default_factory=lambda: os.environ.get("MARLOW_MODEL") or "sonnet")
    effort: str = field(default_factory=lambda: os.environ.get("MARLOW_EFFORT") or "medium")

    # Piece 3: scheduled work thinks harder than chat. Nobody is waiting on the
    # 07:30 briefing, so higher effort's one real cost — latency — is not paid
    # there, while the quality of an unattended daily scan has a long half-life.
    # Not a spend concern: measured at ~2,900 thinking tokens against 5.8M
    # cache-read in the same day, effort is a latency dial, not a cost one.
    cron_effort: str = field(
        default_factory=lambda: os.environ.get("MARLOW_CRON_EFFORT") or "medium"
    )

    # APNs (docs/tasks/apns-push-delivery.md). Mike created separate .p8 keys
    # per environment on Apple's recommendation, so key id and environment are
    # independent settings rather than one derived from the other. All four of
    # key_path/key_id/team_id/topic must be set for push.py to construct a
    # client; absent, it logs once and no-ops rather than refusing to start —
    # local dev and CI have none of this.
    # Confine `claude -p` in a bubblewrap namespace where the secret paths do
    # not exist (probe CONFIRMED 2026-08-06, scripts/probe-bwrap-confinement.sh).
    # Requires deploy/apparmor-bwrap installed, or bwrap cannot make a namespace
    # on Ubuntu 24.04 -- see deploy/README.md §11.
    #
    # Defaults ON since 2026-08-06, after a real turn was watched through it on
    # the box (confined=True in the invoke log, no errors behind it, reply
    # delivered). A fresh host gets confinement without anyone remembering to
    # enable it, which is the right default for the layer that replaced
    # bash_policy.py as the secret boundary.
    #
    # MARLOW_BWRAP_ENABLED=0 opts out, and is not just a test hook: bwrap needs
    # an AppArmor exception on Ubuntu 24.04 (deploy/README.md §11), so a host
    # without the profile installed cannot spawn at all. That failure is loud --
    # invoke.py names bwrap in the FileNotFoundError -- but the escape hatch has
    # to exist for anyone bringing up a box in a different order.
    bwrap_enabled: bool = field(
        default_factory=lambda: os.environ.get("MARLOW_BWRAP_ENABLED", "1") == "1"
    )
    bwrap_bin: str = field(default_factory=lambda: os.environ.get("MARLOW_BWRAP_BIN", "bwrap"))

    apns_key_path: Path | None = field(
        default_factory=lambda: _optional_env_path("MARLOW_APNS_KEY_PATH")
    )
    apns_key_id: str | None = field(default_factory=lambda: os.environ.get("MARLOW_APNS_KEY_ID"))
    apns_team_id: str | None = field(default_factory=lambda: os.environ.get("MARLOW_APNS_TEAM_ID"))
    apns_topic: str | None = field(default_factory=lambda: os.environ.get("MARLOW_APNS_TOPIC"))
    apns_environment: str = field(
        default_factory=lambda: os.environ.get("MARLOW_APNS_ENVIRONMENT", "sandbox")
    )

    # Where the built web client lives (docs/tasks/portable-web-client.md).
    # Overridable so tests can point at a tmp dir and a deployment can serve a
    # build from outside the checkout. Missing is fine: public_app still
    # starts and just answers the client paths with a 404 + hint.
    web_dist_dir: Path = field(
        default_factory=lambda: _env_path(
            "MARLOW_WEB_DIST_DIR", str(Path(__file__).resolve().parent.parent / "web" / "dist")
        )
    )
    # VAPID `sub` claim. RFC 8292 requires one; push services only use it as a
    # contact if we misbehave. Chrome's and Firefox's services accept this
    # placeholder; Apple's (Safari, incl. an iPhone home-screen web app) does
    # not: every send got 403 until a real `mailto:` was set (Mike's box,
    # 2026-10-25). install.sh --email writes the box owner's own address in a
    # drop-in. Boxes installed before that still run on the placeholder until
    # it is set; webpush.send's 403 log says so.
    webpush_subject: str = field(
        default_factory=lambda: os.environ.get("MARLOW_WEBPUSH_SUBJECT", WEBPUSH_PLACEHOLDER_SUBJECT)
    )

    # Speech to text for the web client (server/transcribe.py,
    # docs/tasks/voice-mode-android.md). The model is a sherpa-onnx NeMo
    # transducer folder under data_dir, so it sits with the server's private
    # state: hard-denied to the agent and out of its bwrap namespace, where a
    # turn can't swap the model the server loads. The 110M is the default
    # because it fits a 2-core, 4 GB VPS beside a turn; a box with memory to
    # spare can name sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8 instead.
    stt_model: str = field(
        default_factory=lambda: os.environ.get("MARLOW_STT_MODEL")
        or "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8"
    )
    stt_threads: int = field(
        default_factory=lambda: int(os.environ.get("MARLOW_STT_THREADS") or os.cpu_count() or 1)
    )

    def __post_init__(self) -> None:
        # It is written into a crontab command line, so it may hold nothing a
        # shell or cron would read as syntax.
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", str(self.cron_entrypoint)):
            raise ValueError(f"MARLOW_CRON_ENTRYPOINT must be a plain absolute path, got {str(self.cron_entrypoint)!r}")
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", self.stt_model):
            raise ValueError(f"MARLOW_STT_MODEL must be a model folder name, got {self.stt_model!r}")
        # push.py does `apns_environment == "sandbox"` — a typo'd env var
        # (wrong case, extra space) would silently resolve to "production"
        # instead of failing loud, routing a sandbox-issued token at the
        # production APNs endpoint where it's simply invalid. Fail at startup
        # instead.
        if self.apns_environment not in ("sandbox", "production"):
            raise ValueError(
                f"MARLOW_APNS_ENVIRONMENT={self.apns_environment!r} is invalid; "
                'must be "sandbox" or "production"'
            )

    @property
    def device_tokens_path(self) -> Path:
        return self.data_dir / "device_tokens.json"

    @property
    def vapid_private_key_path(self) -> Path:
        """Web Push signing key (server/webpush.py). In data_dir, so it is
        covered by SECRET_PATHS like the device tokens it protects."""
        return self.data_dir / "vapid_private.pem"

    @property
    def session_state_path(self) -> Path:
        """Attended-session + retirement bookkeeping (server/session_state.py).

        In data_dir with the device tokens rather than alongside the logs: it
        is server-private state the CLI has no business reading, and data_dir
        is already both hard-denied for Read and tmpfs'd out of the bwrap
        namespace. Not a secret, but nothing is gained by making it visible.
        """
        return self.data_dir / "session_state.json"

    @property
    def last_location_path(self) -> Path:
        """The rounded last-known position per device (server/location.py).
        In data_dir for the session_state reason: server-private, hard-denied
        for Read and tmpfs'd out of the bwrap namespace."""
        return self.data_dir / "last_location.json"

    @property
    def preferences_path(self) -> Path:
        """Choices every client shares, such as Marlow's face
        (server/preferences.py). In data_dir for the session_state reason."""
        return self.data_dir / "preferences.json"

    @property
    def schedules_path(self) -> Path:
        """Marlow's declarative schedule store (docs/tasks/schedules-and-reminders.md).

        In the working tree, not data_dir, because the model writes it — it is
        the one server-consumed file `file_policy` auto-allows.

        Defined HERE rather than in server/schedules.py, which owns it, because
        server/turn_context.py has to read it too and cannot import that module
        (schedules.py imports is_valid_timezone from turn_context). Re-spelling
        the path in each reader would make a later move fail SILENTLY: the
        renderer would write the new location, the reader would find nothing
        where it looked, and its feature would switch off with every test still
        green (/code-review medium, 2026-08-31).
        """
        return self.working_dir / "cron" / "schedules.json"

    @property
    def ideas_path(self) -> Path:
        """Marlow's ideas store (docs/tasks/ideas-panel.md). Model-written.

        In its own `ideas/` directory, NOT `memory/`: under the default
        `memory_mode=inject_all` every file in `memory/` is pasted into every
        turn, and neither this nor the responses file below is worth that.
        Defined here for the same one-spelling reason as `schedules_path`.
        """
        return self.working_dir / "ideas" / "ideas.json"

    @property
    def ideas_responses_path(self) -> Path:
        """Mike's taps on ideas. Server-written only; the model reads it so the
        nightly job can learn from dismissals. `file_policy` leaves it out of
        the vetted-write set, so a model write reaches the phone as a prompt
        (and is denied unattended). That makes it a record of taps, not a proof
        of one."""
        return self.working_dir / "ideas" / "ideas-responses.json"

    @property
    def stt_model_dir(self) -> Path:
        """The speech model's folder (see `stt_model`). A name, not a path, in
        the env var (checked in __post_init__), so it can only ever point
        inside data_dir/models."""
        return self.data_dir / "models" / self.stt_model

    @property
    def stt_used_marker_path(self) -> Path:
        """Present once anyone has dictated on this box: from then on the
        speech model loads at every start (server/transcribe.py). Holds a
        short note saying so, and the date of first use."""
        return self.data_dir / "stt_has_been_used"

    @property
    def pairing_challenges_path(self) -> Path:
        return self.data_dir / "pairing_challenges.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attachments_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


settings = Settings()
