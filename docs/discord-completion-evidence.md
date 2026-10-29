# Discord completion evidence

`src/commitments/discord_completion.py` checks active, dated commitments that
have an original Discord message. It marks one `DONE` only when the original
author directly replies in the same channel with an explicit completion
statement. A posted date, an unrelated message, and a question about whether
work is done do not qualify. The latest attributable direct reply must be the
explicit completion, and a source message linked to multiple active dated
commitments requires individual review.

The hourly system service in `src/commitments/systemd/` runs this strict check
before the morning brief. It reads the Discord archive and writes only the
canonical commitment database. Each completion stores the reply message ID as
`commitment_evidence` with role `completion`, the reply time as `resolved_at`,
and a field history entry. No Discord message body is copied into the
commitment database or the service log.

Some strong evidence is an outcome report without the word “completed”. Such
cases require operator review. The `confirm` subcommand verifies that the
supplied message directly replies to a source message by the same author in
the same channel. It defaults to a read-only preview; `--apply` records the
reviewed completion. Review the actual message before applying it. The
command never treats a passed date alone as proof.

The backfill path preserves an already completed commitment, while an
explicit admin reopen remains possible. The agenda projection omits completed
commitments, so they no longer appear as overdue items.

Both `scan` and `confirm` are safe to retry. The source reply uniquely
identifies the completion evidence, and a second application makes no change.

## Deployment

Runtime files: `src/commitments/` now matches the deployed package
(`/srv/hermes/hermes-data/tools/commitments/`), owner routing included, so the
whole directory can be deployed from this repository. The two files this
feature added are:

- `/srv/hermes/hermes-data/tools/commitments/discord_completion.py`
- `/srv/hermes/hermes-data/tools/commitments/store.py` (the 7-line backfill
  guard)

`reconcile-line.sh` runs `commitments.admin backfill` with
`PYTHONPATH=tools:hermes-data`, so the LINE backfill uses the guarded `store.py`.

### History

- 2026-09-25: first deployed as a user-level unit of the operator account
  (`~/.config/systemd/user/`), with no `User=` and only `NoNewPrivileges=`.
  It ran hourly with exit 0. The only `DONE` it ever recorded came from an
  operator `confirm --apply` (2026-09-24); automatic `scan` applied 0 for a
  month.
- 2026-10-27: moved to a dedicated, sandboxed system unit. The user unit is
  disabled; its files were retired into the staging directory and copied into
  the root-only backup.

### System unit (2026-10-27)

- Identity `hermescompletion`: system account, `nologin`, home `/nonexistent`,
  own group only. Not in `hermesreconcile`, `sudo`, `docker` or `adm`.
- DAC: named ACL entries only, no mask widened.
  `events/` `rwx` + default `rw-`, `events.db` `rw-`; Discord archive dir `r-x`
  + default `r--`, `messages.db{,-wal,-shm}` `r--`. **No entry on
  `/srv/hermes/hermes-data` itself**, so the account cannot reach `events.db`
  (or `.env`, `auth.json`, `config.yaml`) through the normal path.
- Why bind mounts: the Hermes agent calls `secure_parent_dir()` whenever it saves
  an auth store and re-tightens `HERMES_HOME` (`/srv/hermes/hermes-data`) to
  `0700`, overriding `HERMES_HOME_MODE=0710`. `chmod 0700` sets the ACL mask to
  `---`, which silently disables every named ACL on that directory. The unit
  therefore uses `BindPaths=` / `BindReadOnlyPaths=` (resolved by PID 1) to expose
  `events/` and `tools/commitments/` under `/opt/hermes-discord-completion/` and
  never traverses `hermes-data`.
- Sandbox: `ProtectSystem=strict`, `ProtectHome`, `PrivateNetwork`,
  `RestrictAddressFamilies=AF_UNIX`, `IPAddressDeny=any`, empty capability set,
  `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service ~@privileged`,
  `MemoryMax=250M`. `ExecStartPre=test -w/-r` fails fast if the ACL or mounts
  do not grant what the run needs.
