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

### Commit authorization

Before exact-file staging:

1. `COMMIT_EXACT_FILES` preflight `PASS`
2. Dirty/untracked set exactly matches approved allowlist (via `--allowlist-file` sidecar — see §10)
3. No staged files before staging when configured
4. Sandbox flow guard satisfied — `sandbox.flow_start_enabled` must be `false` locally when manifest forbids flow (see §10)

### Codex review (dirty tree)

Before Codex submission, verify:

1. `Preflight Evidence` present with manifest `CODEX_REVIEW_DIRTY_TREE`
2. Verdict `PASS` (or phase documents why not)
3. Dirty files match authorized scope and allowlist (via `--allowlist-file` sidecar — see §10)
4. No unauthorized files in `git diff --name-status`
5. Sandbox flow guard satisfied when manifest forbids flow (see §10)

### Push authorization

Before `git push origin main`:

1. `PUSH_ONLY` preflight `PASS`
2. Exactly one commit ahead of `origin/main`; clean tree; no staged files

### Live validation

Before live session authorization:

1. `E4C_SDLC_LIVE` preflight `PASS` in a separately authorized phase
2. Preflight PASS does **not** authorize the session — only confirms runtime hygiene for the manifest scope

---

## 10. Allowlist Sidecar (`--allowlist-file`)

`CODEX_REVIEW_DIRTY_TREE` and `COMMIT_EXACT_FILES` **ship with empty allowlists** in the
repository. This remains **intentional fail-closed** when `--allowlist-file` is omitted.

For operational gates, supply a **phase-scoped sidecar JSON** via CLI:

```
.\.venv\Scripts\python.exe -m preflight `
  --phase COMMIT_EXACT_FILES `
  --allowlist-file C:\path\to\phase-allowlist.json `
  --format json
