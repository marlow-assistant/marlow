"""Static vetting of Bash command strings (the logic half of boundaries.py).

**This module is not the secret boundary.** It was, for one day. Three
/security-review passes over 2026-08-05 each found a real bypass of the pass
before it -- `git diff --no-index`, then brace expansion, then `expanduser()`
raising on `~nosuchuser` -- and the pattern was the point rather than any
individual hole: deciding what a shell will do by reading the string first is
structurally lossy, and each fix bought a boundary that held until someone
looked again.

The boundary moved into the kernel instead. invoke.py spawns `claude -p` inside
a bubblewrap namespace where the secret paths are tmpfs and simply do not exist
(probe CONFIRMED 2026-08-06, scripts/probe-bwrap-confinement.sh). A miss here
now costs a prompt, not a key.

So read the two jobs below as latency management with a safety net, not as the
last line:

  command_touches_secret_path() -- a HARD deny, refused even if Mike would
      approve it. Defense in depth: it turns a read that bwrap would answer with
      a confusing "no such file" into an explicit refusal with a reason, and it
      is the only thing standing behind credentials_path, which stays visible in
      the namespace because the CLI authenticates from it.

  vet_bash_command() -- ALLOW or PROMPT, never deny. Decides whether the hook
      can clear a command locally instead of waking the phone. Anything it does
      not recognise falls through to the prompt, so a gap here costs latency.

The vetting rule is containment, not verb-spotting: every path-shaped argument
must resolve inside the working tree. That is what kills `--no-index` without
knowing anything about git, and what will kill the next verb nobody thought of.

Containment only holds if the token the shell executes is the token this module
read, so vetting is refused for anything whose characters are not on an explicit
allowlist. Both halves are allowlists on purpose: this module has now been
caught twice by a denylist that omitted a case (the file-reading verbs that
omitted `git`, and the metacharacter set that omitted brace expansion).

Keep both functions strict anyway. Confinement is a *flag*
(MARLOW_BWRAP_ENABLED) -- on by default since 2026-08-06, but a box that opts
out, or one without the AppArmor profile, still rests on this module alone.
"""
from __future__ import annotations

import os
import re
import shlex
import unicodedata
from pathlib import Path

from server.boundaries import (
    CONTENT_VERB_VALUE_OPTS,
    CONTENT_VERBS,
    NL_BODY_STYLE_OPT,
    PATTERN_EXTRA_CHARS,
    PIPE_SINK_COMMANDS,
    SINK_BARE_ARGUMENTS,
    SINK_VALUE_OPTS,
    SED_LINE_RANGE_SCRIPT,
    VETTED_SINK_OPTS,
    UNVETTABLE_RAW_CHARS,
    VETTED_COMMANDS,
    VETTED_CONTENT_VERB_OPTS,
    VETTED_GIT_GLOBAL_OPTS,
    VETTED_GIT_SUBCOMMAND_OPTS,
    VETTED_GIT_SUBCOMMANDS,
    VETTED_TOKEN_CHARS,
    WORKING_TREE,
    is_agent_config_path,
    is_harness_only_path,
    is_secret_path,
)

ALLOW = "allow"
PROMPT = "prompt"


def _option_value(token: str) -> str:
    """`--output=/etc/passwd` hides a path from a naive token scan."""
    if token.startswith("-") and "=" in token:
        return token.split("=", 1)[1]
    return token


def _tokenize(command: str) -> list[str] | None:
    """None when the command cannot be parsed (unbalanced quotes)."""
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _scan_tokens(command: str) -> list[str]:
    """Best-effort tokens for the hard-deny scan, which must work even on
    commands too gnarly to vet. Falls back to whitespace splitting so an
    unparseable command is still checked rather than waved through."""
    tokens = _tokenize(command)
    return tokens if tokens is not None else command.split()


def _containment_candidates(value: str) -> list[str]:
    """The token itself, plus each colon-separated segment.

    git's `<rev>:<path>` syntax means a token is not always one path. Joining
    `HEAD:../../.ssh/config` to the tree normalises to a path *inside* it,
    because `HEAD:..` is a single component that `..` never cancels -- so the
    whole-token check alone calls it contained. git would reject that path for
    leaving the repo, but the boundary should not be resting on git's argument
    parsing.
    """
    return [value, *value.split(":")] if ":" in value else [value]


