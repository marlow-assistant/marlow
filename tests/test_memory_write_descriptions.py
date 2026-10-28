"""Tests for the frontmatter surgery in scripts/memory_write_descriptions.py.

The API call itself isn't tested here — it's a one-shot offline migration and
mocking the SDK would test the mock. What *is* tested is the part that edits
Mike's real memory files in the private identity repo: if `_insert_description`
mangles a file, the damage is to personal content this repo can't see.
"""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "memory_write_descriptions.py"
_spec = importlib.util.spec_from_file_location("memory_write_descriptions", _SCRIPT)
mwd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mwd)


def test_inserts_after_name_in_existing_frontmatter():
    text = "---\nname: ci-minutes\nmetadata:\n  type: project\n---\n\nBody text.\n"
    out = mwd._insert_description(text, "macOS CI burns 10x quota minutes")

    lines = out.splitlines()
    assert lines[1] == "name: ci-minutes"
    assert lines[2] == 'description: "macOS CI burns 10x quota minutes"'
    assert lines[3] == "metadata:"
    assert "Body text." in out
    assert mwd._has_description(out)


def test_inserts_at_top_when_no_name_field():
    text = "---\nmetadata:\n  type: user\n---\n\nBody.\n"
    out = mwd._insert_description(text, "who Mike is")

    lines = out.splitlines()
    assert lines[0] == "---"
    assert lines[1] == 'description: "who Mike is"'
    assert mwd._has_description(out)


def test_creates_frontmatter_when_file_has_none():
    text = "# Legacy note\n\nSome prose about the deploy box.\n"
    out = mwd._insert_description(text, "the deploy box's succession plan")

    assert out.startswith('---\ndescription: "the deploy box\'s succession plan"\n---\n\n')
    assert "# Legacy note" in out
    assert mwd._has_description(out)


def test_quotes_and_escapes_yaml_hostile_descriptions():
    text = "---\nname: x\n---\n\nbody\n"
    out = mwd._insert_description(text, 'uses "ask" not: a hook \\ here')

    assert 'description: "uses \\"ask\\" not: a hook \\\\ here"' in out
    # The block must still parse as frontmatter afterwards.
    front, close = mwd._split_frontmatter(out)
    assert close != -1
    assert mwd._has_description(out)


def test_unterminated_frontmatter_is_not_treated_as_frontmatter():
    # A file opening with `---` but never closing it is a horizontal rule, not
    # frontmatter. Inserting into it would corrupt the body.
    text = "---\nthis never closes\n\nbody\n"
    assert mwd._split_frontmatter(text) == ([], -1)
    out = mwd._insert_description(text, "desc")
    assert out.startswith('---\ndescription: "desc"\n---\n\n')
    assert "this never closes" in out


def test_preserves_trailing_newline_state():
    with_nl = "---\nname: a\n---\n\nbody\n"
    without_nl = "---\nname: a\n---\n\nbody"
    assert mwd._insert_description(with_nl, "d").endswith("\n")
    assert not mwd._insert_description(without_nl, "d").endswith("\n")


@pytest.mark.parametrize(
    "text,expected",
    [
        ('---\ndescription: already here\n---\n\nbody', True),
        ('---\ndescription: "quoted"\n---\n\nbody', True),
        ("---\ndescription:\n---\n\nbody", False),  # present but empty
        ("---\nname: a\n---\n\nbody", False),
        ("no frontmatter at all", False),
        ("---\nmetadata:\n  description: nested\n---\n\nbody", False),
    ],
)
def test_has_description_detection(text, expected):
    assert mwd._has_description(text) is expected


def test_insert_is_idempotent_via_the_skip_check():
    text = "---\nname: a\n---\n\nbody\n"
    once = mwd._insert_description(text, "first")
    # The runner skips anything _has_description accepts, so a second pass
    # never happens — this asserts the guard actually catches its own output.
    assert mwd._has_description(once)
