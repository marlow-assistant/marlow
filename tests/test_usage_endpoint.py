"""GET /v1/usage and the report builder behind it (server/usage_report.py)."""
import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

from server import usage_report
from server.app import public_app
from server.config import settings as real_settings
from server.invoke import PlanUsage
from server.usage_state import ConversationReading, UsageState, usage_state

NOW = 1_800_000_000.0
FIVE_H = 5 * 3600
SEVEN_D = 7 * 86400


def _ts(epoch: float) -> str:
    # Exactly telemetry.record_invocation's format.
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def _rec(at, source="chat", session="s1", inp=0, cc=0, cr=0, out=0, error=False):
    return json.dumps(
        {
            "ts": _ts(at), "source": source, "session_id": session,
            "input_tokens": inp, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr, "output_tokens": out,
            "is_error": error,
        }
    )


def _use_telemetry(monkeypatch, tmp_path, lines):
    path = tmp_path / "usage.jsonl"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(
        usage_report, "settings", dataclasses.replace(real_settings, telemetry_path=path)
    )
    return path


def _state(plan=True, conversation=True) -> UsageState:
    state = UsageState()
    if plan:
        state.plan = PlanUsage(
            observed_at=NOW - 60, status="allowed",
            windows={
                "five_hour": {"utilization": 0.14, "resets_at": int(NOW + 3600)},
                "seven_day": {"utilization": 0.08, "resets_at": int(NOW + 86400)},
            },
        )
    if conversation:
        state.conversation = ConversationReading("s1", 61_000, NOW - 30)
    return state


# --- auth -----------------------------------------------------------------------


