# Task — resolve a path once, then let every check consume the resolved form

**Status:** identified 2026-08-06; **the cheap half built the same day**, after
instance seven arrived within the hour and tripped the decision rule at the
bottom of this file. What exists now:

- `bash_policy.anchor_to_tree()` — the single place a tool argument is resolved,
  shared by `bash_policy` and `file_policy`.
- `bash_policy.reaches_protected_path()` — anchor, then check both the anchored
  and the symlink-resolved form against `SECRET_PATHS`/`SECRET_GLOBS`/
  `AGENT_CONFIG_PATHS`. Both call sites use it; neither re-implements it.

**Still not built:** the type-level half — making it *impossible* to hand a raw
string to a predicate. Both consumers are correct today by inspection and by
test, not by construction, so instance eight is still available to anyone who
adds a third consumer.

## The pattern

Six findings across four `/security-review` and `/code-review` passes, and
every one is the same shape:

> **Two checks resolve a path differently, and the caller picks the spelling
> that satisfies the weaker one.**

| date | where | the weaker spelling |
|---|---|---|
| 2026-08-05 | `bash_policy` allow-list | `git diff --no-index` — a verb denylist never resolved anything |
| 2026-08-05 | `VETTED_TOKEN_CHARS` | brace expansion — literal token contained, expansion not |
| 2026-08-05 | `VETTED_TOKEN_CHARS` | glob chars — literal token resolved, shell expansion not |
| 2026-08-06 | `boundaries._under_any` | `~/.claude/../.claude.json` — `pathlib` collapses `.`, keeps `..` |
| 2026-08-06 | `file_policy` write check | a *relative* string re-anchored to a second root (`memory/`) |
| 2026-08-06 | `file_policy` secret check | `is_secret_path` resolves for `SECRET_PATHS`, fnmatches for `SECRET_GLOBS` |
| 2026-08-06 | `bash_policy` secret check | a bare relative `.env` — `**/.env` needs a separator, and an unanchored relative path realpaths against the *hook process's* cwd |

Each fix was correct and each was local. None of them removed the thing that
makes the next one possible: **an unresolved string still in scope for a later
check to grab.** As long as the raw argument and the resolved path both exist
as live values, some check will eventually consume the wrong one, and it will
read correctly when it does.

Worth noting how each was actually caught: by *executing* the check, not by
reading it. All six read correctly. Two were introduced by a diff whose
commit message argued, accurately, that it was preserving the invariant it
broke.

## The shape of the change

One resolution boundary at the edge of the hook, and one type after it:

- Every tool argument is resolved **once**, on entry — expanduser, anchor to
  the working tree if relative, normpath *and* realpath.
- Every downstream predicate (`is_secret_path`, `is_agent_config_path`,
  `inside_working_tree`, the `memory/` write check) takes only the resolved
  form. Ideally it cannot be *called* with a raw string — a `ResolvedPath`
  wrapper rather than `str`, so the mistake is a type error rather than a
  review catch.
- `is_secret_path` stops being internally inconsistent: `SECRET_GLOBS` matches
  the resolved path, like `SECRET_PATHS` already does.

The one real design question: **both spellings genuinely matter in different
places.** `_under_any` deliberately uses `any` over normpath/realpath ("does
this reach something protected?"), while `inside_working_tree` uses `all`
("is this safely contained?"). A single resolved value must therefore carry
*both* resolutions, not pick one — that asymmetry is load-bearing and a naive
"just realpath everything" collapses it and breaks macOS autofs besides.

## The decision rule

Written as: *build it when a seventh instance appears, or when a new tool
surface is added to the hook.* **Both fired within the hour** — instance seven
was `grep` on `VETTED_COMMANDS`, which is exactly "a new tool surface gaining a
fresh consumer of the raw string." The shared helpers above were built in
response; that is the rule working, not the rule being ignored.

For what remains (the `ResolvedPath` type), the rule is now: build it when a
**third** consumer of `anchor_to_tree` appears. Two can be kept honest by
reading them side by side; three cannot.
