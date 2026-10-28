"""The path-taking-tool allowlist (server/file_policy.py).

Regression suite for the 2026-08-06 bug: `deploy/marlow-settings.json` was
installed on the box and the very first turn -- "add a note to memory" -- raised
five phone prompts, two of them for a `Read` and an `Edit` that the allow list
explicitly covers. The hook is consulted for allow-matched calls and its answer
wins, so settings.json's allow list was dead letter for every tool but Bash.

Same discipline as tests/test_bash_policy.py: ALLOW asserted narrowly, PROMPT
asserted generously, because a miss costs a prompt and a false ALLOW costs the
boundary.
"""
from __future__ import annotations

import pytest

from conftest import FAKE_HOME, FAKE_TREE
from server.bash_policy import ALLOW, PROMPT
from server.boundaries import PATH_ARG_KEYS
from server.file_policy import vet_file_tool, vet_pathless_tool

TREE = FAKE_TREE


def _decide(tool: str, path: str | None = None, key: str = "file_path", tree: str = TREE):
    tool_input = {} if path is None else {key: path}
    return vet_file_tool(tool, tool_input, working_tree=tree)[0]


# --- the bug itself --------------------------------------------------------

@pytest.mark.parametrize("tool,path,key", [
    ("Read", f"{TREE}/memory/projects.md", "file_path"),
    ("Read", f"{TREE}/SOUL.md", "file_path"),
    ("Edit", f"{TREE}/memory/projects.md", "file_path"),
    ("Write", f"{TREE}/memory/new-note.md", "file_path"),
    ("Grep", f"{TREE}/memory", "path"),
    ("Glob", f"{TREE}", "path"),
])
def test_the_five_prompt_turn_is_vetted_locally(tool, path, key) -> None:
    """Each of these matched an allow rule and woke the phone anyway."""
    assert _decide(tool, path, key) == ALLOW


def test_a_relative_path_is_resolved_against_the_tree() -> None:
    """The CLI runs with cwd=working_dir, so tool paths are frequently
    relative. Treating those as unvettable would leave the bug half-fixed."""
    assert _decide("Edit", "memory/projects.md") == ALLOW


@pytest.mark.parametrize("tool", ["Glob", "Grep"])
def test_an_absent_path_is_the_working_tree(tool) -> None:
    """Glob/Grep default to cwd, which invoke.py always sets to working_dir."""
    assert _decide(tool, None) == ALLOW


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
def test_an_absent_path_on_a_path_required_tool_prompts(tool) -> None:
    """No default to reason about -- a malformed call is not a vetted one."""
    assert _decide(tool, None) == PROMPT


# --- writes are narrower than reads ----------------------------------------

@pytest.mark.parametrize("path", [
    f"{TREE}/SOUL.md",
    f"{TREE}/USER.md",
    f"{TREE}/memory/../SOUL.md",   # the same file, spelled to look contained
])
def test_identity_files_are_readable_but_never_writable(path) -> None:
    """The contract in tests/test_marlow_settings.py, enforced at the hook.

    Marlow rewriting its own character or Mike's profile is an attended-only
    act. Reading them is ordinary work and happens on every turn.
    """
    assert _decide("Read", path) == ALLOW
    assert _decide("Edit", path) == PROMPT
    assert _decide("Write", path) == PROMPT


@pytest.mark.parametrize("tool", ["Edit", "Write"])
@pytest.mark.parametrize("path", ["SOUL.md", "USER.md", "./SOUL.md", "memory/../SOUL.md"])
def test_a_relative_identity_write_is_not_re_anchored(tool, path) -> None:
    """/code-review 2026-08-06, and the reason this file's other tests missed it.

    The first version handed the same RELATIVE string to two containment checks
    with two different roots: `SOUL.md` against `<tree>` (inside, fine) and then
    `SOUL.md` against `<tree>/memory` -- where it resolved to
    `<tree>/memory/SOUL.md`, landed inside memory/, and was vetted. The tool
    would have written `<tree>/SOUL.md`.

    Relative is the NORMAL form, not an edge case: invoke.py launches
    `claude -p` with cwd=working_dir. Every other test here used absolute paths,
    which is exactly why they all passed.
    """
    assert _decide(tool, path) == PROMPT


