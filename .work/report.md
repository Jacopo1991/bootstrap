# SPEC-0016 task report

## What changed

Pinned builder, base SHA, criteria and verification commands in `.work/task.json` with a revision hash. Added preflight checks, generated the single report template, and made `task check` run the gate, pinned `Verify:` commands and pre-commit step with per-step evidence and process-group timeouts. Added review prompt and recording, evidence-based merge/close gates, override exceptions, archive and repeat-merge handling, detached run locks/cancel/status, improvement and intervention notes, denial summaries, and hook denial logging. Updated the PM-only hook command list and generated project instructions. Added temporary Git repository and fake-tool coverage.

## How I verified

- `python3 ci/task-test.py` — PASS, 32 tests.
- `python3 ci/managed-policy-test.py` — PASS.
- `task check` — PASS; full `ci/lint.sh` gate completed.
- `git diff --check` — PASS.
- The first full gate attempt found a generated ignored `__pycache__` from syntax compilation; I removed that generated cache and the next full gate passed.

## Criteria

#1 met: `test_acceptance_1_merge_gate_lists_missing_evidence_and_override_is_visible` confirms all missing merge evidence is named, then an override merges and appears in status.
#2 met: no-Verify refusal and recorded `--no-verify` reason are tested; `test_acceptance_2_verify_is_required_pinned_and_runs_after_gate` proves commands are pinned and failing Verify fails the check; the T-22-shaped chained Verify test confirms it runs through `bash -c`.
#3 met: `test_acceptance_3_review_requires_head_other_vendor_and_every_criterion` rejects missing criterion judgment, wrong SHA and builder-vendor review.
#4 met: `test_merge_brings_main_in_merges_and_cleans_up` merges a moved default branch after the gate passes; `test_conflict_is_reported_not_forced` confirms conflicts stop safely.
#5 met: `test_acceptance_5_timeout_kills_process_group_and_repo_timeout_wins` uses a one-second `.task.toml` timeout and verifies the spawned process is gone.
#6 met: detached check locking, cancellation and interrupted status are covered by `test_acceptance_6_detached_check_lock_cancel_and_interrupted_status`; detached merge completion and repeat invocation are covered by `test_acceptance_6_detached_merge_finishes_and_rerun_observes_merge`.
#7 met: close tests confirm only reviewed-pass criteria are ticked, partial close requires the three-part retro, and noticed items are committed to `improvements.md` with the close.
#8 met: `ci/managed-policy-test.py` verifies denial entries omit arguments and that a broken log path still returns a deny decision.
#9 met: `test_check_gives_an_older_worktree_its_environment` migrates a pre-record worktree with `task start --refresh --builder` and then checks it.
#10 met: all 23 pre-existing task tests remain in the suite and pass alongside nine new acceptance tests; `ci/lint.sh` passed through `task check`.

## Noticed, not done

The status view does not yet include idle age or per-project open-improvement counts. Detached run records preserve process state and rerun checks actual Git state, but do not record each named merge substep as it completes.

## Stopped or blocked by

None.
