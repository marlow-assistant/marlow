import dataclasses
from pathlib import Path

import pytest

import server.identity as identity_module
from server.config import settings as real_settings
from server.identity import (
    IdentityLoadError,
    build_identity_context,
    compose_identity_context,
    get_identity_context,
)


@pytest.fixture(autouse=True)
def _clear_identity_session_cache():
    identity_module._reset_session_cache_for_tests()
    yield
    identity_module._reset_session_cache_for_tests()


def test_compose_orders_soul_user_memory():
    ctx = compose_identity_context(
        soul="I am the soul.",
        user="Mike likes concise answers.",
        memory_entries=[("a-note.md", "Remember this."), ("b-note.md", "And this.")],
    )
    soul_idx = ctx.index("I am the soul.")
    user_idx = ctx.index("Mike likes concise answers.")
    a_idx = ctx.index("Remember this.")
    b_idx = ctx.index("And this.")

    assert soul_idx < user_idx < a_idx < b_idx
    assert "# SOUL" in ctx
    assert "# USER" in ctx
    assert "# MEMORY" in ctx
    assert "a-note.md" in ctx
    assert "b-note.md" in ctx


def test_compose_omits_memory_section_when_no_entries():
    ctx = compose_identity_context(soul="soul text", user="user text", memory_entries=[])
    assert "# MEMORY" not in ctx
    assert "soul text" in ctx
    assert "user text" in ctx


