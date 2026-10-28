"""The server-owned transcript (docs/tasks/server-transcript.md): the store, the
two endpoints, and where `_drain` and `POST /v1/message` write to it.

The store is a file under the sandbox `data_dir`; `tests/conftest.py`'s autouse
`_isolated_transcript` empties it around every test.
"""
import asyncio
import base64
import dataclasses
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from server import auth_monitor, transcript
from server.app import public_app
from server.config import settings as real_settings
from server.invoke import InvocationResult, InvokeError
from server.queue import TurnStatus, WorkQueue

DAY = 24 * 3600


def _user(text="hi", turn_id="t-1", client_msg_id=None, **kwargs):
    return transcript.append_user(
        turn_id=turn_id, text=text, source="chat", client_msg_id=client_msg_id, **kwargs
    )


def _assistant(text="reply", turn_id="t-1", status="done", **kwargs):
    return transcript.append_assistant(
        turn_id=turn_id, text=text, source="chat", status=status, **kwargs
    )


def _success(text="ok") -> InvocationResult:
    return InvocationResult(
        exit_code=0, is_error=False, subtype="success", result_text=text,
        session_id="session-1", permission_denials=[],
    )


def _all_rows():
    return transcript.read_after(0, transcript.MAX_LIMIT)["rows"]


