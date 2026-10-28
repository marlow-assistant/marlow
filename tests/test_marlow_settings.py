"""The marlow user's permission policy is security-relevant config that used to
live only on the box, hand-edited, checked by nothing -- which is exactly how
START's Known gap #7 happened (gate #2's audit was applied to `mike` and never
replicated for `marlow`). These tests exist to make that class of drift loud.

They assert structure and lockstep with server/boundaries.py, not taste: the
*contents* of the allow list are a judgement call recorded in
docs/tasks/marlow-settings-permissions-policy.md, and are expected to change.
"""
from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path

import pytest

from server.boundaries import (
    AGENT_CONFIG_PATHS,
    ATTACHMENT_ROOT,
    PRODUCT_OWNED_PATHS,
    SECRET_GLOBS,
    SECRET_PATHS,
    VETTED_PATHLESS_TOOLS,
)
from server.file_policy import READ_TOOLS, WRITE_TOOLS

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
POLICY = DEPLOY / "marlow-settings.json"

# Paths boundaries.py protects that are expressible as settings.json prefix
# rules. SECRET_PATHS itself is computed from the *runtime* config (it picks up
# $HOME and MARLOW_* env vars), so it can't be compared literally against a file
# written for /home/marlow -- these are the marlow-box renderings of it.
#
# These /home/marlow literals are the ONE place that must keep the real box
# path: they are compared against the shipped deploy/marlow-settings.json, so
# they are quoting a file, not naming a directory. Everywhere else uses
# conftest's FAKE_HOME/FAKE_TREE instead -- see the note there for why. Nothing
# here is resolved, so none of it pays the automounter cost that motivated it.
EXPECTED_SECRET_READ_DENIES = (
    "Read(//home/marlow/.claude/.credentials.json)",
    "Read(//home/marlow/.ssh/**)",
    "Read(//home/marlow/.marlow/data/**)",
    "Read(//home/marlow/.marlow/apns/**)",
    "Read(//home/marlow/.marlow/tls/**)",
    "Read(//home/marlow/.marlow/admin/**)",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _rule(tool: str, path: str) -> str:
    """`_rule("Edit", "/home/marlow/x")` -> `Edit(//home/marlow/x)`.

    The tuples in this file name real absolute paths; the policy file has to
    spell them with TWO slashes, because a single leading slash in a user
    settings file anchors at ~/.claude (test_path_rules_are_not_single_slash_absolute).
    """
    assert path.startswith("/") and not path.startswith("//"), path
    return f"{tool}(/{path})"


def _rule_pattern(rule: str) -> str | None:
    """The path pattern inside a `Read(...)`/`Edit(...)`/`Write(...)` rule, or
    None for anything else (Bash rules are command prefixes, not paths)."""
    for tool in ("Read(", "Edit(", "Write("):
        if rule.startswith(tool) and rule.endswith(")"):
            return rule[len(tool):-1]
    return None


def test_path_rules_are_not_single_slash_absolute(policy) -> None:
    """Every path rule must be `//abs`, `~/home-relative` or plain relative.

    In a user settings file -- which is what this file is installed as -- a
    single leading slash anchors at ~/.claude, not the filesystem root (the
    permissions docs, and probed on the box 2026-10-19 at 2.1.301:
    scripts/probe-path-rule-anchor.sh). Every `/home/marlow/...` rule here was
    therefore inert from 2026-08-06 until 2026-10-19: fourteen denies that denied
    nothing, kept safe only by the hook. The CLI does not warn about it, so
    this is the only thing that will.

    A real anchored-at-~/.claude rule would be legitimate but nothing here
    wants one; if that day comes, spell it `~/.claude/...` and it passes.
    """
    rules = policy["permissions"]["allow"] + policy["permissions"]["deny"]
    single = [
        r for r in rules
        if (p := _rule_pattern(r)) is not None and p.startswith("/") and not p.startswith("//")
    ]
    assert not single, (
        f"single-slash path rules: {single}. In ~/.claude/settings.json `/x` means "
        "~/.claude/x, so these match nothing. Use `//x` for an absolute path."
    )


@pytest.fixture
def policy() -> dict:
    """The one policy file. There used to be two, parametrised over -- see
    `test_there_is_exactly_one_policy_file` for why there is now one."""
    return _load(POLICY)


def test_policies_are_valid_json_with_the_expected_blocks(policy) -> None:
    assert "permissions" in policy
    assert isinstance(policy["permissions"].get("allow"), list)
    assert isinstance(policy["permissions"].get("deny"), list)


def test_every_boundaries_secret_path_has_a_deny_rule(policy) -> None:
    """boundaries.py denies these in the hook; settings.json denies them again.

    The hook fires even for allowed tools (probed 2026-08-04,
    scripts/probe-allow-vs-hook.sh), so this is defence in depth rather than the
    only line -- but a settings.json that silently lost a rule while the hook
    kept working is precisely the drift worth catching early.
    """
    deny = policy["permissions"]["deny"]
    for rule in EXPECTED_SECRET_READ_DENIES:
        assert rule in deny, f"{rule} missing from deny"


def test_no_secret_path_was_added_to_boundaries_without_a_deny_rule() -> None:
    """The drift direction that actually matters, and the one the test above
    misses: a NEW entry in boundaries.SECRET_PATHS that nobody mirrored into the
    JSON. The rules can't be compared literally -- SECRET_PATHS is resolved from
    the runtime config ($HOME, MARLOW_DATA_DIR, MARLOW_APNS_KEY_PATH) and the
    JSON is written for /home/marlow -- so compare counts and fail loudly enough
    that the person adding the path knows what else to touch.
    """
    assert len(SECRET_PATHS) == len(EXPECTED_SECRET_READ_DENIES), (
        f"boundaries.SECRET_PATHS has {len(SECRET_PATHS)} entries but this test "
        f"knows about {len(EXPECTED_SECRET_READ_DENIES)}. A secret path was added "
        "or removed: mirror it into deploy/marlow-settings.json and "
        "EXPECTED_SECRET_READ_DENIES above."
    )


def test_every_attachment_root_has_an_allow_rule(policy) -> None:
    """boundaries.ATTACHMENT_ROOT is the read-ALLOW twin of the deny tuples
    above -- same lockstep discipline, opposite direction. Mirroring it here is
    necessary but not sufficient: server/file_policy.py's vet_file_tool is the
    third leg, since an allow entry in this file is inert unless the hook
    independently agrees (the WebSearch/WebFetch lesson, 2026-08-06) -- that
    half is covered in tests/test_file_policy.py instead, this only pins the
    settings.json rendering.
    """
    allow = policy["permissions"]["allow"]
    assert "Read(//home/marlow/.marlow/attachments/**)" in allow


def test_no_attachment_root_was_added_to_boundaries_without_an_allow_rule() -> None:
    """Same drift guard as the SECRET_PATHS/AGENT_CONFIG_PATHS ones above, for
    the one tuple that's an allow instead of a deny."""
    assert len(ATTACHMENT_ROOT) == 1, (
        f"boundaries.ATTACHMENT_ROOT has {len(ATTACHMENT_ROOT)} entries but this "
        "test knows about 1. A root was added or removed: mirror it into "
        "deploy/marlow-settings.json's allow list."
    )


def test_secret_globs_are_denied(policy) -> None:
    deny = policy["permissions"]["deny"]
    for glob in SECRET_GLOBS:
        assert f"Read({glob})" in deny, f"SECRET_GLOBS entry {glob} has no deny rule"


def test_no_agent_config_path_was_added_to_boundaries_without_a_deny_rule(policy) -> None:
    """The same drift guard as the SECRET_PATHS one above, for the other tuple.

    Added because the drift happened: `<working_dir>/.git` went into
    AGENT_CONFIG_PATHS on 2026-08-08 (/security-review — .git/config names
    commands git executes, and identity_git.py now runs git in that repo as the
    server process every turn) and nothing mirrored it here, on a file whose own
    comment says it exists because gap #7 was a box where the hook was never
    wired at all.

    Edit only, since 2026-09-06. There used to be a `Write(...)` twin of every
    line below and this test required both. The CLI warns that a `Write(path)`
    rule does not participate in file-permission checks at all and that an
    `Edit` rule covers every file-editing tool, so the twins were inert — the
    paths stayed unwritable only because someone happened to write both
    spellings. Asserting on them made this test agree with a rule that did
    nothing, which is the failure it exists to catch.

    Bash is a third case and is not mirrored here at all. The
    `--output=.git/config` primitive /security-review actually found is a Bash
    call, and Bash rules in this file are command-prefix matches with no path
    awareness (see its _allow_comment) — that half is the hook's alone.
    """
    deny = policy["permissions"]["deny"]
    # The marlow-box rendering of AGENT_CONFIG_PATHS, same reason as
    # EXPECTED_SECRET_READ_DENIES: the tuple resolves from the runtime config.
    expected = (
        "/home/marlow/.claude.json",
        "/home/marlow/.claude/**",
        "/home/marlow/marlow-identity/.mcp.json",
        "/home/marlow/marlow-identity/.claude/**",
        "/home/marlow/marlow-identity/.git/**",
    )
    for path in expected:
        assert _rule("Edit", path) in deny, f"{_rule('Edit', path)} missing from deny"
        assert _rule("Write", path) not in deny, (
            f"{_rule('Write', path)} is back in the deny list. The CLI does not match "
            "Write(...) rules against file-permission checks — the rule is "
            "inert and the Edit(...) line above is what protects this path. "
            "Re-adding it makes the file claim protection it is not providing."
        )

    # AGENT_CONFIG_PATHS carries ~/.claude/settings.json, settings.local.json and
    # plugins/ separately; all three collapse into the ~/.claude/** rule above,
    # so seven tuple entries mirror to five rules.
    assert len(AGENT_CONFIG_PATHS) - 2 == len(expected), (
        f"boundaries.AGENT_CONFIG_PATHS has {len(AGENT_CONFIG_PATHS)} entries but "
        f"this test knows about {len(expected)} rules. One was added or removed: "
        "mirror it into deploy/marlow-settings.json and the tuple above."
    )


def test_every_working_dir_rule_names_the_configured_working_dir(policy, monkeypatch) -> None:
    """The policy spells the working dir as an absolute path, and the CLI
    matches it literally. A rule left on another name protects nothing and
    says nothing: a missed deny goes inert, the failure
    docs/tasks/settings-path-anchor.md records. The dir was renamed from
    ~/claude-assistant to ~/marlow-identity on 2026-10-27, and this is what
    holds the policy to the config default rather than to a list of rules.

    Every top-level name under the home is either one of the other homes this
    policy covers or the working dir, so a stale one shows up as a stranger."""
    from server.config import _default_working_dir

    monkeypatch.delenv("MARLOW_WORKING_DIR", raising=False)
    default = _default_working_dir()
    assert default.startswith("~/") and "/" not in default[2:]
    working_dir = default[2:]
    others = {".claude", ".claude.json", ".marlow", ".ssh"}
    names = []
    for rules in policy["permissions"].values():
        for rule in rules:
            pattern = _rule_pattern(rule)
            if pattern and pattern.startswith("//home/marlow/"):
                names.append(pattern.removeprefix("//home/marlow/").split("/")[0])
    strangers = sorted({n for n in names if n not in others | {working_dir}})
    assert not strangers, f"rules under /home/marlow name {strangers}, not ~/{working_dir}"
    assert names.count(working_dir) >= 10, "the working dir's rules went missing"


def test_product_owned_rules_are_edit_denied_and_still_readable(policy) -> None:
    """Mirror of boundaries.PRODUCT_OWNED_PATHS (docs/tasks/box-upgrades.md).

    Edit only, for the reason the AGENT_CONFIG_PATHS test gives. And never a
    Read deny: these are the agent's own instructions, which is the whole
    difference from that tuple."""
    deny = policy["permissions"]["deny"]
    expected = (
        "/home/marlow/marlow-identity/CLAUDE.md",
        "/home/marlow/marlow-identity/FIRST-RUN.md",
        "/home/marlow/marlow-identity/cron/email-flash.md",
        "/home/marlow/marlow-identity/cron/ideas.md",
        "/home/marlow/marlow-identity/cron/consolidate.md",
    )
    for path in expected:
        assert _rule("Edit", path) in deny, f"{_rule('Edit', path)} missing from deny"
        assert _rule("Read", path) not in deny, f"{path} must stay readable"
    assert len(PRODUCT_OWNED_PATHS) == len(expected), (
        f"boundaries.PRODUCT_OWNED_PATHS has {len(PRODUCT_OWNED_PATHS)} entries "
        f"but this test knows about {len(expected)}. Mirror the change into "
        "deploy/marlow-settings.json and the tuple above."
    )


def test_identity_files_are_not_writable_unattended(policy) -> None:
    """SOUL.md, USER.md and PREFERENCES.md must fall through to the phone, never
    be pre-allowed.

    Marlow rewriting its own character or Mike's profile is an attended-only
    act. They are absent from `allow` rather than present in `deny` on purpose:
    a deny would block them in the attended case too.

    Matched by glob coverage, not by substring: a rule like
    `Edit(/home/marlow/marlow-identity/*.md)` names neither file and is not the
    literal working-tree-wide rule, but would grant exactly what this forbids.
    """
    identity_files = (
        "/home/marlow/marlow-identity/SOUL.md",
        "/home/marlow/marlow-identity/USER.md",
        "/home/marlow/marlow-identity/PREFERENCES.md",
    )
    allow = policy["permissions"]["allow"]
    assert "Edit(//home/marlow/marlow-identity/memory/**)" in allow

    for rule in allow:
        if not rule.startswith("Edit("):
            continue
        pattern = rule[len("Edit("):].rstrip(")")
        # Resolve the rule's anchor before comparing, or a rule in any other
        # spelling never matches an absolute path and this check passes
        # vacuously: `//abs` is absolute, `~/x` is home-relative, and a bare
        # pattern is relative to the cwd, which is the working tree.
        if pattern.startswith("//"):
            pattern = pattern[1:]
        elif pattern.startswith("~/"):
            pattern = "/home/marlow/" + pattern[2:]
        elif not pattern.startswith("/"):
            pattern = "/home/marlow/marlow-identity/" + pattern.removeprefix("./")
        for path in identity_files:
            # `**` is fnmatch-equivalent to `*` here since `*` already spans "/".
            assert not fnmatch(path, pattern), (
                f"allow rule {rule!r} covers identity file {path} -- it must fall "
                "through to the phone, not run unattended"
            )


def test_no_bash_rules_in_the_allow_list(policy) -> None:
    """Bash is vetted in the hook (server/bash_policy.py), not here.

    This replaces a denylist of file-reading verbs -- cat, head, grep and
    friends -- which passed `Bash(git diff:*)` because `git` was not a name
    anyone thought to list, and `git diff --no-index ~/.ssh/id_ed25519
    /dev/null` reads the file (/security-review 2026-08-05). The lesson was not
    "add git to the denylist": settings.json Bash rules are command-prefix
    matches with no path awareness, so no entry here can express "…but not
    outside the working tree". The hook can, and a Bash call with no allow rule
    reaches it anyway, so an entry here buys nothing and costs the boundary.
    """
    offenders = [r for r in policy["permissions"]["allow"] if r.startswith("Bash(")]
    assert not offenders, (
        f"Bash rules in the allow list: {offenders}. Vet the command in "
        "server/bash_policy.py's VETTED_COMMANDS instead -- a rule here is "
        "path-blind and cannot be made safe."
    )


def test_connector_tools_are_read_only(policy) -> None:
    """Gmail and Calendar are granted tool-by-tool, never at server scope.

    The CLI accepts a bare `mcp__claude_ai_Gmail`, which covers every tool that
    server exposes -- and both connectors are read/write (Gmail: create_draft,
    update_draft, label_thread; Calendar: create_event, delete_event,
    respond_to_event). Server-scope would hand an unattended morning scan the
    ability to act on Mike's mailbox in response to text it just read out of
    that same mailbox, with the auto-deny that should have caught it satisfied
    by a rule nobody re-read.

    Guards the shape, not a list of tool names: a connector entry with no `__`
    tool suffix is server-scope, and any verb below is a mutation.
    """
    writing_verbs = ("create", "update", "delete", "send", "label", "unlabel", "respond", "apply")
    for rule in policy["permissions"]["allow"]:
        if not rule.startswith(CONNECTOR_PREFIX):
            continue
        server = _connector_of(rule)
        tool = rule[len(CONNECTOR_PREFIX) + len(server):].removeprefix("__")
        assert tool, (
            f"connector rule {rule!r} is server-scope -- it grants every tool "
            f"{server} exposes, including the writing ones. Name the tools."
        )
        assert not tool.startswith(writing_verbs), (
            f"connector rule {rule!r} grants a writing tool. Reading is the "
            "whole job; a write belongs on the phone, and in cron it belongs "
            "auto-denied."
        )


# The connectors Mike has decided Marlow may use unattended. NOT the list of
# connectors his account exposes -- see the test below.
GRANTED_CONNECTORS = frozenset({"Gmail", "Google_Calendar"})

CONNECTOR_PREFIX = "mcp__claude_ai_"


def _connector_of(rule: str) -> str:
    """`mcp__claude_ai_Google_Drive__read_file_content` -> `Google_Drive`.

    Splitting on the first `__` is not enough: the prefix itself contains one,
    so it yields `claude_ai_Google_Drive`. Strip the prefix first.
    """
    return rule[len(CONNECTOR_PREFIX):].partition("__")[0]


def test_only_decided_connectors_appear_in_the_allow_list(policy) -> None:
    """Dropping `--strict-mcp-config` grants whatever the *account* has
    connected, not whatever this repo enumerated.

    Found 2026-08-07, reading `marlow`'s CLI transcript during the stage-3
    retest: eleven `mcp__claude_ai_Google_Drive__*` tools were exposed to
    Marlow -- `read_file_content`, `download_file_content`, `search_files`,
    `create_file`, `get_file_permissions` -- and nothing in this repo had ever
    mentioned Drive. It was fail-closed only by omission: no Drive rule exists,
    so every call falls through to the hook, prompts when attended and
    auto-denies in cron. Mike confirmed that is what he wants for Drive.

    Fail-closed-by-accident is the part worth fixing. Connecting a fourth
    connector on claude.ai makes its tools appear here silently, and the
    read-only test above only constrains rules that already exist -- it had
    nothing to say about a server nobody had listed. This makes the arrival of
    a new connector a decision instead of a surprise: if you are adding one on
    purpose, add it here and to the allow list together.
    """
    for rule in policy["permissions"]["allow"]:
        if not rule.startswith(CONNECTOR_PREFIX):
            continue
        server = _connector_of(rule)
        assert server in GRANTED_CONNECTORS, (
            f"connector rule {rule!r} names {server!r}, which nobody decided to "
            f"grant (granted: {sorted(GRANTED_CONNECTORS)}). The account may well "
            "expose it -- that is not the same as Marlow being allowed to use it "
            "unattended. Add it to GRANTED_CONNECTORS deliberately, or drop the rule."
        )


def test_undecided_connectors_are_not_vetted_by_the_hook() -> None:
    """The same guard on the other half of the lockstep.

    An allow entry and a `VETTED_PATHLESS_TOOLS` entry are separately load-
    bearing (an allow entry is inert without the vetter; a vetter without the
    entry is a hook more permissive than the policy). The test above pins the
    JSON; this pins the Python, so a Drive tool can't be vetted locally by
    someone who never touched the settings files.
    """
    offenders = sorted(
        tool for tool in VETTED_PATHLESS_TOOLS
        if tool.startswith(CONNECTOR_PREFIX)
        and _connector_of(tool) not in GRANTED_CONNECTORS
    )
    assert not offenders, (
        f"vetted tools from undecided connectors: {offenders}. The hook would "
        "clear these locally, without them appearing in any settings file."
    )


def test_pathless_allow_entries_are_honoured_by_the_hook(policy) -> None:
    """The lockstep that makes an allow entry mean something.

    A bare tool name in `allow` (no `(...)` scope) is a pathless entry, and the
    hook only agrees with it if the tool is in `VETTED_PATHLESS_TOOLS`. Without
    that agreement the entry is decoration: it prompts the phone when attended
    and burns the 55s window when not, which is exactly what `WebSearch` and
    `WebFetch` did on the box on 2026-08-06 while sitting in this list.

    Asserted both directions -- an entry with no vetter is inert, and a vetted
    tool with no entry is a hook that is more permissive than the policy file
    it is supposed to implement.
    """
    listed = {rule for rule in policy["permissions"]["allow"] if "(" not in rule}
    assert listed - VETTED_PATHLESS_TOOLS == set(), (
        f"allow entries the hook will ignore: {sorted(listed - VETTED_PATHLESS_TOOLS)}. "
        "Add them to boundaries.VETTED_PATHLESS_TOOLS or drop them -- an allow "
        "entry is inert unless the hook independently agrees with it."
    )
    assert VETTED_PATHLESS_TOOLS - listed == set(), (
        f"vetted without being allowed: {sorted(VETTED_PATHLESS_TOOLS - listed)}. "
        "The hook would clear these while this file does not name them."
    )


def test_vetted_pathless_tools_are_pathless() -> None:
    """`VETTED_PATHLESS_TOOLS` skips containment because there is no path to
    contain. A path-taking tool in it would be allowed on its NAME alone, with
    its path never resolved -- so the two sets must stay disjoint, and a tool
    that grows a path argument moves to file_policy's vetter instead.
    """
    overlap = VETTED_PATHLESS_TOOLS & (READ_TOOLS | WRITE_TOOLS)
    assert overlap == frozenset(), (
        f"path-taking tools in VETTED_PATHLESS_TOOLS: {sorted(overlap)}. These "
        "would be allowed without their path ever being resolved."
    )


@pytest.mark.parametrize("tool", [
    "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__get_message",
])
def test_full_message_bodies_are_not_a_standing_grant(policy, tool) -> None:
    """Reading is not one privilege (Mike, 2026-08-07).

    The standing grant is the Gmail LIST VIEW -- senders, subjects, snippets,
    which is what `search_threads` returns. Full message bodies are a
    per-occasion call, as on v1 where Mike denied `get_thread`. These read as
    the obvious omission from an otherwise complete read-only set, which is
    exactly why they need a test: without one, a later session "finishes" the
    enumeration and silently widens the grant.

    Absent rather than in `deny`, deliberately -- absent still reaches the phone
    when attended, so Mike can say yes to a specific thread. In cron it is the
    auto-deny, which is the point: bodies are where injected text lives, and
    cron WebFetch is vetted locally now, so the two grants would compound.
    """
    assert tool not in policy["permissions"]["allow"]
    assert tool not in VETTED_PATHLESS_TOOLS


def test_pretooluse_hook_is_wired(policy) -> None:
    """The omission that caused Known gap #7. Without this the whole permission
    round-trip is dead and every call runs under the CLI's bare default."""
    hooks = policy["hooks"]["PreToolUse"]
    commands = [h["command"] for entry in hooks for h in entry["hooks"]]
    assert any("hooks.pretooluse" in c for c in commands)


def test_thinking_effort_is_low(policy) -> None:
    """A fresh session on the box defaulted to `high` thinking effort (found
    2026-08-15), driving both latency and token spend on every turn.

    Pinned here because the live file is a hand-copied duplicate of this one:
    Mike's fix on the box survives only until the next `cp`, so without the key
    in the repo the regression is silent and permanent. Asserted as an exact
    value rather than "present" because the whole point is the *default*, and a
    typo'd tier would read as configured while behaving like the old default.
    """
    assert policy["effortLevel"] == "low"


def test_the_policy_has_no_ask_block(policy) -> None:
    """`ask` auto-denies in headless and overrides a hook allow (gate #1 §2).

    Since this file governs every turn, an entry here would silently kill the
    phone round-trip for anything listed -- the exact false-negative gate #1
    spent time chasing -- AND buy nothing unattended, where the round-trip
    already denies outright (server/app.py).
    """
    assert "ask" not in policy["permissions"]


def test_there_is_exactly_one_policy_file() -> None:
    """The regression this replaces two tests with (2026-08-07).

    `marlow-settings.cron.json` sat here for three days as the unattended
    policy while being read by nothing, and when finally diffed it turned out
    to differ from the attended file by exactly `ask: [NotebookEdit, Task]`.
    Two auto-denies, in a file whose comments claimed a blast radius it never
    had. The never-prompt policy is code now (server/active_turn.py, and the
    early deny in server/app.py's permission-request handler), where it covers
    every unvetted tool and is testable.

    A second file reappearing means someone is re-implementing that in JSON.
    The CLI's own precedence between a settings file and the hook is exactly
    the thing gate #1 had to probe to establish -- keep the decision in one
    place that can be unit-tested instead.
    """
    found = sorted(p.name for p in DEPLOY.glob("marlow-settings*.json"))
    assert found == ["marlow-settings.json"], (
        f"unexpected policy files: {found}. The attended/unattended split lives "
        "in server/active_turn.py, not in a second settings file."
    )


# --- The tool-surface trim (2026-08-24) -------------------------------------
#
# A deny here removes the tool's NAME from the model's listing, not just the
# ability to call it -- probed on the box at CLI 2.1.221, see
# docs/tasks/marlow-tool-surface-trimming.md. That is what makes these entries
# worth having: everything named below was ALREADY fail-closed (absent from the
# allow list and from VETTED_PATHLESS_TOOLS, so it fell through to a phone
# prompt or a cron auto-deny). The trim buys context and removes misreaches --
# Marlow proposed `CronCreate` for a "remind me later" request -- not safety.

DENIED_CONNECTOR_SERVERS = (
    "mcp__claude_ai_Trello",
    "mcp__claude_ai_Excalidraw",
    "mcp__claude_ai_Google_Drive",
    "mcp__claude_ai_Mermaid_Chart",
)

DENIED_HARNESS_TOOLS = (
    "CronCreate",
    "CronDelete",
    "CronList",
    "RemoteTrigger",
    "DesignSync",
    "EnterWorktree",
    "ExitWorktree",
    "TaskOutput",
    "TaskStop",
    "NotebookEdit",
    # Decided 2026-08-24: no subagents. Denying TaskOutput/TaskStop while
    # leaving Agent reachable was a dead end -- Marlow could start a background
    # agent it had no tool to collect from. Resolved toward denying all four,
    # because this server cannot host a subagent: the queue serializes every
    # claude -p call against a 20-minute timeout, the activity parser reports
    # the first tool_use and stops, and each nested call needing approval would
    # fire its own 55s fail-closed phone prompt. See the JSON's
    # _tool_surface_comment. These two are also the only EAGERLY loaded entries
    # in the list, so they cost a full schema on every turn.
    "Agent",
    "ListAgents",
)


def test_trimmed_tools_are_denied(policy) -> None:
    deny = policy["permissions"]["deny"]
    for tool in DENIED_CONNECTOR_SERVERS + DENIED_HARNESS_TOOLS:
        assert tool in deny, f"{tool} missing from deny -- the trim is incomplete"


def _tool_surface_denies(policy) -> list[str]:
    """Deny entries that name a TOOL rather than a path.

    The deny list holds two kinds of rule. `Read(/home/marlow/.ssh/**)` and
    `Bash(cat ...)` are path/command rules, matched per call. A bare tool name
    is the other kind, and it elides the tool from the model's listing entirely
    (probed 2026-08-24, docs/tasks/marlow-tool-surface-trimming.md). Only the
    second kind can silently remove a capability, so only it is checked below.

    The split is the parenthesis: every rule of the first kind is `Tool(arg)`.
    """
    return [rule for rule in policy["permissions"]["deny"] if "(" not in rule]


def test_the_trim_never_denies_something_that_is_granted(policy) -> None:
    """The failure this exists to catch: denying a tool Marlow actually uses.

    A deny wins over an allow, and because it also elides the NAME, the symptom
    would not be a denial in `permission_denials` -- it would be Marlow calmly
    reporting it cannot read mail, with nothing anywhere saying why. That is the
    same silent shape as the inert `Write(...)` denies found 2026-08-23, running
    in the opposite direction.

    **Reads the policy's own deny list, not this file's tuples.** The first
    version iterated DENIED_CONNECTOR_SERVERS/DENIED_HARNESS_TOOLS, which meant
    it could only catch a mistake made in two places at once -- and the mistake
    it exists to catch arrives in exactly one, an entry added straight to the
    JSON. Found in `/code-review medium` on this branch. If you add a tuple
    here, this test does not need to know about it.

    Checked against both halves that have to agree, since either alone is inert
    (see test_pathless_allow_entries_are_honoured_by_the_hook).
    """
    granted = set(policy["permissions"]["allow"]) | set(VETTED_PATHLESS_TOOLS)

    for rule in _tool_surface_denies(policy):
        assert rule not in granted, (
            f"{rule} is both denied and granted. A deny wins AND elides the name, "
            "so the symptom is Marlow reporting a missing capability, not a denial."
        )
        # Server scope: `mcp__claude_ai_Gmail` covers every tool that server
        # exposes, so an exact-name check alone would miss it silently killing
        # `mcp__claude_ai_Gmail__search_threads` and the morning brief with it.
        clobbered = sorted(t for t in granted if t.startswith(f"{rule}__"))
        assert not clobbered, (
            f"{rule} is a server-scope deny that would elide granted tools: {clobbered}"
        )


def test_gmail_and_calendar_write_tools_are_not_trimmed(policy) -> None:
    """Mike's call, 2026-08-24: they stay listed and keep falling through.

    Denying them is the biggest context win available and it is a BEHAVIOURAL
    change, not a cleanup -- the fall-through to the phone is how the
    per-occasion decision gets made (see _connector_comment). A deny would take
    that option away silently, because the tool would not be listed to ask
    about. If this ever changes it is a decision, so make the test say so.
    """
    deny = policy["permissions"]["deny"]
    for entry in deny:
        assert entry not in ("mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar"), (
            f"{entry} is a server-scope deny over a connector Marlow uses -- it would "
            "elide the granted read tools as well as the write ones"
        )


def test_no_allow_entry_names_a_tool_the_cli_is_not_given(policy) -> None:
    """An allow entry for a tool that does not exist is the quietest dead rule
    of the family, and the only one no CLI warning can surface.

    `TodoWrite` sat in `allow` and in `VETTED_PATHLESS_TOOLS` until 2026-09-06,
    agreeing with itself across both layers while the CLI offered no such tool
    (read from the init event's `tools` array at 2.1.251 — 54 tools, not that
    one). The existing lockstep test passed the whole time precisely BECAUSE
    both halves were dead: it checks the two lists against each other, and two
    inert entries are still consistent.

    So this checks them against the thing that decides what actually exists —
    `VETTED_BUILTIN_TOOLS`, the list `server/invoke.py` hands the CLI as
    `--tools`. Connector tools are exempt: they come from the account's MCP
    servers, not the built-in set, and `--tools` does not govern them.

    Failing here means one of two things, and they need opposite fixes: the
    name is misspelled or retired (drop it), or it is a real tool nobody added
    to the `--tools` allowlist (add it there, deliberately — it widens what the
    model may reach for before the hook ever sees it).
    """
    from server.boundaries import VETTED_BUILTIN_TOOLS

    bare = {rule for rule in policy["permissions"]["allow"] if "(" not in rule}
    builtins = {name for name in bare if not name.startswith("mcp__")}
    unknown = builtins - set(VETTED_BUILTIN_TOOLS)
    assert unknown == set(), (
        f"allow entries naming tools the CLI is never given: {sorted(unknown)}. "
        "Either the name is dead (drop it from allow AND from "
        "VETTED_PATHLESS_TOOLS -- an inert pair still passes the lockstep test "
        "above) or it is real and missing from boundaries.VETTED_BUILTIN_TOOLS."
    )