def test_build_identity_context_reads_real_files(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL_MARKER")
    (tmp_path / "USER.md").write_text("USER_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "one.md").write_text("MEMORY_MARKER")

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        memory_mode="inject_all",
    )

    ctx = build_identity_context(s)
    assert "SOUL_MARKER" in ctx
    assert "USER_MARKER" in ctx
    assert "MEMORY_MARKER" in ctx


def test_missing_soul_fails_loud(tmp_path):
    (tmp_path / "USER.md").write_text("USER_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",  # does not exist
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        memory_mode="inject_all",
    )

    with pytest.raises(IdentityLoadError):
        build_identity_context(s)


def test_missing_user_fails_loud(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",  # does not exist
        memory_dir=memory_dir,
        memory_mode="inject_all",
    )

    with pytest.raises(IdentityLoadError):
        build_identity_context(s)


def test_empty_soul_fails_loud(tmp_path):
    (tmp_path / "SOUL.md").write_text("   \n  ")
    (tmp_path / "USER.md").write_text("USER_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        memory_mode="inject_all",
    )

    with pytest.raises(IdentityLoadError):
        build_identity_context(s)


def test_missing_memory_dir_warns_not_fatal(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL_MARKER")
    (tmp_path / "USER.md").write_text("USER_MARKER")

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=tmp_path / "memory",  # does not exist
        memory_mode="inject_all",
    )

    ctx = build_identity_context(s)
    assert "SOUL_MARKER" in ctx
    assert "USER_MARKER" in ctx
    assert "# MEMORY" not in ctx


def test_empty_memory_dir_warns_not_fatal(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL_MARKER")
    (tmp_path / "USER.md").write_text("USER_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        memory_mode="inject_all",
    )

    ctx = build_identity_context(s)
    assert "# MEMORY" not in ctx


def test_unreadable_memory_entry_is_skipped_not_fatal(tmp_path):
    # Code-review finding: a single bad file in memory/ (bad encoding, stray
    # binary) must not take down every future invocation the way an
    # empty/missing memory dir is already handled as a warning, not fatal.
    (tmp_path / "SOUL.md").write_text("SOUL_MARKER")
    (tmp_path / "USER.md").write_text("USER_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "good.md").write_text("GOOD_MARKER")
    (memory_dir / "bad.md").write_bytes(b"\xff\xfe\x00bad-non-utf8")

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        memory_mode="inject_all",
    )

    ctx = build_identity_context(s)
    assert "GOOD_MARKER" in ctx
    assert "bad-non-utf8" not in ctx


def test_unsupported_memory_mode_raises_not_implemented(tmp_path):
    """Pins the seam for the future index-only mode: it's a recognized-but-not-built
    branch, not silently treated as inject_all."""
    (tmp_path / "SOUL.md").write_text("SOUL_MARKER")
    (tmp_path / "USER.md").write_text("USER_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    s = dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        # index_only is implemented now (docs/tasks/memory-index-split.md); this
        # test needs a mode that genuinely isn't.
        memory_mode="retrieve_on_demand",
    )

    with pytest.raises(NotImplementedError):
        build_identity_context(s)


def _frozen_settings(tmp_path, marker: str):
    (tmp_path / "SOUL.md").write_text(f"SOUL_{marker}")
    (tmp_path / "USER.md").write_text(f"USER_{marker}")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    return dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        memory_mode="inject_all",
        identity_freeze_max_age_seconds=3600.0,
    )


def test_same_session_id_reuses_frozen_string_and_reads_disk_once(tmp_path, monkeypatch):
    s = _frozen_settings(tmp_path, "V1")

    read_calls = []
    real_read_required = identity_module._read_required

    def spy_read_required(path, label):
        read_calls.append(label)
        return real_read_required(path, label)

    monkeypatch.setattr(identity_module, "_read_required", spy_read_required)

    first = get_identity_context(s, session_id="sess-1", resume=True)
    second = get_identity_context(s, session_id="sess-1", resume=True)

    assert first == second
    assert "SOUL_V1" in first
    # SOUL + USER read once each on the first call, never again on the second.
    assert read_calls == ["SOUL.md", "USER.md"]


def test_different_session_ids_each_get_fresh_read(tmp_path):
    s = _frozen_settings(tmp_path, "V1")

    a = get_identity_context(s, session_id="sess-a", resume=True)
    b = get_identity_context(s, session_id="sess-b", resume=True)

    assert "SOUL_V1" in a
    assert "SOUL_V1" in b
    # Different cache entries, even though the underlying content is identical.
    assert identity_module._session_cache["sess-a"] is not identity_module._session_cache["sess-b"]


def test_resume_false_always_reads_fresh_even_with_existing_cache_entry(tmp_path):
    s = _frozen_settings(tmp_path, "V1")
    get_identity_context(s, session_id="sess-1", resume=True)  # seeds the cache

    fresh = get_identity_context(s, session_id=None, resume=False)
    assert "SOUL_V1" in fresh
    # A resume=False call with the same session_id as a cached entry also
    # bypasses the cache — it's not a "resume this session" call.
    fresh_same_id = get_identity_context(s, session_id="sess-1", resume=False)
    assert "SOUL_V1" in fresh_same_id


def test_memory_write_mid_session_does_not_change_frozen_string(tmp_path):
    s = _frozen_settings(tmp_path, "V1")

    first = get_identity_context(s, session_id="sess-1", resume=True)
    (s.memory_dir / "new-note.md").write_text("SHOULD NOT APPEAR YET")

    second = get_identity_context(s, session_id="sess-1", resume=True)
    assert first == second
    assert "SHOULD NOT APPEAR YET" not in second

    # Proves it's the freeze and not coincidence: a fresh (non-cached) read
    # against the same settings does pick up the new file.
    fresh = build_identity_context(s)
    assert "SHOULD NOT APPEAR YET" in fresh


def test_frozen_entry_recomputed_after_max_age(tmp_path, monkeypatch):
    s = dataclasses.replace(_frozen_settings(tmp_path, "V1"), identity_freeze_max_age_seconds=100.0)

    clock = {"t": 1000.0}
    monkeypatch.setattr(identity_module.time, "monotonic", lambda: clock["t"])

    first = get_identity_context(s, session_id="sess-1", resume=True)

    (s.soul_path).write_text("SOUL_V2")
    clock["t"] += 50.0  # within the 100s ceiling
    still_frozen = get_identity_context(s, session_id="sess-1", resume=True)
    assert still_frozen == first
    assert "SOUL_V2" not in still_frozen

    clock["t"] += 100.0  # now past the ceiling
    recomputed = get_identity_context(s, session_id="sess-1", resume=True)
    assert "SOUL_V2" in recomputed


def test_expired_entries_are_pruned_not_left_to_accumulate_forever(tmp_path, monkeypatch):
    # Code-review finding: _session_cache only ever grew — a session cached
    # once and never resumed again sat in memory for the life of the process.
    s = dataclasses.replace(_frozen_settings(tmp_path, "V1"), identity_freeze_max_age_seconds=100.0)

    clock = {"t": 1000.0}
    monkeypatch.setattr(identity_module.time, "monotonic", lambda: clock["t"])

    get_identity_context(s, session_id="abandoned-session", resume=True)
    assert "abandoned-session" in identity_module._session_cache

    # abandoned-session is never touched again; some other session's cache
    # miss, long after abandoned-session has expired, should sweep it out.
    clock["t"] += 500.0
    get_identity_context(s, session_id="another-session", resume=True)

    assert "abandoned-session" not in identity_module._session_cache
    assert "another-session" in identity_module._session_cache


# --- index_only mode (docs/tasks/memory-index-split.md) ---------------------


def _index_settings(tmp_path, mode="index_only"):
    (tmp_path / "SOUL.md").write_text("SOUL_MARKER")
    (tmp_path / "USER.md").write_text("USER_MARKER")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    return dataclasses.replace(
        real_settings,
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
        memory_mode=mode,
    )


def _write_memory(tmp_path, name, text):
    path = tmp_path / "memory" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


FRONTMATTER_ENTRY = """---
name: ci-minutes
description: macOS CI burns 10x quota minutes
metadata:
  type: project
---

BODY_MARKER the full body text lives here.
"""


def test_index_only_lists_names_and_descriptions_without_bodies(tmp_path):
    s = _index_settings(tmp_path)
    _write_memory(tmp_path, "a-ci.md", FRONTMATTER_ENTRY)
    _write_memory(tmp_path, "b-plain.md", "# Heading\n\nFirst real line here.\n\nBODY2_MARKER")

    ctx = build_identity_context(s)

    assert "a-ci.md" in ctx and "b-plain.md" in ctx
    assert "macOS CI burns 10x quota minutes" in ctx
    assert "First real line here." in ctx
    # The whole point: no entry body reaches the block.
    assert "BODY_MARKER" not in ctx
    assert "BODY2_MARKER" not in ctx
    assert "SOUL_MARKER" in ctx and "USER_MARKER" in ctx


def test_index_only_is_materially_smaller_than_inject_all(tmp_path):
    body = "x" * 5000
    for i in range(5):
        _write_memory(tmp_path, f"m{i}.md", f"---\ndescription: short desc {i}\n---\n\n{body}")

    index_ctx = build_identity_context(_index_settings(tmp_path, "index_only"))
    full_ctx = build_identity_context(_index_settings(tmp_path, "inject_all"))

    assert len(index_ctx) < len(full_ctx) / 10


def test_index_is_byte_stable_across_calls_and_mtime_touches(tmp_path):
    s = _index_settings(tmp_path)
    path = _write_memory(tmp_path, "a-ci.md", FRONTMATTER_ENTRY)
    _write_memory(tmp_path, "b-plain.md", "just a line")

    first = build_identity_context(s)
    second = build_identity_context(s)
    assert first == second

    # Content unchanged, mtime moved — the block must not budge. This is the
    # regression test for the "index grows a timestamp" trap.
    import os
    st = path.stat()
    os.utime(path, (st.st_atime + 10_000, st.st_mtime + 10_000))
    assert build_identity_context(s) == first


def test_adding_one_entry_changes_exactly_one_line(tmp_path):
    s = _index_settings(tmp_path)
    _write_memory(tmp_path, "a.md", "---\ndescription: alpha\n---\n\nbody")
    _write_memory(tmp_path, "c.md", "---\ndescription: gamma\n---\n\nbody")

    before = build_identity_context(s).splitlines()
    _write_memory(tmp_path, "b.md", "---\ndescription: beta\n---\n\nbody")
    after = build_identity_context(s).splitlines()

    assert len(after) == len(before) + 1
    added = [line for line in after if line not in before]
    assert len(added) == 1
    assert "b.md" in added[0]


def test_index_ordering_is_deterministic_not_filesystem_order(tmp_path):
    s = _index_settings(tmp_path)
    for name in ("z.md", "a.md", "m.md"):
        _write_memory(tmp_path, name, f"---\ndescription: {name}\n---\n\nbody")

    ctx = build_identity_context(s)
    assert ctx.index("a.md") < ctx.index("m.md") < ctx.index("z.md")


def test_index_falls_back_to_first_non_heading_line(tmp_path):
    s = _index_settings(tmp_path)
    _write_memory(tmp_path, "legacy.md", "# Title\n\n## Sub\n\nThe actual summary line.\nmore")
    ctx = build_identity_context(s)
    assert "The actual summary line." in ctx
    assert "more" not in ctx.split("legacy.md")[1].splitlines()[0]


def test_index_ignores_nested_metadata_description(tmp_path):
    s = _index_settings(tmp_path)
    _write_memory(
        tmp_path,
        "nested.md",
        "---\nname: x\nmetadata:\n  description: WRONG_FIELD\n---\n\nRight fallback line.",
    )
    ctx = build_identity_context(s)
    assert "WRONG_FIELD" not in ctx
    assert "Right fallback line." in ctx


def test_index_description_is_single_line_and_bounded(tmp_path):
    s = _index_settings(tmp_path)
    _write_memory(tmp_path, "long.md", "---\ndescription: " + "word " * 200 + "\n---\n\nbody")

    ctx = build_identity_context(s)
    memory_lines = [ln for ln in ctx.splitlines() if "long.md" in ln]
    assert len(memory_lines) == 1
    assert len(memory_lines[0]) < 260


def test_index_only_skips_unreadable_entry_without_failing(tmp_path):
    s = _index_settings(tmp_path)
    _write_memory(tmp_path, "good.md", "---\ndescription: fine\n---\n\nbody")
    (tmp_path / "memory" / "bad.bin").write_bytes(b"\xff\xfe\x00\x01binary junk")

    ctx = build_identity_context(s)
    assert "good.md" in ctx
    assert "bad.bin" not in ctx


def test_index_only_survives_multibyte_char_split_at_scan_boundary(tmp_path):
    s = _index_settings(tmp_path)
    scan = identity_module._DESCRIPTION_SCAN_BYTES
    header = "---\ndescription: ok\n---\n\n"
    # The multibyte char must genuinely straddle the cut, or _decode_prefix's
    # trim loop is never exercised and this test passes against a bare
    # `raw.decode("utf-8")` — which is the regression it exists to catch.
    # "→" is 3 bytes; start it one byte before the boundary.
    padding = "a" * (scan - len(header.encode("utf-8")) - 1)
    body = header + padding + "→ tail"
    _write_memory(tmp_path, "wide.md", body)

    raw = body.encode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        raw[:scan].decode("utf-8")  # the prefix really is cut mid-character

    ctx = build_identity_context(s)
    assert "wide.md" in ctx
    assert "ok" in ctx


def test_decode_prefix_recovers_a_split_codepoint():
    raw = ("a" + "→").encode("utf-8")  # 1 + 3 bytes
    assert identity_module._decode_prefix(raw[:2]) == "a"
    assert identity_module._decode_prefix(raw[:3]) == "a"
    assert identity_module._decode_prefix(raw) == "a→"


def test_decode_prefix_still_raises_on_genuinely_bad_bytes():
    with pytest.raises(UnicodeDecodeError):
        identity_module._decode_prefix(b"\xff\xfe\x00\x01binary junk")


def test_unterminated_frontmatter_does_not_index_as_a_horizontal_rule(tmp_path):
    # A file opening with `---` that never closes it (a horizontal rule, or a
    # block running past the scan window) must not yield the literal "---" as
    # its description — the lint would report that as present, and Marlow
    # cannot route on it.
    s = _index_settings(tmp_path)
    _write_memory(tmp_path, "rule.md", "---\nThe actual first line.\n\nmore\n")

    ctx = build_identity_context(s)

    assert "The actual first line." in ctx
    assert "`rule.md` — ---" not in ctx


def test_frontmatter_running_past_the_scan_window_falls_back_cleanly(tmp_path):
    s = _index_settings(tmp_path)
    scan = identity_module._DESCRIPTION_SCAN_BYTES
    # Frontmatter opens, then a huge field pushes both `description:` and the
    # closing fence beyond the prefix the loader reads.
    body = "---\nnotes: " + ("x" * (scan + 100)) + "\ndescription: never seen\n---\n\nbody\n"
    _write_memory(tmp_path, "huge.md", body)

    ctx = build_identity_context(s)

    assert "huge.md" in ctx
    assert "never seen" not in ctx
    assert "`huge.md` — ---" not in ctx


def test_index_preamble_names_the_configured_memory_dir(tmp_path):
    # The preamble used to hardcode `memory/` while MARLOW_MEMORY_DIR is
    # independently configurable — a mismatch would make every body silently
    # unreachable, with no error anywhere.
    s = _index_settings(tmp_path)
    custom = tmp_path / "brain"
    custom.mkdir()
    (custom / "a.md").write_text("---\ndescription: alpha\n---\n\nbody")
    s = dataclasses.replace(s, memory_dir=custom, working_dir=tmp_path)

    ctx = build_identity_context(s)

    assert "`brain/`" in ctx
    assert "`memory/`" not in ctx


def test_memory_label_falls_back_to_an_absolute_path_outside_the_working_dir(tmp_path):
    from server.identity import memory_label

    assert memory_label(tmp_path / "wd" / "memory", tmp_path / "wd") == "memory/"
    # Configured outside the tree: a relative path would not resolve from the
    # CLI's cwd, so the absolute one is the only useful answer.
    assert memory_label(Path("/elsewhere/mem"), tmp_path / "wd") == "/elsewhere/mem"


def test_index_only_empty_memory_dir_omits_section(tmp_path):
    s = _index_settings(tmp_path)
    ctx = build_identity_context(s)
    assert "# MEMORY" not in ctx


def test_inject_all_rendering_unchanged_by_new_mode_param(tmp_path):
    entries = [("a.md", "body a"), ("b.md", "body b")]
    assert compose_identity_context("s", "u", entries) == compose_identity_context(
        "s", "u", entries, "inject_all"
    )


def test_unsupported_memory_mode_still_raises(tmp_path):
    s = _index_settings(tmp_path, "nonsense_mode")
    with pytest.raises(NotImplementedError):
        build_identity_context(s)


@pytest.mark.parametrize("rule", ["---", "----", "*****", "___", "- - -"])
def test_horizontal_rules_are_never_used_as_a_description(tmp_path, rule):
    s = _index_settings(tmp_path)
    _write_memory(tmp_path, "r.md", f"{rule}\nThe real first line.\n")
    ctx = build_identity_context(s)
    assert "The real first line." in ctx
    assert f"`r.md` — {rule}" not in ctx
