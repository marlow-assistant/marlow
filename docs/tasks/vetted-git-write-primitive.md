# Task — `git --output=` is a write that skips the write policy

**Status: FIXED and MERGED 2026-08-28**, after `/code-review medium` and
`/security-review` each found a hole in the fix itself. It was a hard
prerequisite for `schedules-and-reminders.md`, whose approval gate was decorative
until this closed. **Kept in-house deliberately** (Mike, 2026-08-28): the
security-critical half of the pair, built in parallel with the half handed out
(disjoint files), landed first.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## The bug

`bash_policy.py` vets `git log`, `git diff` and `git show` as **reads**
(`boundaries.VETTED_GIT_SUBCOMMANDS`), cleared locally with no phone prompt. All
three accept `--output=<file>`, and `--format=%x..` emits arbitrary bytes, so
`git log --output=<path-in-tree> --format=%x7b...` is an **arbitrary file write by
a verb vetted as read-only, with no human in the loop.** `_option_value` sees the
path (it unwraps `--output=` so the token scan can't miss it), but the only test
applied is *containment*, and a path inside the tree passes as vetted.

That is the asymmetry: `file_policy` governs `Edit`/`Write` tightly (auto-cleared
only under `memory/`, because `SOUL.md`/`USER.md` sit at the root and rewriting
Marlow's character must reach Mike's phone), while `git log --output=SOUL.md`
faces none of that. **Two write paths into one tree under two policies, the looser
reached through a tool vetted for reading.**

It was caught in August as a symptom, not as the class: the 2026-08-08
`/security-review` found this primitive writing `.git/config`, and the fix put
`.git` into `AGENT_CONFIG_PATHS`. That closed the *destination*, not the
*primitive*, which stayed live for every other path in the tree.

## Fix: allowlist the options, don't denylist `--output`

`--output` was **not** added to `DENY_ARG_SHAPES`: git has more than one spelling
(`-o`, `--output-directory` on `format-patch`), and enumerating write options is
the losing shape of the verb denylist that let `git diff --no-index` through on
2026-08-05 and the metacharacter denylist `VETTED_TOKEN_CHARS` replaced the same
day. Instead `boundaries.VETTED_GIT_SUBCOMMAND_OPTS` enumerates the options
accepted after a vetted subcommand (mirroring `VETTED_GIT_GLOBAL_OPTS`), checked by
`bash_policy._vet_git_subcommand_opts`; anything else abandons vetting and goes to
the phone. Consistent with the module's contract: **failing to vet does not deny**,
a gap costs a prompt, not a boundary. Option NAMES are vetted; values stay with the
containment check that runs on every token. The starting set is what Marlow uses
reviewing its own memory history; widen from real denials, not imagination.

**The list's first real denial arrived immediately**, evidence that instruction is
not ceremony: the first draft covered `log`/`diff`/`show` and broke
`git status --short`, because the other seven vetted subcommands' options had not
been thought about. Suite 839 -> 873.

## `/code-review medium`, 2026-08-28: five findings, all fixed

The review caught a **worse bug than the one being fixed**, and the mistake is
reusable. The first version used ONE FLAT SET of options across all vetted
subcommands; a short option means different things to different subcommands, so
an entry chosen thinking of one silently applied to all ten:

- **`-u`**, added for `git status --untracked-files`, is `--set-upstream-to` on
  `git branch`, which **writes `.git/config`**, and no argv path check can see it.
- **`-M`**, added for `git diff --find-renames`, is `--move --force` on `git
  branch`: an unprompted ref rename in the repo `identity_git.py` commits to.
- **`-a`**, added for `git branch --all`, is `--annotate` on `git tag`.

**The tell was visible in behaviour:** `--set-upstream-to=origin/main` prompted
while its own shorthand `-u` sailed through. Keying the allowlist by subcommand
makes that asymmetry impossible, and `test_every_vetted_subcommand_has_an_option_policy`
fails an unconsidered subcommand in the suite instead of production. None were
regressions (with no option vetting every one was already allowed); the harm was
that writing them down made them look **considered**. An accidental gap invites a
second look; a sanctioned one does not.

The other two: **`tag` never belonged in `VETTED_GIT_SUBCOMMANDS`** (`git tag
<name>` writes a ref with no options, so no option allowlist can reach it).
Removed, and because `command_is_git_write` inverts that set, `git tag` in any form
is now a **hard deny** (the cost: `git tag --list` on a repo with no tags). And
**bundled/attached short options** (`git status -sb`, `git log -n5`) prompted on
ordinary reads, the "prompt that is always approved" failure; `_short_option_forms`
splits them and checks each constituent, so a bundle can't launder an unvetted flag
(`git branch -vu` still prompts).

## `/security-review`, 2026-08-28: one HIGH, in the fix

The `--` handling was wrong in the same *shape* as the flat option set: a rule that
sounded true, written from how `--` reads in a shell rather than how git parses
argv. The code stopped scanning at `--`, assuming a pathspec follows. **That holds
only once git has reached its end-of-options marker.** When the preceding
allowlisted option takes a separate-token value, git consumes the `--` as *that
option's value* and RESUMES option parsing. The policy stopped reading; git kept
going (`--since`/`--until`/`--author` on shortlog, `--format`/`--sort` on branch,
`-L` on log and blame). Confirmed on git 2.50.1, both halves (policy ALLOW with
no prompt, git performed the effect):

| command | effect |
|---|---|
| `git shortlog --until -- --output=memory/notes.md --format=%x41 HEAD` | exit 0, **attacker-chosen bytes** written in-tree |
| `git log -L -- --output=SOUL.md` | fatal, but **SOUL.md truncated to 0 bytes**: `--output` opens during parse. A failing exit code is not a mitigation. |
| `git branch --format -- -m main pwned` / `-D main` | ref renamed / deleted |
| `git branch --format -- -u origin/main main` | `[branch "main"]` **written to `.git/config`** |

So the original vulnerability was live in a second spelling, plus writes to the
refs and config `identity_git.py` solely owns. `.git` survived only because
`reaches_protected_path` still ran on the token. **Fix: never break; scan every
dash-prefixed token wherever it sits.** Recognising git's real end-of-options from
argv alone needs a table of which options take separate values, the enumeration
problem this module has lost twice. Cost: a pathspec literally named `--stat` is
one prompt.

Raised and **dropped**: `git branch <name>` creates a ref with no options, so no
option allowlist can reach it, the property that got `tag` removed. Real but
**pre-existing** (`branch` was vetted on `main` with no option checking, and this
change moves it strictly safer: `-m`, `-d`, `-u`, `-f` all went allow → prompt).
Removing `branch` would make `git branch` a hard deny, a behavioural call.
**Open follow-up: decide whether `branch` follows `tag` out of
`VETTED_GIT_SUBCOMMANDS`.**