def test_the_endpoint_requires_a_device_token():
    with TestClient(public_app) as client:
        assert client.get("/v1/usage").status_code == 401
        assert client.get("/v1/usage", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_the_endpoint_with_nothing_yet(monkeypatch, tmp_path, paired_device_token):
    _use_telemetry(monkeypatch, tmp_path, [])
    headers = {"Authorization": f"Bearer {paired_device_token}"}
    with TestClient(public_app) as client:
        body = client.get("/v1/usage", headers=headers).json()
    assert body["plan"] is None
    assert body["conversation"] is None
    # A readable (empty) log is a real "nothing happened", not "unknown".
    assert body["marlow"]["five_hour"]["chat"] == {"turns": 0, "weighted_tokens": 0}


def test_the_endpoint_serves_the_live_state(monkeypatch, tmp_path, paired_device_token):
    now = time.time()
    _use_telemetry(monkeypatch, tmp_path, [_rec(now - 120, inp=10)])
    usage_state.plan = PlanUsage(
        observed_at=now - 5, status="allowed",
        windows={"five_hour": {"utilization": 0.14, "resets_at": int(now + 1000)}},
    )
    usage_state.conversation = ConversationReading("s1", 61_000, now - 5)
    headers = {"Authorization": f"Bearer {paired_device_token}"}
    with TestClient(public_app) as client:
        resp = client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["windows"]["five_hour"] == {"utilization": 0.14, "resets_at": int(now + 1000)}
    assert body["plan"]["status"] == "allowed"
    assert body["conversation"]["session_id"] == "s1"
    assert body["conversation"]["context_tokens"] == 61_000
    assert body["conversation"]["compaction_threshold_tokens"] == real_settings.idle_compaction_threshold_tokens
    assert body["marlow"]["five_hour"]["chat"]["turns"] == 1


# --- marlow's own activity ----------------------------------------------------------


def test_marlow_buckets_by_source_with_weighted_tokens(monkeypatch, tmp_path):
    _use_telemetry(
        monkeypatch, tmp_path,
        [
            # 100*1 + 40*1.25 + 1000*0.1 + 10*5 = 300
            _rec(NOW - 600, "chat", inp=100, cc=40, cr=1000, out=10),
            _rec(NOW - 500, "chat", inp=100),
            _rec(NOW - 400, "cron", inp=7),
            _rec(NOW - 300, "system", out=2),
            _rec(NOW - 200, "compaction", cr=10_000),
            _rec(NOW - 100, "mystery", inp=999_999),  # unclassified: counted nowhere
        ],
    )
    five = usage_report.build(_state(), now=NOW)["marlow"]["five_hour"]
    assert five["chat"] == {"turns": 2, "weighted_tokens": 400}
    assert five["scheduled"] == {"turns": 2, "weighted_tokens": 17}
    assert five["compaction"] == {"turns": 1, "weighted_tokens": 1000}


def test_a_bad_line_is_skipped_and_null_counters_count_as_zero(monkeypatch, tmp_path):
    _use_telemetry(
        monkeypatch, tmp_path,
        [
            "not json at all",
            "[1, 2]",
            json.dumps({"ts": "garbage", "source": "chat"}),
            json.dumps({"source": "chat"}),  # no ts
            json.dumps({"ts": _ts(NOW - 60), "source": "chat", "input_tokens": None,
                        "output_tokens": "many"}),
            _rec(NOW - 50, "chat", inp=5),
        ],
    )
    chat = usage_report.build(_state(), now=NOW)["marlow"]["five_hour"]["chat"]
    assert chat == {"turns": 2, "weighted_tokens": 5}


def test_windows_start_at_resets_at_minus_length(monkeypatch, tmp_path):
    # five_hour resets in 1h, so it began 4h ago; seven_day resets in 1d, so 6d ago.
    _use_telemetry(
        monkeypatch, tmp_path,
        [
            _rec(NOW - 5 * 86400, "chat"),           # in seven_day only
            _rec(NOW - 5 * 3600, "chat"),            # before five_hour's start
            _rec(NOW - 3 * 3600, "chat"),            # in both
            _rec(NOW - 8 * 86400, "chat"),           # in neither
            _rec(NOW + 3600, "chat"),                # the future: neither
        ],
    )
    marlow = usage_report.build(_state(), now=NOW)["marlow"]
    assert marlow["five_hour"]["chat"]["turns"] == 1
    assert marlow["seven_day"]["chat"]["turns"] == 3  # the 5d, 5h and 3h records


def test_without_a_plan_reading_the_windows_roll_back_from_now(monkeypatch, tmp_path):
    _use_telemetry(
        monkeypatch, tmp_path,
        [_rec(NOW - FIVE_H + 60), _rec(NOW - FIVE_H - 60), _rec(NOW - SEVEN_D + 60), _rec(NOW - SEVEN_D - 60)],
    )
    marlow = usage_report.build(_state(plan=False), now=NOW)["marlow"]
    assert marlow["five_hour"]["chat"]["turns"] == 1
    assert marlow["seven_day"]["chat"]["turns"] == 3


def test_a_reading_from_an_ended_window_falls_back_to_the_rolling_span(monkeypatch, tmp_path):
    _use_telemetry(monkeypatch, tmp_path, [_rec(NOW - 4 * 3600), _rec(NOW - 6 * 3600)])
    state = _state()
    state.plan = PlanUsage(
        observed_at=NOW - 9 * 3600, status="allowed",
        windows={"five_hour": {"utilization": 0.9, "resets_at": int(NOW - 3600)}},
    )
    assert usage_report.build(state, now=NOW)["marlow"]["five_hour"]["chat"]["turns"] == 1


def test_an_unreadable_log_makes_marlow_null_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        usage_report, "settings",
        dataclasses.replace(real_settings, telemetry_path=tmp_path / "missing.jsonl"),
    )
    body = usage_report.build(_state(), now=NOW)
    assert body["marlow"] is None
    assert body["conversation"]["started_at"] is None
    assert body["conversation"]["compactions"] == 0
    assert body["plan"] is not None


# --- the conversation block ----------------------------------------------------------