async def _wait_for(condition, what: str, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {timeout}s")


# --- the store ----------------------------------------------------------------


def test_row_shape_matches_the_documented_columns():
    row = _user("hello", turn_id="t-9", client_msg_id="c-1")
    assert set(row) == {
        "seq", "id", "role", "source", "turn_id", "client_msg_id",
        "text", "status", "answers", "created_at", "attachments", "quote",
    }
    assert row["answers"] == []
    assert row["quote"] is None
    assert (row["role"], row["source"], row["turn_id"], row["client_msg_id"]) == (
        "user", "chat", "t-9", "c-1",
    )
    assert row["status"] is None
    assert row["attachments"] == []

    reply = _assistant("answer", turn_id="t-9")
    assert (reply["role"], reply["status"], reply["client_msg_id"]) == ("assistant", "done", None)
    assert reply["seq"] > row["seq"]


def test_a_repeated_client_msg_id_writes_one_row_and_returns_the_first():
    first = _user("hello", turn_id="t-1", client_msg_id="same")
    again = _user("hello", turn_id="t-2", client_msg_id="same")

    assert again == first
    assert len(_all_rows()) == 1


def test_a_repeat_does_not_copy_its_attachment_again(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"jpeg")
    _user(client_msg_id="same", attachments=[(str(src), "image/jpeg")])
    _user(client_msg_id="same", attachments=[(str(src), "image/jpeg")])

    assert len(list(transcript.attachments_dir().iterdir())) == 1


def test_messages_without_a_client_msg_id_never_dedupe():
    _user("a")
    _user("b")
    assert [r["text"] for r in _all_rows()] == ["a", "b"]


def test_attachments_are_listed_in_order_with_their_mime(tmp_path):
    files = []
    for name, data in (("one", b"1"), ("two", b"22")):
        path = tmp_path / name
        path.write_bytes(data)
        files.append(str(path))

    row = _user(attachments=[(files[0], "image/jpeg"), (files[1], "application/pdf")])

    assert [a["mime"] for a in row["attachments"]] == ["image/jpeg", "application/pdf"]
    assert set(row["attachments"][0]) == {"id", "mime"}
    first = transcript.attachment_path(row["attachments"][0]["id"])
    second = transcript.attachment_path(row["attachments"][1]["id"])
    # No extension: the stored mime is the only thing that says what it is.
    assert (first.read_bytes(), first.suffix) == (b"1", "")
    assert second.read_bytes() == b"22"
    assert _all_rows()[0]["attachments"] == row["attachments"]


def test_an_attachment_that_cannot_be_copied_does_not_lose_the_message(tmp_path):
    row = _user("hello", attachments=[(str(tmp_path / "missing.jpg"), "image/jpeg")])
    assert row["text"] == "hello"
    assert row["attachments"] == []


def test_after_reads_ascending_and_reports_has_more():
    for i in range(5):
        _user(f"m{i}", turn_id=f"t-{i}")

    page = transcript.read_after(0, 2)
    assert [r["text"] for r in page["rows"]] == ["m0", "m1"]
    assert page["has_more"] is True
    assert page["latest_seq"] == 5

    page = transcript.read_after(page["rows"][-1]["seq"], 10)
    assert [r["text"] for r in page["rows"]] == ["m2", "m3", "m4"]
    assert page["has_more"] is False

    page = transcript.read_after(5, 10)
    assert page["rows"] == [] and page["has_more"] is False and page["latest_seq"] == 5


def test_before_reads_descending_and_none_is_the_newest_page():
    for i in range(5):
        _user(f"m{i}", turn_id=f"t-{i}")

    page = transcript.read_before(None, 2)
    assert [r["text"] for r in page["rows"]] == ["m4", "m3"]
    assert page["has_more"] is True

    page = transcript.read_before(page["rows"][-1]["seq"], 10)
    assert [r["text"] for r in page["rows"]] == ["m2", "m1", "m0"]
    assert page["has_more"] is False


def test_an_exact_fit_is_not_has_more():
    for i in range(3):
        _user(f"m{i}", turn_id=f"t-{i}")
    assert transcript.read_after(0, 3)["has_more"] is False
    assert transcript.read_before(None, 3)["has_more"] is False


def test_limit_is_clamped_not_rejected():
    assert transcript.clamp_limit(None) == transcript.DEFAULT_LIMIT
    assert transcript.clamp_limit(0) == 1
    assert transcript.clamp_limit(-5) == 1
    assert transcript.clamp_limit(10**9) == transcript.MAX_LIMIT

    for i in range(3):
        _user(f"m{i}", turn_id=f"t-{i}")
    assert len(transcript.read_after(0, 0)["rows"]) == 1


def test_a_page_never_exceeds_the_maximum():
    with transcript._connect() as conn:
        conn.executemany(
            "INSERT INTO transcript (id, role, source, turn_id, text, created_at)"
            " VALUES (?, 'user', 'ios', 't', 'x', 1)",
            [(f"id-{i}",) for i in range(transcript.MAX_LIMIT + 5)],
        )
    page = transcript.read_after(0, 10**9)
    assert len(page["rows"]) == transcript.MAX_LIMIT
    assert page["has_more"] is True


def test_prune_removes_old_rows_and_their_attachments_and_keeps_new_ones(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"jpeg")
    now = time.time()
    old = _user("old", turn_id="t-old", client_msg_id="c-old",
                attachments=[(str(src), "image/jpeg")], now=now - 31 * DAY)
    _assistant("old reply", turn_id="t-old", now=now - 31 * DAY)
    new = _user("new", turn_id="t-new", client_msg_id="c-new",
                attachments=[(str(src), "image/jpeg")], now=now - 29 * DAY)
    old_file = transcript.attachment_path(old["attachments"][0]["id"])
    new_file = transcript.attachment_path(new["attachments"][0]["id"])
    assert old_file.exists() and new_file.exists()

    assert transcript.prune(now) == 2

    assert [r["text"] for r in _all_rows()] == ["new"]
    assert not old_file.exists()
    assert new_file.exists()
    with transcript._connect() as conn:
        left = [r[0] for r in conn.execute("SELECT id FROM transcript_attachments")]
    assert left == [new["attachments"][0]["id"]]


def test_the_cursor_survives_a_prune_that_empties_the_table():
    _user("old", now=time.time() - 40 * DAY)
    _user("old2", turn_id="t-2", now=time.time() - 40 * DAY)
    transcript.prune()

    page = transcript.read_after(2, 10)
    assert page["rows"] == [] and page["latest_seq"] == 2
    # And a new row does not reuse a number a client may already hold.
    assert _user("fresh", turn_id="t-3")["seq"] == 3


def test_writes_swallow_an_unwritable_store(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setattr(transcript, "db_path", lambda: blocker / "transcript.db")

    assert _user("hello", client_msg_id="c") is None
    assert _assistant("reply") is None
    assert transcript.prune() == 0
    assert "failed to append user row" in caplog.text
    assert "failed to append assistant row" in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        "", "x", "../secret", "..", "../../etc/passwd", "a/b", "/etc/passwd",
        "00000000-0000-0000-0000-000000000000/../x", "00000000-0000-0000-0000-00000000000g",
        "0000000000000000000000000000000a",  # a uuid, but not the canonical form
        "00000000-0000-0000-0000-00000000000A",  # canonical digits, wrong case
        "00000000-0000-0000-0000-000000000000\x00",
    ],
)
def test_only_a_canonical_uuid_ever_becomes_a_path(bad):
    assert transcript.attachment_path(bad) is None
    assert transcript.get_attachment(bad) is None


def test_a_canonical_uuid_stays_inside_the_attachments_dir():
    path = transcript.attachment_path("123e4567-e89b-42d3-a456-426614174000")
    assert path.parent == transcript.attachments_dir()


# --- the endpoints ------------------------------------------------------------


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _send(client, headers, **body):
    body.setdefault("text", "hi")
    resp = client.post("/v1/message", json=body, headers=headers)
    assert resp.status_code == 202, resp.text
    return resp.json()["turn_id"]


