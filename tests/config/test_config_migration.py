import json
import socket
from pathlib import Path
from unittest.mock import patch

from nanobot.config.loader import _apply_health_runtime_overrides, load_config, save_config
from nanobot.config.schema import Config
from nanobot.health.storage import HealthWorkspace
from nanobot.providers.factory import _apply_health_runtime_overrides as _apply_provider_overrides
from nanobot.security.network import validate_url_target


def _fake_resolve(host: str, results: list[str]):
    """Return a getaddrinfo mock that maps the given host to fake IP results."""
    def _resolver(hostname, port, family=0, type_=0):
        if hostname == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in results]
        raise socket.gaierror(f"cannot resolve {hostname}")
    return _resolver


def test_load_config_keeps_max_tokens_and_ignores_legacy_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 1234,
                        "memoryWindow": 42,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.agents.defaults.max_tokens == 1234
    assert config.agents.defaults.context_window_tokens == 65_536
    assert not hasattr(config.agents.defaults, "memory_window")


def test_save_config_writes_context_window_tokens_but_not_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 2222,
                        "memoryWindow": 30,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = saved["agents"]["defaults"]

    assert defaults["maxTokens"] == 2222
    assert defaults["contextWindowTokens"] == 65_536
    assert "memoryWindow" not in defaults


