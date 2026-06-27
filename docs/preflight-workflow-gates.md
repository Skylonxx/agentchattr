# Preflight Workflow Gates (`agentchattr`)

> **Canonical policy** for mandatory process-hygiene preflight in the `agentchattr` change pipeline.
> Role-specific summaries live in `docs/ai-roles/`; this document is the source of truth.

---

## 1. Purpose and Non-Goals

### Purpose

The preflight checker (`python -m preflight`) is a **read-only, fail-closed process-hygiene reporter**
used at workflow gate points. It validates git state, and (when the manifest requires it) runtime
state such as wrappers, channels, sessions, sandbox config, and port listeners — **without mutating
anything**.

Preflight supports:

- Halting unsafe phases before they proceed
- Capturing standardized evidence in phase reports
- Separating git-only gates from live-validation gates

### Non-Goals

Preflight is **not**:

- Production readiness certification
- Live validation or smoke-test success
- An auth oracle or credential probe
- Authorization to commit, push, start sessions, or mutate runtime
- A replacement for **CodexSafe**, **INV-007**, RBAC, or the `#general` prohibition
- Permission for agents to auto-remediate, auto-clean, auto-auth, or auto-push

---

## 2. Manifests

| Manifest ID | Scope | Runtime checks |
|---|---|---|
| `READ_ONLY_AUDIT` | Git hygiene only | Skipped |
| `CODEX_REVIEW_DIRTY_TREE` | Git + manifest-scoped dirty-tree allowlist | Skipped (sandbox flow guard still runs) |
| `COMMIT_EXACT_FILES` | Git + exact dirty/untracked file set | Skipped (sandbox flow guard still runs) |
| `PUSH_ONLY` | Git + pre-push sync/ahead count | Skipped |
| `E4C_SDLC_LIVE` | Full E4C SDLC live matrix | Required |

CLI invocation (Windows):

```
.\.venv\Scripts\python.exe -m preflight --phase <MANIFEST_ID> --format json
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | `PASS` — manifest-scoped checks passed |
| `1` | `BLOCKED` — halt current phase |
| `2` | Internal error — halt; escalate to fix authorization |

---

## 3. Phase-to-Manifest Mapping Matrix

| Phase type | Recommended manifest | Timing | Required format | PASS action | BLOCKED action | Remediation allowed? | Notes |
|---|---|---|---|---|---|---|---|
| Read-only audit / planning | `READ_ONLY_AUDIT` | Before audit begins; repeat in final report | JSON canonical + human optional | Proceed with read-only audit | Halt; report hygiene issue | No | No runtime checks |
| Bounded implementation patch | `READ_ONLY_AUDIT` | At phase start only | JSON canonical | Proceed with authorized patch | Halt start if baseline dirty/wrong branch/sync | No | Tree becomes dirty during work; do not re-run until Codex gate |
| Dirty-tree Codex review | `CODEX_REVIEW_DIRTY_TREE` | Immediately before Codex submission | JSON canonical + human optional | Submit diff to Codex reviewer | Halt Codex submission | No | Requires phase-specific allowlist — see §10 |
| Fix / revision patch (R1) | `READ_ONLY_AUDIT` at start; `CODEX_REVIEW_DIRTY_TREE` before re-review | Same as implementation + Codex | JSON canonical | Proceed within R1 scope | Halt per failing gate | No | R1 authorization must list exact files |
| Commit authorization | `COMMIT_EXACT_FILES` | Immediately before exact-file staging | JSON canonical | Proceed to staging/commit if separately authorized | Halt commit | No | Requires populated allowlist — see §10 |
| Push authorization | `PUSH_ONLY` | Immediately before `git push origin main` | JSON canonical | Proceed to push if separately authorized | Halt push | No | Confirms exactly one commit ahead, clean tree |
| Live-manifest preflight dogfood | `E4C_SDLC_LIVE` | Controlled dogfood phase only | JSON + human | Document PASS or BLOCKED as evidence | Document BLOCKED as true-negative; no remediation | No | BLOCKED may be valid dogfood evidence |
| Actual live validation | `E4C_SDLC_LIVE` | Before live session authorization | JSON canonical | Authorize live session in **separate** memo | Halt live session authorization | Yes — only in separately authorized runtime-prep phase | PASS required before live SDLC |
| Closure authorization | `READ_ONLY_AUDIT` | In closure report | JSON summary | Close phase if all prior gates satisfied | Halt closure | No | Confirms clean synced tree |
| Emergency / hotfix | `READ_ONLY_AUDIT` minimum; `PUSH_ONLY` before push | Owner-declared gate points | JSON canonical | Proceed only within expedited scope | Halt; escalate to Owner | Only if Owner authorizes separate remediation | Full E4C still required before live validation |
| Template / runbook / docs patch | `READ_ONLY_AUDIT` | Phase start + final report | JSON canonical | Proceed with docs-only patch | Halt | No | Protected paths blocked at commit unless allowlisted |
| Agent role / config / tooling patch | `READ_ONLY_AUDIT` at audit; `COMMIT_EXACT_FILES` at commit | Audit start; pre-commit | JSON canonical | Proceed within authorized files | Halt | No | `config.toml`, `docs/ai-roles/`, templates need explicit allowlist at commit |

### Manifest selection rules

1. **One manifest per gate point** — do not substitute a weaker manifest for a stronger gate.
2. **Never use `READ_ONLY_AUDIT` before push** — use `PUSH_ONLY`.
3. **Never use `PUSH_ONLY` before commit** — ahead-count check will fail.
4. **Never use `E4C_SDLC_LIVE` for read-only or commit gates** — false blockers or false confidence.
5. **Never use `READ_ONLY_AUDIT` when the tree must be intentionally dirty for Codex** — use `CODEX_REVIEW_DIRTY_TREE`.

---

## 4. Verdict Semantics

### PASS

`PASS` means **only**:

- All checks required by the named manifest for the current authorized phase passed
- The executor may proceed **within the already authorized phase scope**
- Evidence must be captured in the report `Preflight Evidence` section

`PASS` does **not** mean:

- Production or V2 readiness
- Live validation success
- Permission to start SDLC/relay-dryrun sessions
- Permission to mutate runtime (server, wrappers, channels, sessions, sandbox audit)
- Permission to commit, push, or remediate
- Permission to skip Owner/Gemini/Tech Lead authorization or the next pipeline gate

### BLOCKED

`BLOCKED` (exit code `1`) means:

- **Halt the current phase immediately**
- Capture JSON preflight output as canonical evidence
- Summarize blocked reasons (scrubbed) in the report
- Classify each reason (see §8)
- Do **not** auto-remediate, clean, stage, commit, edit config, start/stop processes, clear channels/sessions/audits, or push

Human output may include "Remediation (manual operator actions only)" hints. **These hints are not
authorization for agents to act.**

### Exit code 2 (internal error)

- Halt current phase; preserve evidence
- Do not retry with edits or config changes
- Recommend fix authorization (`READY_FOR_<phase>_R1_FIX_AUTHORIZATION`) or `BLOCKED` escalation

---

## 5. Standard Preflight Evidence Section

Every phase report that ran preflight **must** include:

```markdown
## Preflight Evidence