def _wait_done(client, headers, turn_id):
    deadline = time.time() + 5
    while time.time() < deadline:
        body = client.get(f"/v1/turn/{turn_id}", headers=headers).json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError("turn did not finish")


def test_both_endpoints_require_a_device_token():
    with TestClient(public_app) as client:
        assert client.get("/v1/transcript").status_code == 401
        assert client.get("/v1/transcript/attachment/123e4567-e89b-42d3-a456-426614174000").status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert client.get("/v1/transcript", headers=bad).status_code == 401
        assert client.get("/v1/transcript/attachment/123e4567-e89b-42d3-a456-426614174000", headers=bad).status_code == 401


def test_a_retried_send_writes_one_user_row_carrying_the_returned_turn_id(paired_device_token):
    headers = _headers(paired_device_token)
    with TestClient(public_app) as client:
        first = _send(client, headers, text="hello", client_msg_id="retry-1")
        second = _send(client, headers, text="hello", client_msg_id="retry-1")
        assert second == first

        users = [r for r in client.get("/v1/transcript", headers=headers).json()["rows"] if r["role"] == "user"]
        assert len(users) == 1
        assert users[0]["turn_id"] == first
        assert users[0]["text"] == "hello"
        assert users[0]["client_msg_id"] == "retry-1"
        _wait_done(client, headers, first)


def test_the_user_row_is_there_before_the_turn_id_is_handed_back(paired_device_token, monkeypatch):
    """The 202 is the client's first sight of the turn_id, so the row it names
    must exist by then -- checked from inside enqueue, which is as late as the
    write may run."""
    from server.queue import work_queue

    seen = []
    real_enqueue = work_queue.enqueue

    def spying_enqueue(*args, **kwargs):
        seen.append([r["turn_id"] for r in _all_rows()])
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(work_queue, "enqueue", spying_enqueue)
    headers = _headers(paired_device_token)
    with TestClient(public_app) as client:
        turn_id = _send(client, headers, client_msg_id="before-enqueue")
        _wait_done(client, headers, turn_id)
    assert seen == [[turn_id]]


def test_transcript_pages_over_http(paired_device_token):
    headers = _headers(paired_device_token)
    with TestClient(public_app) as client:
        # Seeded after startup: rows already there when the server starts are
        # the restart pass's business (`mark_interrupted`).
        for i in range(5):
            _user(f"m{i}", turn_id=f"t-{i}")
        newest = client.get("/v1/transcript", params={"limit": 2}, headers=headers).json()
        assert [r["text"] for r in newest["rows"]] == ["m4", "m3"]
        assert newest["has_more"] is True and newest["latest_seq"] == 5

        older = client.get("/v1/transcript", params={"before": 4, "limit": 10}, headers=headers).json()
        assert [r["text"] for r in older["rows"]] == ["m2", "m1", "m0"]
        assert older["has_more"] is False

        later = client.get("/v1/transcript", params={"after": 3}, headers=headers).json()
        assert [r["text"] for r in later["rows"]] == ["m3", "m4"]

        clamped = client.get("/v1/transcript", params={"after": 0, "limit": 10**9}, headers=headers)
        assert clamped.status_code == 200 and len(clamped.json()["rows"]) == 5
        assert len(client.get("/v1/transcript", params={"limit": 0}, headers=headers).json()["rows"]) == 1


def test_after_and_before_together_are_refused(paired_device_token):
    with TestClient(public_app) as client:
        resp = client.get("/v1/transcript", params={"after": 1, "before": 2}, headers=_headers(paired_device_token))
        assert resp.status_code == 422
        resp = client.get("/v1/transcript", params={"after": -1}, headers=_headers(paired_device_token))
        assert resp.status_code == 422


def test_an_image_round_trips_as_an_attachment(paired_device_token):
    headers = _headers(paired_device_token)
    jpeg = b"\xff\xd8 not really a jpeg"
    with TestClient(public_app) as client:
        turn_id = _send(
            client, headers, text="look", client_msg_id="img-1",
            image_b64=base64.b64encode(jpeg).decode(), image_mime="image/jpeg",
        )
        # The attachment dir the agent reads is gone once the turn ends; the
        # transcript's copy must not be.
        _wait_done(client, headers, turn_id)

        rows = client.get("/v1/transcript", headers=headers).json()["rows"]
        user = next(r for r in rows if r["role"] == "user")
        assert [a["mime"] for a in user["attachments"]] == ["image/jpeg"]

        got = client.get(f"/v1/transcript/attachment/{user['attachments'][0]['id']}", headers=headers)
        assert got.status_code == 200
        assert got.content == jpeg
        assert got.headers["content-type"] == "image/jpeg"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert "content-disposition" not in got.headers