def inside_working_tree(value: str, working_tree: str | os.PathLike[str]) -> bool:
    """Every token is resolved, not just the ones that look like paths.

    Public rather than private because server/file_policy.py vets the
    path-taking tools against the same rule. Deliberately NOT duplicated there:
    the two comparisons below and the reason each is paired with a root computed
    the same way are the hard-won part, and a second copy would drift.

    An earlier version only checked tokens that were absolute or contained
    `..`, reasoning that anything else could not climb out of the tree. A
    symlink can: `wc -l escape/secret`, where `escape` points at ~/.ssh, has no
    `..` and no leading slash. Resolving everything is both simpler and
    stricter, and it costs nothing in false prompts -- a non-path token like
    `origin/main` or `HEAD` resolves to a non-existent entry *inside* the tree
    and passes.
    """
    tree = os.path.expanduser(str(working_tree))
    try:
        expanded = Path(value).expanduser()
    except RuntimeError:
        # `~nosuchuser/x` -- see boundaries.is_secret_path. Unresolvable means
        # uncontainable, so it is not vetted and goes to the phone.
        return False
    if not expanded.is_absolute():
        expanded = Path(tree) / expanded

    # Two comparisons, each against a root computed the SAME way. Pairing a
    # normalised candidate with a realpath'd root silently fails everything on
    # macOS, where /home is an autofs mount and realpath rewrites it to
    # /System/Volumes/Data/home. Both must hold: normpath alone is fooled by a
    # symlink inside the tree pointing at ~/.ssh, and realpath alone would let a
    # path through if the tree itself is reached via a symlink.
    pairs = (
        (os.path.normpath(str(expanded)), os.path.normpath(tree)),
        (os.path.realpath(str(expanded)), os.path.realpath(tree)),
    )
    return all(
        candidate == root or candidate.startswith(root + os.sep)
        for candidate, root in pairs
    )


def anchor_to_tree(value: str, working_tree: str | os.PathLike[str]) -> str | None:
    """Absolute form of an argument, anchored to the tree if it is relative.

    None when it cannot be resolved (`~nosuchuser/x`), which callers must treat
    as uncontainable rather than as a pass.

    The single place this repo resolves a tool argument, shared with
    server/file_policy.py. `docs/tasks/resolve-paths-once.md` catalogues seven
    findings that all came from two checks resolving the same string
    differently; the seventh (/security-review 2026-08-06) was the one that made
    this function exist, and consolidating here is the cheap half of the fix
    that doc describes.
    """
    tree = os.path.expanduser(str(working_tree))
    try:
        expanded = Path(value).expanduser()
    except RuntimeError:
        return None
    return str(expanded if expanded.is_absolute() else Path(tree) / expanded)


def reaches_protected_path(value: str, working_tree: str | os.PathLike[str]) -> bool:
    """Does an argument reach a secret or the agent's own config, once anchored?

    `is_secret_path` alone answers this wrongly for a RELATIVE argument, and the
    working tree is full of them because the CLI runs with cwd=working_dir:

      - its SECRET_GLOBS half is an fnmatch on the string, and `**/.env` needs a
        separator, so a bare `.env` does not match;
      - its SECRET_PATHS half realpaths, but an unanchored relative path
        realpaths against the HOOK PROCESS's cwd (/home/marlow/marlow-server),
        which is not the tree the tool will act in.

    Both spellings are checked, because is_secret_path resolves symlinks for one
    half of its own tuple and not the other (see file_policy's note on the
    in-tree `.env` link).
    """
    anchored = anchor_to_tree(value, working_tree)
    if anchored is None:
        return True  # unresolvable -- treat as reaching something, i.e. not vetted
    return any(
        is_secret_path(form) or is_agent_config_path(form) or is_harness_only_path(form)
        for form in (anchored, os.path.realpath(anchored))
    )


def command_touches_secret_path(command: str) -> bool:
    return any(is_secret_path(_option_value(token)) for token in _scan_tokens(command))


def command_touches_harness_only_path(command: str) -> bool:
    """Same token scan again, against the paths only the harness may touch.

    Defence in depth behind the bwrap tmpfs, not the primary boundary: with
    confinement ON the cron spool does not exist for the CLI at all. This is
    what stands when `MARLOW_BWRAP_ENABLED` is off (dev boxes, and the CI
    suite), where the unit's `SupplementaryGroups=crontab` would otherwise be
    the only thing between the agent and a persistent unsandboxed cron line.
    """
    return any(is_harness_only_path(_option_value(token)) for token in _scan_tokens(command))


def command_touches_agent_config(command: str) -> bool:
    """Same token scan as the secret check, against the harness's own config.

    Separate function rather than an extra path in SECRET_PATHS: these files
    must stay VISIBLE to the CLI (it reads all of them at startup), and
    SECRET_PATHS is what invoke.py hides behind a tmpfs. See the comment on
    boundaries.AGENT_CONFIG_PATHS.
    """
    return any(is_agent_config_path(_option_value(token)) for token in _scan_tokens(command))


# git global options that consume the NEXT token as their value. shlex keeps
# `-C dir` as two tokens, so a scan that strips only the flag reads `dir` as the
# subcommand. `--opt=value` needs no entry — it is a single token.
_GIT_GLOBAL_OPTS_WITH_VALUE: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)

