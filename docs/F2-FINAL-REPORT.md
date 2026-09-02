# Phase F.2 Independent Coding Review & Fix Loop — Final Report

Host: Raspberry Pi 4 / Debian 13 arm64 / 3.7 GiB RAM
Date: 2026-09-02

## Architecture

```text
Implementation:
Hermes CLI (worker-only, v0.20.6)
CommandCode
z-ai/glm-5.3-flash

Reviewer:
Hermes CLI (worker-only, reviewer HERMES_HOME)
CommandCode
meta/muse-spark-1.2
```

> Spec deviation, user-confirmed this session: the spec pins
> `meta/muse-spark-1.2-contributor`. This deployment runs the standard
> `meta/muse-spark-1.2` on data-policy grounds. **Measured, not assumed:**
> both ids are present in the live CommandCode `/provider/v1/models` listing,
> so the STOP condition "reviewer model unavailable" does not apply and this
> is a documented deviation, not a fallback. The single source of truth is
> `REVIEWER_MODEL` in `jobs/review.py`; the reviewer launch script and the
> reviewer Hermes config carry the same note. An earlier revision of those
> comments justified the choice with a Hermes "data-policy guard" — no such
> guard exists in the Hermes source (`contributor_audit.py` is a release-notes
> script); that unverified claim was removed.

## Database

```text
migration:        additive only. reviews table (CREATE TABLE IF NOT EXISTS)
                  + _ensure_review_columns() ALTERs for attempt_no and the
                  new content_progress. jobs / runs / event_log untouched.
review schema:    review_id job_id cycle attempt_no status reviewer_agent
                  reviewer_model base_sha target_sha diff_hash content_sha
                  content_progress started_at finished_at verdict summary
                  findings_json blocking_count fingerprints_json usage_json
                  error_summary
existing job rows: 43 jobs / 7 pre-existing review rows preserved
integrity:        PRAGMA integrity_check = ok
idempotency:      init_db() run 3x in a row in test, 2x on the production DB —
                  no schema drift, no data change (unit test:
                  test_init_db_twice_is_safe_and_additive)
backup:           /srv/hermes/backups/f2-review-20260902-055343/
                  (sqlite .backup + sha256, supervisor / jobs MCP / bridge /
                  bin / reviewer config / worker registry / README / crontab;
                  no secrets)
```

## Reviewer Model Verification

```text
requested model:  meta/muse-spark-1.2
actual model:     meta/muse-spark-1.2   (from --usage-file metadata of every
                  review run, i.e. what the provider actually served)
availability:     GET https://api.commandcode.ai/provider/v1/models
                  62 models; muse family present:
                    meta/muse-spark-1.1
                    meta/muse-spark-1.2
                    meta/muse-spark-1.2-contributor
fallback:         none. REVIEWER_MODEL is fixed in supervisor code and in the
                  bridge agent registry; no MCP/config/task path can change
                  the reviewer model. Unavailability fails safe
                  (REVIEW_FAILED -> WAITING_HUMAN), never a silent switch.
```

## Independence

```text
different model:                  YES  glm-5.3-flash vs muse-spark-1.2
                                       (verified from usage metadata, both
                                        roles, every run)
different HERMES_HOME:            YES  /agent-home/hermes vs
                                       /agent-home/reviewer-hermes
different session:                YES  separate container, separate one-shot
                                       hermes -z invocation, separate config
implementation transcript:        ABSENT
implementation final response:    ABSENT
implementation worktree write:    ABSENT (measured, see Security)
```

Measured on a real reviewer input package (`prompt.txt`, 5212 bytes):

```text
PRESENT: "# Review target", "## Original task", "## Acceptance criteria",
         "## Canonical changed files", "Deterministic validation evidence",
         "canonical secret scan", "Output contract"
ABSENT:  "I fixed", "Everything passes", "implementation is correct",
         "assistant:", "HERMES_HOME", "agent-home/hermes", "session_id",
         "transcript", "COMMANDCODE_API_KEY", "sk-"
```

## Review State Machine

