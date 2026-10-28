#!/usr/bin/env python3
"""Cron entrypoint (docs/tasks/scheduled-work.md). Stdlib only, plus the
stdlib-only server config/log/schedule modules they import — no venv required
to run this file, so it works from cron's minimal environment.

Installed root-owned, not writable by the marlow user (deploy/README.md
"Scheduled (cron) work") — the marlow user's crontab invokes this and only
this; there is no per-job shell script for Marlow to quietly rewrite into an
escape. Deployed as a standalone copy at /opt/marlow/cron/entrypoint.py,
separate from the software checkout — set MARLOW_SERVER_REPO in the crontab
so `import server.config` can still find the `server/` package.

Usage: entrypoint.py <job-name>
       entrypoint.py --entry <entry-id>

  1. Reads $MARLOW_WORKING_DIR/cron/<job-name>.md — missing or empty is a
     loud non-zero exit with the path in the message, not a silent no-op
     (the v1 failure — a scan that quietly stopped firing — this exists to
     prevent).
  2. POSTs {"job", "text", "client_msg_id"} to the loopback-only
     /internal/cron/message endpoint. client_msg_id is f"{job}-{date}" so a
     same-day re-run is absorbed by the work queue's existing idempotency
     index instead of double-enqueuing.
  3. Exits non-zero on a non-2xx response or a connection failure, so a
     wedged or stopped server is visible rather than silent.

``--entry`` reads the fully validated schedules store and dispatches its opaque
id. ``notify`` POSTs a pre-written synthetic turn; ``prompt`` uses the same
model-backed endpoint as a named job. A due one-off prunes itself after a
successful POST, and re-renders the crontab so its own now-dead line goes with
it. An unknown/pruned id exits zero regardless, which keeps a stale line
harmless.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow `import server.config` when invoked directly
# (`python3 /opt/marlow/cron/entrypoint.py job-name`), where sys.path[0] is
# this file's own directory, not the repo root — and where, per
# deploy/README.md's install convention, this file is a standalone root-owned
# copy with no `server/` package anywhere nearby. MARLOW_SERVER_REPO (set
# alongside the other mirrored MARLOW_* vars in the marlow user's crontab)
# points at the actual software checkout; falling back to this file's own
# parent directory only serves running it straight out of the repo (dev,
# tests) where `cron/` really does sit under the repo root.
_repo_root = os.environ.get("MARLOW_SERVER_REPO") or str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _repo_root)

from server.config import settings  # noqa: E402
from server.internal_client import InternalAPIError, post_json  # noqa: E402
from server.log import get_logger  # noqa: E402
from server.schedules import at_instant  # noqa: E402
from server.schedules import load as load_schedules  # noqa: E402
from server.schedules import prune_fired, sync_after_turn  # noqa: E402

logger = get_logger(__name__)


def read_job_prompt(job: str) -> str:
    path = settings.working_dir / "cron" / f"{job}.md"
    if not path.is_file():
        raise SystemExit(f"cron job {job!r}: no prompt file at {path}")
    text = path.read_text()
    if not text.strip():
        raise SystemExit(f"cron job {job!r}: prompt file is empty at {path}")
    return text


def _post(path: str, body: dict, label: str) -> dict:
    sock = settings.internal_socket_path
    try:
        return post_json(sock, path, body, timeout=30)
    except InternalAPIError as e:
        if e.status is not None:
            raise SystemExit(f"{label}: server returned HTTP {e.status}") from e
        raise SystemExit(f"{label}: could not reach server at {sock}: {e}") from e
    except ValueError as e:
        raise SystemExit(f"{label}: server reply was not JSON: {e}") from e


def post_job(job: str, text: str, client_msg_id: str | None = None) -> dict:
    return _post(
        "/internal/cron/message",
        {
            "job": job,
            "text": text,
            "client_msg_id": client_msg_id or f"{job}-{date.today().isoformat()}",
        },
        f"cron job {job!r}",
    )


def post_inline_prompt(entry_id: str, text: str, client_msg_id: str) -> dict:
    """A schedules.json `prompt` entry. Deliberately NOT post_job(): the id is
    model-chosen, and the server keys attach/retire behaviour off `job`."""
    return _post(
        "/internal/cron/message",
        {"entry": entry_id, "text": text, "client_msg_id": client_msg_id},
        f"cron entry {entry_id!r}",
    )


def post_notification(entry_id: str, text: str, client_msg_id: str) -> dict:
    return _post(
        "/internal/cron/notify",
        {"entry": entry_id, "text": text, "client_msg_id": client_msg_id},
        f"cron reminder {entry_id!r}",
    )


def _prune_and_rerender(schedules_path: Path, entry_id: str) -> None:
    """Drop a spent one-off AND rewrite the crontab it left behind.

    The prune alone leaves a dead line installed until something else happens
    to write the store (defect 3, 2026-08-30). Harmless -- an unknown id exits
    zero -- but the line is YEARLESS, so it would otherwise sit there and fire
    again on this date next year. Rendering here is safe because cron runs this
    as the `marlow` user, the same crontab the server installs, and `install()`
    compares byte-for-byte before replacing anything.
    """
    if prune_fired(schedules_path, entry_id):
        sync_after_turn(schedules_path)


def _run_entry(entry_id: str) -> int:
    schedules_path = settings.schedules_path
    schedules = load_schedules(schedules_path)
    if schedules is None:
        return 1
    entry = schedules.entry(entry_id)
    if entry is None:
        # Load-bearing no-op: a one-off prunes itself after firing, while its
        # old crontab line remains until the next successful render.
        return 0

    local_now = datetime.now(ZoneInfo(schedules.timezone))
    if entry.at is not None:
        # Compared as INSTANTS, not as wall-clock strings. The string form
        # silently depends on which zone resolves it, and that dependency is
        # what made a mistimed reminder vanish instead of arriving late
        # (defect 1, 2026-08-30): the guard below saw `now > scheduled`,
        # pruned, and notified nobody.
        scheduled = at_instant(entry.at, schedules.timezone).astimezone(timezone.utc)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        due = scheduled.replace(second=0, microsecond=0)
        if now != due:
            # A box that was down at the scheduled minute deliberately does
            # not fire late. Prune past entries so cron's yearless line cannot
            # resurrect the reminder next year; a manual early invocation is
            # simply a no-op and leaves it scheduled.
            if now > due:
                logger.warning(
                    "cron entry %r was due at %s but ran at %s; not firing late, pruning it",
                    entry.id,
                    entry.at,
                    local_now.strftime("%Y-%m-%dT%H:%M"),
                )
                _prune_and_rerender(schedules_path, entry.id)
            else:
                logger.info(
                    "cron entry %r invoked early (due %s, now %s); leaving it scheduled",
                    entry.id,
                    entry.at,
                    local_now.strftime("%Y-%m-%dT%H:%M"),
                )
            return 0

    client_msg_id = f"{entry.id}-{local_now.strftime('%Y-%m-%d-%H%M')}"
    if entry.notify is not None:
        response = post_notification(entry.id, entry.notify, client_msg_id)
    elif entry.prompt is not None:
        response = post_inline_prompt(entry.id, entry.prompt, client_msg_id)
    elif entry.job is not None:
        # A ONE-OFF job. Recurring jobs never arrive here -- they render in the
        # direct `<job-name>` form and go through main() -- so this branch is
        # reached only for `at`+`job`, which the validator refused until
        # 2026-09-06 precisely because there was nothing here to receive it.
        #
        # post_job, not post_inline_prompt: the job name is Marlow's own file
        # under cron/, not a model-chosen id, and the server keys attach/retire
        # behaviour off `job`. A one-off job should behave like its recurring
        # sibling in every way except that it happens once.
        #
        # The entry's client_msg_id is kept rather than post_job's f"{job}-{date}"
        # default, which is right for a daily job and wrong here: it carries the
        # minute, so a one-off cannot collide with a same-day manual run.
        response = post_job(entry.job, read_job_prompt(entry.job), client_msg_id)
    else:
        logger.error("cron entry %r has no dispatchable payload", entry.id)
        return 1

    if entry.at is not None:
        _prune_and_rerender(schedules_path, entry.id)
    if "turn_id" in response:
        logger.info("cron entry %r created turn %s", entry.id, response["turn_id"])
    else:
        logger.info("cron entry %r skipped by server: %s", entry.id, response.get("reason", "unknown"))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--entry":
        return _run_entry(argv[2])
    if len(argv) != 2 or argv[1] == "--entry":
        print(
            f"usage: {Path(argv[0]).name} <job-name> | --entry <entry-id>",
            file=sys.stderr,
        )
        return 2
    job = argv[1]
    text = read_job_prompt(job)
    response = post_job(job, text)
    if "turn_id" in response:
        logger.info("cron job %r enqueued turn %s", job, response["turn_id"])
    else:
        # A skip (e.g. an "attach" job with no attended session to resume —
        # docs/tasks/nightly-consolidation.md piece 2) is not a failure: the
        # server already logged why. Exiting 0 keeps cron's own log quiet
        # rather than reporting a job failure that didn't happen.
        logger.info("cron job %r skipped by server: %s", job, response.get("reason", "unknown"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
