import dataclasses
import importlib
from pathlib import Path

from conftest import FAKE_HOME, FAKE_TREE
from server.boundaries import is_secret_path, matches_deny_arg_shape


def test_credentials_path_is_secret():
    from server.config import settings

    assert is_secret_path(str(settings.credentials_path))


def test_ssh_dir_and_children_are_secret():
    assert is_secret_path("~/.ssh")
    assert is_secret_path("~/.ssh/authorized_keys")


def test_env_files_anywhere_are_secret():
    assert is_secret_path(f"{FAKE_TREE}/.env")
    assert is_secret_path(f"{FAKE_HOME}/.claude/channels/discord/.env")
    assert is_secret_path("/some/deep/path/.env.production")


def test_ordinary_working_tree_path_is_not_secret():
    assert not is_secret_path(f"{FAKE_TREE}/docs/plan.md")


def test_apns_keys_dir_and_children_are_secret():
    assert is_secret_path("~/.marlow/apns")
    assert is_secret_path("~/.marlow/apns/AuthKey_ABC123DEFG.p8")


def test_apns_sibling_key_still_covered_once_one_environment_is_configured():
    # Mike keeps a separate .p8 per environment (sandbox + production) side by
    # side in ~/.marlow/apns/ (docs/marlow-v2-START.md "Still open").
    # Configuring MARLOW_APNS_KEY_PATH for one of them must not stop covering
    # the other — SECRET_PATHS must key off the containing directory, not the
    # one configured file.
    import server.boundaries as boundaries_module
    from server.config import settings as real_settings

    configured = dataclasses.replace(
        real_settings,
        apns_key_path=Path("~/.marlow/apns/AuthKey_SANDBOX.p8").expanduser(),
    )

    boundaries_module.settings = configured
    try:
        importlib.reload(boundaries_module)
        assert boundaries_module.is_secret_path("~/.marlow/apns/AuthKey_SANDBOX.p8")
        # The sibling production key, never referenced by the configured
        # apns_key_path, must still be covered.
        assert boundaries_module.is_secret_path("~/.marlow/apns/AuthKey_PRODUCTION.p8")
    finally:
        importlib.reload(boundaries_module)  # restore real settings-derived state


def test_find_exec_and_delete_flagged():
    assert matches_deny_arg_shape("find . -name '*.tmp' -exec rm {} \\;")
    assert matches_deny_arg_shape("find /tmp -delete")


def test_find_without_dangerous_flags_not_flagged():
    assert not matches_deny_arg_shape("find . -name '*.py'")


def test_rm_rf_either_flag_order_flagged():
    assert matches_deny_arg_shape("rm -rf /tmp/foo")
    assert matches_deny_arg_shape("rm -fr /tmp/foo")


def test_git_push_force_flagged_but_plain_push_is_not():
    assert matches_deny_arg_shape("git push --force origin main")
    assert matches_deny_arg_shape("git push -f origin main")
    assert not matches_deny_arg_shape("git push origin main")
