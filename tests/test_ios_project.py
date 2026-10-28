"""Static invariants for the generated iOS project."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_SPEC = REPO_ROOT / "ios" / "Marlow" / "project.yml"
PBX_PROJECT = REPO_ROOT / "ios" / "Marlow" / "Marlow.xcodeproj" / "project.pbxproj"
PROCESSED_INFO_PLIST = "$(BUILT_PRODUCTS_DIR)/$(INFOPLIST_PATH)"


def test_git_stamp_waits_for_xcodes_processed_info_plist():
    """Otherwise ProcessInfoPlistFile can erase the stamped keys afterward."""
    assert f'"{PROCESSED_INFO_PLIST}"' in PROJECT_SPEC.read_text()
    assert f'"{PROCESSED_INFO_PLIST}"' in PBX_PROJECT.read_text()


def test_no_apple_team_id_is_committed():
    """The team ID is an Apple account identifier, so it lives in the
    gitignored `Signing.local.xcconfig` (2026-10-26). A fork
    signs with its own; without the file a simulator build still works."""
    import re

    ios = REPO_ROOT / "ios" / "Marlow"
    assert not re.search(r"DEVELOPMENT_TEAM: \w", PROJECT_SPEC.read_text())
    assert not re.search(r"DEVELOPMENT_TEAM = \w", PBX_PROJECT.read_text())
    for swift in ios.rglob("*.swift"):
        # A literal keychain group spells the prefix out: "ABCDE12345.com.…".
        assert not re.search(r'"[A-Z0-9]{10}\.com\.', swift.read_text()), swift
    signing = (ios / "Config" / "Signing.xcconfig").read_text()
    assert '#include? "Signing.local.xcconfig"' in signing
    assert "ios/Marlow/Config/Signing.local.xcconfig" in (REPO_ROOT / ".gitignore").read_text()
