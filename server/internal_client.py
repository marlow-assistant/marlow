"""HTTP to this instance's internal API, over its Unix socket.

The one client for hooks/pretooluse.py, cron/entrypoint.py and
deploy/identity-push.py. Stdlib only and import-light: the hook runs on every
tool call. Why a socket and not 127.0.0.1 is in config.py
(`internal_socket_path`).
"""
from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any


class InternalAPIError(OSError):
    """The server could not be reached, or answered with a non-2xx status.

    An OSError, so a caller's existing "server unreachable" branch catches it.
    `status` is the HTTP code when there was one, else None.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        # The host only fills the Host header; nothing resolves it.
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(socket_path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def post_json(socket_path: Path, path: str, body: dict[str, Any], *, timeout: float) -> Any:
    """POST `body` as JSON and return the decoded JSON reply.

    Raises InternalAPIError for anything transport or HTTP (an OSError,
    timeouts included), and ValueError when a 2xx reply isn't JSON.
    """
    conn = _UnixHTTPConnection(socket_path, timeout)
    try:
        conn.request(
            "POST",
            path,
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise InternalAPIError(f"could not reach {socket_path}: {e}") from e
    finally:
        conn.close()
    if resp.status // 100 != 2:
        raise InternalAPIError(f"server returned HTTP {resp.status}", status=resp.status)
    return json.loads(raw)
