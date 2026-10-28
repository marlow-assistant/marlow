"""invoke._confinement_prefix — the bubblewrap argv that makes the secret paths
not exist for `claude -p`.

This is the boundary that lets server/bash_policy.py stop being one. A mistake
here is not a missed prompt, it is a secret staying reachable, so the tests
assert the exclusions as hard as the inclusions.
"""
from __future__ import annotations

import dataclasses
import os

import pytest

from server import invoke
from server.config import Settings
from server.config import settings as base_settings


@pytest.fixture
def enabled(monkeypatch):
    """Settings is a frozen dataclass, so override by replacing the object
    invoke.py actually reads rather than mutating fields.

    Returns a callable so a test can layer further overrides on top.
    """
    def apply(**overrides):
        monkeypatch.setattr(
            invoke,
            "settings",
            dataclasses.replace(base_settings, bwrap_enabled=True, bwrap_bin="bwrap", **overrides),
        )

    apply()
    return apply


def test_opting_out_adds_nothing(monkeypatch) -> None:
    """MARLOW_BWRAP_ENABLED=0 is the escape hatch for a host without the
    AppArmor profile (deploy/README.md §11), where bwrap cannot spawn at all."""
    monkeypatch.setattr(invoke, "settings", dataclasses.replace(base_settings, bwrap_enabled=False))
    assert invoke._confinement_prefix() == []


def test_confinement_is_on_by_default(monkeypatch) -> None:
    """The boundary should not depend on anyone remembering a drop-in. Asserts
    the default itself, not a replace()d copy, so flipping it back is a test
    failure rather than a silent loss of the layer bash_policy.py was demoted
    in favour of."""
    monkeypatch.delenv("MARLOW_BWRAP_ENABLED", raising=False)
    assert Settings().bwrap_enabled is True


def test_prefix_is_bwrap_then_a_terminator(enabled) -> None:
    prefix = invoke._confinement_prefix()
    assert prefix[0] == "bwrap"
    assert prefix[-1] == "--", "without the terminator, claude's own argv is read as bwrap options"
    assert "--dev-bind" in prefix and "--die-with-parent" in prefix


def test_the_network_is_never_unshared(enabled) -> None:
    """The PreToolUse hook runs INSIDE this namespace and POSTs to the server
    over loopback. --unshare-net would kill the permission round-trip that
    gate #1 exists to prove -- silently, and only for tool calls."""
    assert "--unshare-net" not in invoke._confinement_prefix()


def test_credentials_are_never_hidden(enabled, tmp_path, monkeypatch) -> None:
    """The CLI authenticates from .credentials.json and writes a refreshed token
    back on every headless run. Hiding it breaks the subscription auth that the
    CLI-not-SDK rule exists to protect."""
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}")
    enabled(credentials_path=creds)
    monkeypatch.setattr(invoke, "SECRET_PATHS", (str(creds),))

    assert str(creds) not in invoke._confinement_prefix()


def test_secret_dirs_are_hidden(enabled, tmp_path, monkeypatch) -> None:
    secret = tmp_path / "dot-ssh"
    secret.mkdir()
    monkeypatch.setattr(invoke, "SECRET_PATHS", (str(secret),))

    prefix = invoke._confinement_prefix()
    assert prefix.count("--tmpfs") == 1
    assert str(secret) in prefix


def test_working_tree_paths_are_never_hidden(enabled, tmp_path, monkeypatch) -> None:
    """Blanking part of the agent's own workspace would break Marlow in a way
    that reads as a model failure rather than a config one."""
    tree = tmp_path / "marlow-identity"
    (tree / "memory").mkdir(parents=True)
    enabled(working_dir=tree)
    monkeypatch.setattr(invoke, "SECRET_PATHS", (str(tree / "memory"), str(tree)))

    assert "--tmpfs" not in invoke._confinement_prefix()


def test_nonexistent_paths_are_skipped(enabled, tmp_path, monkeypatch) -> None:
    """bwrap would happily create the mountpoint, so a typo'd path yields a
    tmpfs that protects nothing while looking like it does."""
    monkeypatch.setattr(invoke, "SECRET_PATHS", (str(tmp_path / "not-there"),))
    assert "--tmpfs" not in invoke._confinement_prefix()


def test_tilde_paths_are_expanded(enabled, monkeypatch) -> None:
    """SECRET_PATHS carries a literal "~/.ssh". An unexpanded tilde would make
    bwrap mount a tmpfs over a directory named "~" and confine nothing."""
    monkeypatch.setattr(invoke, "SECRET_PATHS", ("~",))
    prefix = invoke._confinement_prefix()
    assert prefix.count("--tmpfs") == 1
    assert os.path.expanduser("~") in prefix