def test_a_non_image_attachment_is_served_as_a_download(paired_device_token, tmp_path):
    src = tmp_path / "doc"
    src.write_bytes(b"%PDF-1.4")
    row = _user(attachments=[(str(src), "application/pdf")])
    headers = _headers(paired_device_token)
    with TestClient(public_app) as client:
        got = client.get(f"/v1/transcript/attachment/{row['attachments'][0]['id']}", headers=headers)
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"] == "attachment"
    assert got.headers["x-content-type-options"] == "nosniff"


def test_bad_attachment_ids_are_404_and_never_touch_the_filesystem(paired_device_token, monkeypatch):
    secret = transcript.attachments_dir().parent / "secret"
    secret.write_bytes(b"do not serve")
    headers = _headers(paired_device_token)

    # The handler's only filesystem touches are `is_file` and the FileResponse
    # it builds; neither may ever be reached for these ids.
    opened = []
    real_is_file = type(secret).is_file
    data_root = str(transcript.attachments_dir().parent)

    def spying_is_file(self):
        # Only the transcript's own tree: the server's startup checks stat
        # other files (identity) and are not what is under test.
        if str(self).startswith(data_root):
            opened.append(self)
        return real_is_file(self)

    monkeypatch.setattr(type(secret), "is_file", spying_is_file)
    import server.app as app_module
    real_file_response = app_module.FileResponse
    monkeypatch.setattr(
        app_module, "FileResponse", lambda *a, **k: (opened.append(a), real_file_response(*a, **k))[1]
    )

    with TestClient(public_app) as client:
        for bad in ("..%2Fsecret", "%2E%2E%2Fsecret", "secret", "not-a-uuid",
                    "00000000-0000-0000-0000-000000000000",  # well formed, no such attachment
                    "123E4567-E89B-42D3-A456-426614174000"):
            resp = client.get(f"/v1/transcript/attachment/{bad}", headers=headers)
            assert resp.status_code == 404, bad
            assert b"do not serve" not in resp.content
    assert opened == []


def test_a_row_whose_file_is_gone_is_a_404(paired_device_token, tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"jpeg")
    row = _user(attachments=[(str(src), "image/jpeg")])
    transcript.attachment_path(row["attachments"][0]["id"]).unlink()
    with TestClient(public_app) as client:
        resp = client.get(
            f"/v1/transcript/attachment/{row['attachments'][0]['id']}",
            headers=_headers(paired_device_token),
        )
    assert resp.status_code == 404


def test_a_transcript_write_failure_does_not_break_post_message(paired_device_token, tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setattr(transcript, "db_path", lambda: blocker / "transcript.db")
    headers = _headers(paired_device_token)

    with TestClient(public_app) as client:
        turn_id = _send(client, headers, text="still works", client_msg_id="fail-1")
        body = _wait_done(client, headers, turn_id)
    assert body["status"] == "done"


# --- _drain and the system message --------------------------------------------


def _patch_auth(monkeypatch, state):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "server.queue.auth_monitor.get_status",
        lambda: auth_monitor.AuthStatus(
            credentials_found=True,
            access_token_expires_at=now + timedelta(hours=1),
            refresh_token_expires_at=(now - timedelta(hours=1)) if state["reauth_needed"] else (now + timedelta(days=9)),
            subscription_type="pro",
        ),
    )
    monkeypatch.setattr(
        "server.queue.settings", dataclasses.replace(real_settings, auth_recheck_interval_seconds=0.02)
    )


def _assistant_rows():
    return [r for r in _all_rows() if r["role"] == "assistant"]


def test_a_done_turn_writes_its_reply(monkeypatch):
    async def fake_run(prompt, **kwargs):
        return _success("the answer")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _wait_for(_assistant_rows, "the assistant row")
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())
    (row,) = _assistant_rows()
    assert (row["turn_id"], row["text"], row["status"], row["source"]) == (turn_id, "the answer", "done", "chat")


def test_an_errored_turn_writes_its_error(monkeypatch):
    async def fake_run(prompt, **kwargs):
        raise InvokeError("the CLI fell over")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _wait_for(_assistant_rows, "the assistant row")
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())
    (row,) = _assistant_rows()
    assert (row["turn_id"], row["text"], row["status"]) == (turn_id, "the CLI fell over", "error")


