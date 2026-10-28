"""Serving the built web client from public_app (server/web_static.py)."""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from server import app as app_module
from server.app import public_app
from server.config import settings
from server.preferences import preferences_store
from server.web_static import CONTENT_SECURITY_POLICY


@pytest.fixture
def dist(tmp_path, monkeypatch):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>marlow</title>")
    (root / "assets" / "app-abc123.js").write_text("console.log(1)")
    (root / "assets" / "app-abc123.css").write_text("body{}")
    (root / "sw.js").write_text("self.skipWaiting()")
    (root / "manifest.webmanifest").write_text("{}")
    (root / "icon-192.png").write_bytes(b"\x89PNG")
    # A secret beside dist, to prove traversal cannot reach it.
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    monkeypatch.setattr(app_module, "settings", replace(settings, web_dist_dir=root))
    return root


def _assert_security_headers(resp):
    assert resp.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_csp_is_the_documented_policy():
    assert CONTENT_SECURITY_POLICY == (
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
        "img-src 'self' blob: data:; "
        "connect-src 'self' https://huggingface.co https://*.hf.co; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


def test_index_served_at_root(dist):
    with TestClient(public_app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "<title>marlow</title>" in resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"
    _assert_security_headers(resp)


def test_hashed_asset_is_cached_forever(dist):
    with TestClient(public_app) as client:
        js = client.get("/assets/app-abc123.js")
        css = client.get("/assets/app-abc123.css")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert css.headers["cache-control"] == "public, max-age=31536000, immutable"
    _assert_security_headers(js)


def test_service_worker_and_manifest_are_no_cache(dist):
    with TestClient(public_app) as client:
        sw = client.get("/sw.js")
        manifest = client.get("/manifest.webmanifest")
        icon = client.get("/icon-192.png")
    assert sw.headers["cache-control"] == "no-cache"
    assert sw.headers["content-type"].startswith("text/javascript")
    assert manifest.headers["cache-control"] == "no-cache"
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert icon.headers["cache-control"] == "no-cache"
    for resp in (sw, manifest, icon):
        _assert_security_headers(resp)


_DEFAULT_ICONS = [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]


@pytest.fixture
def face_dist(dist, monkeypatch):
    """A dist with a real manifest and face 4's icons, and a settable face."""
    (dist / "manifest.webmanifest").write_text(json.dumps({"name": "Marlow", "icons": _DEFAULT_ICONS}))
    (dist / "faces").mkdir()
    for name in ("app-4-512.png", "app-4-maskable-512.png"):
        (dist / "faces" / name).write_bytes(b"\x89PNG")
    chosen = {"face": None}
    monkeypatch.setattr(preferences_store, "face", lambda: chosen["face"])
    return chosen


def _manifest(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    assert resp.headers["cache-control"] == "no-cache"
    _assert_security_headers(resp)
    return resp.json()


def test_manifest_wears_the_chosen_face(face_dist):
    # Android draws the home-screen icon and the launch splash from these.
    face_dist["face"] = 4
    with TestClient(public_app) as client:
        manifest = _manifest(client)
        icons = [client.get(i["src"]) for i in manifest["icons"]]
    assert manifest["name"] == "Marlow"
    assert [(i["src"], i["purpose"]) for i in manifest["icons"]] == [
        ("/faces/app-4-512.png", "any"),
        ("/faces/app-4-maskable-512.png", "maskable"),
    ]
    assert all(r.status_code == 200 for r in icons)


@pytest.mark.parametrize("face", [None, 1, 9])
def test_manifest_is_untouched_without_a_face_with_icons(face_dist, face):
    # None: nothing picked. 1: the original icons. 9: no icons in this build.
    face_dist["face"] = face
    with TestClient(public_app) as client:
        assert _manifest(client)["icons"] == _DEFAULT_ICONS


def test_spa_fallback_serves_index_for_unknown_route(dist):
    with TestClient(public_app) as client:
        resp = client.get("/some/route")
    assert resp.status_code == 200
    assert "<title>marlow</title>" in resp.text
    assert resp.headers["cache-control"] == "no-cache"
    _assert_security_headers(resp)


def test_missing_hashed_asset_is_a_404_not_index_html(dist):
    # After a redeploy a stale chunk name must fail loudly, not come back as
    # HTML that the browser then refuses to run as a script.
    with TestClient(public_app) as client:
        resp = client.get("/assets/gone-deadbeef.js")
    assert resp.status_code == 404
    assert "<title>" not in resp.text
    _assert_security_headers(resp)


def test_head_is_supported(dist):
    with TestClient(public_app) as client:
        resp = client.head("/")
    assert resp.status_code == 200
    assert resp.content == b""


def test_unknown_v1_path_stays_the_api_json_404(dist):
    with TestClient(public_app) as client:
        resp = client.get("/v1/nope")
        nested = client.get("/v1/turn/does/not/exist")
    for r in (resp, nested):
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == {"detail": "Not Found"}
        assert "<title>" not in r.text


def test_api_route_404_is_not_replaced_by_the_client(dist, paired_device_token):
    # A real route raising its own 404 must keep its JSON detail.
    with TestClient(public_app) as client:
        resp = client.get(
            "/v1/turn/no-such-turn", headers={"Authorization": f"Bearer {paired_device_token}"}
        )
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_health_is_still_the_health_json(dist, paired_device_token):
    with TestClient(public_app) as client:
        resp = client.get("/health", headers={"Authorization": f"Bearer {paired_device_token}"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_without_a_token_is_the_api_401_not_the_client(dist):
    with TestClient(public_app) as client:
        resp = client.get("/health")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")


def test_post_to_unknown_path_is_still_404(dist):
    with TestClient(public_app) as client:
        resp = client.post("/some/route", json={})
    assert resp.status_code == 404
    assert "<title>" not in resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2E%2E/%2e%2e/secret.txt",
        "/assets/../../secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/%5c..%5csecret.txt",
        "//etc/passwd",
        "/%2fetc/passwd",
        "/assets/../../pyproject.toml",
    ],
)
def test_traversal_does_not_leak_files_outside_dist(dist, path):
    with TestClient(public_app) as client:
        resp = client.get(path)
    assert "TOP-SECRET" not in resp.text
    assert "root:" not in resp.text
    assert "[project]" not in resp.text and "[tool." not in resp.text
    # Whatever it answers, it is index.html or an error, never the file.
    assert resp.status_code in (200, 400, 404)
    if resp.status_code == 200:
        assert "<title>marlow</title>" in resp.text


def test_resolver_rejects_traversal_directly(dist):
    # The HTTP client normalizes many of these before they leave, so also
    # exercise the resolver with the raw, already-decoded path it would see.
    from server.web_static import _resolve

    for bad in ("/../secret.txt", "/assets/../../secret.txt", "/x/../../secret.txt", "/a\\..\\secret.txt", "/a\0b"):
        assert _resolve(dist, bad) is None


def test_symlink_out_of_dist_is_refused(dist, tmp_path):
    (dist / "leak.txt").symlink_to(tmp_path / "secret.txt")
    with TestClient(public_app) as client:
        resp = client.get("/leak.txt")
    assert "TOP-SECRET" not in resp.text


def test_missing_dist_is_a_plain_404_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        app_module, "settings", replace(settings, web_dist_dir=tmp_path / "nowhere")
    )
    with TestClient(public_app) as client:
        root = client.get("/")
        route = client.get("/some/route")
        health = client.get("/health")
        api = client.get("/v1/nope")
    for r in (root, route):
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/plain")
        assert "npm ci && npm run build" in r.text
        _assert_security_headers(r)
    assert health.status_code == 401  # the API's answer, not the client's 404 hint
    assert api.status_code == 404 and api.json() == {"detail": "Not Found"}


def test_dist_dir_without_index_html_is_treated_as_missing(tmp_path, monkeypatch):
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(app_module, "settings", replace(settings, web_dist_dir=tmp_path / "empty"))
    with TestClient(public_app) as client:
        resp = client.get("/")
    assert resp.status_code == 404
    assert "web client not built" in resp.text


def test_only_the_manifest_reads_the_face(dist, monkeypatch):
    calls = []
    monkeypatch.setattr(preferences_store, "face", lambda: calls.append(1))
    with TestClient(public_app) as client:
        client.get("/")
        client.get("/sw.js")
        client.get("/icon-192.png")
        assert calls == []
        client.get("/manifest.webmanifest")
    assert calls == [1]


def test_a_manifest_that_is_not_an_object_is_served_as_built(face_dist, dist):
    (dist / "manifest.webmanifest").write_text("[]")
    face_dist["face"] = 4
    with TestClient(public_app) as client:
        assert _manifest(client) == []