def test_a_relative_write_into_memory_is_still_vetted() -> None:
    """The fix must not be "prompt on everything relative"."""
    assert _decide("Edit", "memory/projects.md") == ALLOW
    assert _decide("Write", "memory/new.md") == ALLOW


def test_a_write_to_the_tree_root_prompts() -> None:
    assert _decide("Write", f"{TREE}/notes.md") == PROMPT


@pytest.mark.parametrize("tool", ["Edit", "Write"])
@pytest.mark.parametrize("path", [
    f"{TREE}/cron/schedules.json",
    "cron/schedules.json",
    "./cron/schedules.json",
])
def test_schedules_store_is_the_one_named_write_outside_memory(tool, path) -> None:
    assert _decide(tool, path) == ALLOW


@pytest.mark.parametrize("path", [
    f"{TREE}/cron/job.md",
    f"{TREE}/cron/other.json",
    f"{TREE}/cron/schedules.json/child",
    f"{TREE}/cron-schedules.json",
])
def test_schedules_grant_does_not_open_the_cron_directory(path) -> None:
    assert _decide("Write", path) == PROMPT


def test_schedules_named_file_grant_does_not_follow_a_symlink(tmp_path) -> None:
    tree = tmp_path / "tree"
    (tree / "cron").mkdir(parents=True)
    (tree / "SOUL.md").write_text("identity\n")
    (tree / "cron" / "schedules.json").symlink_to("../SOUL.md")

    assert _decide("Write", "cron/schedules.json", tree=str(tree)) == PROMPT


def test_memory_prefixed_siblings_are_not_the_memory_directory() -> None:
    """`memory-scratch` starts with `memory` as a string but is not under it."""
    assert _decide("Write", f"{TREE}/memory-scratch/x.md") == PROMPT


# --- containment -----------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/etc/passwd",
    f"{TREE}/../.claude.json",
    "~/.ssh/id_ed25519",
    f"{FAKE_HOME}/.claude/settings.json",
])
def test_paths_outside_the_tree_prompt(path) -> None:
    assert _decide("Read", path) == PROMPT


def test_a_symlink_out_of_the_tree_is_not_vetted(tmp_path) -> None:
    """The realpath half of containment. No `..`, no leading slash -- the shape
    that defeated an earlier literal-looking check in bash_policy."""
    tree = tmp_path / "tree"
    (tree / "memory").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (tree / "memory" / "escape").symlink_to(outside)

    assert _decide("Read", str(tree / "memory" / "escape" / "secret"), tree=str(tree)) == PROMPT


def test_a_glob_pattern_is_a_path_not_a_filter() -> None:
    """/code-review 2026-08-06. Glob's `pattern` decides what is enumerated on
    its own and may be absolute; it is not a filter applied under `path`.

    `Glob(pattern="<box home>/.ssh/*")` carries no `path` key, so a check
    reading only file_path/path saw an argument-less call, concluded "defaults
    to cwd", and vetted it. That was a REGRESSION -- before the fast path
    existed the call went to the phone -- and nothing upstream covers it either:
    boundary_deny_reason's _PATH_KEYS omits `pattern`, so no hard deny fires.
    """
    assert _decide("Glob", f"{FAKE_HOME}/.ssh/*", key="pattern") == PROMPT
    assert _decide("Glob", "../../../etc/*", key="pattern") == PROMPT
    assert _decide("Glob", f"{TREE}/memory/*.md", key="pattern") == ALLOW
    assert _decide("Glob", "**/*.md", key="pattern") == ALLOW


def test_greps_glob_key_is_checked_but_its_regex_is_not() -> None:
    """`glob` is a path filter and gets the same treatment as Glob's pattern.
    `pattern` is a REGEX -- resolving it would cost real prompts (a regex
    routinely holds characters no path would) and buy nothing.
    """
    assert vet_file_tool("Grep", {"pattern": "TODO", "glob": "../../../etc/*"},
                         working_tree=TREE)[0] == PROMPT
    assert vet_file_tool("Grep", {"pattern": r"^\s*TODO|foo"},
                         working_tree=TREE)[0] == ALLOW


def test_every_path_argument_is_checked_not_just_the_first() -> None:
    """A contained `path` must not vouch for an escaping `pattern`."""
    assert vet_file_tool("Glob", {"path": f"{TREE}/memory", "pattern": "/etc/*"},
                         working_tree=TREE)[0] == PROMPT


# --- overlap with the hard-deny set ----------------------------------------

