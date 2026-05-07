"""Configuration loader for runbook-exec.

Builds a Config from three sources in priority order:
1. Built-in defaults (Pydantic field defaults)
2. `.runbook-exec.toml` (if present in CWD)
3. CLI flags (passed in as kwargs)

Environment variables are read here and merged into the config.
"""

import os
import tomllib
from pathlib import Path
from typing import Any

from runbook_exec.exceptions import ConfigError
from runbook_exec.models import Config

_CONFIG_FILE = ".runbook-exec.toml"


def load_config(**cli_overrides: Any) -> Config:
    """Load config from file + env vars, then apply CLI overrides.

    Priority (highest wins):
      CLI overrides > .runbook-exec.toml > built-in defaults

    Environment variables:
      ANTHROPIC_API_KEY  — required; raises ConfigError if absent
      SLACK_BOT_TOKEN    — optional; defaults to ""
      SLACK_APP_TOKEN    — optional; defaults to ""

    Args:
        **cli_overrides: CLI flag values. Only non-None values override lower-priority sources.

    Returns:
        Fully populated Config instance.

    Raises:
        ConfigError: If ANTHROPIC_API_KEY is not set in the environment.
    """
    # --- 1. Validate required env vars ---
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. "
            "Export it before running runbook-exec:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-..."
        )

    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")

    # --- 2. Start with built-in defaults ---
    merged: dict[str, Any] = {
        "slack_bot_token": slack_bot_token,
        "slack_app_token": slack_app_token,
    }

    # --- 3. Apply .runbook-exec.toml overrides ---
    config_path = Path(_CONFIG_FILE)
    if config_path.is_file():
        with config_path.open("rb") as f:
            file_data = tomllib.load(f)
        merged.update(file_data)

    # --- 4. Apply CLI overrides (None = "not provided", skip those) ---
    for key, value in cli_overrides.items():
        if value is not None:
            merged[key] = value

    return Config(**merged)