# Tokens after which the next token is a COMMAND rather than an argument. An
# allowlist, so an unrecognised predecessor means "argument" — the direction that
# costs a prompt instead of a bad hard deny. shlex does not split on shell
# punctuation, so `ls; git commit` yields `ls;`: the suffix test below is what
# catches that, and it is why this cannot be a plain set membership check.
_COMMAND_BOUNDARY_TOKENS: frozenset[str] = frozenset(
    # shell keywords, then the wrappers that run another command as their
    # argument -- `env git commit` and `xargs git commit` must still be seen.
    {"then", "do", "else", "elif", "!", "env", "nohup", "time", "timeout", "nice",
     "xargs", "sudo", "command", "exec", "eval", "sh", "bash", "zsh"}
)
_COMMAND_BOUNDARY_SUFFIXES = ("&", "|", ";", "(", ")", "{", "}")


def _is_command_boundary(previous: str) -> bool:
    return previous in _COMMAND_BOUNDARY_TOKENS or previous.endswith(
        _COMMAND_BOUNDARY_SUFFIXES
    )


def command_is_git_write(command: str) -> bool:
    """True for a git invocation that is not a read — a HARD deny.

    Git in the identity dir is harness-owned (`server/identity_git.py`), not
    Marlow's discretion: the server commits every turn deterministically. So a
    model-issued `git commit`/`add`/`reset`/`push` is not merely unnecessary, it
    interleaves a second author into a history the harness is supposed to own.
    Read subcommands stay approvable *and* locally vetted — reviewing the history
    of its own memory is a real thing a persistent-memory assistant does.

    **An allowlist inversion, deliberately not a list of write verbs.** The
    START doc suggested extending `boundaries.DENY_ARG_SHAPES`, which is the
    right instinct (reuse the existing mechanism) but the wrong shape here: this
    module's own history is two denylists that each omitted a case, and a
    hand-written set of write verbs omits `stash`, `apply`, `cherry-pick`,
    `worktree`, `filter-branch`, and whatever git adds next. Inverting
    `VETTED_GIT_SUBCOMMANDS` means a new git subcommand is denied until someone
    decides it is a read, which is the direction the mistake should fall.
    `DENY_ARG_SHAPES` keeps its `git push --force` entry as defense in depth.

    Costs: `git help <x>` is now denied rather than prompted. Accepted — the
    property is worth a trivial loss, and Mike can run git himself over SSH.

    **Only `git` in COMMAND position counts.** Scanning every `git` token, as the
    first version did, made `grep -n git memory/notes.md` and `echo git things`
    into HARD denies — unapprovable, since a deny never reaches the phone, and
    grepping its own memory for the word "git" is an ordinary thing for Marlow to
    do (/code-review 2026-08-08). Nothing is lost on the path that matters: the
    only way a git write reaches *auto-approval* is `vet_bash_command` clearing
    it, and that requires `git` as token 0 (`VETTED_COMMANDS` gates the verb
    slot). A `git` token anywhere else means the verb is something unvetted —
    `env`, `xargs`, `sh` — which falls through to the phone regardless. So this
    trades a hard deny for a prompt in the odd cases, never for a silent allow.
    Basename-matched so `/usr/bin/git commit` cannot walk around it.
    """
    tokens = _scan_tokens(command)
    for index, token in enumerate(tokens):
        if os.path.basename(token) != "git":
            continue
        if index and not _is_command_boundary(tokens[index - 1]):
            continue  # an argument that happens to be the word `git`
        rest = tokens[index + 1 :]
        while rest and rest[0].startswith("-"):
            # An option that takes a SEPARATE value consumes it, or the value
            # lands in the subcommand slot and every `git -C <dir> log` — a read —
            # gets hard-denied (/code-review 2026-08-08). Unknown options are
            # assumed value-less: if that guess is wrong the value is not a
            # vetted subcommand and this denies, which is the safe direction.
            takes_value = rest[0] in _GIT_GLOBAL_OPTS_WITH_VALUE
            rest = rest[2:] if takes_value and len(rest) > 1 else rest[1:]
        if rest and rest[0] not in VETTED_GIT_SUBCOMMANDS:
            return True
    return False


def _vet_git(rest: list[str]) -> str | None:
    """None if the git invocation is acceptable, else the reason it isn't."""
    while rest and rest[0].startswith("-"):
        if rest[0] not in VETTED_GIT_GLOBAL_OPTS:
            return f"unvetted git global option: {rest[0]}"
        rest = rest[1:]
    if not rest:
        return "git with no subcommand"
    if rest[0] not in VETTED_GIT_SUBCOMMANDS:
        return f"git subcommand not on the allowlist: {rest[0]}"
    return _vet_git_subcommand_opts(rest[0], rest[1:])


def _short_option_forms(name: str) -> list[str]:
    """The individual short flags a single-dash token stands for.

    git accepts bundles (`git status -sb`) and attached values (`git log -n5`,
    `git diff -U3`), and matching the token whole missed both -- ordinary
    read-only usage that fell through to a phone prompt (/code-review medium).
    A prompt that is always approved is the failure mode this allowlist exists
    to avoid, so the common spellings have to land.

    Returns [name] unchanged for anything not recognised as a bundle or an
    attached value, which then fails the allowlist and PROMPTs -- the safe
    direction. Each constituent is checked separately by the caller, so a bundle
    can never clear a flag that is not itself vetted.
    """
    body = name[1:]
    if name.startswith("--") or not body:
        return [name]
    if body.isalpha() and len(body) > 1:
        return [f"-{char}" for char in body]  # `-sb` -> `-s`, `-b`
    if len(body) > 1 and body[0].isalpha() and body[1:].isdigit():
        return [f"-{body[0]}"]                # `-n5` -> `-n`; the value is a count
    return [name]


