"""Configuration loading utilities."""

import json
import os
from pathlib import Path
from typing import Any

import pydantic
from loguru import logger

from nanobot.config.schema import Config
from nanobot.health.storage import HealthWorkspace

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    config = Config()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            missing_env_vars: set[str] = set()
            data = _resolve_env_placeholders(data, missing_env_vars)
            config = Config.model_validate(data)
            _apply_health_runtime_overrides(config)
            if missing_env_vars:
                missing = ", ".join(sorted(missing_env_vars))
                logger.warning("Config references unset environment variables: {}", missing)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning(f"Failed to load config from {path}: {e}")
            logger.warning("Using default configuration.")

    _apply_health_runtime_overrides(config)
    _apply_ssrf_whitelist(config)
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from nanobot.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            data = _preserve_env_placeholders(data, existing)
        except (json.JSONDecodeError, OSError, ValueError):
            logger.debug("Skipping env placeholder preservation for {}", path)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data


def _apply_health_runtime_overrides(config: Config) -> None:
    """Inject hosted health setup values into runtime config when active."""
    health = HealthWorkspace(config.workspace_path)
    overrides = health.runtime_overrides()
    if not overrides:
        return

    provider = overrides["provider"]
    config.agents.defaults.provider = provider["provider"]
    config.agents.defaults.model = provider["model"]
    config.agents.defaults.context_window_tokens = 204_800
    config.agents.defaults.temperature = min(config.agents.defaults.temperature, 0.2)
    provider_config = getattr(config.providers, provider["provider"], None)
    if provider_config is not None:
        provider_config.api_key = provider["api_key"]

    for channel_name, channel_override in overrides["channels"].items():
        current = getattr(config.channels, channel_name, None)
        if current is None:
            current = {}
        elif hasattr(current, "model_dump"):
            current = current.model_dump(by_alias=True)
        elif not isinstance(current, dict):
            current = dict(current)
        merged = {**current, **channel_override}
        merged["allowFrom"] = merged.get("allowFrom") or merged.get("allow_from") or ["*"]
        setattr(config.channels, channel_name, merged)


def _resolve_env_placeholders(value: Any, missing_env_vars: set[str]) -> Any:
    """Replace ENV:NAME strings with environment variable values."""
    if isinstance(value, dict):
        return {
            key: _resolve_env_placeholders(item, missing_env_vars)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_env_placeholders(item, missing_env_vars) for item in value]
    if isinstance(value, str) and value.startswith("ENV:"):
        env_name = value[4:].strip()
        resolved = os.environ.get(env_name, "")
        if not resolved:
            missing_env_vars.add(env_name)
        return resolved
    return value


def _preserve_env_placeholders(current: Any, original: Any) -> Any:
    """Keep ENV:NAME placeholders from the on-disk config when saving."""
    if isinstance(original, str) and original.startswith("ENV:"):
        return original
    if isinstance(current, dict) and isinstance(original, dict):
        return {
            key: _preserve_env_placeholders(current[key], original[key])
            if key in original
            else current[key]
            for key in current
        }
    if isinstance(current, list) and isinstance(original, list):
        preserved: list[Any] = []
        for index, item in enumerate(current):
            if index < len(original):
                preserved.append(_preserve_env_placeholders(item, original[index]))
            else:
                preserved.append(item)
        return preserved
    return current