```text
WAITING_REVIEW:      YES  set at IMPLEMENTATION_ACCEPTED (production jobs)
REVIEWING:           YES  single-owner CAS claim; a losing runner exits
APPROVED:            YES  never auto-completes; the final gate still runs
CHANGES_REQUIRED:    YES  -> CONTINUE_REQUIRED -> implementation fix attempt
REVIEW_INTERRUPTED:  YES  runner death reconciled, job preserved, bounded
                          retry via job_review
CANCELLED:           YES  terminal during review; a later verdict cannot
                          resurrect the job
```

Also implemented: `REVIEW_FAILED` (invalid output past the repair budget,
or a bridge rejection) -> `WAITING_HUMAN`, and `NOT_REQUIRED` for
`kind != implementation`.

## Review Validation

```text
structured JSON:     reviewer writes /jobs/review-result.json; chat text is
                     never authoritative
schema:              review.validate_review_output() — verdict/severity/
                     category enums, confidence range, bool typing
path validation:     absolute paths, "..", backslashes and paths outside the
                     repo scope rejected
conflicting verdict: APPROVE + any blocking=true finding -> invalid
                     (CHANGES_REQUIRED with no blocking finding also invalid)
finding bounds:      max 30 findings; summary/explanation/suggested_fix 2000
                     chars; title 200 chars
fingerprints:        sha1 over category + file + line range + normalized
                     title + normalized explanation (16 hex), stored per row
invalid JSON:        1 strict repair retry, then REVIEW_FAILED -> WAITING_HUMAN
                     (no unbounded repair loop)
```

## Completion Gate

`completion_decision()` is the single canonical gate and the only writer of
`COMPLETED` anywhere in the supervisor (asserted by unit test
`test_only_completion_decision_writes_completed`).

```text
implementation acceptance: required (acceptance_decision(); a test that never
                           ran is never PASS)
Muse approval:             required, with a valid bound review record
                           (verdict APPROVE + status approved)
target SHA binding:        approved target_sha == freshly re-read HEAD
content binding:           approved content_sha == freshly recomputed content
                           hash of the canonical changed set
diff hash binding:         approved diff_hash == freshly recomputed diff hash
                           (added this session; legacy rows without a stored
                           diff_hash are not retro-invalidated)
secret scan:               fresh canonical scan, zero hits required
tests:                     registered suite re-run at gate time, PASS required
cancel guard:              cancel_requested_at must be NULL
CAS:                       cas_status(job, COMPLETED, from=IMPLEMENTATION_ACCEPTED)
```

A stale binding does not fail the job: the approval is invalidated back to
`WAITING_REVIEW` and a fresh review cycle runs.

Audited against the live database — every `kind=implementation` job that has
reached `COMPLETED` since F.2 went in carries exactly one bound approval:

```text
completed implementation jobs after the first review record
(2026-09-01T16:57Z):                                          16
  ... of which carry a bound APPROVE review record
      (verdict APPROVE, status approved, target_sha and
       content_sha both non-null):                            16
  ... of which do not:                                         0

one pre-F.2 legacy row exists: job_3dbec244ba, COMPLETED at
2026-09-01T06:52Z with review_status=NOT_STARTED — roughly ten hours before
the first review record. It predates the gate rather than bypassing it, and
under the current code its path no longer exists (unit test
test_completion_requires_review_at_all).
```

## Existing Job Real Review

```text
job:                  job_0313cae01a (the F.1.1-Pi.3 production E2E job)
implementation model: z-ai/glm-5.3-flash
reviewer model:       meta/muse-spark-1.2
target SHA:           02dd871c188d5e8d15a479b47ff47c3e6a30197a
review cycle:         1
verdict:              APPROVE (blocking 0)
final job status:     COMPLETED
```

This job was reviewed and completed before this session, under the same
infrastructure. It is included for completeness; the runs below were executed
this session against the current code.

## Real Fix Loop

```text
fixture/job:  job_25ece703f8, repository f2-loop2
              (a fresh 2-file repo; task = pkg/numfmt.py with sixteen
               separately specified public functions; the registered suite
               covers TWO of them, clamp and sign_word)
```