def test_a_dotenv_inside_the_tree_is_not_vetted() -> None:
    """"Inside the tree" and "safe to read" are different sets: gate #2 found a
    real `.env` in the working dir, which SECRET_GLOBS matches. The hook denies
    it before reaching here, so this asserts the belt rather than the braces --
    but it is the one overlap where an ordering slip would hand over a secret.
    """
    assert _decide("Read", f"{TREE}/.env") == PROMPT
    assert _decide("Read", f"{TREE}/memory/.env.local") == PROMPT


def test_a_symlink_to_an_in_tree_dotenv_is_not_vetted(tmp_path) -> None:
    """/code-review 2026-08-06, third pass -- and the sharpest of the three.

    is_secret_path() is not internally consistent about symlinks: SECRET_PATHS
    is matched with normpath+realpath, SECRET_GLOBS ('**/.env') is a plain
    fnmatch on the string. Containment DOES resolve symlinks, and a link whose
    target is inside the tree passes it correctly -- so a link at
    memory/notes.md pointing to ../.env satisfied every check and was vetted,
    where before this branch it went to the phone.

    Not hypothetical: gate #2 found a real .env in the working dir, and the
    credential in it was a live Discord bot token.
    """
    tree = tmp_path / "tree"
    (tree / "memory").mkdir(parents=True)
    (tree / ".env").write_text("DISCORD_TOKEN=live\n")
    (tree / "memory" / "notes.md").symlink_to("../.env")

    assert _decide("Read", "memory/notes.md", tree=str(tree)) == PROMPT
    assert _decide("Read", str(tree / ".env"), tree=str(tree)) == PROMPT


def test_agent_config_inside_the_tree_is_not_vetted() -> None:
    """`.mcp.json` and `.claude/` live in the working tree by definition, so
    containment alone would vet exactly the files AGENT_CONFIG_PATHS exists to
    protect.

    Built from the live `settings.working_dir` rather than the stand-in
    literal the rest of this file uses. AGENT_CONFIG_PATHS resolves $HOME at
    import, so a hardcoded box path passes on the box and silently asserts
    nothing anywhere else -- the same trap tests/test_bash_policy.py's `_secret`
    helper exists to avoid.
    """
    from server.config import settings

    live_tree = str(settings.working_dir)
    assert _decide("Read", f"{live_tree}/.mcp.json", tree=live_tree) == PROMPT
    assert _decide("Write", f"{live_tree}/.claude/settings.json", tree=live_tree) == PROMPT


# --- attachment root (docs/tasks/ios-image-input.md) -----------------------

ATTACHMENTS = f"{FAKE_HOME}/.marlow/attachments"


def test_read_under_attachment_root_is_vetted_locally() -> None:
    """The read grant an uploaded image needs. Without this branch, every image
    send would wake the phone for a Read that was always going to be approved --
    there is no per-invocation `--allowedTools` escape hatch (probed
    2026-08-11), and a settings.json allow entry alone is inert unless this
    hook independently agrees (the WebSearch/WebFetch lesson)."""
    assert vet_file_tool(
        "Read", {"file_path": f"{ATTACHMENTS}/turn-1/0.jpg"},
        working_tree=TREE, attachment_roots=(ATTACHMENTS,),
    )[0] == ALLOW


def test_attachment_root_grants_read_only_not_write() -> None:
    """Nothing ever writes here except the server process itself, outside the
    CLI's reach -- a model-issued Write/Edit under the attachment root is not a
    legitimate call and must not be vetted on the strength of this branch."""
    for tool in ("Write", "Edit"):
        assert vet_file_tool(
            tool, {"file_path": f"{ATTACHMENTS}/turn-1/0.jpg"},
            working_tree=TREE, attachment_roots=(ATTACHMENTS,),
        )[0] == PROMPT


def test_attachment_root_does_not_widen_to_its_parent_or_siblings() -> None:
    """Containment, not a prefix match on the string -- `<box home>/.marlow`
    (the parent, holding `apns/` and `data/`) and a sibling directory must both
    still prompt."""
    for path in (
        f"{FAKE_HOME}/.marlow",
        f"{FAKE_HOME}/.marlow/apns/AuthKey_X.p8",
        f"{FAKE_HOME}/.marlow/attachments-scratch/x.jpg",
    ):
        assert vet_file_tool(
            "Read", {"file_path": path},
            working_tree=TREE, attachment_roots=(ATTACHMENTS,),
        )[0] == PROMPT