def test_an_error_with_no_text_is_stored_empty_not_as_cli_output(monkeypatch):
    async def fake_run(prompt, **kwargs):
        return InvocationResult(
            exit_code=1, is_error=True, subtype="error", result_text="raw CLI output",
            session_id=None, permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        wq.enqueue("hello", source="chat")
        await _wait_for(_assistant_rows, "the assistant row")
        await wq.stop()

    asyncio.run(_body())
    (row,) = _assistant_rows()
    assert (row["text"], row["status"]) == ("", "error")


def test_a_coalesced_run_gives_only_the_surviving_turn_the_reply(monkeypatch):
    state = {"reauth_needed": True}
    _patch_auth(monkeypatch, state)

    async def fake_run(prompt, **kwargs):
        return _success("one reply to three")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [wq.enqueue(f"m{i}", source="chat", in_conversation=True) for i in range(3)]
        await asyncio.sleep(0.1)  # let the drain hold on the first, queue behind it
        state["reauth_needed"] = False
        await _wait_for(_assistant_rows, "the assistant row")
        await asyncio.sleep(0.1)  # and give a wrongly-written second row time to land
        statuses = [wq.get_turn(i).status for i in ids]
        await wq.stop()
        return ids, statuses

    ids, statuses = asyncio.run(_body())
    assert statuses == [TurnStatus.MERGED, TurnStatus.MERGED, TurnStatus.DONE]
    (row,) = _assistant_rows()
    assert (row["turn_id"], row["text"]) == (ids[-1], "one reply to three")


def test_a_cancelled_compaction_and_a_ran_compaction_write_nothing(monkeypatch):
    async def fake_run(prompt, **kwargs):
        return _success("compacted")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        # Both enqueues run before the drain task is next scheduled, so the
        # attended one finds the compaction still PENDING and cancels it.
        cancelled = wq.enqueue("compact", source="compaction", raw_prompt=True)
        attended = wq.enqueue("hello", source="chat")
        await _wait_for(_assistant_rows, "the attended reply")
        ran = wq.enqueue("compact again", source="compaction", raw_prompt=True)
        await _wait_for(lambda: wq.get_turn(ran).status == TurnStatus.DONE, "the compaction run")
        await asyncio.sleep(0.1)  # a row for it would land by now
        status = wq.get_turn(cancelled).status
        await wq.stop()
        return status, attended

    status, attended = asyncio.run(_body())
    assert status == TurnStatus.CANCELLED
    assert [r["turn_id"] for r in _assistant_rows()] == [attended]


def test_the_row_exists_before_the_turn_ready_push_goes_out(monkeypatch):
    """A push-woken client asks the server for rows after its cursor straight
    away, so the reply has to be in the store by the time the push is
    dispatched. The fake push reads the store at the moment it is called."""
    from server.push import PushType

    in_store_at_push = []

    def fake_push(payload):
        if payload.type == PushType.TURN_READY:
            in_store_at_push.append([r["turn_id"] for r in _assistant_rows()])

    async def fake_run(prompt, **kwargs):
        return _success()

    monkeypatch.setattr("server.queue.push_broadcast_nowait", fake_push)
    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _wait_for(lambda: in_store_at_push, "the push")
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())
    assert in_store_at_push == [[turn_id]]


def test_a_cron_turn_writes_a_row_with_its_source(monkeypatch):
    async def fake_run(prompt, **kwargs):
        return _success("your briefing")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        wq.enqueue("brief me", source="cron", label="morning")
        await _wait_for(_assistant_rows, "the assistant row")
        await wq.stop()

    asyncio.run(_body())
    (row,) = _assistant_rows()
    assert (row["source"], row["text"]) == ("cron", "your briefing")