**Implementation cycle 1** — `z-ai/glm-5.3-flash`

```text
attempt 1: killed at the bounded agent window (exit 137), no edit
attempt 2: wrote pkg/numfmt.py with 2 of the 16 required functions
result:    every deterministic criterion PASS
           (module exists, clamp exists, sign_word exists, registered suite
            green, scope clean)  ->  IMPLEMENTATION_ACCEPTED
```

This is the case the phase exists for: the deterministic gate was satisfied
by an implementation that delivered an eighth of the task, because the
registered tests happened to cover exactly the part that was written.

**Muse review cycle 1** — `meta/muse-spark-1.2`, 91 s, 6 API calls

```text
verdict:  CHANGES_REQUIRED
blocking: 4      nonblocking: 1
  critical  requirement_violation  14 of 16 required public functions missing        conf 1.00
  critical  requirement_violation  __all__ missing; must list exactly 16 names       conf 1.00
  high      correctness            clamp signature + error message deviate from spec conf 0.99
  medium    requirement_violation  sign_word parameter name deviates (value vs x)    conf 0.95
  medium    verification_gap       registered suite covers 2/16; PASS is not
                                   sufficiency evidence                   (non-blocking, conf 0.98)
```

Every finding is correct and evidence-backed. Nothing was suggested to the
reviewer: it read the task, the criteria, the diff and the snapshot.

**Fix cycles 2 and 3 — a measured failure mode, correctly bounded**

```text
cycle 2: CHANGES_REQUIRED (blocking 5)  content_progress = unchanged
cycle 3: CHANGES_REQUIRED (blocking 4)  content_progress = unchanged
-> attempt limit reached -> WAITING_HUMAN
```

Each fix attempt ran with the production 1200 s agent window / 900 s hang
watchdog and was killed before writing anything: on this Pi the model spends
the whole window planning a fourteen-function edit. The supervisor recorded
both cycles as `content_progress = unchanged` — the field added this session
— and stopped the loop at its bounds instead of grinding. That is the
correct behaviour, and it is why the marker exists.

**Operator decision, then Fix cycle 4 — the real loop closing**

The blocking findings were correct and the remaining work was real, so the
operator raised the agent window (1200 s → 2700 s, hang watchdog 900 s →
1800 s) and the review-cycle budget (3 → 5) and ran `job_continue`.
**Container resource limits and combined concurrency were not touched**
(memory 1250m, cpus 2, pids 256, concurrency 1). Both settings were restored
to their documented defaults immediately after the run.

```text
implementation fix (attempt 5, z-ai/glm-5.3-flash, 21 min, 26 API calls):
    pkg/numfmt.py  17 lines -> 233 lines, 16 of 16 functions, __all__ = 16 names
    changed files: 1        registered suite: PASS
    deterministic acceptance: PASS -> IMPLEMENTATION_ACCEPTED

Muse review cycle 4 (fresh row, fresh binding, 300 s, 7 API calls):
    verdict:            APPROVE
    blocking:           0        nonblocking: 2
    content_progress:   changed          <-- the implementation actually moved
    content binding:    2085f284b988fb47… -> 0da1f2e0278e1b91fb7a…

final exact-revision validation: PASS
    target SHA match, content binding match, diff hash match,
    registered tests re-run PASS, secret scan clean, no cancel
final status: COMPLETED   (compare-and-set from IMPLEMENTATION_ACCEPTED)
```

Full review history for the job (immutable, one row per cycle):

```text
cycle 1  CHANGES_REQUIRED  blocking=4  progress=first_cycle  content=2085f284b988…
cycle 2  CHANGES_REQUIRED  blocking=5  progress=unchanged    content=2085f284b988…
cycle 3  CHANGES_REQUIRED  blocking=4  progress=unchanged    content=2085f284b988…
cycle 4  APPROVE           blocking=0  progress=changed      content=0da1f2e0278e…
```

Every call in that sequence was a real CommandCode API call: real GLM
implementation, real Muse review, real GLM fix, real fresh Muse re-review.
Nothing in the loop was mocked.

### The other twelve real jobs this session

