---
name: verify-ui
description: Independently verify a built UI against a task's acceptance criteria with Playwright. Use when asked to verify, accept or black-box test a task's work (role A authors the acceptance tests, role B drives the running product and issues the verdict), never for the lane that built it.
---

# verify-ui

Black-box verification of a running product against a task's acceptance criteria, on Playwright. This is the method from SPEC-0015 (the old oracle and blackbox verification, rebuilt on maintained tooling).

## Independence

- You are never the lane that built the work, and you never read its implementation. Your inputs are the task's acceptance criteria and the running product. Nothing else.
- Default pairing: a different vendor from the builder (Codex verifies Claude's work and the other way round), or an orw worker on a cheap model for repeat runs. If neither is available, use a fresh session, never the builder's own.
- If you cannot decide a criterion without the source, the criterion is underspecified. Mark it `underspecified` and say what is missing. Do not open the code.

## Roles

One session may play A then B, or two sessions may split them. The verifier never skips A: acceptance tests are derived from the criteria, not from the product's behaviour.

**Role A, author.** Turn each acceptance criterion into executable tests in the knowledge repository's `acceptance/` (set up once by `verify-enable <knowledge-repo>`).

- Tag every test with its task and criterion, for example `{ tag: '@T-4-AC2' }`.
- Create real conditions with network interception you control: `page.route(...)` for slow, failing, empty or offline backends. Do not rely on the product happening to be in that state.
- Use `expectNoA11yViolations(page, testInfo)` (axe-core) where a criterion touches accessibility.
- Tests run on both desktop and phone viewports. Prefer role, label and text locators over CSS or test ids so the tests do not depend on the implementation.
- Build lanes work in the code repository and cannot edit these tests; the PM merges them.

**Role B, verify.** Act on the running product as a user would.

1. Explore it with the Playwright MCP server (`playwright`, registered at user scope): drive the flows, take accessibility snapshots, read console and network.
2. Run the suite: `just verify <app-url>`, or one criterion with `just verify <app-url> @T-4-AC2`. All tests run; a failure does not stop the rest.
3. Judge every criterion. Reproduce every failure by hand and write the steps down.

## Verdict

- Every criterion gets `pass`, `fail` or `underspecified`. All of them are run; no short-circuit after the first failure.
- A `fail` and an `underspecified` both need a reproduction (steps, expected, actual) or the missing information.
- "It compiled", "the page loaded" or "the suite is green" is never a pass by itself: a criterion passes only when a test or an observation checks the behaviour it describes.
- `overall` is `pass` only if every criterion is `pass`.

Write `verdict.json` into the run's evidence directory:

```json
{
  "task": "T-4",
  "run": "20261005T120000Z",
  "url": "http://localhost:3000",
  "verifier": { "seat": "codex", "model": "model-name", "roles": ["A", "B"] },
  "builder": { "seat": "claude" },
  "attestation": {
    "inputs": ["acceptance criteria of T-4", "running product at the url"],
    "read_source": false,
    "statement": "Verified from the acceptance criteria and the running product only; no implementation was read."
  },
  "criteria": [
    {
      "id": "T-4-AC1",
      "result": "pass",
      "evidence": ["results.json", "artifacts/example-desktop/trace.zip"],
      "reproduction": null,
      "notes": ""
    },
    {
      "id": "T-4-AC2",
      "result": "fail",
      "evidence": ["artifacts/checkout-phone/test-failed-1.png"],
      "reproduction": "1. Open /cart on a 412x915 viewport. 2. Press Pay with the payment API returning 500. Expected an error message; the page stayed blank.",
      "notes": ""
    }
  ],
  "overall": "fail"
}
```

`read_source` must be `false`. If it would be `true`, you are not the verifier: stop and report that.

## Evidence

Everything goes under `~/project-data/<project>/verification/<run>/`, outside the repositories (`just verify` creates `<run>`, a UTC timestamp, and prints the path):

- `results.json`: the Playwright JSON report, every test with its tags and result.
- `artifacts/`: traces and screenshots for failed tests, axe results as attachments.
- `verdict.json`: yours, as above. Screenshots, accessibility snapshots and console or network captures you take by hand through the MCP server go here too, referenced from `evidence`.

Report in chat: the run directory, the overall verdict, and each failed or underspecified criterion with its reproduction. The PM records the verdict on the task. Browsers are already installed in the shared cache; if the browser is missing, report that instead of downloading one.