def _vet_git_subcommand_opts(subcommand: str, rest: list[str]) -> str | None:
    """None if every option after the subcommand is vetted, else the reason.

    Exists because a vetted READ verb was also a write primitive:
    `git log --output=<path-in-tree> --format=%x..` wrote arbitrary bytes and
    was auto-approved, since containment was the only test and the path was
    inside the tree (docs/tasks/vetted-git-write-primitive.md, 2026-08-28).

    Options are looked up UNDER THE SUBCOMMAND, never in a flat set: `-u` is
    `--untracked-files` to `status` and `--set-upstream-to` to `branch`, and the
    first version of this conflated them into an unprompted `.git/config` write.
    See the comment on VETTED_GIT_SUBCOMMAND_OPTS.

    Non-option tokens are left alone -- they are revs and pathspecs, and
    vet_bash_command containment-checks every one of them afterwards.
    """
    allowed = VETTED_GIT_SUBCOMMAND_OPTS.get(subcommand)
    if allowed is None:
        # A subcommand vetted as a read with no option policy written for it.
        # Fail closed: the caller turns this into a prompt, not an allow.
        return f"no option policy for git subcommand: {subcommand}"
    for token in rest:
        if not token.startswith("-") or token == "--":
            # A `--` is SKIPPED, never a stopping point. The first version broke
            # here, on the assumption that everything after `--` is a pathspec.
            # It is not: git consumes a separate-token `--` as the VALUE of a
            # preceding value-taking option and then RESUMES option parsing, so
            # the policy stopped reading while git kept going. All confirmed on
            # git 2.50.1 (/security-review 2026-08-28), each ALLOW before this
            # change and PROMPT without the `--`:
            #   git shortlog --until -- --output=memory/notes.md --format=%x41 HEAD
            #       -> exit 0, arbitrary attacker-chosen bytes written in-tree
            #   git log -L -- --output=SOUL.md
            #       -> fatal, but SOUL.md truncated to 0 bytes; --output is
            #          opened during parse, before -L's value is validated
            #   git branch --format -- -m main pwned      -> ref renamed
            #   git branch --format -- -D main            -> ref deleted
            #   git branch --format -- -u origin/main main
            #       -> `[branch "main"]` written to .git/config, the file
            #          AGENT_CONFIG_PATHS exists to keep unwritable
            # Recognising git's real end-of-options from argv alone needs a table
            # of which options take separate values -- the enumeration problem
            # this module has already lost twice. So scan EVERY dash-prefixed
            # token wherever it sits. The cost is a pathspec literally named
            # `--stat`, which is one prompt.
            continue
        # `--format=%h` and `--since=2026-08-01` are one token; only the name is
        # the option. The VALUE is left to containment, same as any other token.
        name = token.split("=", 1)[0]
        # `git log -5` is a count, not an option. Bounded to digits so it cannot
        # become a general escape.
        if len(name) > 1 and name[0] == "-" and name[1:].isdigit():
            continue
        for form in _short_option_forms(name):
            if form not in allowed:
                return f"unvetted git option for {subcommand}: {form}"
    return None


# --- Shape decomposition -----------------------------------------------------
#
# Everything below runs BEFORE the token-character allowlist, and each function
# either removes a construct entirely or refuses to vet. That ordering is the
# safety property: `|`, `;`, `&&` and `2>` are gone from the segments the vetter
# finally reads, so VETTED_TOKEN_CHARS still refuses every OTHER metacharacter
# exactly as it did before. Nothing here widens what a segment may contain.

# `$(...)`, backticks and `${...}` are REFUSED, never parsed. Their whole point
# is that the token the shell runs is not the token this module read, which is
# the assumption containment rests on. `cat $(ls)` names no path to check.
_SUBSTITUTION_MARKERS: tuple[str, ...] = ("$(", "`", "${", "$[")

# `2>/dev/null` and `2>&1` only. Every other redirect -- `>`, `>>`, `<`, `&>`,
# `1>` -- survives into the segment and dies on the character allowlist, which
# is correct: those are write primitives or input redirection, and
# `git log --output=` is the precedent for taking a vetted read verb's ability
# to write seriously (docs/tasks/vetted-git-write-primitive.md).
_STDERR_REDIRECT = re.compile(r"\s*2>\s*(?:/dev/null|&1)(?=\s|$)")

_SEQUENCING_OPERATORS: tuple[str, ...] = ("&&", "||")