Eleven further production jobs were run against real models on three fixture
repositories, deliberately probing for quality defects the registered tests
could not see: an explicit 7-clause numeric contract, a single-flight
concurrency contract with lock-scope and re-entrancy clauses, an API
extension with error-wrapping and `__all__` clauses, a maintenance task on a
module whose docstring contradicted its code in three places, two regression
traps (one against a tested consumer, one against a consumer with no test at
all), a bespoke ordering spec whose rules deliberately invert SemVer, a
ten-function module, and a repo-wide error-type refactor across seven
modules.

**All eleven were approved on review cycle 1**, and independent oracles
written for the two hardest (the bespoke ordering spec, 26/26; the repo-wide
refactor, 0 unmet requirements) confirm the reviewer was right to approve
them. Two further jobs were cancelled by the operator because the fixture
itself was faulty, not the agent (one had a registered test that contradicted
the task; one was too large for the agent to begin).

The finding is about the implementation model, and it is worth recording:
**GLM-5.3-Flash satisfies an explicitly written contract on the first
attempt with high reliability, including clauses no test exercises** — exact
parameter names, exact error-message text, `bool`-is-`int`, half-away-from-
zero rounding, negative-zero normalisation, lock scope, and two rules
specifically written to invert a spec it would have memorised. The one thing
it does not do reliably is finish a task larger than its time window, which
is exactly the failure the independent reviewer caught.

## Usage

Per role and per cycle, for the real fix-loop job `job_25ece703f8`:

```text
                                   in      out  reasoning     total  api_calls
implementation cycle 1 (att 2)  10250      552        164     47986          5
review cycle 1                  15758     4498       2210     78357          6
implementation fix (att 3)      10250      552        164     47986          5
review cycle 2                  17255     5618       3410     71886          5
implementation fix (att 4)      10250      552        164     47986          5
review cycle 3                  18089     6102       3676    100054          7
implementation fix (att 5)      45155    44387      33208    557894         26
review cycle 4                  41729    25035      20637    141027          7
```

Whole session (14 production jobs, three fixture repositories):

```text
role=implementation   in=444179  out=191665  reasoning=134855  total=3127364  api_calls=181
role=review           in=567588  out=236082  reasoning=181726  total=2258310  api_calls=113
combined              in=1011767 out=427747  reasoning=316581  total=5385674  api_calls=294

review records in the DB: 22   (17 approved, 5 changes_required)
review duration: 90 s – 315 s per cycle (measured, finished_at − started_at)
estimated cost: unavailable (see below)
```

`estimated_cost` is **`unavailable`** for every call: CommandCode reports
`cost_status: "unknown"` / `cost_source: "none"`. No price-table estimate is
written to the jobs DB (spec section 76); token counts are kept so a cost can
be computed later, outside the DB. This was a real defect found and fixed this
session — the aggregator previously recorded the harness's placeholder `0.0`
as if it were a measured cost of zero.

## Muse Quality Observations

```text
valid JSON first try:   13 / 13 review runs this session parsed and schema-validated
                        on the first attempt; the repair budget was never used
blocking finding quality: every blocking finding this session was correct and
                        evidence-backed; two were verified against independent
                        oracles written afterwards
false positives:        none observed this session
misses discovered by deterministic tests: none (the deterministic gate
                        re-ran the registered suite after every approval and
                        never contradicted a verdict)
review convergence:     converged in every job; the one job that needed a fix
                        loop converged at cycle 4 once the fix could actually run
```

Evidence that the reviews are substantive rather than rubber stamps:

- `rev_e8a1e40e4b` enumerated all seven contract clauses individually and
  named the concrete values it checked (`0.0625 -> 0.063`, `2.0005 -> 2.001`,
  `-0.0` normalisation, the `isinstance(bool)` guard, check ordering).
- `rev_edf1534b36` approved but filed a **correct** non-blocking edge-case
  finding at confidence 0.82: `isinstance(values, Iterable)` rejects legacy
  `__getitem__`-only iterables that `iter()` would accept. It stated it had
  verified this by executing a `GetItemOnly` class. That is a real defect in
  a real implementation, found without any prompting toward it.