def _drain_replies(monkeypatch, sends):
    """Run each (source, reply) through a real drain, in order; return the
    assistant rows written and the turn ids that got a TURN_READY push. The
    last send must be one that IS delivered: its push marks the moment every
    earlier turn has been fully handled, because the drain is serial."""
    from server.push import PushType

    replies = iter(reply for _source, reply in sends)
    pushed = []

    async def fake_run(prompt, **kwargs):
        return _success(next(replies))

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    monkeypatch.setattr(
        "server.queue.push_broadcast_nowait",
        lambda payload: pushed.append(payload.ref) if payload.type == PushType.TURN_READY else None,
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [wq.enqueue(f"prompt {i}", source=source, label="email-flash") for i, (source, _r) in enumerate(sends)]
        await _wait_for(lambda: ids[-1] in pushed, "the last push")
        await wq.stop()
        return ids

    ids = asyncio.run(_body())
    return ids, _assistant_rows(), pushed


@pytest.mark.parametrize("reply", ["[silent]", "  [silent]\n", "`[silent]`", "[SILENT]", "", "   "])
def test_a_cron_turn_that_replies_silent_is_not_delivered(monkeypatch, reply):
    ids, rows, pushed = _drain_replies(monkeypatch, [("cron", reply), ("chat", "next")])
    assert [r["turn_id"] for r in rows] == [ids[1]]
    assert pushed == [ids[1]]


@pytest.mark.parametrize(
    "reply", ["Inbox check: nothing high-priority. [silent]", "[silent] -- except one thing"]
)
def test_a_cron_reply_that_only_mentions_the_token_is_delivered(monkeypatch, reply):
    ids, rows, pushed = _drain_replies(monkeypatch, [("cron", reply), ("chat", "next")])
    assert [(r["turn_id"], r["text"]) for r in rows] == [(ids[0], reply), (ids[1], "next")]
    assert pushed == ids


def test_the_silent_token_means_nothing_on_a_phone_turn(monkeypatch):
    # The phone is waiting on this reply: a turn Mike sent always answers.
    # One send only: two queued phone turns would coalesce into one.
    ids, rows, pushed = _drain_replies(monkeypatch, [("chat", "[silent]")])
    assert [(r["turn_id"], r["text"]) for r in rows] == [(ids[0], "[silent]")]
    assert pushed == ids


def test_an_errored_cron_turn_is_delivered_even_with_no_text(monkeypatch):
    from server.push import PushType

    pushed = []

    async def fake_run(prompt, **kwargs):
        raise InvokeError("")

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    monkeypatch.setattr(
        "server.queue.push_broadcast_nowait",
        lambda payload: pushed.append(payload.ref) if payload.type == PushType.TURN_READY else None,
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("check mail", source="cron", label="email-flash")
        await _wait_for(lambda: pushed, "the push")
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())
    (row,) = _assistant_rows()
    assert (row["turn_id"], row["status"]) == (turn_id, "error")
    assert pushed == [turn_id]


def test_a_system_message_writes_a_row():
    wq = WorkQueue()
    turn_id = wq.post_system_message("I couldn't reach you in time.")

    (row,) = _assistant_rows()
    assert (row["turn_id"], row["source"], row["status"], row["text"]) == (
        turn_id, "system", "done", "I couldn't reach you in time.",
    )


def test_a_system_message_resend_is_not_written_twice():
    wq = WorkQueue()
    first = wq.post_system_message("reminder", client_msg_id="rem-1")
    again = wq.post_system_message("reminder", client_msg_id="rem-1")
    assert first == again
    assert len(_assistant_rows()) == 1


def test_a_failing_transcript_write_does_not_stop_the_drain_or_the_push(monkeypatch, tmp_path):
    from server.push import PushType

    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setattr(transcript, "db_path", lambda: blocker / "transcript.db")
    pushes = []
    monkeypatch.setattr(
        "server.queue.push_broadcast_nowait",
        lambda payload: pushes.append(payload.type) if payload.type == PushType.TURN_READY else None,
    )

    async def fake_run(prompt, **kwargs):
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        first = wq.enqueue("one", source="chat")
        await _wait_for(lambda: wq.get_turn(first).status == TurnStatus.DONE, "the first turn")
        # The loop is still alive: a second turn drains too.
        second = wq.enqueue("two", source="cron")
        await _wait_for(lambda: wq.get_turn(second).status == TurnStatus.DONE, "the second turn")
        await _wait_for(lambda: len(pushes) == 2, "both pushes")
        wq.post_system_message("also fine")
        await wq.stop()

    asyncio.run(_body())


def test_start_prunes_the_transcript_and_stop_ends_the_daily_loop(monkeypatch):
    calls = []
    monkeypatch.setattr("server.queue.transcript.prune", lambda *a: calls.append(1) or 0)

    async def _body():
        wq = WorkQueue()
        wq.start()
        await _wait_for(lambda: calls, "the startup prune")
        task = wq._retention_task
        assert task is not None and not task.done()
        await wq.stop()
        assert task.cancelled() and wq._retention_task is None

    asyncio.run(_body())
    assert calls == [1]


# --- answers ------------------------------------------------------------------


def test_a_plain_turn_answers_its_own_user_row_and_cron_and_system_answer_nothing(monkeypatch):
    async def fake_run(prompt, **kwargs):
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        chat_turn = wq.enqueue("hello", source="chat")
        cron = wq.enqueue("brief", source="cron")
        await _wait_for(lambda: len(_assistant_rows()) == 2, "both replies")
        wq.post_system_message("notice")
        await wq.stop()
        return chat_turn, cron

    chat_turn, cron = asyncio.run(_body())
    by_turn = {r["turn_id"]: r for r in _assistant_rows()}
    assert by_turn[chat_turn]["answers"] == [chat_turn]
    assert by_turn[cron]["answers"] == []
    system = next(r for r in _assistant_rows() if r["source"] == "system")
    assert system["answers"] == []


def test_a_merged_run_answers_every_message_in_it(monkeypatch):
    state = {"reauth_needed": True}
    _patch_auth(monkeypatch, state)

    async def fake_run(prompt, **kwargs):
        return _success("one reply")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [wq.enqueue(f"m{i}", source="chat", in_conversation=True) for i in range(3)]
        await asyncio.sleep(0.1)
        state["reauth_needed"] = False
        await _wait_for(_assistant_rows, "the assistant row")
        await wq.stop()
        return ids

    ids = asyncio.run(_body())
    (row,) = _assistant_rows()
    assert row["turn_id"] == ids[-1]
    assert row["answers"] == ids  # oldest first, the survivor last


def test_a_merged_message_that_was_itself_a_retry_carries_its_original_row_too(monkeypatch):
    state = {"reauth_needed": True}
    _patch_auth(monkeypatch, state)

    async def fake_run(prompt, **kwargs):
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        first = wq.enqueue("m0", source="chat", in_conversation=True)
        retry = wq.enqueue("m1", source="chat", in_conversation=True, answers_turn_ids=["original"])
        await asyncio.sleep(0.1)
        state["reauth_needed"] = False
        await _wait_for(_assistant_rows, "the assistant row")
        await wq.stop()
        return first, retry

    first, retry = asyncio.run(_body())
    (row,) = _assistant_rows()
    assert row["answers"] == [first, "original"]
    assert retry not in row["answers"]  # a fresh id no user row carries


def test_a_retry_after_an_error_answers_the_original_row(paired_device_token, monkeypatch):
    calls = []

    async def fake_run(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise InvokeError("API 522")
        return _success("second time lucky")

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    headers = _headers(paired_device_token)

    with TestClient(public_app) as client:
        first = _send(client, headers, text="hello", client_msg_id="retry-after-error")
        _wait_done(client, headers, first)
        retry = _send(client, headers, text="hello", client_msg_id="retry-after-error")
        _wait_done(client, headers, retry)
        deadline = time.time() + 5
        while time.time() < deadline and len(_assistant_rows()) < 2:
            time.sleep(0.02)

    assert retry != first
    (user,) = [r for r in _all_rows() if r["role"] == "user"]
    assert user["turn_id"] == first  # not rewritten, not duplicated
    replies = {r["turn_id"]: r for r in _assistant_rows()}
    assert (replies[first]["status"], replies[first]["answers"]) == ("error", [first])
    assert (replies[retry]["status"], replies[retry]["answers"]) == ("done", [first])


def test_a_retry_after_a_failed_first_write_is_stamped_with_the_id_the_client_gets(
    paired_device_token, tmp_path, monkeypatch
):
    """The first send's transcript write fails, so the retry writes the row. The
    queue already holds a live turn for the client_msg_id and will hand that id
    back; the row has to carry it, not a fresh one enqueue is about to discard."""
    real_path = transcript.db_path
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    headers = _headers(paired_device_token)

    with TestClient(public_app) as client:
        monkeypatch.setattr(transcript, "db_path", lambda: blocker / "transcript.db")
        first = _send(client, headers, text="hello", client_msg_id="write-failed-once")
        monkeypatch.setattr(transcript, "db_path", real_path)
        retry = _send(client, headers, text="hello", client_msg_id="write-failed-once")
        _wait_done(client, headers, first)

    assert retry == first
    (user,) = [r for r in _all_rows() if r["role"] == "user"]
    assert user["turn_id"] == retry


# --- a restart ----------------------------------------------------------------


def test_mark_interrupted_answers_only_rows_nothing_answers():
    now = time.time()
    _user("answered", turn_id="a", now=now - 100)
    _assistant("reply", turn_id="a", answers=["a"], now=now - 90)
    _user("merged away", turn_id="m1", now=now - 80)
    _user("survivor", turn_id="m2", now=now - 70)
    _assistant("reply", turn_id="m2", answers=["m1", "m2"], now=now - 60)
    _user("lost", turn_id="lost", now=now - 50)
    _user("too old", turn_id="old", now=now - 31 * DAY)
    _user("sent after the restart", turn_id="live", now=now + 5)

    assert transcript.mark_interrupted(now, now=now) == 1

    (row,) = [r for r in _assistant_rows() if r["turn_id"] == "lost"]
    assert (row["source"], row["status"], row["answers"]) == ("system", "error", ["lost"])
    assert row["text"] == transcript.INTERRUPTED_TEXT
    assert len(_assistant_rows()) == 3
    # Idempotent: a second start finds it answered.
    assert transcript.mark_interrupted(now, now=now) == 0


def test_a_legacy_ios_row_is_still_answered_after_a_restart():
    """Transcript rows written before the `ios` -> `chat` rename keep
    `source='ios'` for the 30-day retention window. The restart pass finds lost
    messages by role and answers, never by source, so a legacy row is answered
    like any other and still reads back as stored."""
    now = time.time()
    transcript.append_user(turn_id="legacy", text="sent before the rename", source="ios",
                           client_msg_id=None, now=now - 50)

    assert transcript.mark_interrupted(now, now=now) == 1

    rows = transcript.read_after(0, 100)["rows"]
    legacy = next(r for r in rows if r["role"] == "user")
    assert legacy["source"] == "ios"
    assert [r["turn_id"] for r in rows if r["role"] == "assistant"] == ["legacy"]


def test_start_answers_the_messages_a_restart_lost(monkeypatch):
    _user("lost in the restart", turn_id="lost", now=time.time() - 60)

    async def _body():
        wq = WorkQueue()
        wq.start()
        await _wait_for(_assistant_rows, "the interrupted row")
        await wq.stop()

    asyncio.run(_body())
    (row,) = _assistant_rows()
    assert (row["turn_id"], row["answers"], row["status"]) == ("lost", ["lost"], "error")


def test_a_message_sent_while_start_runs_is_not_marked_interrupted(monkeypatch):
    async def _body():
        wq = WorkQueue()
        wq.start()
        # Written after start() captured its clock, before the pass can have
        # been told the message was lost.
        _user("live", turn_id="live", now=time.time() + 1)
        await asyncio.sleep(0.2)
        await wq.stop()

    asyncio.run(_body())
    assert _assistant_rows() == []


# --- retention in batches -----------------------------------------------------


def test_prune_deletes_in_bounded_batches(monkeypatch, tmp_path):
    monkeypatch.setattr(transcript, "PRUNE_BATCH", 3)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"jpeg")
    now = time.time()
    files = []
    for i in range(8):
        row = _user(f"old{i}", turn_id=f"t-{i}", attachments=[(str(src), "image/jpeg")] if i % 2 else [],
                    now=now - 40 * DAY)
        files += [transcript.attachment_path(a["id"]) for a in row["attachments"]]
    _user("new", turn_id="t-new", now=now)

    statements = []
    real_connect = transcript._connect

    def tracing_connect():
        conn = real_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(transcript, "_connect", tracing_connect)

    assert transcript.prune(now) == 8
    assert [r["text"] for r in _all_rows()] == ["new"]
    assert not any(f.exists() for f in files) and len(files) == 4
    # 8 rows at 3 a batch: three transactions, not one.
    deletes = [s for s in statements if s.startswith("DELETE FROM transcript WHERE id IN")]
    assert len(deletes) == 3


# --- initialised once ---------------------------------------------------------


def test_the_schema_is_set_up_once_per_path_not_per_call(monkeypatch, tmp_path):
    import sqlite3

    statements = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)
    _user("one")
    _user("two", turn_id="t-2")
    transcript.read_after(0)
    transcript.read_before(None)

    creates = [s for s in statements if "CREATE TABLE" in s]
    assert len(creates) == 2  # transcript + transcript_attachments, once