def _split_unquoted(text: str, operators: tuple[str, ...]) -> list[str] | None:
    """Split `text` on any of `operators` that appear OUTSIDE quotes.

    None when quoting is unbalanced. Quote-aware by hand rather than via shlex,
    because shlex discards the quoting and both callers need to know that the
    `;` in `grep "a;b" f.md` and the `|` in `grep "a|b" f.md` are DATA. Splitting
    naively on `|` cost real coverage before this was shared: grep alternation
    patterns are everywhere in the corpus, and every one of them looked like a
    pipeline.

    A single `&` is deliberately NOT an operator: it backgrounds a command,
    which would outlive the vetting round-trip. It stays in the segment and dies
    on the character allowlist.
    """
    segments: list[str] = []
    current = ""
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            current += char
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            current += char
        else:
            match = next(
                (op for op in operators if text.startswith(op, index)), None
            )
            if match is None:
                current += char
            else:
                segments.append(current)
                current = ""
                index += len(match) - 1
        index += 1
    if quote is not None:
        return None  # unbalanced -- _tokenize would refuse it too
    segments.append(current)
    return [segment.strip() for segment in segments if segment.strip()]


def _split_on_operators(command: str) -> list[str] | None:
    """Split on `;`, `&&`, `||` outside quotes. None if it cannot be trusted."""
    return _split_unquoted(command, ("&&", "||", ";"))


def _strip_pipe_sinks(segment: str) -> str | None:
    """Drop trailing `| sink` stages. None if any stage is not a bare sink.

    A sink truncates a stream and opens nothing, so it adds no read to vet --
    but only while it names no path. `wc -l` is a sink; `wc -l secrets.txt`
    reads a second file and is not. Options are allowed (`head -20`), bare
    arguments are not, and that single test is what keeps this from becoming a
    second, weaker copy of the containment rule.
    """
    stages = _split_unquoted(segment, ("|",))
    if stages is None:
        return None
    if len(stages) == 1:
        return segment
    for stage in stages[1:]:
        tokens = _tokenize(stage)
        # `| sed -n '1,80p'` -- the line-range print the content-verb path
        # already vets, as a pipe tail. Matched whole rather than run through
        # the sink loop below: sed's one bare token is its SCRIPT, the dangerous
        # half (boundaries.SED_LINE_RANGE_SCRIPT), so it is never a budgeted
        # "pattern" and nothing else may ride along with it.
        if tokens and tokens[0] == "sed":
            if len(tokens) == 3 and tokens[1] == "-n" and SED_LINE_RANGE_SCRIPT.fullmatch(tokens[2]):
                continue
            return None
        if not tokens or tokens[0] not in PIPE_SINK_COMMANDS:
            return None
        allowed = VETTED_SINK_OPTS.get(tokens[0], frozenset())
        # Bare tokens a sink may carry. Zero for all but grep, whose one bare
        # token is its PATTERN rather than a path -- see
        # boundaries.SINK_BARE_ARGUMENTS.
        bare_budget = SINK_BARE_ARGUMENTS.get(tokens[0], 0)
        arguments = tokens[1:]
        index = 0
        while index < len(arguments):
            token = arguments[index]
            index += 1
            # `nl -ba` carries its value ATTACHED, so it must be settled BEFORE
            # the fold below -- which reads `-ba` as the value-taking `-b` and
            # would swallow a following digit. `| nl -ba 5` then cleared while
            # nl read a FILE named 5: the 2026-09-11 hole in a new shape
            # (/security-review 2026-10-02, caught before merge).
            if tokens[0] == "nl" and NL_BODY_STYLE_OPT.fullmatch(token):
                continue
            # `head -n 20` -- an option's value in a separate token. Consumed
            # only when it is ALL DIGITS, which no path can be, so this cannot
            # become a way to hide `sort -o secrets`.
            #
            # AND only for an option that actually TAKES a value. Folding for
            # any dash token let a boolean absorb the digit that should have
            # spent grep's bare-token budget, so the FILE spent it instead:
            # `| grep -v 999999 /etc/passwd` reached ALLOW
            # (/security-review 2026-09-11). See boundaries.SINK_VALUE_OPTS.
            if (
                token.startswith("-")
                and index < len(arguments)
                and arguments[index].isdigit()
                and not token[1:].isdigit()
                and any(
                    form in SINK_VALUE_OPTS.get(tokens[0], frozenset())
                    for form in _short_option_forms(token.split("=", 1)[0])
                )
            ):
                arguments[index] = "-" + arguments[index]
            # A bare argument is a second READ (`wc -l secrets.txt`) and is what
            # separates a sink from an ordinary command -- EXCEPT for the one
            # grep is allowed, which is a regex and never reaches the
            # filesystem. Spend the budget before refusing, so the SECOND bare
            # token (`| grep pat secrets.txt`) still fails here.
            if not token.startswith("-"):
                if bare_budget <= 0:
                    return None
                bare_budget -= 1
                # Defence in depth, NOT the primary control -- SINK_VALUE_OPTS
                # is. A pattern rarely needs a slash and a path almost always
                # has one, so refusing it here is a cheap backstop against the
                # next value-consuming option nobody thought to table. Costs
                # nothing measured: no `| grep` in the 191-command corpus
                # carries one.
                if "/" in token:
                    return None
                # Widened by the same characters as the content-verb path, not
                # exempted: alternations are the common shape and `;`/`'` must
                # still fail. See boundaries.PATTERN_EXTRA_CHARS for the command
                # execution the wide version allowed.
                if _pattern_stray_characters(token):
                    return None
                continue
            # And an OPTION can be a WRITE: `sort -o/tmp/pwned` is a sink by
            # verb and an arbitrary file write by argument. Accepting anything
            # dash-prefixed was exactly the `git log --output=` mistake, one
            # verb later (see boundaries.VETTED_SINK_OPTS).
            if "=" in token or _stray_characters(token):
                return None
            if token[1:].isdigit():
                continue  # `head -20`
            if any(form not in allowed for form in _short_option_forms(token)):
                return None
    return stages[0].strip()