One historical `CHANGES_REQUIRED` exists in the DB (`rev_a307187ff7`,
`job_4f1b65121d`, before this session). Its blocking finding was a
**fixture-contamination artifact, not a code defect**: the old fixture repo
had committed the tests of earlier fixture versions without their
implementations, so the agent had to create those modules to make the
registered suite pass, and the reviewer read the extra files as scope creep.
The agent could not act on it without breaking the tests, so cycle 2 bound a
byte-identical `content_sha` and the reviewer changed its mind. **That is not
fix-loop evidence** — it is what motivated the `content_progress` field below.

Sample size is small. No conclusion about long-run reviewer quality is drawn
from it, and the reviewer model is not auto-switched on any of these numbers.

## Race / Failure Tests

```text
approval stale after SHA change:      unit-tested (target SHA, content
                                      binding, and diff hash independently)
cancel during review:                 unit-tested (terminal; a later verdict
                                      cannot resurrect)
review worker crash:                  unit-tested -> REVIEW_INTERRUPTED,
                                      reconcile + bounded retry
invalid JSON:                         unit-tested (1 repair, then
                                      REVIEW_FAILED / WAITING_HUMAN)
duplicate review dispatch:            unit-tested (single-owner CAS; MCP
                                      job_review also pre-claims)
review cycle exhaustion:              unit-tested (3 cycles ->
                                      WAITING_HUMAN, review_cycle_limit_reached)
review no-progress stall:             unit-tested (same fingerprints + no
                                      content progress + no acceptance
                                      progress -> WAITING_HUMAN)
review-less completion, any job kind: unit-tested (kind=test skips review and
                                      still cannot complete; only
                                      completion_decision writes COMPLETED)
```

Regression suite: **126 tests OK** (108 pre-existing F.1.1 hardening + F.2
review tests, plus 18 added this session in `tests/test_f2_binding.py`).

Three of these were also observed on real hardware, not only in unit tests:

```text
cancel during a fix attempt (section 72):
  job_41c34b5d7a was cancelled while a GLM fix attempt was running after a
  real CHANGES_REQUIRED. cancel_requested_at was stamped and persisted, the
  worker was torn down, the event log recorded "cancel detected after agent
  stop; validation skipped", and the job stayed CANCELLED — the pending
  review verdict could not resurrect it.

review cycle / attempt exhaustion (sections 62/63):
  job_25ece703f8 ran three real review cycles whose fix attempts made no
  content progress, then stopped at WAITING_HUMAN ("attempt limit 4
  reached") instead of looping. Both no-progress cycles are recorded as
  content_progress = unchanged.

deterministic gate accepting an incomplete job (the reason F.2 exists):
  the same job reached IMPLEMENTATION_ACCEPTED with 2 of 16 required
  functions, because the registered suite covered exactly those two. The
  independent reviewer blocked it.
```

## Security

```text
Personal Hermes:        ABSENT from the reviewer container
                        (/opt/data, /srv/hermes, /home/<PI_USER> all absent)
Implementation worktree: ABSENT — and measured actively, not assumed:
                        the reviewer attempts a write to the implementation
                        worktree root, the implementation repos root, the
                        implementation agent-home, personal Hermes data and
                        the host root on every review.
                        Result: FileNotFoundError x5, OSError (read-only) x1,
                        write_attempts_all_failed = true.
                        The one write that succeeds goes to the ephemeral
                        /workspace snapshot; the host confirms
                        .reviewer-write-probe never appeared in the
                        implementation worktree, and the snapshot worktree
                        was removed after the review.
credential leakage:     0. 341 files scanned for the live CommandCode key and
                        a distinctive fragment of it — all review prompts (39),
                        review results (9), review usage files (9),
                        implementation usage files (11), reviewer probes (9),
                        bridge/runner logs, and jobs.db itself. Zero hits.
                        No personal .env is mounted; the key is bridge-injected
                        container env at run time only.
docker.sock:            ABSENT in the reviewer container
privileged:             NO (cap-drop ALL, no-new-privileges, read-only rootfs,
                        uid 1000, memory 1250m / memory-swap equal,
                        cpus 2, pids 256)
ports:                  0 new. The reviewer container publishes nothing; the
                        host's only published ports remain the pre-existing
                        Hermes 8642/9119 on the LAN address.
canonical secret scan:  PASS (0 hits on every job reviewed this session)
tirith:                 UNAVAILABLE — the binary is not installed on this host
                        or in the Hermes container. Reported separately from
                        the canonical bridge secret scanner; NOT counted as
                        PASS.
```