```

### Rules

- Official manifests in `preflight_manifests/` remain **fixed** — sidecar does not replace manifests.
- Sidecar may inject **only**:
  - `git.approved_dirty_paths` for `CODEX_REVIEW_DIRTY_TREE`
  - `git.approved_file_allowlist` for `COMMIT_EXACT_FILES`
- **No** `--manifest-dir`, env override, or stdin allowlist.
- **`force_add_paths` is outside preflight** — commit authorization memos handle exact force-add.
- Missing, invalid, mismatched, or unsafe sidecar → **BLOCKED** (exit `1`).
- Wildcards, absolute paths, drive/UNC paths, and `..` traversal are rejected.

### Sidecar schema

**CODEX_REVIEW_DIRTY_TREE:**

```json
{
  "manifest_id": "CODEX_REVIEW_DIRTY_TREE",
  "authorization_phase_id": "TOOLING-AGENTCHATTR-V2-EXAMPLE-CODEX-REVIEW",
  "approved_dirty_paths": [
    "docs/preflight-workflow-gates.md"
  ],
  "notes": "Example dirty-tree review allowlist"
}
```

**COMMIT_EXACT_FILES:**

```json
{
  "manifest_id": "COMMIT_EXACT_FILES",
  "authorization_phase_id": "TOOLING-AGENTCHATTR-V2-EXAMPLE-COMMIT",
  "approved_file_allowlist": [
    "docs/preflight-workflow-gates.md"
  ],
  "notes": "Example exact-file commit allowlist"
}
```

Optional: `notes` (scrubbed in output if needed). Unknown keys fail closed.

### JSON evidence

When `--allowlist-file` is used, JSON output includes:

```json
"allowlist": {
  "source_basename": "phase-allowlist.json",
  "authorization_phase_id": "TOOLING-...",
  "fields_applied": ["approved_file_allowlist"],
  "path_count": 5
}
```

Human output includes a concise `Allowlist: …` line. Full user profile paths are not exposed.

### Ignored files

If an allowlisted path is gitignored, preflight may still BLOCK on exact-set mismatch. Preflight
does **not** auto-stage or authorize force-add — that remains a separate commit-operator step.

### Sidecar storage convention

- Sidecars must live **outside the repository**.
- Recommended directory:

```
C:\Users\Narachat\OneDrive\Ai-Report\claude\allowlists\
```

- Never commit sidecars to git.
- Never store sidecars under the repo tree.
- Treat sidecars as **operator evidence artifacts** alongside phase reports under Ai-Report.

### Naming convention

Recommended basename pattern:

```
{phase-short-id}-{gate}-allowlist.json
```

Examples:

```
e5j-commit-allowlist.json
e5l-codex-allowlist.json
e5l-commit-allowlist.json
```

Guidance:

- Include gate intent in the basename (`commit`, `codex`, etc.).
- Set `authorization_phase_id` inside the JSON to the coordinator memo ID or full approved phase string (e.g. `TOOLING-AGENTCHATTR-V2-E5L-SIDECAR-OPERATIONALIZATION-DOCS-PATCH`).

### Lifecycle policy

- Create a **fresh sidecar** per authorized phase/gate — one sidecar per commit or Codex authorization.
- Do **not** reuse stale sidecars after dirty tree, file list, or authorization memo changes.
- Retain sidecars as audit evidence under Ai-Report until phase closure.
- Archive or delete only after closure if Owner chooses.
- Authorization memos must cite the **exact sidecar path** used for preflight dogfood.

### Windows encoding (UTF-8 without BOM)

- Write sidecar JSON as **UTF-8 without BOM**.
- A UTF-8 BOM causes `allowlist.invalid_json` and preflight `BLOCKED`.
- Windows PowerShell/editor defaults may add BOM depending on method and version.
- Prefer a BOM-free writer. Example using Python (no secrets):

```
@'
{
  "manifest_id": "COMMIT_EXACT_FILES",
  "authorization_phase_id": "TOOLING-AGENTCHATTR-V2-EXAMPLE-COMMIT",
  "approved_file_allowlist": [
    "docs/preflight-workflow-gates.md"
  ],
  "notes": "Example exact-file commit allowlist"
}
'@ | python -c "import sys, pathlib; pathlib.Path(r'C:\Users\Narachat\OneDrive\Ai-Report\claude\allowlists\example-commit-allowlist.json').write_text(sys.stdin.read(), encoding='utf-8')"
```

### Sandbox flow prerequisite

- `COMMIT_EXACT_FILES` and `CODEX_REVIEW_DIRTY_TREE` may still enforce **sandbox checks** from the official manifest.
- The sidecar allowlist does **not** bypass sandbox checks.
- If local `sandbox.flow_start_enabled = true`, commit/Codex preflight may `BLOCKED` on `sandbox.flow_enabled`.
- `config.local.toml` must remain **ignored/uncommitted** — never stage or commit it.
- Disabling or re-enabling sandbox flow requires **explicit authorization** in a separate phase memo.
- Default local posture for commit/Codex preflight work:

```toml
[sandbox]
flow_start_enabled = false
```

(or equivalent existing TOML nesting in `config.local.toml`).

### Local sandbox restoration policy

- Re-enable sandbox flow (`flow_start_enabled = true`) only under an **explicit sandbox-flow or live dogfood authorization**.
- Do not restore it casually while commit/Codex gates are in flight.
- A restoration memo must state:
  - target key (`flow_start_enabled`)
  - target value (`true` or `false`)
  - reason for change
  - whether commit/Codex gates are currently in flight
- Leaving `flow_start_enabled = false` is preferred during commit/Codex preflight work.
- Turning it `true` too early can cause unexpected `COMMIT_EXACT_FILES` or `CODEX_REVIEW_DIRTY_TREE` `BLOCKED`.

### Force-add separation

- `force_add_paths` remains **outside preflight** — sidecar schema rejects it.
- A sidecar allowlist is **not** force-add authorization.
- Gitignored but tracked paths (e.g. under `docs/`) may require **separate explicit force-add authorization** in the commit memo.
- If `git add` refuses an approved path because of ignore rules: **halt**, do not use `git add -f`, request separate force-add authorization.

### Manual sidecar creation vs future automation

- **Manual sidecar creation remains acceptable** — operators author JSON per authorization memo.
- **Helper tooling is deferred** — no in-repo generator in this phase.
- Any future helper that writes or validates sidecars must be a **separately authorized tooling phase** and reviewed separately from docs or preflight behavior changes.

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