# A backslash or `$` immediately before a quote changes where a quoted region
# STARTS OR ENDS, and _split_unquoted implements neither escaping nor ANSI-C
# quoting. That divergence is not cosmetic: `\'` is a literal quote to the
# shell, so `grep -A \'1;id \' f.md` runs `id` while the scanner sees one quoted
# segment with no operator in it (/security-review 2026-09-06, self-review
# before merge -- it was auto-approved). Refused rather than parsed. Teaching
# the scanner to escape correctly is the analysis that failed four times across
# three review passes on this module; refusing the construct is cheap and no
# command in the replay corpus contains one.
_QUOTE_DESYNC_ESCAPES: tuple[str, ...] = ("\\'", '\\"', "$'", '$"')


def _decompose(command: str) -> tuple[list[str] | None, str]:
    """(segments, "") or (None, reason). Applies the shape allowances."""
    if any(marker in command for marker in _SUBSTITUTION_MARKERS):
        return None, "command substitution -- the shell would run something not vetted here"
    if any(escape in command for escape in _QUOTE_DESYNC_ESCAPES):
        return None, "escaped quote -- where the quoting ends cannot be decided statically"
    segments = _split_on_operators(command)
    if segments is None:
        return None, "unbalanced quoting -- cannot vet statically"
    if not segments:
        return None, "empty command"
    resolved: list[str] = []
    for segment in segments:
        # AFTER the split, not before. `cat notes.md 2>/dev/null; ls` ends the
        # redirect at a `;` rather than at whitespace, and a pattern anchored on
        # whitespace left it in place -- six corpus commands prompted for a
        # construct this was written to allow.
        segment = _STDERR_REDIRECT.sub("", segment).strip()
        without_sinks = _strip_pipe_sinks(segment)
        if without_sinks is None:
            return None, f"pipeline stage is not a bare output sink: {segment}"
        resolved.append(without_sinks)
    return resolved, ""


# --- Content verbs -----------------------------------------------------------


def _stray_characters(token: str, extra: frozenset[str] = frozenset()) -> str | None:
    """The reason a token fails the character allowlist, or None.

    `extra` widens the allowlist for ONE caller: grep's pattern, by the closed
    set in PATTERN_EXTRA_CHARS (and letters, via _pattern_stray_characters).
    It is never a way to skip the check -- see boundaries.PATTERN_EXTRA_CHARS
    for the command execution that a wider exemption allowed.
    """
    stray = set(token) - VETTED_TOKEN_CHARS - extra
    return f"token contains unvettable characters {sorted(stray)}: {token}" if stray else None


def _pattern_stray_characters(token: str) -> str | None:
    """_stray_characters for a grep PATTERN: PATTERN_EXTRA_CHARS, plus letters
    outside ASCII.

    Marlow searches its own notes in the language they are written in -- three
    greps for `Percé` and `Gaspé` prompted during one trip (2026-10-02 replay).
    Every character the shell treats specially is ASCII, so a letter above 127
    can neither quote, split, expand nor substitute. LETTERS only (Unicode
    category L*), not "anything non-ASCII": a fullwidth `；` or a combining mark
    is not a letter and still prompts, which keeps this a statement about what
    the character IS rather than a guess about what bash ignores.
    """
    letters = {
        char for char in token
        if ord(char) > 127 and unicodedata.category(char).startswith("L")
    }
    return _stray_characters(token, PATTERN_EXTRA_CHARS | letters)