## Pi Resources

```text
peak RAM:               ~1.29 GiB of 3.7 GiB used host-wide during a run
                        (implementation container ~186 MiB of its 1.221 GiB
                        limit; Hermes ~748 MiB of 2 GiB)
swap:                   0 MB used throughout (2 GiB available, never touched)
load average:           < 0.4 throughout
max simultaneous workers: 1. job_start / review_start / job_verify all return
                        "busy" while any job or review is active, so
                        implementation and review never overlap and the Chief
                        of Staff is never starved.
```

## Existing System Regression

```text
Provider:        config model default z-ai/glm-5.3-flash, gateway running
LINE:            line_knowledge MCP configured, module compiles
Event:           event_layer MCP configured, module compiles
Jobs:            jobs_supervisor MCP loads; 12 typed tools
                 (job_create/get/list/status/dispatch/continue/validate/
                  cancel/reconcile + job_review/job_get_review/
                  job_list_reviews). No generic shell / model / prompt tool.
Morning:         chief-morning-review cron enabled (30 23 * * *)
Nightly:         chief-nightly-reflection cron enabled (0 13 * * *)
Watchdog:        chief_watchdog.sh cron enabled (0 */6 * * *)
LINE refresh:    line_refresh_daily.sh cron enabled (15 18 * * *)
API:             gateway_state = running, api_server = connected
Dashboard:       ports 8642 / 9119 bound to the LAN address, unchanged
Docker root:     /srv/hermes/docker (unchanged)
RequiresMountsFor: RequiresMountsFor=/srv/hermes drop-in present (unchanged)
```

## Remaining Risks

- `diff_hash` is the empty-diff constant on this worker topology, because the
  coding agent never commits (`base_sha == target_sha`). `content_sha` is the
  binding that actually detects change; the diff-hash equality check is kept
  so the gate stays correct for a worker that does commit. This is stated
  plainly rather than reported as a passing check that means something.
- Reviewer quality data is a handful of samples. Nothing about long-run
  reviewer suitability can be concluded from it yet.
- The reviewer needs outbound HTTPS to the CommandCode API. That egress is
  unchanged from F.1.1-Pi and remains the known residual risk in the threat
  model; there is no inbound surface.
- Implementation and reviewer still share one CommandCode account and key
  (MVP policy, unchanged). Separation is a later decision once real coding
  usage has been measured.
- `tirith` is absent, so its pre-exec scanning contributes nothing here.
- **Fixture repositories left registered.** The repository registry (both
  `bridge/config.json` and `worker_config.json`) now carries five entries:
  `coding-worker-smoke-test` and `fix-loop-fixture` from earlier phases, plus
  `f2-fixloop`, `f2-loop2` and `f2-loop3` added this session. All are
  disposable toy repositories with `allow_simulated: false` and scoped
  `allowed_paths`; none contains personal data and none is deployed. Note
  that `f2-fixloop` is **not a clean baseline** — it holds eight commits of
  reviewed-and-landed agent output from this session's fixtures, so a future
  job against it starts from that state. `f2-loop2` and `f2-loop3` were
  single-use and can be deregistered whenever convenient.
- The fix loop's ability to converge inside the default agent window depends
  on the size of the fix, not on the review machinery (see Final Readiness).

## Reviewer Recommendation

実測結果から:

```text
meta/muse-spark-1.2 (the STANDARD variant actually deployed here, not the
spec's meta/muse-spark-1.2-contributor) is suitable as the default reviewer.
```

Grounds, kept narrow because the sample is small (13 review runs this
session, 22 review records in total):