def test_attachment_root_does_not_apply_when_not_configured() -> None:
    """The default parameter is boundaries.ATTACHMENT_ROOT, not a hardcoded
    string -- confirms an unrelated tree's attachment path isn't accidentally
    vetted when the caller passes no override."""
    assert _decide("Read", f"{ATTACHMENTS}/turn-1/0.jpg") == PROMPT


# --- scope -----------------------------------------------------------------

@pytest.mark.parametrize("tool", ["Bash", "WebFetch", "Task", "NotebookEdit", "NotebookRead"])
def test_unrecognised_tools_prompt(tool) -> None:
    """Bash has its own vetting path, and so, now, does WebFetch -- but neither
    is vetted *here*, and a WebFetch carrying a file_path is not a WebFetch this
    module has anything to say about. The notebook tools are on the cron policy's
    `ask` list, where an entry overrides a hook allow (gate #1 §4), so vetting
    them here would be a rule that does nothing where it matters."""
    assert _decide(tool, f"{TREE}/memory/projects.md") == PROMPT


# --- pathless tools --------------------------------------------------------
#
# The same bug as above, in the half of the toolset the fix above didn't cover:
# 10 of the 15 allow entries carried no path, matched no vetter, and reached the
# phone regardless of settings.json (found on the box 2026-08-06).

@pytest.mark.parametrize("tool,tool_input", [
    ("WebSearch", {"query": "alberta politics this week"}),
    ("WebFetch", {"url": "https://example.com/x", "prompt": "summarise"}),
    ("ToolSearch", {"query": "select:WebSearch", "max_results": 5}),
    ("mcp__claude_ai_Google_Calendar__list_events", {"calendar_id": "primary"}),
    ("mcp__claude_ai_Gmail__search_threads", {"q": "is:unread"}),
])
def test_the_allowed_pathless_tools_are_vetted_locally(tool, tool_input) -> None:
    """Each of these is in `permissions.allow` and woke the phone anyway. The
    morning job needs Calendar + Gmail + WebSearch, and unattended there is no
    phone to answer -- every one of its calls burned the 55s window."""
    assert vet_pathless_tool(tool, tool_input)[0] == ALLOW


@pytest.mark.parametrize("tool", [
    "Read", "Edit", "Bash", "Task", "NotebookEdit",
    "mcp__claude_ai_Gmail__create_draft",
    "mcp__claude_ai_Google_Calendar__delete_event",
])
def test_unlisted_pathless_tools_prompt(tool) -> None:
    """Membership IS the rule here, so the connector *write* tools have to be
    absent rather than merely unmentioned: this is what stops an unattended
    inbox scan acting on what it just read."""
    assert vet_pathless_tool(tool, {})[0] == PROMPT


@pytest.mark.parametrize("key", ["file_path", "path", "notebook_path", "cwd", "glob", "pattern"])
def test_a_pathless_tool_carrying_a_path_is_not_vetted(key) -> None:
    """`pathless` is load-bearing, not descriptive: there is no containment
    check in this vetter, so a listed tool that grows a path argument would be
    allowed on its name alone with the path never resolved. It prompts instead,
    and the real fix is to move the tool to vet_file_tool."""
    assert vet_pathless_tool("WebFetch", {key: f"{FAKE_HOME}/.ssh/id_ed25519"})[0] == PROMPT


def test_every_canonical_path_key_defeats_pathless_vetting() -> None:
    """The anti-drift pin. There were three separately-maintained lists of
    path-shaped keys until 2026-08-07, and the hook's had one key the others
    didn't -- a path that gets secret-scanned in one module and is invisible in
    another. Adding a key to `boundaries.PATH_ARG_KEYS` must reach this guard
    without anyone remembering to edit it.
    """
    for key in PATH_ARG_KEYS:
        assert vet_pathless_tool("WebFetch", {key: "/anywhere"})[0] == PROMPT, key


def test_webfetchs_url_is_not_treated_as_a_path() -> None:
    """Mike's 2026-08-07 call was to allow WebFetch outright (option 2 of
    three), accepting the egress hole until §9 lands. Counting `url` as
    path-shaped would reinstate the prompt he removed, by the back door."""
    assert vet_pathless_tool("WebFetch", {"url": "https://example.com"})[0] == ALLOW