def _content_verb_targets(verb: str, rest: list[str]) -> tuple[list[str] | None, str]:
    """The paths a content verb will READ, or (None, reason) if the argument list
    cannot be read confidently.

    This function applies the character allowlist ITSELF, per token, rather than
    leaving it to the caller -- because of the one deliberate exemption below.

    Options are looked up under the verb (boundaries.VETTED_CONTENT_VERB_OPTS)
    and anything unrecognised refuses the whole command, so a flag nobody has
    considered costs a prompt rather than a guess. grep's first non-option
    argument is its PATTERN, not a path; getting that backwards made an early
    replay under-count by a third, since every `grep -n "Cabot Trail" travel.md`
    scored as naming a file called `"Cabot`.

    **THE PATTERN TOKEN IS EXEMPT FROM VETTED_TOKEN_CHARS, and it is the only
    exemption in this module.** 13 of the corpus's greps are alternations --
    `grep -n -i "physio\\|9pm\\|cron" health.md` -- and `\\` and `|` are not on
    the character allowlist, so the most common shape of the exact command this
    rule was written for still prompted. The exemption is safe for a reason
    specific to this one token: the allowlist exists so that the token the SHELL
    executes is the token this module read (see VETTED_TOKEN_CHARS), which
    matters because the token is then resolved as a path. A pattern is never
    resolved as a path -- it is handed to grep as a regex and never reaches the
    filesystem. Three things still hold over it: `_decompose` has already
    refused command substitution anywhere in the command, `_split_unquoted` has
    already split on unquoted operators (and errs toward OVER-splitting, which
    costs a prompt rather than an allow), and the value is not counted as a
    target, so it cannot become a file that gets read.
    """
    allowed = VETTED_CONTENT_VERB_OPTS.get(verb)
    if allowed is None:
        return None, f"no option policy for content verb: {verb}"
    value_opts = CONTENT_VERB_VALUE_OPTS.get(verb, {})
    targets: list[str] = []
    pattern_seen = verb != "grep"
    # sed's leading bare token is its SCRIPT, and the script is where sed's
    # write and exec primitives live (boundaries.SED_LINE_RANGE_SCRIPT). Unlike
    # grep's pattern it is not merely "not a path" -- it is the dangerous half,
    # so it is matched against a closed regex instead of a character allowlist.
    script_seen = verb != "sed"
    index = 0
    while index < len(rest):
        token = rest[index]
        index += 1
        if token.startswith("-") and token != "-" and token != "--":
            problem = _stray_characters(token)
            if problem:
                return None, problem
            # `head -20` -- a count written as an option. Bounded to digits so
            # it cannot become a general escape.
            if token[1:].isdigit():
                continue
            if verb == "nl" and NL_BODY_STYLE_OPT.fullmatch(token):
                continue
            name = token.split("=", 1)[0]
            for form in _short_option_forms(name):
                if form not in allowed:
                    return None, f"unvetted option for {verb}: {form}"
            # `--include=*.md` and `-A5` carry their value in the same token, so
            # the NEXT token is an argument and must not be consumed.
            forms = _short_option_forms(name)
            if "=" in token or (forms != [name] and len(forms) == 1):
                continue
            if forms != [name]:
                # A BUNDLE. getopt gives the next token to the bundle's LAST
                # flag if that flag takes a value, and gives an earlier
                # value-taking flag the REST OF THE BUNDLE instead. Skipping
                # bundles outright -- the old rule -- left `-nf`'s value to be
                # read as grep's pattern, so `grep -nf /etc/passwd notes.md`
                # and `grep -vf .env notes.md` cleared with the -f file never
                # containment-checked (/code-review high, 2026-10-02).
                if any(value_opts.get(form) for form in forms[:-1]):
                    return None, f"value-taking option inside a bundle: {token}"
                name = forms[-1]
            kind = value_opts.get(name)
            if kind is None:
                continue  # a boolean flag
            if index >= len(rest):
                return None, f"{name} with no value"
            value = rest[index]
            index += 1
            # EVERY value is character-checked, whatever its kind. A value is
            # not a path to this module, but it is still a WORD to the shell:
            # `grep -v -e ../outside/* notes.md` expands the glob into extra
            # FILE operands, and `-v` prints them -- an out-of-tree read and a
            # `.env` read (`-e .[ec]*`), both auto-approved. Every value kind
            # had it: `-A`, `head -n`, `nl -s`. Found by an adversarial
            # differential-execution pass, 2026-10-02; it predates the pattern
            # widening and was live. The pattern keeps its widened set.
            problem = (
                _pattern_stray_characters(value) if kind == "pattern"
                else _stray_characters(value)
            )
            if problem:
                return None, problem
            if kind == "path":
                targets.append(value)
            elif kind == "pattern":
                # `grep -e pat file.md` -- the pattern arrived by option, so the
                # first bare argument is a FILE. Failing to record that left the
                # command with no targets and prompted it.
                pattern_seen = True
            continue
        if not script_seen:
            # Deliberately ahead of the `token == "--"` branch below, which
            # means `sed -n -- '1,5p' f` consumes `--` as the script and
            # prompts. Over-refusal, and the same thing grep already does with
            # `--` as its pattern -- moving the `--` branch above both would
            # change grep's vetting too, which is not this change's business.
            if not SED_LINE_RANGE_SCRIPT.fullmatch(token):
                return None, f"sed script is not a bare line-range print: {token}"
            script_seen = True
            continue
        if not pattern_seen:
            # grep's regex. Widened by a closed set, not exempted -- see the
            # docstring and boundaries.PATTERN_EXTRA_CHARS.
            problem = _pattern_stray_characters(token)
            if problem:
                return None, problem
            pattern_seen = True
            continue
        if token == "--":
            # Unlike git (see _vet_git_subcommand_opts), `--` really does end
            # option parsing for these verbs. Nothing after it is exempted from
            # the target checks below.
            continue
        problem = _stray_characters(token)
        if problem:
            return None, problem
        targets.append(token)
    if not script_seen:
        # `sed -n file.md` -- sed would read the file as its SCRIPT. Refusing an
        # absent script matters as much as vetting a present one.
        return None, "sed names no script"
    if not targets:
        return None, f"{verb} names no target -- it would read a directory tree or stdin"
    return targets, ""


