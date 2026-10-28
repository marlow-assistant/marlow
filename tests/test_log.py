"""Redaction is security-critical: a logging regression leaks the exact secrets
this whole design keeps off Apple's infrastructure. Exercise the filter directly
against realistic log lines."""
from __future__ import annotations

import logging

import pytest

from server.log import RedactingFilter


def _render(msg: str, *args) -> str:
    record = logging.LogRecord("t", logging.INFO, __file__, 0, msg, args, None)
    assert RedactingFilter().filter(record) is True
    return record.getMessage()


@pytest.mark.parametrize(
    "secret",
    [
        '{"accessToken": "sk-abc123.def"}',
        '{"refreshToken":"rt-zzz-999"}',
        '{"device_token": "dt-secret-value"}',
        '{"apns_token": "apns-secret-value"}',
        "Authorization: Bearer eyJhbGciOi.payload.sig",
        "Bearer eyJhbGciOi.payload.sig",
    ],
)
def test_known_secret_fields_are_redacted(secret: str) -> None:
    out = _render("outbound %s", secret)
    assert "[REDACTED]" in out
    for leak in ("sk-abc123.def", "rt-zzz-999", "dt-secret-value", "apns-secret-value", "eyJhbGciOi.payload.sig"):
        assert leak not in out


def test_redaction_survives_lazy_args_interpolation() -> None:
    # The filter must fold args in (record.args cleared) so a secret passed as a
    # %s arg can't slip through un-scrubbed at a later formatting stage.
    out = _render('token blob: %s', '{"accessToken": "leaky"}')
    assert "leaky" not in out
    assert "[REDACTED]" in out


def test_ordinary_text_is_left_intact() -> None:
    out = _render("queue depth is %d, all healthy", 3)
    assert out == "queue depth is 3, all healthy"