- It produced valid, schema-conforming JSON on the first attempt every time;
  the repair budget was never exercised.
- Its verdicts were correct in both directions. Where it approved, two
  independently written oracles later confirmed the implementation met the
  contract. Where it blocked, the findings were real: a task delivered at
  2/16 completeness that the deterministic gate had accepted, a paraphrased
  error message and parameter name, a missing `__all__`, and — on a separate
  job — a regression it *predicted* at confidence 1.00 by reading a consumer
  the change would break.
- It does not rubber-stamp a green test run. It filed a `verification_gap`
  finding against the registered suite itself on two different jobs, and its
  approval summaries enumerate the individual clauses it checked and the
  concrete values it evaluated.
- Its one non-blocking edge-case finding this session (`isinstance(values,
  Iterable)` rejecting legacy `__getitem__`-only iterables) was correct, and
  it stated it had verified the behaviour by executing a test class.

No false positive was observed this session. The one historical
`CHANGES_REQUIRED` that looked like one (`rev_a307187ff7`) was a fixture
defect, not a reviewer defect.

This is a recommendation on the evidence available, not a conclusion about
long-run quality. The reviewer model is **not** auto-switched on any of these
numbers, and it should not be changed without a comparable measurement.

## Final Readiness

```text
Phase F.2 completed.

Production coding jobs now require:

GLM-5.3-Flash implementation
+
deterministic implementation acceptance
+
independent Muse Spark 1.2 review
+
exact-revision approval
+
final deterministic validation

before COMPLETED.

CONDITION ON THIS CLAIM: the fix loop was demonstrated end to end at a RAISED
agent budget — agent window 2700 s, hang watchdog 1800 s, review cycles 5.
At the restored production defaults (1200 s / 900 s / 3 cycles) the same job
stopped correctly at WAITING_HUMAN, because each fix attempt was killed
before it could write. The review, binding and completion machinery is
unaffected by that budget; the ability of THIS model to finish a fix inside
the default window is. Both settings are back at their documented defaults.
```

The loop below was executed end to end on the Pi with real API calls in both
roles (`job_25ece703f8`):

```text
Task
  -> GLM-5.3-Flash implementation
  -> deterministic validation PASS
  -> IMPLEMENTATION_ACCEPTED
  -> independent Muse Spark 1.2 review     -> CHANGES_REQUIRED (4 blocking)
  -> GLM-5.3-Flash fix   (content binding moved: 2085f284… -> 0da1f2e0…)
  -> deterministic validation PASS
  -> fresh Muse Spark 1.2 review           -> APPROVE
  -> exact target SHA + exact content binding + exact diff hash
     + final tests + secret scan + cancel guard + CAS
  -> COMPLETED
```

and the isolation invariants were measured, not assumed:

```text
Muse Reviewer  X  implementation session      (transcript / final prose /
                                               agent-home all absent from the
                                               review package and container)
Muse Reviewer  X  implementation worktree RW  (six write attempts, all failed;
                                               snapshot writes discarded and
                                               verified not to propagate)
Muse Reviewer  X  personal Hermes data        (/opt/data, /srv/hermes,
                                               /home/<PI_USER> absent)
Approval for SHA A  X  modified SHA B         (target SHA, content binding and
                                               diff hash each independently
                                               invalidate an approval)
```

### One operational note the operator should know

The production agent window (1200 s, 900 s hang watchdog) is tight for this
Pi: a fix attempt that has to write more than a few dozen lines can spend the
whole window planning and be killed before it writes anything. That is what
cycles 2 and 3 of the fix-loop job recorded as `content_progress = unchanged`,
and the loop correctly stopped at `WAITING_HUMAN` rather than grinding. The
run was completed by raising the window to 2700 s (hang watchdog 1800 s) and
the review-cycle budget to 5 for that job, then `job_continue`; container
resource limits and the combined concurrency of 1 were not touched, and both
settings were restored to their documented defaults afterwards.

Nothing in F.2 depends on the larger window. But if production coding tasks
are expected to be more than a few hundred lines, raising `agent_timeout_s`
is the change to consider — the review loop already reports exactly when it
is needed.