def test_started_at_and_compactions_come_from_this_sessions_records(monkeypatch, tmp_path):
    _use_telemetry(
        monkeypatch, tmp_path,
        [
            _rec(NOW - 9000, "chat", session="other"),
            _rec(NOW - 8000, "compaction", session="other"),
            _rec(NOW - 7000, "cron", session="s1"),      # first record for s1 (any source)
            _rec(NOW - 6000, "chat", session="s1"),
            _rec(NOW - 5000, "compaction", session="s1"),
            _rec(NOW - 4000, "chat", session="s1"),
            _rec(NOW - 3000, "compaction", session="s1"),
            # A failed attempt compacted nothing and must not count.
            _rec(NOW - 2000, "compaction", session="s1", error=True),
        ],
    )
    convo = usage_report.build(_state(), now=NOW)["conversation"]
    assert convo["started_at"] == int(NOW - 7000)
    assert convo["compactions"] == 2
    assert convo["observed_at"] == NOW - 30
    assert convo["compaction_threshold_tokens"] == real_settings.idle_compaction_threshold_tokens


def test_a_session_with_no_records_has_a_null_start(monkeypatch, tmp_path):
    _use_telemetry(monkeypatch, tmp_path, [_rec(NOW - 100, session="other")])
    convo = usage_report.build(_state(), now=NOW)["conversation"]
    assert convo["started_at"] is None
    assert convo["compactions"] == 0


def test_conversation_with_an_unknown_size_still_reports_the_session(monkeypatch, tmp_path):
    _use_telemetry(monkeypatch, tmp_path, [])
    state = _state()
    state.conversation = ConversationReading("s1", None, NOW)
    convo = usage_report.build(state, now=NOW)["conversation"]
    assert convo["context_tokens"] is None
    assert convo["session_id"] == "s1"


def test_a_session_retired_by_the_nightly_rollover_is_flagged(monkeypatch, tmp_path):
    # Review 2026-10-04: readings are chat-only, so before Mike's first message
    # of the day the sheet still holds last night's session. Say so.
    _use_telemetry(monkeypatch, tmp_path, [])
    monkeypatch.setattr(usage_report.session_state, "is_retired", lambda sid: sid == "s1")
    assert usage_report.build(_state(), now=NOW)["conversation"]["retired"] is True
    monkeypatch.setattr(usage_report.session_state, "is_retired", lambda sid: False)
    assert usage_report.build(_state(), now=NOW)["conversation"]["retired"] is False


# --- the plan's name ----------------------------------------------------------------


def _with_subscription(monkeypatch, value):
    from server.auth_monitor import AuthStatus

    monkeypatch.setattr(
        usage_report.auth_monitor, "get_status",
        lambda: AuthStatus(True, None, None, value),
    )


@pytest.mark.parametrize("value", ["pro", "max"])
def test_the_subscription_type_is_passed_through(monkeypatch, tmp_path, value):
    _use_telemetry(monkeypatch, tmp_path, [])
    _with_subscription(monkeypatch, value)
    assert usage_report.build(_state(), now=NOW)["subscription_type"] == value


@pytest.mark.parametrize("value", [None, "", 7])
def test_an_unknown_subscription_type_is_null(monkeypatch, tmp_path, value):
    _use_telemetry(monkeypatch, tmp_path, [])
    _with_subscription(monkeypatch, value)
    assert usage_report.build(_state(), now=NOW)["subscription_type"] is None


def test_no_credentials_file_means_no_subscription_type(monkeypatch, tmp_path):
    from server import auth_monitor

    _use_telemetry(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        auth_monitor, "settings",
        dataclasses.replace(real_settings, credentials_path=tmp_path / "missing.json"),
    )
    assert usage_report.build(_state(), now=NOW)["subscription_type"] is None


def test_a_corrupt_credentials_file_still_serves_the_report(monkeypatch, tmp_path):
    # A string expiresAt makes auth_monitor.get_status() raise TypeError.
    from server import auth_monitor

    creds = tmp_path / "creds.json"
    creds.write_text('{"claudeAiOauth": {"expiresAt": "soon", "subscriptionType": "pro"}}')
    _use_telemetry(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        auth_monitor, "settings", dataclasses.replace(real_settings, credentials_path=creds)
    )
    body = usage_report.build(_state(), now=NOW)
    assert body["subscription_type"] is None
    assert body["plan"] is not None
