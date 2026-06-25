"""Shared config loader — merges config.toml + config.local.toml.

Used by run.py, wrapper.py, and wrapper_api.py so the server and all
wrappers see the same agent definitions.

Per-invocation overrides: the following environment variables, if set,
override values from config.toml. This lets dotfiles/launcher layers run
isolated instances per project without editing the repo's config file.

  AGENTCHATTR_DATA_DIR        → server.data_dir
  AGENTCHATTR_PORT            → server.port           (int)
  AGENTCHATTR_MCP_HTTP_PORT   → mcp.http_port         (int)
  AGENTCHATTR_MCP_SSE_PORT    → mcp.sse_port          (int)
  AGENTCHATTR_UPLOAD_DIR      → images.upload_dir

Relative paths in env var overrides resolve against the current working
directory (where the user invoked the command from), not agentchattr's
install directory.
"""

import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent

SAFE_LOCAL_AGENT_OVERRIDE_KEYS = frozenset({
    "run_mode",
    "cwd",
    "inject_delay",
    "skip_vt_input",
    "print_timeout",
    "exec_prompt_suffix",
    "strip_env",
})


# Mapping: env var name → (config section, key, is_int)
_ENV_OVERRIDES = [
    ("AGENTCHATTR_DATA_DIR",      "server", "data_dir",   False),
    ("AGENTCHATTR_PORT",          "server", "port",       True),
    ("AGENTCHATTR_MCP_HTTP_PORT", "mcp",    "http_port",  True),
    ("AGENTCHATTR_MCP_SSE_PORT",  "mcp",    "sse_port",   True),
    ("AGENTCHATTR_UPLOAD_DIR",    "images", "upload_dir", False),
]

# Mapping: CLI flag → env var (for apply_cli_overrides)
CLI_OVERRIDE_FLAGS = [
    ("--data-dir",      "AGENTCHATTR_DATA_DIR"),
    ("--port",          "AGENTCHATTR_PORT"),
    ("--mcp-http-port", "AGENTCHATTR_MCP_HTTP_PORT"),
    ("--mcp-sse-port",  "AGENTCHATTR_MCP_SSE_PORT"),
    ("--upload-dir",    "AGENTCHATTR_UPLOAD_DIR"),
]


def apply_cli_overrides(argv: list[str] | None = None) -> None:
    """Scan argv for --data-dir/--port/etc and set matching env vars in-place.

    Called by run.py, wrapper.py, and wrapper_api.py BEFORE load_config() so
    all entry points respect the same overrides when launched with the same
    flags. No effect if a flag isn't present. Supports both `--flag value`
    and `--flag=value` forms.

    Arguments after a literal `--` are treated as pass-through (e.g. for the
    agent CLI in wrapper.py) and are NOT scanned — `python wrapper.py claude
    -- --port 9999` sets `--port 9999` on the agent, not on agentchattr.
    """
    if argv is None:
        argv = sys.argv

    # Truncate at pass-through separator so agent CLI args don't leak in.
    try:
        end = argv.index("--")
        scan = argv[:end]
    except ValueError:
        scan = argv

    for flag, env in CLI_OVERRIDE_FLAGS:
        # Iterate in order; first match wins (ignore later duplicates).
        for i, arg in enumerate(scan):
            if arg == flag and i + 1 < len(scan):
                os.environ[env] = scan[i + 1]
                break
            if arg.startswith(flag + "="):
                os.environ[env] = arg.split("=", 1)[1]
                break


def _apply_env_overrides(config: dict) -> None:
    """Apply AGENTCHATTR_* env vars to the config dict in-place."""
    for env_var, section, key, is_int in _ENV_OVERRIDES:
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        if is_int:
            try:
                value = int(raw)
            except ValueError:
                print(f"  Warning: {env_var}={raw!r} is not a valid integer, ignoring")
                continue
        else:
            # Path values: resolve relative paths against current working dir,
            # not against agentchattr's install directory.
            p = Path(raw)
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            value = str(p)
        config.setdefault(section, {})[key] = value


def _merge_local_agents(config: dict, local: dict) -> None:
    """Merge local [agents] with a strict allowlist for existing agents."""
    local_agents = local.get("agents", {})
    config_agents = config.setdefault("agents", {})
    for name, agent_cfg in local_agents.items():
        if name not in config_agents:
            config_agents[name] = agent_cfg
            continue
        if not isinstance(config_agents[name], dict) or not isinstance(agent_cfg, dict):
            print(f"  Warning: Ignoring local agent '{name}' (incompatible config shape)")
            continue

        safe_updates = {
            key: value for key, value in agent_cfg.items()
            if key in SAFE_LOCAL_AGENT_OVERRIDE_KEYS
        }
        ignored_keys = sorted(set(agent_cfg) - set(safe_updates))
        config_agents[name].update(safe_updates)

        if safe_updates:
            applied = ", ".join(sorted(safe_updates))
            print(f"  Info: Applied safe local overrides for agent '{name}': {applied}")
        if ignored_keys:
            ignored = ", ".join(ignored_keys)
            print(f"  Warning: Ignoring unsafe local overrides for agent '{name}': {ignored}")


def load_config(root: Path | None = None) -> dict:
    """Load config.toml and merge config.local.toml if it exists.

    config.local.toml is gitignored and intended for user-specific agents
    (e.g. local LLM endpoints) that shouldn't be committed.
    [agents] from local are added alongside (not replacing) config.toml entries.
    [sandbox] from local overrides committed [sandbox] defaults.

    AGENTCHATTR_* environment variables override values from config.toml
    (see module docstring for the list).
    """
    root = root or ROOT
    config_path = root / "config.toml"

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    local_path = root / "config.local.toml"
    if local_path.exists():
        with open(local_path, "rb") as f:
            local = tomllib.load(f)

        # Merge [agents] section. New local agents are added as-is. For existing
        # agents, only an explicit allowlist of runtime-safe keys may override the
        # committed config. Executable/identity-shaping keys stay protected.
        _merge_local_agents(config, local)

        # Merge [sandbox] — local overrides committed defaults (Owner opt-in enablement).
        local_sandbox = local.get("sandbox")
        if isinstance(local_sandbox, dict) and local_sandbox:
            config_sandbox = config.setdefault("sandbox", {})
            if not isinstance(config_sandbox, dict):
                config_sandbox = {}
                config["sandbox"] = config_sandbox
            config_sandbox.update(local_sandbox)

    _apply_env_overrides(config)

    return config
