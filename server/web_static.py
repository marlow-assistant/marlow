"""Serves the built web client (web/dist) from public_app
(docs/tasks/portable-web-client.md, "Serving").

Wired as a 404 handler rather than a catch-all route: routing stays exactly
as it was, so an API route always wins, an unknown /v1/* is still the API's
JSON 404, and a POST to an unknown path is still a 404 (a catch-all GET route
would turn it into a 405). Only a GET/HEAD that nothing else claimed lands
here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

# One constant so the one edit the task doc anticipates (`'unsafe-inline'` on
# style-src, if React style props turn out to need it) is one line.
# Voice mode's Phonon voice (phonon-tts, 2026-10-28): 'wasm-unsafe-eval' is what
# compiling its WebAssembly needs (not JS eval); huggingface.co serves the model
# lookup and the small files; the large ones redirect to a CDN under hf.co, which
# was us.aws.cdn.hf.co from one machine and has been cas-bridge.xethub.hf.co,
# so the region and bridge host are not pinned. The worker and .wasm are
# same-origin files under 'self'.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
    "img-src 'self' blob: data:; "
    "connect-src 'self' https://huggingface.co https://*.hf.co; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache"

# mimetypes on a minimal box may not know these; a wrong type on the
# manifest or the service worker silently breaks install / registration.
_MEDIA_TYPES = {
    ".webmanifest": "application/manifest+json",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
}

_NOT_BUILT_HINT = "web client not built: run `npm ci && npm run build` in web/\n"


def _is_api_path(path: str) -> bool:
    return path == "/health" or path == "/v1" or path.startswith("/v1/")


def _resolve(dist: Path, url_path: str) -> Path | None:
    """The file inside dist for this URL path, or None. The path arrives
    already percent-decoded, so `%2e%2e` is `..` by now."""
    rel = url_path.lstrip("/")
    if not rel:
        return None
    if "\0" in rel or "\\" in rel or ".." in rel.split("/"):
        return None
    root = dist.resolve()
    candidate = (root / rel).resolve()
    # resolve() follows symlinks, so a link inside dist pointing out is
    # refused here too.
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _file_response(path: Path, rel: str) -> FileResponse:
    cache = _IMMUTABLE if rel.startswith("assets/") else _NO_CACHE
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix.lower()),
        headers={**_SECURITY_HEADERS, "Cache-Control": cache},
    )


def _face_icons(dist: Path, face: int | None) -> list[dict[str, str]] | None:
    """The manifest icons for the box's chosen face, or None to leave the
    built manifest as it is: no face yet, face 1 (the original icons), or an
    id this build has no icons for (scripts/face-app-icons.sh)."""
    if face is None or face == 1:
        return None
    any_ = f"faces/app-{face}-512.png"
    maskable = f"faces/app-{face}-maskable-512.png"
    if _resolve(dist, any_) is None or _resolve(dist, maskable) is None:
        return None
    return [
        {"src": f"/{any_}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": f"/{maskable}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ]


def _manifest_response(path: Path, get_face: Callable[[], int | None]) -> Response:
    """The manifest wearing the chosen face. Android draws both the home-screen
    icon and the launch splash from it, so a static one showed face 1 on both
    whatever was picked (the Android user, 2026-10-19). Chrome re-reads the manifest on
    a later launch and offers the new icon. The face is box-wide
    (server/preferences.py), so this needs no device token, which a manifest
    fetch couldn't send anyway."""
    icons = _face_icons(path.parent, get_face())
    if icons is None:
        return _file_response(path, "manifest.webmanifest")
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return _file_response(path, "manifest.webmanifest")
    if not isinstance(manifest, dict):
        return _file_response(path, "manifest.webmanifest")
    manifest["icons"] = icons
    return Response(
        json.dumps(manifest),
        media_type=_MEDIA_TYPES[".webmanifest"],
        headers={**_SECURITY_HEADERS, "Cache-Control": _NO_CACHE},
    )


def _no_face() -> int | None:
    return None


def serve(dist: Path, url_path: str, get_face: Callable[[], int | None] = _no_face) -> Response:
    """`get_face` is called only for the manifest, so other files never read
    the preferences."""
    index = dist / "index.html"
    if not index.is_file():
        return PlainTextResponse(_NOT_BUILT_HINT, status_code=404, headers=_SECURITY_HEADERS)
    found = _resolve(dist, url_path)
    if found is not None:
        rel = found.relative_to(dist.resolve()).as_posix()
        if rel == "manifest.webmanifest":
            return _manifest_response(found, get_face)
        return _file_response(found, rel)
    rel = url_path.lstrip("/")
    if rel.startswith("assets/"):
        # A stale hashed chunk after a redeploy must be a 404, not index.html
        # served as JavaScript.
        return PlainTextResponse("not found\n", status_code=404, headers=_SECURITY_HEADERS)
    return _file_response(index, "index.html")


def install(app: FastAPI, get_dist_dir, get_face: Callable[[], int | None] = _no_face) -> None:
    """`get_dist_dir` is called per request so the setting is read late
    (tests swap it). `get_face` is the box's chosen face, for the manifest."""

    @app.exception_handler(StarletteHTTPException)
    async def _not_found_to_client(request: Request, exc: StarletteHTTPException) -> Response:
        if (
            exc.status_code == 404
            and request.method in ("GET", "HEAD")
            and not _is_api_path(request.url.path)
            and exc.detail == "Not Found"
        ):
            return serve(Path(get_dist_dir()), request.url.path, get_face)
        return await http_exception_handler(request, exc)
