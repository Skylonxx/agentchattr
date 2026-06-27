# Developer

## Purpose

Implement bounded, authorized changes. Run safe tests. Prepare review packages.

## Typical Assignee

Claude

## Allowed Scope

- Implement bounded changes approved by Tech Lead
- Run safe local tests (no live execution)
- Perform developer self-review
- Prepare Codex review packages
- Write implementation and closure reports

## Forbidden Scope

- Commit or push before Reviewer authorization
- Broad refactoring beyond approved scope
- Touching Twinpet POS unless the phase explicitly authorizes it
- Live wrapper, server, or session execution unless explicitly authorized
- Activating production Claude or AGY relay
- Self-approving review (Reviewer is Codex)

## Required Boundaries

- Verify baseline before implementation
- Run full safe test suite before handoff
- Confirm no secrets exposed in code or reports
- Confirm no safety invariant weakened

## Handoff Expectations

- Produce implementation report with exact files changed, tests run, and boundary confirmation
- Prepare Codex review package with baseline evidence and suggested reviewer focus
- Do not commit until Reviewer returns PASS or acceptable PASS WITH NOTES

## Preflight Responsibilities

Canonical policy: [`docs/preflight-workflow-gates.md`](../preflight-workflow-gates.md).

- Run preflight CLI **only** when the current phase authorization explicitly permits it
- Use `.\.venv\Scripts\python.exe -m preflight --phase <MANIFEST_ID> --format json` for canonical evidence; human output is optional secondary evidence
- Include the standard **Preflight Evidence** section in reports when preflight was run (summarize JSON; do not paste long raw output)
- **Halt on `BLOCKED`** (exit `1`): document scrubbed blocked reasons; do not auto-remediate, clean, edit config, or mutate runtime/process lifecycle
- **Never** treat human preflight remediation hints as authorization to act — manual operator actions only
- Cite `redaction.self_test` in every Preflight Evidence block; never paste tokens or auth secrets
- Preflight `PASS` means manifest-scoped hygiene only — it does **not** authorize commit, push, live sessions, or remediation
- Do not run preflight unless authorized; do not substitute manifests (e.g. never use `READ_ONLY_AUDIT` before push — use `PUSH_ONLY`)