def test_a_fresh_path_is_initialised_on_first_use(monkeypatch, tmp_path):
    _user("in the default store")
    monkeypatch.setattr(transcript, "db_path", lambda: tmp_path / "elsewhere" / "transcript.db")
    assert transcript.read_after(0)["rows"] == []
    assert _user("in the new one", turn_id="t-2")["seq"] == 1


def test_a_pre_quote_database_gains_the_column(tmp_path, monkeypatch):
    """Boxes have a transcript table from before `quote` existed, and CREATE
    IF NOT EXISTS leaves it as it was."""
    import sqlite3
    monkeypatch.setattr(
        transcript, "settings", dataclasses.replace(real_settings, data_dir=tmp_path)
    )
    with sqlite3.connect(tmp_path / "transcript.db") as conn:
        conn.executescript(transcript._SCHEMA.replace(
            "    created_at    REAL    NOT NULL,\n", "    created_at    REAL    NOT NULL\n"
        ).replace(
            "    -- User rows from a \"Do it\" tap: JSON {idea_id, emoji, title}, the idea the\n"
            "    -- bubble quotes (docs/tasks/ideas-panel.md). NULL everywhere else.\n"
            "    quote         TEXT\n", ""
        ))
        assert "quote" not in {r[1] for r in conn.execute("PRAGMA table_info(transcript)")}
    row = transcript.append_user(
        turn_id="t1", text="Do it", source="chat", client_msg_id="c1",
        quote={"idea_id": "x", "emoji": "💡", "title": "T"},
    )
    assert row["quote"] == {"idea_id": "x", "emoji": "💡", "title": "T"}
