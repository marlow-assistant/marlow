"""Tests for scripts/memory_index_lint.py.

The exit code is the point. This script is the gate on flipping
MARLOW_MEMORY_MODE to index_only (docs/tasks/memory-index-split.md), so "did it
pass" has to mean "every entry has a real frontmatter description" — not "it
managed to produce an index line for everything". A first-body-line fallback
produces a plausible-looking index entry that is really just whatever the file
opened with, which is the one failure mode that survives a skim of the report.
"""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "memory_index_lint.py"
_spec = importlib.util.spec_from_file_location("memory_index_lint", _SCRIPT)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

DESCRIBED = '---\nname: a\ndescription: "A real description"\n---\n\nBody.\n'
FALLBACK = "# Heading\n\nFirst body line becomes the index entry.\n"
BARE = "# Heading only\n"


def _run(tmp_path, monkeypatch, capsys, files: dict[str, str]) -> tuple[int, str]:
    memory = tmp_path / "memory"
    memory.mkdir()
    for name, text in files.items():
        (memory / name).write_text(text)
    monkeypatch.setattr("sys.argv", ["memory_index_lint.py", str(memory)])
    code = lint.main()
    return code, capsys.readouterr().out


def test_all_entries_described_passes(tmp_path, monkeypatch, capsys):
    code, out = _run(tmp_path, monkeypatch, capsys, {"a.md": DESCRIBED, "b.md": DESCRIBED})
    assert code == 0
    assert "OK: all 2 entries" in out


def test_a_fallback_entry_fails_the_lint(tmp_path, monkeypatch, capsys):
    """The regression that matters: a fallback used to exit 0, so a half-migrated
    memory dir passed the gate."""
    code, out = _run(tmp_path, monkeypatch, capsys, {"a.md": DESCRIBED, "b.md": FALLBACK})

    assert code == 1
    assert "1 entry falls back to its first body line" in out
    assert "FAIL: 1 of 2 entries lacks" in out
    # The report still prints, so a mid-migration preview is not punished.
    assert "index Marlow would receive" in out


def test_report_agrees_in_number(tmp_path, monkeypatch, capsys):
    """Plural forms, against the singular case above. This report is the
    migration's only feedback loop and is read on the box mid-task."""
    _, out = _run(
        tmp_path, monkeypatch, capsys, {"a.md": FALLBACK, "b.md": FALLBACK, "c.md": BARE}
    )

    assert "2 entries fall back to their first body line" in out
    assert "1 entry has NO usable description — it indexes as a bare filename" in out
    assert "FAIL: 3 of 3 entries lack a frontmatter" in out


def test_single_described_entry_reads_correctly(tmp_path, monkeypatch, capsys):
    code, out = _run(tmp_path, monkeypatch, capsys, {"a.md": DESCRIBED})
    assert code == 0
    assert "OK: the 1 entry has a frontmatter `description:`." in out


def test_an_entry_with_no_usable_description_fails(tmp_path, monkeypatch, capsys):
    code, out = _run(tmp_path, monkeypatch, capsys, {"a.md": BARE})
    assert code == 1
    assert "NO usable description" in out


def test_fallback_and_missing_are_counted_together(tmp_path, monkeypatch, capsys):
    code, out = _run(
        tmp_path, monkeypatch, capsys, {"a.md": DESCRIBED, "b.md": FALLBACK, "c.md": BARE}
    )
    assert code == 1
    assert "FAIL: 2 of 3 entries" in out


def test_missing_directory_is_a_usage_error_not_a_lint_failure(tmp_path, monkeypatch):
    """Exit 2, distinct from 1 — a typo'd path must not read as "memory is
    un-migrated" to whatever is gating on this."""
    monkeypatch.setattr("sys.argv", ["memory_index_lint.py", str(tmp_path / "nope")])
    assert lint.main() == 2


def test_empty_memory_dir_does_not_pass_the_gate(tmp_path, monkeypatch, capsys):
    """Nothing was checked, so this is not a pass. It used to print
    "OK: all 0 entries" and exit 0, greenlighting the mode flip on zero
    evidence — the failure mode this whole gate exists to prevent."""
    code, out = _run(tmp_path, monkeypatch, capsys, {})

    assert code == 2
    assert "OK:" not in out


def test_a_dir_of_only_unreadable_entries_does_not_pass_the_gate(
    tmp_path, monkeypatch, capsys
):
    """Same false pass by another route: `_read_memory_entries` skips these
    with a warning, leaving zero entries behind."""
    code, out = _run(tmp_path, monkeypatch, capsys, {"blank.md": "   \n\n", "also-blank.md": ""})

    assert code == 2
    assert "OK:" not in out
