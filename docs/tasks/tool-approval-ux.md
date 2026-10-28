# Task — the approval sheet asks too often and says too little

**Status: the Bash half SHIPPED 2026-09-06. The `sed` rung and the pipe-filter
allowance SHIPPED 2026-09-25**. They ran on the
box as a branch from 2026-09-11, two weeks rather than the planned 24-48h,
before being merged and the box returned to `main`. No result from the Sunday
corpus re-run described below was recorded, so treat whether the new wording
changed what Marlow reaches for as unmeasured.
**Pieces 2 and 3 (Drive) are still not started.**
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

**2026-10-03: the pane redesign SHIPPED.**
Mike tried it on the phone and called it "working great". Open follow-ups:
- *Web client.* Ported 2026-10-05, merged 2026-10-09.
  `PermissionCard.tsx` replaces the modal, with the same layers, drain bar
  and Allow similar. The push carries the server's title and body, with
  Review and Deny actions. Deny posts from the service worker. Not ported:
  the dictation pill, because the web has no in-app dictation (keyboard
  voice typing doesn't cancel on a modal). Unseen on a real Android phone.
- *Not yet seen on a device:* a locked-phone request (Review and Deny, Deny
  from the lock screen), the pill showing while dictating, the haptic, and
  VoiceOver.
- *Known and left alone (review, 2026-10-02):*
  - A Bash grant covers the verb and not the path. One `grep` allows later
    `grep`s of any file that isn't hard-denied, for the rest of the reply.
    That was Mike's call.
  - The lock-screen notification lingers after the request is answered in
    the app.
- *Adversarial pass:* only `find` was attacked by actually running commands.
  The other verbs' keys were checked by reasoning about the code.

**2026-10-02: the vetter rewidened, and the pane was redesigned.** A replay of
the month's prompts found most Bash prompts were reads refused over one
character in a quoted pattern (an accented letter, `^#`); widening that (plus
`| sed -n 'N,Mp'` as a pipe sink and `nl -b[atn]`) took the corpus from 138 to
145 of 190 cleared, and its reviews closed two live read holes: option values
were never character-checked, and a bundled `-nf PATH` was never
containment-checked. The pane is a centred card in three layers: a
server-written title, a preview lifted from the input, and Marlow's own Bash
`description` labelled as its words (this relaxes the 2026-08-03 rule, and the
raw request is always one tap away). *Allow similar for this reply* keys on an
allowlist in `server/approval_summary.py` and lasts one turn
(`permissions.SimilarGrants`); cron never reaches it. Every push is
Review/Deny: Mike decided the lock screen never offers Allow. A Marlow-written
description of a tool call is, in his words, "quite unlikely to be a privacy
issue". Piece 1's "not worth building" verdict below was about a grant keyed on
input *shape*; a grant keyed on the parsed verb is what shipped.

**What the soak was for, kept because the question is still open.**
107 -> 111 of 191 is small and measured. The unmeasured half is behavioural:
the identity previously listed five verbs in a way that read as *examples*, and
Marlow typed `sed` anyway. It now says that is the whole list. Whether that
changes what it reaches for cannot be answered from the old corpus, because
every command in it was written under the old rules. So re-run
`scripts/replay-bash-vetting.py` against the GROWN corpus and look at the new
commands, not the headline percentage: did `sed` or `| grep` appear at all?
Probes must still read 36 with 0 leaks; if that number moved, roll back and do
not debate it. Content-verb vetting plus three shape allowances live in
`server/bash_policy.py`; replayed against Marlow's real transcripts it removes
**48 of the 143 Bash prompts (33.6%)**. Read "What the replay found" before
proposing anything else here — four separate framings in this file and in
conversation were wrong about the size of the prize, in both directions, and
the corrective in each case was replaying the corpus rather than arguing.

Genesis: three wishlist entries in `memory/marlow-features.md` on the box (all
2026-08-18, all from one session — Mike downloading eight trip-research docs
from Drive). He gave up and `scp`'d the files himself.

## What Mike asked for

1. **"Yes to similar."** A turn that fires several near-identical approvals
   prompts for each one; approve the rest of the batch at once.
2. **Show the document title, not the file ID.** The sheet displays
   an opaque `file_id: 1AbC…` (44 characters).
3. **Standing per-doc read approval**, scoped to one document, not to Drive.

## Confirmed on the box, 2026-09-06 evening

The replay predicted 48 of 143. The deploy landed at 16:15:32 and the pid change
splits the journal cleanly: **five prompts in the seven minutes before it, one
in the six attended turns after** — while Mike was deliberately trying to
provoke more. Small n, and the direction is right.

**The `sed` rung is done (2026-09-11)** — `sed -n '210,280p' file` only,
gated on a closed regex over the SCRIPT (`boundaries.SED_LINE_RANGE_SCRIPT`)
with an option set of exactly `-n`. It needed no character exemption, because
`,` is already on `VETTED_TOKEN_CHARS`; that is why this allowance does not
extend the surface `PATTERN_EXTRA_CHARS` had to be narrowed to close. Replayed
on the box: **107 -> 108 of 191 cleared, 28 attack probes, 0 leaks.** One
command is an honest number: there are only two `sed` calls in the whole
corpus and the other is malformed (`'210,280,'`), so it still prompts and
should. The case was never the historical count but the recurring prompt,
since `sed` was the single survivor in the six attended turns after the 2026-09-06 deploy. It
is NOT a one-line addition to `CONTENT_VERBS`: `-i` writes in place, the `w`
command writes an arbitrary path, and `e` executes a shell command, so
allowing it meant restricting the SCRIPT, the same parsing family as the two
HIGH findings `/security-review` pulled out of the pipe handling.

**The pipe-filter allowance shipped with it**, because it is the same kind of
mistake one verb over. `grep` and `cat` are now pipe sinks. A sink is "reads
stdin, opens nothing", and a `grep` naming no file is exactly that -- it was
excluded by an ACCIDENT, not a decision: the sink rule refuses any bare
argument, and grep's bare argument is its PATTERN. That is the identical
misread `_content_verb_targets` already had to correct, where it once scored
`grep -n "Two Words" notes.md` as naming a file called `"Two`. The fix is
a per-sink budget of ONE bare token for grep and zero for everything else
(`boundaries.SINK_BARE_ARGUMENTS`), so `| grep pat secrets.txt` still prompts.

One test had to be deleted to ship it, and that is worth recording rather than
glossing: `cat notes.md | grep pattern` was asserted to PROMPT, filed under the
heading "a sink that names a path is not a sink", alongside `wc -l /etc/passwd`
and `tee /tmp/out`. But `pattern` is not a path. The test encoded the accident,
not a reason -- which is the whole argument for replaying a corpus rather than
reading the rule and agreeing with it.

**Measured on the box, all three states, same corpus of 191:** 107 cleared
before this branch, 108 with `sed`, **111 with both (56.0% -> 58.1%)**. The
pipe half is worth 3, not the 6 first estimated from counting blocked pipes --
several of those commands carry a glob or an outside-tree path as well, so
clearing the pipe did not free them. 28 attack probes, 0 leaks at every step.

**The pipe half opened a critical hole and the review caught it — record this
one, it is the most instructive thing on this branch.** `_strip_pipe_sinks`
folds a following all-digit token into the preceding option (`head -n 20` ->
`-20`), and it did that for ANY dash token, with no table of which options take
a value. That was inert for as long as no sink could carry a bare argument. The
moment grep got a budget of one, it became an arbitrary out-of-tree read:

    cat notes.md | grep -v 999999 /etc/passwd      # reached ALLOW

`-v` is boolean, but the fold ate `999999` as its value, so the budget was never
spent on the pattern — and `/etc/passwd` spent it instead, passing the character
allowlist cleanly. Real grep parses the same argv the other way: boolean `-v`,
pattern `999999`, FILE `/etc/passwd`; `-v` inverts, so a digit pattern matching
nothing dumps the file. `.env`, `.claude/settings.json` and `.git/config` all
reached ALLOW this way — the sink path never calls `reaches_protected_path`, so
this re-opened the 2026-08-06 `wc -l .env` finding through a pipe.

Three things worth keeping from it:

- **The fix is `boundaries.SINK_VALUE_OPTS`** — fold only for an option that
  genuinely takes a value, so a boolean can never absorb the budget token. Plus
  one backstop: a sink's bare token may not contain `/`. That is defence in
  depth against the next un-tabled option, not the control, and it costs
  nothing measured (no `| grep` in the corpus carries a slash).
- **The danger was in code this change did not touch.** The diff added a budget;
  the hole was a four-year-old fold three lines up. A review scoped to the added
  lines would have passed it.
- **Reading the rule was not enough — four careful passes over this loop missed
  it, including mine.** It was found by differential execution: run every
  ALLOW-reaching command under real bash with a `grep` that logs the paths it
  opens, then compare what bash did against what the policy believed. That
  technique now has two catches on this module and should be the default for
  any change to it. The probes are in `scripts/replay-bash-vetting.py`
  (36 now, 0 leaks) as well as in pytest.

**What is left, and what should stay left.** Of the 80 that still prompt, the
biggest bucket is ~16 multi-line commands, not fixable: a newline is how two
commands become one. Then ~9 outside-tree paths and ~7 globs, both deliberate.
`crontab -l` is 5, and it should NOT be allowed even though it is read-only:
those calls predate the schedules store, and making the obsolete tool cheap
again is exactly the schedules-store failure (the model reaches for what it is
told is available). Marlow also did NOT reach for a vetted verb it had been
told about: the identity's "Shell commands" section lists
`cat`/`grep`/`head`/`tail`/`nl`, and it typed `sed` anyway. That list reads as
examples, not as a boundary.

## The mechanism, verified in code 2026-09-01

Every Drive/Docs call reaches the phone by design (only the read-only
Gmail/Calendar halves are in `boundaries.VETTED_PATHLESS_TOOLS`), so the eight
prompts were legitimate fall-throughs, not a bug. A decision resolves exactly
one request: there is no grant wider than a single call and nothing that says
two requests are related, a concept pieces 1 and 3 both need. The sheet renders
raw tool input (`AnyCodableValue.formattedPairs` in
`ios/Marlow/Marlow/Models/APIModels.swift`), so the file-ID complaint is one
client line. `ios-permission-round-trip-ui.md`, "Deferred: a friendlier
summary, without trusting the narrator", already made the argument that the
sheet is built to **deny** rather than to **describe**; read it, don't
re-derive it.

## Where the prompts actually come from

`~/.marlow/logs/marlow-server.log`, 2026-07-23 → 2026-09-06, ~151 prompts:

| tool | prompts |
| --- | --- |
| `Bash` | **107** |
| `Edit` | 12 |
| Drive (`download_file_content` + `search_files`) | 13 |
| `Read` | 5 |
| everything else | ~14 |

**Bash is 70% of the taps.** The Drive session that opened this task is one day;
the standing tax is Bash. Worst burst: 18 Bash prompts in seven minutes on
2026-09-03, roughly one every ten seconds.

This also fixes the scope of pieces 2 and 3: they address 8% of the volume. They
are still worth what they are worth, but they are not the ergonomics fix.

## What the replay found — and what it cost to get the number right

160 `Bash` calls from `~/.claude/projects/-home-marlow-marlow-identity/*.jsonl`,
classified by the real vetter against the real working tree. **143 of 160 (89%)
prompted before this change.** First-blocker breakdown:

| cause | n | share |
| --- | --- | --- |
| **shape** — `2>/dev/null`, `\|`, `;`, `&&`, `*`, newline, quoting | **123** | **86%** |
| verb not on `VETTED_COMMANDS` (`cat` 11, `grep` 3, `crontab` 1) | 15 | 10% |
| containment — argument outside the tree | 5 | 4% |

**Read that table correctly: it reports the FIRST blocker, not an independent
one.** That is the trap this task fell into twice. The verb allowlist looks like
a 10% problem and the shape rules like an 86% one, and both readings are wrong,
because the causes co-occur — `cat notes.md 2>/dev/null | head -20` fails three
tests at once. Measured as separate changes, each rung is nearly worthless and
only the combination pays:

| allowance, measured ALONE | prompts removed |
| --- | --- |
| `2>/dev/null` + `2>&1` | 3 (2.1%) |
| `;` `&&` `\|\|` sequencing | 0 (0.0%) |
| pathless sink after a pipe | 2 (1.4%) |
| content-verb rule | 15 (10.5%) |
| all shape allowances, no verb rule | 13 (9.1%) |
| **all of them together (shipped)** | **48 (33.6%)** |

Superadditive, and that is the whole design argument. **There was no cheap
interim available here** — Mike asked directly (2026-09-06) whether allowing
just `2>/dev/null` would do, since Marlow writes it constantly, and the answer
is that it converts three commands, because the stderr redirect almost always
rides on a verb that fails anyway.

**Three numbers were published for this before one was right: 13%, 18%, 28%.**
The final is 33.6%. Every error came from a hand-written *simulation* of the
proposed rule rather than from the vetter:

- The 13% claim ("a turn-scoped grant targets the 70%") confused Bash's share of
  *prompts* with the verb's share of *Bash's own causes*.
- The 18% and 28% runs tokenized on whitespace, so every
  `grep -n "Two Words" notes.md` scored as naming a file called `"Two`;
  never re-tested a segment after stripping its pipe; and skipped the character
  allowlist entirely for content verbs, which over-counted in the other
  direction.

**So the harness is committed: `scripts/replay-bash-vetting.py` calls
`vet_bash_command` itself and simulates nothing.** Re-run it on the box after any
change to `bash_policy.py`. It also runs 28 attack probes and exits non-zero if
any is cleared.

### The rule that shipped

**A content-printing verb (`cat`, `grep`, `head`, `tail`, `nl`) is vetted only
when it names at least one target, and every target resolves to an existing
REGULAR FILE inside the working tree that is not a secret, agent-config or
harness-only path.** No directory targets, no zero-target invocations. This
answered a question `server/boundaries.py` had carried open since 2026-08-06
(a verb that prints contents needs a different rule than one that prints
names), and it had to be answered rather than deferred because the fallback it
named, the `Grep` tool, was not being offered by the CLI at all
(`marlow-harness-affordances.md`; `--tools` fixed that on 2026-09-06). It does
not retire the rule: `Grep` answers "which file mentions X", and the 160 Bash
calls of `ls`/`git`/`cat`/`head` in the corpus are not searches.

`grep` deliberately did NOT join `VETTED_COMMANDS`. The two sets are cleared by
different tests: a `VETTED_COMMANDS` verb clears on containment alone, which is
why it may only print names, counts or metadata. Moving `grep` up there would
restore exactly the hole `/security-review` found.

Three shape allowances ride with it, each independently safe:

- **`2>/dev/null` and `2>&1`** — stderr suppression, semantically inert for a
  read. Stripped per SEGMENT, after operator splitting: anchored on whitespace
  instead, the redirect in `cat x 2>/dev/null; ls` ends at the `;` and six
  corpus commands prompted for a construct the rule was written to allow.
- **A bare sink on the right of a pipe** — `head`, `tail`, `wc`, `sort`, `uniq`,
  `cut`, `nl`, `tr`, **only when it names no path**. `| tee out.md` and
  `| wc -l /etc/passwd` are not sinks.
- **`;`, `&&`, `||` sequencing**, with every segment vetted independently.

Command substitution is refused outright, never parsed. A single `&` is not an
operator: it backgrounds a command, which would outlive the round-trip.

**One deliberate exemption, and a self-review found a hole in its first draft.**
grep's PATTERN token is allowed two characters beyond `VETTED_TOKEN_CHARS`:
`\` and `|`. 13 corpus greps are alternations (`grep -n -i "foo\|bar" notes.md`)
and without those two characters the commonest shape of the exact command this
rule targets still prompted.

The first draft exempted the pattern from the character allowlist **entirely**,
and that was arbitrary command execution in one step:

```
grep -A \'1;id \' f.md g.md      # ALLOWED by the first draft; bash runs `id`
```

`\'` is a *literal quote* to the shell, so the text between the two of them is
unquoted and the `;` separates commands — bash runs `grep -A '1` and then `id`.
`bash_policy`'s own quote scanner does not implement backslash escaping, so it
read one quoted region containing no operator and handed the whole string over
as a single segment. **The character allowlist had always been what caught that
class**; exempting a token from it removed the backstop, and the exemption was
the only thing standing between a model-authored string and an unprompted
command.

Two independent fixes, deliberately — this module's history is three
`/security-review` passes each finding a bypass of the one before:

1. the exemption is now two characters, so the `'` and `;` in that token fail
   again;
2. `_decompose` refuses any command containing `\'`, `\"`, `$'` or `$"` outright.
   Those are the constructs that move where a quoted region starts or ends, and
   teaching the scanner to escape correctly is precisely the analysis that has
   failed here before. No corpus command contains one.

Closing it cost three of the 51 conversions, which is the right trade and worth
recording as the shape of the trade: the exemption was widened by measurement
and narrowed by attack, and the narrow version still carries the alternations
that motivated it.

**A second self-review finding, same shape, different verb.** The sink rule
accepted any token beginning with `-` as an option, so:

```
cat notes.md | sort -o/tmp/pwned      # ALLOWED by the first draft; it wrote the file
```

A sink is supposed to truncate a stream and open nothing, but `sort -o` is an
output option — this is `git log --output=` (2026-08-28) one verb later, and the
lesson that a read verb is also a write primitive had to be learned a second
time. Sinks now have a per-verb option allowlist (`boundaries.VETTED_SINK_OPTS`)
with no output option in it, and a separate-token value is consumed only when it
is all digits, which no path can be. Cost: nothing — coverage stayed at 48.

**Both findings were in the pipe/redirect machinery, not in the content-verb
rule.** Worth noting for whoever extends this: the rule about which FILES may be
read held up, and what leaked was the shape handling around it.

**The regression guard is a differential test, not an assertion about the rule.**
`tests/test_bash_policy.py` runs adversarial commands through a real `bash`
with a PATH containing nothing but logging stubs, and fails if a command the
vetter ALLOWED causes anything but a content verb or sink to execute. It was
verified by reintroducing the bug: the test fails with
*"vet_bash_command ALLOWED ... but bash ran ['id']"*. Every failure this module
has had was a static reading disagreeing with what bash would really do, and
every one was found by a person noticing. This asks bash.

`server/bash_policy.py` has been caught twice by enumerating the dangerous
cases — the `git diff --no-index` verb denylist and the metacharacter
denylist. Any change here must enumerate the safe set, and the content-verb
rule is written that way deliberately.

### What still prompts, and should

95 residual: `ls` 15, `git` 14, `grep` 10, `find` 9, `cat` 8, `python3` 8,
`crontab` 5, `sudo` 3. Recursive search (`grep -r`) is the notable one — a real
want, and precisely the case containment cannot bound. `find`, `python3` and
`crontab` are different asks entirely.

**`jq`, parked 2026-10-12 (Mike: only if it keeps prompting).** Marlow reached
for it twice in one turn to slice a large Gmail thread the CLI had spilled to
`~/.claude/projects/<id>/<session>/tool-results/*.txt`. Two separate gaps, each
needing its own argument: the filter (`.messages[] | select(.sender|test("x"))`)
fails the character allowlist, so `jq` would need a closed rule like `sed`'s
(options `-r -c -S -e` only; refuse `env`, `$ENV`, `import`, `include`,
`input_filename`, `--rawfile`, `--slurpfile`, `-f`, `-L`); and the target is
outside the working tree, so even `cat` on a spill file prompts. Allowing
reads of the *current session's own* `tool-results/` may be the bigger win.
Replay the corpus and `/security-review` before either.

## What is NOT worth building, and why

**Piece 1 as "yes to similar" for Bash.** 13% coverage, and it needs a new
durable-ish concept (a grant wider than one call), a new decision verb on the
wire, and iOS work. The coverage does not justify it, and the equivalence key is
the whole security surface: for Bash, *every* call has identical argument shape
`{command: str}`, so a shape-key grant after one `ls` would blanket-approve
`rm -rf`. Any Bash grant must key on the parsed command, never on the input
shape.

**It may still be right for Drive**, which is where the repetition actually is —
eight `download_file_content` calls differing only in `file_id`. If piece 1 is
ever built, build it for the connector tools, where the shape key is meaningful,
and leave Bash to static vetting.

## Pieces 2 and 3 — unchanged, still small, still 8% of the volume

**Piece 2 — show the title, not the ID.** Two options and one is a trap.
*Server resolves the ID*: correct, but an outbound Drive call on the latency
path of a prompt Mike is already waiting on. *The model supplies a label*: **do
not**, or not without marking it untrusted — the schedules-store change carries a commit named
exactly this mistake, *"a model-chosen string must not reach a privileged
decision."* A label the model writes is text an injected document can influence,
rendered next to an Allow button. **Recommendation: server-side resolution, or
leave it alone.**

**Piece 3 — standing per-doc read approval.** Needs a durable store
(`cron/schedules.json` is the shape), a scope key that is one document, an
expiry or review path, and an answer to attended-vs-unattended. From
`denied-tool-workarounds.md`: *a vetted-allow is a standing grant and it applies
unattended* — exercised at 08:00 against inbox text written by strangers. **The
safe default is that a grant made on the phone is not exercisable by cron**, and
it should stay the default until Mike says otherwise. His own note draws the
useful line: standing live-read is for documents he is actively *editing*. For
static reference material the "Google Doc → markdown helper" wishlist item is
simpler, and if that ships first piece 3 may never need to exist.

## Still reaching for shell tools — 2026-09-28 (Mike)

**Observed, not measured.** Marlow still reaches for `grep`, `find` and
compound pipes first, despite the identity saying `Grep`/`Glob` are the right
tool. It switches to the built-in tool only after a denial: a prompt Mike
declines, a prompt timeout, or a refusal inside cron. One attended turn on
2026-09-28 raised 4 Bash approvals and 1 decline in a single reply. Mike calls
it "a continual irritant", and adds that compound pipes are efficient but hard
to approve on a phone. That partly answers the open soak question above ("did
`| grep` appear at all?"): yes, the identity nudge has not stuck.

**Direction (Mike): redouble on both ends of the decision rule** in
`denied-tool-workarounds.md`:
- **Auto-approve more, where it is safe.** Widen the vetted set (still by
  enumerating the safe set, never the dangerous one).
- **Auto-deny with coaching, where a built-in tool is the answer.** Today the
  cycle is: agent ignores its instructions → Mike gets a prompt → a denial →
  the agent uses `Grep`. Cut Mike out of it. The hook denies instantly, and the
  reason names the tool to use instead ("use the Grep tool for this search").
  Denial reasons already reach the model, and recovery after a denial is what
  already works. Candidates are the substitutable shapes: recursive `grep`/
  `rg`, `find` by name, `cat`/`head` of a file `Read` would take. A pipe could
  get "split this into steps", but that one needs care, because some pipes are
  already vetted-free.

**Rejected in conversation: an LLM approver.** The server's only model access
is `claude -p` through the one serialized queue, so a judge would need the
API at runtime, and the CLI-only rule exists to keep the server off API
billing. It would also be a model deciding the prompts that protect Mike
from its own agent.

**Measure before and after.** The activity log (ux-lane §2) now records every
turn's tools and Mike's approvals and declines, by name, in
`data_dir/activity/`. A count of Bash calls, approvals and declines per week
from those files is the baseline this change should move.

## The decision that remains

33.6% is not the majority, and no further vetting work gets there: the residual
is either heterogeneous (`find`, `python3`, one-off `git`) or legitimately worth
asking about (`crontab`, `sudo`, arguments outside the tree). That is the
boundary working, not a gap.

So the open question is unchanged in kind, only smaller: **change the posture, or
accept the remainder.** A standing auto-approve for contained reads on attended
turns — closer to how Claude Code itself now handles allowlisting — is the only
route to the majority, and it is a decision about how much Mike wants to see
rather than an engineering problem. He named it himself on 2026-09-06 as the
"scarier" next jump. The remainder is now ~95 prompts over six weeks, about two
a day, which is a materially easier thing to accept than the 143 that opened
this task.

Review lesson: `/security-review` diffs against `origin/main` and silently
ignores the working tree, so commit before running it.

Related: `marlow-harness-affordances.md` (why routing Marlow to `Grep` was not
available, and the eager-tool probes) and `denied-tool-workarounds.md` (the
allow-vs-coach decision rule).