- Timer: `*:07` Asia/Tokyo, clear of the Discord event-tick (`*/15`) that also
  writes `events.db`, and before the 08:30 brief.

Install / operate (`src/commitments/systemd/install-discord-completion-root.sh`,
run with sudo from the operator account):

```text
install   backup (events.db copy + integrity, ACL before/after, retired user unit),
          identity, ACL, mount points, units; isolation probe and a no-apply
          dry run inside the real unit sandbox. Does NOT enable the timer.
enable    refuses while the operator's user timer is active, then enables.
status    timer, last result, last journal lines.
rollback  removes the unit, the ACL entries and the identity; then re-enable
          the user unit from the retired copy if wanted.
```

### Verification (2026-10-27)

- Isolation probe inside the unit sandbox: 16 / 16 PASS. Denied: `.env`,
  `auth.json`, listing `hermes-data`, docker socket, `/etc/hermes-secrets`,
  `/home`, `AF_INET`, writes to the code bind and the Discord archive. Allowed:
  create+unlink in `events/`, `events.db` open `r+b`, `events.db` read +
  `quick_check ok`, WAL `messages.db` read-only.
- Outside the sandbox, `runuser -u hermescompletion -- test -r events.db` fails
  (no traversal of `hermes-data`), and the Discord archive is readable.
- Dry run in the sandbox: `{"applied": 0, "candidates": []}`, peak memory 12.1M —
  identical to the retired user unit.
- `systemd-analyze security`: 0.5 SAFE (line-reconcile 3.3, morning-brief 1.3).
- `events.db` `integrity_check ok`, `foreign_key_check` clean.

## Measured yield and options for widening (2026-10-27, counts only)

Population: 58 active, dated, current commitments with a Discord origin
(SHARED/BLOCKED 33, COUNTERPARTY/WAITING 20, SELF/ACTIONABLE 5); 57 are overdue,
all by more than 7 days and 30 by more than 30 days.

The Morning Brief `overdue` section is what the owner actually sees. Thanks to
owner routing it holds only SELF/ACTIONABLE items: 8 in total (5 shown), aged 9
to 53 days; 3 have a LINE origin, 5 a Discord origin.

| Rule | all 58 | the 5 visible Discord items |
|---|---|---|
| current: origin author's direct reply, explicit completion | 0 | 0 |
| B: origin author, same channel, later, explicit completion (7–14 d / 30 d) | 2 / 3 | 0 (any window) |
| C: origin author, same thread, explicit completion | 3 | 0 |
| direct reply by another author, explicit completion | 0 | 0 |
| any other author, same channel, later, explicit completion | 25 | 1 |

The last row fires on 43% of the population and is noise, not evidence. Rule B
also fires on 5 of 95 already-`CANCELLED` commitments, so its precision is not
established.

Conclusion: widening text evidence cannot clear the overdue items the owner
sees — none of them has attributable completion text in Discord, and 3 of 8 are
not from Discord at all. Keep the automatic path strict. Options that address
the visible problem, in order of expected effect:

1. Owner-explicit close from the brief. Number the overdue lines; a strict,
   deterministic DM command (for example `済 2`) is mapped through the persisted
   plan (`notification_id` + `plan_hash`) to one commitment and recorded as
   `DONE` with the DM message as `completion` evidence and `user_explicit`
   authority. Covers LINE-origin items too. Needs a narrow writer identity and a
   gateway command parser; no LLM in the path.
2. Display policy for stale overdue items: collapse items overdue by more than
   N days into one count line instead of listing them. No state change, so
   "a passed date alone is not proof" still holds. With N = 30 this collapses
   3 of the 8 today.
3. A weekly review digest listing overdue SELF items with ready-to-run
   `confirm` / admin commands. No new write path; the operator still uses SSH.
4. Rules B/C as suggestions only (never auto-apply). Measured yield 2–3 items,
   none of them visible in the brief; lowest value.
