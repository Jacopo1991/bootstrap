Verdict: FAIL. The two requested fixes are in place, but the round-2 strict-block layer overrides the explicit first-verdict rule and rejects ordinary plain-text evidence.

**What meets the request**
- The Claude reviewer command adds `--add-dir <kb>` only when the task has a Backlog file (`Task.file`), keeps the read-only tool list, and adds `--permission-mode dontAsk`. The Codex command adds `--add-dir` with the kb path quoted.
- The `task check` advisory behaviour is untouched; the diff only changes the review functions and their tests.
- Markdown normalisation covers headings, list markers, numbering, bold/italic, backticks, trailing spaces, and table pipes. The first verdict line after normalising is used.
- The four fixtures are byte-identical to `.work/real-reviews/`. I checked each expected verdict, commit, vendor and criterion result in the tests against the fixture text, and they match.

**What does not meet it**
1. **Strict block overrides the first-verdict rule.** When a ```` ```review ```` fence exists anywhere in the reply, `parse_review_reply` returns the block and ignores every earlier `Verdict:` line. `test_review_record_prefers_strict_fenced_block` asserts exactly that: a `FAIL` before the fence is overridden by `PASS` inside it. The request said to take the first verdict line after normalising.
2. **Strict block rejects plain evidence with common characters.** The strict regexes exclude `` ` ``, `*`, `_`, `~` and `|` from evidence and from the reviewer field. Evidence such as `executable_task:533` would be rejected with a misleading "Markdown criterion line" error. Repo file names with underscores are common.
3. **Numbered findings can become criterion verdicts (low severity).** The fallback maps any `N. Pass/Fail:` item to criterion N when N is pinned. A findings list item such as `3. **Fail:** ...` with no `#3` line would be recorded as criterion 3's verdict.
4. **Not verified:** `--permission-mode dontAsk` on Claude Code 2.1.296, and `--add-dir` on `codex exec`. Running `claude --help`, `codex exec --help` and a web lookup was denied under the session's permission mode, and I did not route around that. The session is itself in don't-ask mode, which fits the mode name, but the flag support is unconfirmed.
5. **Not re-run:** I could not run `ci/task-test.py` or `ci/managed-policy-test.py` (review is read-only and shell calls were denied), so the lane's pass counts are unverified by me.

**Scope note:** The round-2 strict-block layer and the reviewer prompt's fenced-template change are not in the request text I was given. The lane's `report.md` describes them, but I have no instruction that asked for them, so they need the PM's confirmation.

Reviewed commit: `b85ccb9d8c26080884670ca87c2fd6cd9ae5b998`.

```review
Verdict: FAIL
Commit: b85ccb9d8c26080884670ca87c2fd6cd9ae5b998
Reviewer: claude haiku-5-5
```