def test_agent_config_paths_are_never_hidden(enabled, monkeypatch) -> None:
    """AGENT_CONFIG_PATHS must stay VISIBLE in the namespace: the CLI reads all
    of them at startup, so a tmpfs over ~/.claude would blank the harness's own
    settings and hooks and break every turn -- as an auth-looking failure, not
    an obvious one. They are denied by the hook and confined by nothing, which
    is why they live in their own tuple. Guards the seductive "these are
    dangerous, put them in SECRET_PATHS" refactor."""
    from server import boundaries

    prefix = invoke._confinement_prefix()
    for path in boundaries.AGENT_CONFIG_PATHS:
        assert os.path.expanduser(path) not in prefix


def test_the_two_path_tuples_stay_disjoint() -> None:
    """SECRET_PATHS is hidden by bwrap; AGENT_CONFIG_PATHS must not be. A path
    in both would be simultaneously required and unavailable."""
    from server import boundaries

    secrets = {os.path.expanduser(p) for p in boundaries.SECRET_PATHS}
    configs = {os.path.expanduser(p) for p in boundaries.AGENT_CONFIG_PATHS}
    assert not (secrets & configs)


def test_attachment_root_is_never_hidden(enabled, monkeypatch) -> None:
    """The same "never hidden" property as AGENT_CONFIG_PATHS, for the same
    reason: ATTACHMENT_ROOT is outside SECRET_PATHS by construction (it drives
    an allow, not a deny), but a future refactor that folded it in there would
    tmpfs over the one place the CLI is supposed to Read the image from, and
    the failure would look identical to a permission bug."""
    from server import boundaries

    prefix = invoke._confinement_prefix()
    for path in boundaries.ATTACHMENT_ROOT:
        assert os.path.expanduser(path) not in prefix


def test_attachment_root_and_secret_paths_stay_disjoint() -> None:
    from server import boundaries

    secrets = {os.path.expanduser(p) for p in boundaries.SECRET_PATHS}
    attachments = {os.path.expanduser(p) for p in boundaries.ATTACHMENT_ROOT}
    assert not (secrets & attachments)


def test_the_internal_socket_dir_is_read_only_inside(enabled, tmp_path) -> None:
    """A turn that could unlink the socket and bind its own would answer the
    hook's permission requests itself (config.py, `internal_socket_path`)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    enabled(internal_socket_path=run_dir / "internal.sock")
    prefix = invoke._confinement_prefix()
    real = os.path.realpath(run_dir)
    i = prefix.index("--ro-bind")
    assert prefix[i + 1 : i + 3] == [real, real]
    # After the root bind, or the root bind would mount over it.
    assert i > prefix.index("--dev-bind")


def test_the_internal_socket_dir_is_never_a_secret_path() -> None:
    """The hook inside must still reach the server: a tmpfs over the socket
    dir would deny every tool that needs the phone, as if the server were down."""
    from server import boundaries

    run_dir = os.path.realpath(base_settings.internal_socket_path.parent)
    for secret in boundaries.SECRET_PATHS:
        path = os.path.realpath(os.path.expanduser(secret))
        assert run_dir != path and not run_dir.startswith(path + os.sep)


def test_the_tls_key_dir_is_a_secret_path() -> None:
    """Every instance's user holds a copy of the node's TLS key; a turn that
    read it could impersonate another instance's server across a restart."""
    from server import boundaries

    secrets = {os.path.realpath(os.path.expanduser(p)) for p in boundaries.SECRET_PATHS}
    assert os.path.realpath(base_settings.tls_key_path.parent) in secrets


def test_the_admin_socket_dir_is_a_secret_path() -> None:
    """Connecting to the admin socket mints a pairing invite, and an invite is a
    device token: a turn that reached it could pair itself and answer its own
    permission prompts (config.py, `admin_socket_path`). As a secret path it is
    a tmpfs inside the namespace and the hook denies commands naming it."""
    from server import boundaries

    secrets = {os.path.realpath(os.path.expanduser(p)) for p in boundaries.SECRET_PATHS}
    assert os.path.realpath(base_settings.admin_socket_path.parent) in secrets


def test_hiding_the_admin_socket_leaves_the_internal_one_visible() -> None:
    admin_dir = os.path.realpath(base_settings.admin_socket_path.parent)
    run_dir = os.path.realpath(base_settings.internal_socket_path.parent)
    assert run_dir != admin_dir and not run_dir.startswith(admin_dir + os.sep)
