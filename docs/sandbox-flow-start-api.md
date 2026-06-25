# Sandbox Flow Start API (V2-B)

Local-only endpoint to start the sandbox orchestration flow without the browser session token.

## Enable (Owner only)

In `config.local.toml` (gitignored):

```toml
[sandbox]
flow_start_enabled = true
```

Defaults in committed `config.toml` keep this **OFF**.

## Request

```http
POST /api/sandbox/flow/start
Host: 127.0.0.1:8300
Content-Type: application/json
X-Sandbox-Flow-Confirm: 1

{
  "template_id": "sandbox-bakery-flow",
  "task": "Bakery POS checkout modal UX improvement mock task only",
  "phase": "v2-d"
}
```

## Guards

- Loopback client only (`127.0.0.1`, `::1`, `::ffff:127.0.0.1`)
- Config flag `flow_start_enabled = true`
- `flow_start_channel_prefix` must be exactly `sandbox-flow` in V2-B (fixed/reserved; other values fail config validation and the endpoint returns `INVALID_SANDBOX_CONFIG`)
- Confirm header required
- Client cannot supply `cast`, `channel`, or `dry_run`
- Template must be allowlisted with `flow_coordinator` + `sandbox_only`
- Cast resolved from `[roster]` only
- Max one active sandbox-flow session (configurable)

## PowerShell example

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8300/api/sandbox/flow/start" `
  -Method POST `
  -Headers @{ "X-Sandbox-Flow-Confirm" = "1"; "Content-Type" = "application/json" } `
  -Body '{"task":"Bakery POS checkout modal UX improvement mock task only","phase":"v2-d"}'
```
