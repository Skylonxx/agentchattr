"""Agent trigger — writes to queue files picked up by visible worker terminals."""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _valid_relay_entry(relay_entry) -> bool:
    """Minimal validation for an internally-built relay queue entry.

    relay_entry is internal-only (built by session_relay.make_relay_queue_entry
    and passed by the session engine), but we validate defensively so a
    malformed entry never queues an MCP-disabled turn without the proper
    sealed-prompt + relay_meta contract. On failure the caller falls back to
    normal @mention entry construction.
    """
    if not isinstance(relay_entry, dict):
        return False
    prompt = relay_entry.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    meta = relay_entry.get("relay_meta")
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("relay_mode")) and bool(meta.get("disable_mcp"))


class AgentTrigger:
    def __init__(self, registry, data_dir: str = "./data"):
        self._registry = registry
        self._data_dir = Path(data_dir)

    def is_available(self, name: str) -> bool:
        return self._registry.is_registered(name)

    def get_status(self) -> dict:
        from mcp_bridge import is_online, is_active, get_role
        instances = self._registry.get_all()
        return {
            name: {
                "available": is_online(name),
                "busy": is_active(name),
                "label": info["label"],
                "color": info["color"],
                "role": get_role(name),
            }
            for name, info in instances.items()
        }

    async def trigger(self, agent_name: str, message: str = "", channel: str = "general",
                      job_id: int | None = None, **kwargs):
        """Write to the agent's queue file. The worker terminal picks it up."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time

        relay_entry = kwargs.get("relay_entry")
        if _valid_relay_entry(relay_entry):
            entry = dict(relay_entry)
        else:
            entry = {
                "sender": message.split(":")[0].strip() if ":" in message else "?",
                "text": message,
                "time": time.strftime("%H:%M:%S"),
                "channel": channel,
            }
            custom_prompt = kwargs.get("prompt", "")
            if isinstance(custom_prompt, str) and custom_prompt.strip():
                entry["prompt"] = custom_prompt.strip()
            if job_id is not None:
                entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])

    def trigger_sync(self, agent_name: str, message: str = "", channel: str = "general",
                     job_id: int | None = None, **kwargs):
        """Synchronous version of trigger — writes to queue file without async."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time

        relay_entry = kwargs.get("relay_entry")
        if _valid_relay_entry(relay_entry):
            entry = dict(relay_entry)
        else:
            entry = {
                "sender": message.split(":")[0].strip() if ":" in message else "?",
                "text": message,
                "time": time.strftime("%H:%M:%S"),
                "channel": channel,
            }
            custom_prompt = kwargs.get("prompt", "")
            if isinstance(custom_prompt, str) and custom_prompt.strip():
                entry["prompt"] = custom_prompt.strip()
            if job_id is not None:
                entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])