def _vet_content_verb(
    verb: str, rest: list[str], working_tree: str | os.PathLike[str]
) -> str | None:
    """None if this content verb may be cleared locally, else the reason it may not."""
    targets, problem = _content_verb_targets(verb, rest)
    if targets is None:
        return problem
    for target in targets:
        for value in _containment_candidates(_option_value(target)):
            if not inside_working_tree(value, working_tree):
                return f"argument resolves outside the working tree: {value}"
            if reaches_protected_path(value, working_tree):
                return f"argument reaches a secret or agent-config path: {value}"
        anchored = anchor_to_tree(_option_value(target), working_tree)
        if anchored is None:
            return f"argument cannot be resolved: {target}"
        # The clause containment cannot express, and the reason this rule
        # exists. os.path.isfile follows symlinks and returns False for
        # directories, devices and fifos alike -- and the link's DESTINATION was
        # already containment-checked above via realpath, so a symlink out of
        # the tree has failed before it reaches here.
        if not os.path.isfile(anchored):
            return f"target is not an existing regular file: {target}"
    return None


def _vet_segment(segment: str, working_tree: str | os.PathLike[str]) -> tuple[str, str]:
    """One decomposed segment: allowlisted verb + containment, or a content verb."""
    tokens = _tokenize(segment)
    if tokens is None:
        return PROMPT, "unbalanced quoting -- cannot vet statically"
    if not tokens:
        return PROMPT, "empty command"
    verb, *rest = tokens
    problem = _stray_characters(verb)
    if problem:
        return PROMPT, problem
    if verb in CONTENT_VERBS:
        # Deliberately NOT running the character allowlist over `rest` here:
        # _content_verb_targets applies it per token so that grep's pattern can
        # be exempted. See its docstring for why that one token is safe.
        problem = _vet_content_verb(verb, rest, working_tree)
        if problem:
            return PROMPT, problem
        return ALLOW, "vetted locally: content verb naming regular files inside the working tree"

    for token in rest:
        problem = _stray_characters(token)
        if problem:
            return PROMPT, problem

    if verb not in VETTED_COMMANDS:
        return PROMPT, f"{verb} is not on the vetted-command allowlist"
    if verb == "git":
        problem = _vet_git(rest)
        if problem:
            return PROMPT, problem

    for token in rest:
        for value in _containment_candidates(_option_value(token)):
            if not inside_working_tree(value, working_tree):
                return PROMPT, f"argument resolves outside the working tree: {value}"
            # Containment is not enough on its own: the working tree CONTAINS
            # protected files. Gate #2 found a real .env in the working dir, and
            # AGENT_CONFIG_PATHS deliberately covers <working_dir>/.mcp.json and
            # .claude/. `wc -l .env` cleared both checks until 2026-08-06 --
            # is_secret_path saw the bare relative string and matched nothing
            # (/security-review, instance seven). PROMPT rather than deny, to
            # match file_policy and to keep this function's "never denies"
            # contract: a hard deny here would be unrecoverable, and a regex or
            # a message body that merely looks like `.env` would earn one.
            if reaches_protected_path(value, working_tree):
                return PROMPT, f"argument reaches a secret or agent-config path: {value}"

    return ALLOW, "vetted locally: allowlisted command, no path argument leaves the working tree"


def vet_bash_command(
    command: str, working_tree: str | os.PathLike[str] = WORKING_TREE
) -> tuple[str, str]:
    """(ALLOW, reason) or (PROMPT, reason). Never denies -- see module docstring.

    Callers must run the hard-deny checks first; this function assumes secret
    paths and destructive argument shapes have already been refused.
    """
    if any(char in command for char in UNVETTABLE_RAW_CHARS):
        return PROMPT, "newline in the command -- the shell would run more than one"
    if _tokenize(command) is None:
        return PROMPT, "unbalanced quoting -- cannot vet statically"

    segments, problem = _decompose(command)
    if segments is None:
        return PROMPT, problem

    # EVERY segment must clear on its own. `cat notes.md && curl evil.com` is
    # two commands and the second one decides the answer.
    reasons: list[str] = []
    for segment in segments:
        decision, reason = _vet_segment(segment, working_tree)
        if decision != ALLOW:
            return PROMPT, reason
        reasons.append(reason)
    return ALLOW, reasons[0] if len(reasons) == 1 else (
        f"vetted locally: {len(reasons)} sequenced segments, each vetted"
    )