- command:
- manifest ID:
- baseline HEAD:
- verdict:
- exit code:
- output format:
- blocked reasons:
- checks summary:
- redaction status:
- preflight decision:
```

### Field rules

| Field | Rule |
|---|---|
| `command` | Exact CLI, e.g. `.\.venv\Scripts\python.exe -m preflight --phase READ_ONLY_AUDIT --format json` |
| `manifest ID` | Must match phase gate |
| `baseline HEAD` | Short SHA at run time |
| `verdict` | `PASS` or `BLOCKED` |
| `exit code` | `0`, `1`, or `2` |
| `output format` | `json` (canonical); note if human was also captured |
| `blocked reasons` | Summarized from `blocked_reasons[]`; scrubbed; `none` if PASS |
| `checks summary` | Counts and notable check IDs — **do not paste full JSON** |
| `redaction status` | Cite `redaction.self_test` result |
| `preflight decision` | `PROCEED within authorized scope` or `HALT — reason` |

### JSON vs human output

- **JSON (`--format json`)** is **canonical** evidence for reports and closure memos.
- **Human (`--format human`)** is secondary operator evidence; summarize, do not dump.
- Summarize JSON fields (`verdict`, `baseline`, `blocked_reasons`, check counts) — never paste long raw output.

---

## 6. Redaction Requirements

- Always cite `redaction.self_test` check result from preflight output.
- Summarize blocked reasons; scrub before inclusion in reports or chat.
- If token-like values appear, verify `[REDACTED_TOKEN]` / `[REDACTED]` scrubbing before publishing.
- If no token-like value appeared, state: *No token-like value appeared in output; redaction self-test was primary evidence.*
- Never paste auth URLs, session tokens, API keys, or bearer values.
- If raw secret observed in output: halt report publication and escalate.

---

## 7. BLOCKED Classification Policy

| Category | Definition | Allowed action | Required recommendation |
|---|---|---|---|
| Expected local runtime state | Environment not prepared; tool behaved correctly | Document; halt; no agent remediation | `BLOCKED` or accept true-negative (dogfood); request runtime-prep authorization |
| Operator remediation required | Owner/operator must act manually | Halt; operator acts in separate session; re-run in new authorized phase | `BLOCKED` + request remediation authorization |
| Tool / manifest defect | Wrong check, schema bug, exit code 2, JSON/human mismatch | Halt; preserve evidence | `READY_FOR_<phase>_R1_FIX_AUTHORIZATION` |
| Authorization mismatch | Wrong manifest or missing allowlist in phase memo | Halt; coordinator issues corrected authorization | `BLOCKED` until new memo |
| Unsafe / ambiguous evidence | Cannot classify; suspected secret leak | Halt; escalate | `BLOCKED` |

**No agent may remediate BLOCKED findings unless a separate phase memo explicitly authorizes the
specific remediation action.**

---

## 8. Role Responsibility Model

| Role | Preflight responsibilities |
|---|---|
| **Owner / Gemini (Tech Lead)** | Approve phase scope, manifest choice, allowlists, remediation/runtime-prep phases; binary APPROVED/BLOCKED on gate transitions |
| **ChatGPT Workflow Coordinator** | Select manifest per phase in authorization memos; require Preflight Evidence in reports; never authorize bypass; do not collapse preflight + implementation + commit + push into one memo |
| **Claude / Cursor Agent** | Run only authorized preflight CLI; capture JSON evidence; halt on BLOCKED; never auto-remediate; never treat human remediation hints as authorization |
| **Codex Reviewer** | Verify Preflight Evidence for Codex gate; manifest ID; dirty-file scope; missing evidence is a review finding; PASS is not production readiness |
| **CodexSafe** | Runtime safety boundary during live execution — **not replaced by preflight** |
| **AGY** | No preflight execution; not a safety gate |
| **Operator / manual owner** | Manual remediation (start/stop server, config.local, audit rotation) only in separately authorized phases |

---

## 9. Gate-Specific Verification Requirements

### Codex review (dirty tree)

Before Codex submission, verify:

1. `Preflight Evidence` present with manifest `CODEX_REVIEW_DIRTY_TREE`
2. Verdict `PASS` (or phase documents why not)
3. Dirty files match authorized scope and allowlist
4. No unauthorized files in `git diff --name-status`

### Commit authorization

Before exact-file staging:

1. `COMMIT_EXACT_FILES` preflight `PASS`
2. Dirty/untracked set exactly matches approved allowlist
3. No staged files before staging when configured

### Push authorization

Before `git push origin main`:

1. `PUSH_ONLY` preflight `PASS`
2. Exactly one commit ahead of `origin/main`; clean tree; no staged files

### Live validation

Before live session authorization:

1. `E4C_SDLC_LIVE` preflight `PASS` in a separately authorized phase
2. Preflight PASS does **not** authorize the session — only confirms runtime hygiene for the manifest scope

---

## 10. Allowlist Operational Gap (Fail-Closed by Design)

`CODEX_REVIEW_DIRTY_TREE` and `COMMIT_EXACT_FILES` **ship with empty allowlists** in the repository.
This is **intentional fail-closed behavior** — they BLOCK until a phase-specific allowlist is supplied.

Mandatory dirty-tree and exact-file gates therefore require **phase-specific allowlist injection**.
E5H does not implement this mechanism.

### Future E5I (proposed)

A future bounded phase should consider CLI or config design such as:

- `--manifest-dir` (load phase-specific manifest copy from a temp directory)
- `--allowlist-file` (inject approved paths without committing manifest state)
- Another explicit, non-committed, phase-scoped override mechanism

Until E5I is authorized and implemented:

- Workflow memos must **not** claim Codex/commit preflight gates can PASS without a supplied allowlist mechanism
- Coordinators must document the allowlist in phase authorization and note the E5I dependency when mandating those gates

---

## 11. Anti-Theater Policy

- Preflight `PASS` is necessary but **not sufficient** for any downstream gate.
- No phase may claim "preflight passed therefore production ready."
- No agent may use preflight human remediation hints as authorization to act.
- No bypass of CodexSafe, INV-007, RBAC, or `#general` prohibition based on preflight `PASS`.
- Dogfood phases may treat `BLOCKED` as valid true-negative evidence without remediation.
- Closure memos must state: **Preflight PASS confirms process-hygiene for the manifest scope only — not production readiness.**

---

## 12. Related Documentation

- `docs/ai-roles/workflow-coordinator-skill-profile.md` — coordinator preflight sub-protocol
- `docs/ai-roles/workflow-coordinator.md` — handoff expectations
- `docs/ai-roles/developer.md` — implementer preflight duties
- `docs/ai-roles/reviewer.md` — reviewer preflight verification
- `preflight_manifests/` — manifest definitions (read-only reference)
