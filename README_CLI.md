# agentchattr CLI Architecture Guide

This guide explains how to launch agent **identities**, how `.bat` shortcuts differ from direct `wrapper.py` execution, and how session **roles** map to those identities. It is written for operators running multi-agent sessions on Windows (the same concepts apply on Mac/Linux with `macos-linux/*.sh` launchers).

**Important:** Launching an identity connects an agent to the chat room and session orchestration. It does **not** grant workspace filesystem write access. Workspace access is governed separately by the workspace policy phases (see [Workspace policy boundary](#workspace-policy-boundary) below).

---

## Purpose

agentchattr coordinates multiple AI agents through:

1. A **chat server** (`run.py` / `start.bat`) on port 8300.
2. **Wrappers** (`wrapper.py`) that run each agent CLI and auto-inject @mention prompts.
3. **Role identities** configured in `config.toml` under `[agents.*]` — e.g. `codex_coordinator`, `codex_reviewer`, `claude`, `agy`, `codexsafe`.
4. **Sessions** that bind template roles (coordinator, developer, reviewer, …) to online identities via the Start Session cast.

You must launch the **correct identity** for each session role. A single generic Codex process does not satisfy every cast slot.

---

## `.bat` wrappers vs direct `wrapper.py`

### What `.bat` files are

Files under `windows/` are **convenience launchers**. They typically:

- `cd` to the repo root (`windows/` → `..`)
- Create/activate `.venv` if missing
- Start the server if port 8300 is not listening
- Run one line like `python wrapper.py <identity> [-- extra args]`

They may hardcode **identity**, **model**, or other agent flags. **Always inspect the file** before assuming what it launches:

```powershell
cd C:\tools\agentchattr\repo
type .\windows\start_codex.bat
type .\windows\start_codexsafe.bat
type .\windows\start_agy.bat
```

### What direct `wrapper.py` is

Direct execution is **explicit** — you choose the identity and any forwarded agent arguments:

```powershell
cd C:\tools\agentchattr\repo
.\.venv\Scripts\python.exe wrapper.py codex_reviewer -- --model gpt-5.5
```

Use direct `wrapper.py` when:

- A session cast requires a **split Codex identity** (`codex_coordinator` / `codex_reviewer`) — there are **no** `.bat` files for those identities today.
- You need a specific `--model` or other agent flag not baked into a `.bat`.
- You are debugging which identity is actually running.

### Example: `start_codex.bat` vs reviewer

`windows/start_codex.bat` ends with:

```bat
python wrapper.py codex -- --model gpt-5.5
```

That launches the **`codex`** identity (generic Codex), **not** `codex_coordinator` or `codex_reviewer`. For coordinator-loop sessions you usually need **two separate Codex identities** online at once — launch each explicitly:

```powershell
.\.venv\Scripts\python.exe wrapper.py codex_coordinator -- --model gpt-5.5
.\.venv\Scripts\python.exe wrapper.py codex_reviewer -- --model gpt-5.5
```

### Wrapper-only flags (before `--`)

`wrapper.py` accepts its own flags, for example:

- `--data-dir`, `--port`, `--mcp-http-port`, `--mcp-sse-port`, `--upload-dir`
- `--no-restart`, `--label`

These affect **wrapper/server connection**, not the underlying Codex/Claude/AGY CLI. Put them **before** `--`.

---

## Role identities

### Config identities vs session roles

| Session template role | Typical identity to launch | Notes |
|----------------------|----------------------------|--------|
| `coordinator` | `codex_coordinator` | Workflow routing; must not self-review |
| `developer` | `claude` | Implementation author |
| `ui_lead` | `agy` | UI/UX review (`store_exec` / print mode) |
| `reviewer` | `codex_reviewer` | Independent engineering review |
| `safety_gate` | `codexsafe` | Boundary / safety verdict only |

The `[roster]` section in `config.toml` maps external workflow names to identities:

```toml
[roster]
developer = "claude"
reviewer = "codex"
ui_lead = "agy"
runtime_coordinator = "codex_coordinator"
runtime_reviewer = "codex_reviewer"
safety_guard = "codexsafe"
```

Session **cast** uses template role names; the UI/engine resolves them to the agents you assign. Those assignments must use **distinct identities** where required.

### Why coordinator and reviewer must differ (INV-007)

The session engine enforces **anti-self-review**: one agent identity cannot occupy both an **authoring/coordinator** role and the **reviewer** role in the same session. If both map to the same identity (e.g. both `codex`), session start fails with guard **INV-007**.

That is why `codex_coordinator` and `codex_reviewer` exist as **separate** `[agents.*]` entries sharing the same underlying `command = "codex"` but registering as different online instances.

### Identity reference

| Identity | Underlying CLI | Typical `cwd` (from config) | `run_mode` |
|----------|----------------|----------------------------|------------|
| `claude` | `claude` | `..` | (TUI default) |
| `codex` | `codex` | `C:/tools/agentchattr-scratch` | `exec` |
| `codex_coordinator` | `codex` | `C:/tools/agentchattr-scratch` | `exec` |
| `codex_reviewer` | `codex` | `C:/tools/agentchattr-scratch` | `exec` |
| `codexsafe` | `codex` | `C:/tools/agentchattr-scratch` | `exec` |
| `agy` | `agy` | `C:/tools/agentchattr-scratch` | `store_exec` |

`codexsafe` is a **boundary guard**, not a workflow coordinator. Do not cast it as developer or coordinator.

---

## Direct wrapper command anatomy

```powershell
.\.venv\Scripts\python.exe wrapper.py codex_reviewer -- --model gpt-5.5
```

| Part | Meaning |
|------|---------|
| `.\.venv\Scripts\python.exe` | Repo virtualenv Python (matches what `.bat` launchers activate) |
| `wrapper.py` | agentchattr wrapper: registers identity, MCP, queue watcher |
| `codex_reviewer` | **Agent identity key** from `config.toml` `[agents.codex_reviewer]` |
| `--` | Separator: left = wrapper args, right = forwarded to underlying CLI |
| `--model gpt-5.5` | Passed through to `codex exec` (or store path) as launch args |

After startup, the wrapper prints `Registered as: codex_reviewer (slot …)` — use that to confirm the identity.

---

## Double dash (`--`) routing

agentchattr uses Python `argparse` in `wrapper.py`:

- Arguments **before** `--` are parsed as **wrapper** options (`--port`, `--data-dir`, …).
- Arguments **after** `--` are collected as **extra launch args** and appended to the provider command (e.g. Codex `--model`, AGY `--model "Gemini 3.1 Pro (High)"`).

`config_loader.apply_cli_overrides()` also scans `sys.argv` **only before** the first `--`. That prevents agent flags like `--port` on the Codex CLI from being mistaken for server overrides.

**Without `--`:** Unknown flags may still land in `extra` via `parse_known_args()`, but you lose the explicit boundary and risk future wrapper flags conflicting with agent flags. **Always use `--`** when forwarding model or provider options.

Example with wrapper flag + model:

```powershell
.\.venv\Scripts\python.exe wrapper.py codex_coordinator --port 8300 -- --model gpt-5.5
```

---

## Common mistakes

1. **Assuming `start_codex.bat` launches `codex_reviewer`.** It launches `codex` with `--model gpt-5.5`. Reviewer sessions need `codex_reviewer` explicitly.

2. **Only one Codex window for a multi-Codex cast.** Coordinator-loop templates need coordinator **and** reviewer identities online simultaneously (plus claude, agy, codexsafe as required).

3. **Same identity for coordinator and reviewer.** Causes INV-007 session start failure.

4. **Passing `--model` without `--`.** Fragile; may confuse wrapper/config override scanning. Use: `wrapper.py <identity> -- --model …`.

5. **Confusing model name with role identity.** `gpt-5.5` is a model flag; `codex_reviewer` is the registered agent identity. They are independent.

6. **Assuming one online Codex satisfies every cast role.** The cast picker binds **roles → identities**. Each role needs its assigned agent online.

7. **Using `start_codex_bypass.bat` for normal sessions.** That variant uses `--dangerously-bypass-approvals-and-sandbox` — not appropriate for governed workflow sessions.

---

## Safe troubleshooting checklist

1. **Confirm repo state (read-only):**

   ```powershell
   cd C:\tools\agentchattr\repo
   git status --short
   ```

2. **Read what a launcher actually runs:**

   ```powershell
   type .\windows\start_codex.bat
   type .\windows\start_codexsafe.bat
   type .\windows\start_agy.bat
   ```

3. **Identify running identities:** In the chat UI, check status pills / online agents, or read wrapper startup lines (`Registered as: …`).

4. **Session start blocked?** If the error mentions active session, check RBAC/cast first (INV-007 = same identity in coordinator + reviewer). Fix cast before retrying.

5. **Server not reachable:** Ensure `windows/start.bat` or a launcher has started port 8300, or run `python run.py` manually.

6. **Preflight / hygiene:** The repo includes preflight checks for duplicate or forbidden wrapper processes — use when diagnosing stale Codex instances.

---

## Workspace policy boundary

Workspace filesystem authority is **not** determined by which wrapper you launch. It follows the phased workspace policy design:

| Phase | Status | What it does |
|-------|--------|--------------|
| **Phase 1** | Shipped | Pure validator/schema (`workspace_policy.py`) — no runtime enforcement |
| **Phase 2** | Shipped | Session start payload + immutable policy snapshot in `session_runs.json` + read-only API summary |
| **Phase 3+** | **Not yet runtime-active** | Planned: queue/wrapper enforcement from persisted policy — requires separate authorization |

Until Phase 3+ runtime integration is explicitly approved and deployed:

- Agents continue to use configured `cwd` from `config.toml` (typically `C:/tools/agentchattr-scratch` for Codex/AGY).
- Session `workspace_policy` metadata is **informational at runtime** — persisted at start but not yet enforced by `wrapper.py`.
- Launching an identity does **not** open docs-only or project write access to external repos.

See the Phase 3 runtime integration blueprint in the external planning report (`agentchattr-workspace-policy-phase-3a-cli-guide-plan-report.md`) for future enforcement design.

---

## Quick reference commands

**Server only:**

```powershell
.\windows\start.bat
# or: .\.venv\Scripts\python.exe run.py
```

**Typical coordinator-loop cast (five identities):**

```powershell
.\.venv\Scripts\python.exe wrapper.py codex_coordinator -- --model gpt-5.5
.\.venv\Scripts\python.exe wrapper.py claude
.\.venv\Scripts\python.exe wrapper.py agy -- --model "Gemini 3.1 Pro (High)"
.\.venv\Scripts\python.exe wrapper.py codex_reviewer -- --model gpt-5.5
.\.venv\Scripts\python.exe wrapper.py codexsafe -- --model gpt-5.5
```

**Inspect available identities:**

```powershell
.\.venv\Scripts\python.exe wrapper.py --help
# agent choices come from [agents] keys in config.toml
```

---

## Related files (read-only reference)

| File | Role |
|------|------|
| `wrapper.py` | Wrapper entrypoint, queue, launch arg forwarding |
| `config.toml` | `[agents.*]` identities, `cwd`, `run_mode`, `[roster]` |
| `config_loader.py` | Shared config; `apply_cli_overrides()` respects `--` |
| `session_engine.py` | Session cast guards (INV-007), role routing |
| `workspace_policy.py` | Policy validator + session metadata helpers |
| `windows/*.bat` | Convenience launchers |

For general install and UI usage, see `README.md`.