def test_load_config_resolves_env_placeholders(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "minimax": {
                        "apiKey": "ENV:MINIMAX_API_KEY",
                    }
                },
                "channels": {
                    "telegram": {
                        "enabled": True,
                        "token": "ENV:TELEGRAM_BOT_TOKEN",
                        "allowFrom": ["*"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "minimax-test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-test-token")

    config = load_config(config_path)

    assert config.providers.minimax.api_key == "minimax-test-key"
    assert config.channels.telegram["token"] == "telegram-test-token"


def test_save_config_preserves_env_placeholders(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "minimax": {
                        "apiKey": "ENV:MINIMAX_API_KEY",
                    }
                },
                "channels": {
                    "telegram": {
                        "enabled": True,
                        "token": "ENV:TELEGRAM_BOT_TOKEN",
                        "allowFrom": ["*"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "minimax-live-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-live-token")

    config = load_config(config_path)
    save_config(config, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert saved["providers"]["minimax"]["apiKey"] == "ENV:MINIMAX_API_KEY"
    assert saved["channels"]["telegram"]["token"] == "ENV:TELEGRAM_BOT_TOKEN"


def test_load_config_applies_active_hosted_health_overrides(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "workspace": str(workspace),
                        "provider": "minimax",
                        "model": "MiniMax-M2.7",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    health = HealthWorkspace(workspace)
    health.create_setup_session()
    health.store_provider_secret(
        provider_name="minimax",
        model="MiniMax-M2.7",
        api_key="minimax-live-key",
        secret="test-health-vault-key",
    )
    health.store_telegram_secret(
        bot_token="123:abc",
        bot_id=123,
        bot_username="healthbot_test",
        secret="test-health-vault-key",
    )
    health.store_profile_draft(
        submission={
            "phase1": {
                "full_name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "+15550001111",
                "timezone": "Asia/Colombo",
                "language": "en",
                "preferred_channel": "telegram",
                "age_range": "35-44",
                "sex": "female",
                "gender": "woman",
                "wake_time": "06:30",
                "sleep_time": "22:30",
                "consents": ["privacy"],
            },
            "phase2": {
                "mood_interest": 0,
                "mood_down": 0,
                "activity_level": "moderate",
                "nutrition_quality": "mixed",
                "sleep_quality": "fair",
                "stress_level": "moderate",
            },
        },
        secret="test-health-vault-key",
    )
    health.mark_setup_active()

    config = load_config(config_path)

    assert config.providers.minimax.api_key == "minimax-live-key"
    assert config.channels.telegram["enabled"] is True
    assert config.channels.telegram["token"] == "123:abc"


def test_health_runtime_overrides_do_not_clamp_temperature(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    health = HealthWorkspace(workspace)
    health.create_setup_session()
    health.store_provider_secret(
        provider_name="minimax",
        model="MiniMax-M2.7",
        api_key="minimax-live-key",
        secret="test-health-vault-key",
    )
    health.store_telegram_secret(
        bot_token="123:abc",
        bot_id=123,
        bot_username="healthbot_test",
        secret="test-health-vault-key",
    )
    health.mark_setup_active()

    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "provider": "minimax",
                    "model": "MiniMax-M2.7",
                    "temperature": 0.8,
                }
            }
        }
    )

    _apply_health_runtime_overrides(config)
    provider_config = _apply_provider_overrides(config, workspace)

    assert config.agents.defaults.temperature == 0.8
    assert provider_config.agents.defaults.temperature == 0.8


def test_health_instance_config_template_uses_livelier_temperature() -> None:
    config_path = Path("nanobot/health/config_template.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["temperature"] == 0.55


def test_load_config_applies_active_hosted_openrouter_overrides(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "workspace": str(workspace),
                        "provider": "minimax",
                        "model": "MiniMax-M2.7",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    health = HealthWorkspace(workspace)
    health.create_setup_session()
    health.store_provider_secret(
        provider_name="openrouter",
        model="openai/gpt-4o-mini",
        api_key="sk-or-live-key",
        secret="test-health-vault-key",
    )
    health.store_telegram_secret(
        bot_token="123:abc",
        bot_id=123,
        bot_username="healthbot_test",
        secret="test-health-vault-key",
    )
    health.store_profile_draft(
        submission={
            "phase1": {
                "full_name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "+15550001111",
                "timezone": "Asia/Colombo",
                "language": "en",
                "preferred_channel": "telegram",
                "age_range": "35-44",
                "sex": "female",
                "gender": "woman",
                "wake_time": "06:30",
                "sleep_time": "22:30",
                "consents": ["privacy"],
            },
            "phase2": {
                "mood_interest": 0,
                "mood_down": 0,
                "activity_level": "moderate",
                "nutrition_quality": "mixed",
                "sleep_quality": "fair",
                "stress_level": "moderate",
            },
        },
        secret="test-health-vault-key",
    )
    health.mark_setup_active()

    config = load_config(config_path)

    assert config.agents.defaults.provider == "openrouter"
    assert config.agents.defaults.model == "openai/gpt-4o-mini"
    assert config.providers.openrouter.api_key == "sk-or-live-key"


def test_onboard_does_not_crash_with_legacy_memory_window(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 3333,
                        "memoryWindow": 50,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr("nanobot.cli.commands.get_workspace_path", lambda _workspace=None: workspace)

    from typer.testing import CliRunner
    from nanobot.cli.commands import app
    runner = CliRunner()
    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0


def test_onboard_refresh_backfills_missing_channel_fields(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        json.dumps(
            {
                "channels": {
                    "qq": {
                        "enabled": False,
                        "appId": "",
                        "secret": "",
                        "allowFrom": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr("nanobot.cli.commands.get_workspace_path", lambda _workspace=None: workspace)
    monkeypatch.setattr(
        "nanobot.channels.registry.discover_all",
        lambda: {
            "qq": SimpleNamespace(
                default_config=lambda: {
                    "enabled": False,
                    "appId": "",
                    "secret": "",
                    "allowFrom": [],
                    "msgFormat": "plain",
                }
            )
        },
    )

    from typer.testing import CliRunner
    from nanobot.cli.commands import app
    runner = CliRunner()
    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["channels"]["qq"]["msgFormat"] == "plain"


def test_load_config_resets_ssrf_whitelist_when_next_config_is_empty(tmp_path) -> None:
    whitelisted = tmp_path / "whitelisted.json"
    whitelisted.write_text(
        json.dumps({"tools": {"ssrfWhitelist": ["100.64.0.0/10"]}}),
        encoding="utf-8",
    )
    defaulted = tmp_path / "defaulted.json"
    defaulted.write_text(json.dumps({}), encoding="utf-8")

    load_config(whitelisted)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
        ok, err = validate_url_target("http://ts.local/api")
        assert ok, err

    load_config(defaulted)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
        ok, _ = validate_url_target("http://ts.local/api")
        assert not ok
