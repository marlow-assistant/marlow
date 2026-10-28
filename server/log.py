"""Redacting logger + rotation (plan §3.4/§10.3). No secret ever reaches a log line."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from server.config import settings

# Field-targeted redaction, not a blanket high-entropy-string scrubber — the
# fields we actually emit near secrets are known and few. Extend this tuple if
# a new secret-bearing field shows up rather than reaching for a generic regex.
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'("?(?:access|refresh)[Tt]oken"?\s*[:=]\s*)"[^"]*"'),
    re.compile(r'("?device_token"?\s*[:=]\s*)"[^"]*"'),
    re.compile(r'("?apns_token"?\s*[:=]\s*)"[^"]*"'),
    re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+"),
)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern in _REDACT_PATTERNS:
            msg = pattern.sub(r"\1[REDACTED]", msg)
        record.msg = msg
        record.args = ()
        return True


_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

DENY_LOG_NAME = "marlow-denials.log"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured (module-level singletons per name)

    settings.ensure_dirs()
    logger.setLevel(logging.INFO)
    logger.addFilter(RedactingFilter())

    formatter = logging.Formatter(_FORMAT)

    file_handler = RotatingFileHandler(
        settings.log_dir / "marlow-server.log", maxBytes=10_000_000, backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def get_deny_logger() -> logging.Logger:
    """The tool-denial log — a SEPARATE file from marlow-server.log, on purpose.

    Two independent reasons, either sufficient:

    **It must not share a RotatingFileHandler with the server.** `get_logger`'s
    handler is rotating, and RotatingFileHandler is not multi-process safe: the
    long-lived server process holds marlow-server.log open continuously, and
    hooks/pretooluse.py is a SHORT-LIVED SEPARATE PROCESS spawned per tool call.
    Two processes reaching 10MB together can both attempt the rollover, and the
    classic outcome is one renaming the file out from under the other -- leaving
    the server appending to an unlinked inode and silently losing the very
    evidence stream this log exists to provide.

    **It is read by grepping.** The promotion rule in
    docs/tasks/marlow-harness-affordances.md is "the same avoidable denial twice
    earns a line in the always-on identity block", which means a human or an
    agent periodically reads this. One file containing only denials is a `cat`;
    the same lines interleaved with every turn's ordinary server chatter is a
    filter someone has to get right. ALL FIVE denial classes land here for that
    reason -- hard, unattended, declined, timed-out, no-answer -- each tagged
    `denied/<kind>` so the classes stay separable, because they are emphatically
    not interchangeable evidence. server/deny_detail.py has the table of what a
    repeat of each one actually indicts; read it before promoting anything.

    **Deliberately NOT rotating**, which is the tradeoff this choice buys:

      - Rotation is what makes concurrent writers unsafe, so a rotating handler
        here would reintroduce the exact hazard above between hook processes
        (one `claude -p` can run tools in parallel, so concurrent hooks are
        real even though the work queue serializes the CLI calls themselves).
      - Plain appends from multiple processes ARE safe: the handler opens with
        mode="a" (O_APPEND), and a single write below PIPE_BUF (4096 bytes) is
        atomic. A line here is a few hundred bytes -- the command string is
        already truncated to 200 chars by server/deny_detail.py -- so this holds
        with a wide margin. Do not start logging tool_input wholesale here; that
        would break the size assumption as well as the content discipline.
      - Growth is bounded by how often a denial happens at all. Approvals are
        the common case and are never logged here; a hard deny is the "never
        allowed" class (secrets, the agent's own config, destructive shapes,
        harness-owned git writes) and rare by construction, and the other four
        classes each require either a cron job asking for something it can't
        have, Mike actually answering, or the server being down. In ordinary
        operation that is a handful of lines a week, not a stream.

    So if this file ever gets big, that is not a logging problem to fix with
    rotation -- it is the signal the promotion rule is watching for, arriving
    loudly. The answer at that point is external rotation (logrotate, plus
    swapping this for WatchedFileHandler, which is built for exactly that and
    stays multi-process safe), not a RotatingFileHandler. See
    docs/tasks/marlow-harness-affordances.md.

    Keeps the RedactingFilter, which catches the named fields above -- but do
    not read that as "secrets can't land here". The filter is field-targeted on
    purpose (see _REDACT_PATTERNS), so a secret in some other shape, e.g. a
    `curl -d 'api_key=...'` that trips matches_deny_arg_shape, is written
    verbatim and, this file not rotating, stays. That is accepted rather than
    fixed: anything the model could put in a command string it could already
    read, so a 0700 log beside it on the same box is not an escalation. It
    means this file is as sensitive as the box's other secret material, not
    that it is scrubbed.
    """
    logger = logging.getLogger("marlow.denials")
    if logger.handlers:
        return logger

    settings.ensure_dirs()
    logger.setLevel(logging.INFO)
    logger.addFilter(RedactingFilter())
    # Nothing above this should see denial lines: the hook has no root handlers
    # worth inheriting, and in the server process propagation would duplicate
    # every line into marlow-server.log and undo the separation above.
    logger.propagate = False

    handler = logging.FileHandler(settings.log_dir / DENY_LOG_NAME, mode="a")
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)

    return logger
