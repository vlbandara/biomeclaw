"""Storage helpers for health-enabled workspaces."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from nanobot.utils.helpers import ensure_dir

_PROFILE_NAME = "profile.json"
_VAULT_NAME = "vault.json.enc"
_INVITES_NAME = "invites.json"
_SETUP_NAME = "setup.json"
_SETUP_SECRETS_NAME = "setup-secrets.json.enc"
_DEFAULT_INVITE_TTL_HOURS = 24
_DEFAULT_SETUP_TTL_HOURS = 24 * 7
_DEFAULT_HOSTED_PROVIDER = "minimax"
_DEFAULT_HOSTED_MODEL = "MiniMax-M2.7"


def _clean_list(values: list[str] | None) -> list[str]:
    return [value.strip() for value in values or [] if value and value.strip()]


def _default_setup_payload(token: str, *, ttl_hours: int) -> dict[str, Any]:
    now = _utcnow()
    return {
        "token": token,
        "created_at": _isoformat(now),
        "expires_at": _isoformat(now + timedelta(hours=ttl_hours)),
        "completed_at": None,
        "state": "draft",
        "provider": {
            "provider": _DEFAULT_HOSTED_PROVIDER,
            "model": _DEFAULT_HOSTED_MODEL,
            "validated_at": None,
            "api_key_masked": "",
        },
        "channels": {
            "telegram": {
                "connected": False,
                "validated_at": None,
                "bot_id": None,
                "bot_username": "",
                "bot_url": "",
            },
            "whatsapp": {
                "connected": False,
                "status": "waiting",
                "connected_at": None,
                "jid": "",
                "phone": "",
                "chat_url": "",
            },
        },
        "profile": {
            "submitted_at": None,
            "phase1": {},
            "phase2": {},
        },
    }


def _mask_secret(value: str, *, keep: int = 4) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if len(raw) <= keep:
        return "*" * len(raw)
    return "*" * max(0, len(raw) - keep) + raw[-keep:]


def _compute_setup_state(setup: dict[str, Any]) -> str:
    if setup.get("completed_at"):
        return "active"
    provider_ready = bool(setup.get("provider", {}).get("validated_at"))
    channels = setup.get("channels", {})
    channel_ready = any(
        bool(channel.get("connected"))
        for channel in channels.values()
        if isinstance(channel, dict)
    )
    profile_ready = bool(setup.get("profile", {}).get("submitted_at"))
    if provider_ready and channel_ready and profile_ready:
        return "profile_ready"
    if provider_ready and channel_ready:
        return "channels_ready"
    if provider_ready:
        return "provider_ready"
    return "draft"


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def get_health_vault_secret() -> str:
    secret = os.environ.get("HEALTH_VAULT_KEY", "").strip()
    if not secret:
        raise ValueError("HEALTH_VAULT_KEY is required for health-enabled workspaces.")
    return secret


def encrypt_json(payload: dict[str, Any], *, secret: str | None = None) -> str:
    fernet = Fernet(_derive_fernet_key(secret or get_health_vault_secret()))
    return fernet.encrypt(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).decode("utf-8")


def decrypt_json(ciphertext: str, *, secret: str | None = None) -> dict[str, Any]:
    fernet = Fernet(_derive_fernet_key(secret or get_health_vault_secret()))
    try:
        decrypted = fernet.decrypt(ciphertext.encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt health vault with the configured key.") from exc
    return json.loads(decrypted.decode("utf-8"))


def get_onboarding_base_url() -> str:
    base = (
        os.environ.get("HEALTH_ONBOARDING_BASE_URL")
        or os.environ.get("NANOBOT_PUBLIC_BASE_URL")
        or "http://localhost:8080"
    ).strip()
    return base.rstrip("/")


def health_distribution_enabled(workspace: Path | None = None) -> bool:
    flag = os.environ.get("NANOBOT_HEALTH_MODE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if workspace is None:
        return False
    health_dir = workspace / "health"
    return any(
        path.exists()
        for path in (
            health_dir,
            health_dir / _PROFILE_NAME,
            health_dir / _INVITES_NAME,
            health_dir / _SETUP_NAME,
        )
    )


class HealthWorkspace:
    """File-backed health state for a single nanobot workspace."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.health_dir = ensure_dir(workspace / "health")
        self.profile_path = self.health_dir / _PROFILE_NAME
        self.vault_path = self.health_dir / _VAULT_NAME
        self.invites_path = self.health_dir / _INVITES_NAME
        self.setup_path = self.health_dir / _SETUP_NAME
        self.setup_secrets_path = self.health_dir / _SETUP_SECRETS_NAME

    @property
    def enabled(self) -> bool:
        return self.profile_path.exists()

    def load_profile(self) -> dict[str, Any] | None:
        if not self.profile_path.exists():
            return None
        return json.loads(self.profile_path.read_text(encoding="utf-8"))

    def save_profile(self, profile: dict[str, Any]) -> None:
        ensure_dir(self.health_dir)
        self.profile_path.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load_vault(self, *, secret: str | None = None) -> dict[str, Any] | None:
        if not self.vault_path.exists():
            return None
        ciphertext = self.vault_path.read_text(encoding="utf-8").strip()
        if not ciphertext:
            return None
        return decrypt_json(ciphertext, secret=secret)

    def save_vault(self, vault: dict[str, Any], *, secret: str | None = None) -> None:
        ensure_dir(self.health_dir)
        encrypted = encrypt_json(vault, secret=secret)
        self.vault_path.write_text(encrypted + "\n", encoding="utf-8")

    def load_setup(self) -> dict[str, Any] | None:
        if not self.setup_path.exists():
            return None
        raw = json.loads(self.setup_path.read_text(encoding="utf-8"))
        token = raw.get("token") or secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24]
        setup = _merge_dict(_default_setup_payload(token, ttl_hours=_DEFAULT_SETUP_TTL_HOURS), raw)
        setup["state"] = _compute_setup_state(setup)
        return setup

    def save_setup(self, setup: dict[str, Any]) -> None:
        ensure_dir(self.health_dir)
        normalized = _merge_dict(
            _default_setup_payload(
                setup.get("token") or secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24],
                ttl_hours=_DEFAULT_SETUP_TTL_HOURS,
            ),
            setup,
        )
        normalized["state"] = _compute_setup_state(normalized)
        self.setup_path.write_text(
            json.dumps(normalized, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def load_setup_secrets(self, *, secret: str | None = None) -> dict[str, Any]:
        if not self.setup_secrets_path.exists():
            return {}
        ciphertext = self.setup_secrets_path.read_text(encoding="utf-8").strip()
        if not ciphertext:
            return {}
        return decrypt_json(ciphertext, secret=secret)

    def save_setup_secrets(self, payload: dict[str, Any], *, secret: str | None = None) -> None:
        ensure_dir(self.health_dir)
        encrypted = encrypt_json(payload, secret=secret)
        self.setup_secrets_path.write_text(encrypted + "\n", encoding="utf-8")

    def load_invites(self) -> dict[str, dict[str, Any]]:
        if not self.invites_path.exists():
            return {}
        raw = json.loads(self.invites_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    def save_invites(self, invites: dict[str, dict[str, Any]]) -> None:
        ensure_dir(self.health_dir)
        self.invites_path.write_text(
            json.dumps(invites, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def create_invite(
        self,
        *,
        channel: str,
        chat_id: str,
        ttl_hours: int = _DEFAULT_INVITE_TTL_HOURS,
    ) -> tuple[str, dict[str, Any]]:
        invites = self.load_invites()
        token = secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24]
        now = _utcnow()
        invite = {
            "channel": channel,
            "chat_id": chat_id,
            "created_at": _isoformat(now),
            "expires_at": _isoformat(now + timedelta(hours=ttl_hours)),
            "used_at": None,
        }
        invites[token] = invite
        self.save_invites(invites)
        return token, invite

    def get_invite(self, token: str) -> dict[str, Any] | None:
        return self.load_invites().get(token)

    def validate_invite(self, token: str) -> dict[str, Any] | None:
        invite = self.get_invite(token)
        if not invite:
            return None
        if invite.get("used_at"):
            return None
        expires_at = _parse_timestamp(invite.get("expires_at"))
        if expires_at and expires_at < _utcnow():
            return None
        return invite

    def consume_invite(self, token: str) -> None:
        invites = self.load_invites()
        invite = invites.get(token)
        if not invite:
            return
        invite["used_at"] = _isoformat(_utcnow())
        invites[token] = invite
        self.save_invites(invites)

    def onboarding_url(self, token: str) -> str:
        return f"{get_onboarding_base_url()}/onboard/{token}"

    def find_active_invite(self, *, channel: str, chat_id: str) -> tuple[str, dict[str, Any]] | None:
        for token, invite in self.load_invites().items():
            if invite.get("channel") != channel or invite.get("chat_id") != chat_id:
                continue
            if invite.get("used_at"):
                continue
            expires_at = _parse_timestamp(invite.get("expires_at"))
            if expires_at and expires_at < _utcnow():
                continue
            return token, invite
        return None

    def get_or_create_invite(
        self,
        *,
        channel: str,
        chat_id: str,
        ttl_hours: int = _DEFAULT_INVITE_TTL_HOURS,
    ) -> tuple[str, dict[str, Any]]:
        existing = self.find_active_invite(channel=channel, chat_id=chat_id)
        if existing is not None:
            return existing
        return self.create_invite(channel=channel, chat_id=chat_id, ttl_hours=ttl_hours)

    def create_setup_session(
        self,
        *,
        ttl_hours: int = _DEFAULT_SETUP_TTL_HOURS,
    ) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24]
        setup = _default_setup_payload(token, ttl_hours=ttl_hours)
        self.save_setup(setup)
        return token, setup

    def validate_setup_token(self, token: str) -> dict[str, Any] | None:
        setup = self.load_setup()
        if not setup or setup.get("token") != token:
            return None
        expires_at = _parse_timestamp(setup.get("expires_at"))
        if expires_at and expires_at < _utcnow():
            return None
        return setup

    def get_or_create_setup_session(
        self,
        *,
        ttl_hours: int = _DEFAULT_SETUP_TTL_HOURS,
    ) -> tuple[str, dict[str, Any]]:
        setup = self.load_setup()
        if setup and self.validate_setup_token(setup.get("token", "")):
            return setup["token"], setup
        return self.create_setup_session(ttl_hours=ttl_hours)

    def has_setup_session(self) -> bool:
        setup = self.load_setup()
        if not setup:
            return False
        return self.validate_setup_token(setup.get("token", "")) is not None

    def mark_setup_active(self) -> dict[str, Any]:
        setup = self.load_setup() or {}
        setup["completed_at"] = _isoformat(_utcnow())
        setup["state"] = "active"
        self.save_setup(setup)
        return setup

    def setup_url(self, token: str) -> str:
        return f"{get_onboarding_base_url()}/setup/{token}"

    def connected_channels(self) -> dict[str, dict[str, Any]]:
        setup = self.load_setup() or {}
        channels = setup.get("channels", {})
        return {
            name: info
            for name, info in channels.items()
            if isinstance(info, dict) and info.get("connected")
        }

    def runtime_overrides(self, *, secret: str | None = None) -> dict[str, Any] | None:
        setup = self.load_setup()
        if not setup or setup.get("state") != "active":
            return None
        secrets_payload = self.load_setup_secrets(secret=secret)
        provider_key = secrets_payload.get("provider", {}).get("api_key", "").strip()
        telegram_token = secrets_payload.get("telegram", {}).get("bot_token", "").strip()
        channels = setup.get("channels", {})
        return {
            "provider": {
                "provider": setup.get("provider", {}).get("provider") or _DEFAULT_HOSTED_PROVIDER,
                "model": setup.get("provider", {}).get("model") or _DEFAULT_HOSTED_MODEL,
                "api_key": provider_key,
            },
            "channels": {
                "telegram": {
                    "enabled": bool(channels.get("telegram", {}).get("connected") and telegram_token),
                    "token": telegram_token,
                    "allow_from": ["*"],
                },
                "whatsapp": {
                    "enabled": bool(channels.get("whatsapp", {}).get("connected")),
                    "allow_from": ["*"],
                },
            },
        }

    def bind_chat_session(self, *, channel: str, chat_id: str, secret: str | None = None) -> None:
        profile = self.load_profile()
        if not profile:
            return
        changed = False
        binding = profile.setdefault("channel_binding", {})
        bound_channels = set(_clean_list(binding.get("bound_channels")))
        if channel not in bound_channels:
            bound_channels.add(channel)
            binding["bound_channels"] = sorted(bound_channels)
            changed = True
        if binding.get("last_channel") != channel:
            binding["last_channel"] = channel
            changed = True
        if binding.get("last_chat_id") != chat_id:
            binding["last_chat_id"] = chat_id
            changed = True
        if changed:
            self.save_profile(profile)

        vault = self.load_vault(secret=secret) or {}
        identifiers = vault.setdefault("identifiers", {})
        chat_ids = set(_clean_list(identifiers.get("chat_ids")))
        channels = set(_clean_list(identifiers.get("channels")))
        if chat_id and chat_id not in chat_ids:
            chat_ids.add(chat_id)
            identifiers["chat_ids"] = sorted(chat_ids)
            changed = True
        if channel and channel not in channels:
            channels.add(channel)
            identifiers["channels"] = sorted(channels)
            changed = True
        contact = vault.setdefault("contact", {})
        if not contact.get("invite_channel") and channel:
            contact["invite_channel"] = channel
            changed = True
        if not contact.get("invite_chat_id") and chat_id:
            contact["invite_chat_id"] = chat_id
            changed = True
        if changed:
            self.save_vault(vault, secret=secret)

    def store_provider_secret(
        self,
        *,
        provider_name: str,
        model: str,
        api_key: str,
        secret: str | None = None,
    ) -> dict[str, Any]:
        secrets_payload = self.load_setup_secrets(secret=secret)
        provider = secrets_payload.setdefault("provider", {})
        provider["provider"] = provider_name.strip()
        provider["model"] = model.strip()
        provider["api_key"] = api_key.strip()
        self.save_setup_secrets(secrets_payload, secret=secret)
        setup = self.load_setup() or {}
        setup_provider = setup.setdefault("provider", {})
        setup_provider["provider"] = provider_name.strip() or _DEFAULT_HOSTED_PROVIDER
        setup_provider["model"] = model.strip() or _DEFAULT_HOSTED_MODEL
        setup_provider["api_key_masked"] = _mask_secret(api_key)
        setup_provider["validated_at"] = _isoformat(_utcnow())
        self.save_setup(setup)
        return setup

    def store_telegram_secret(
        self,
        *,
        bot_token: str,
        bot_id: int | None,
        bot_username: str,
        secret: str | None = None,
    ) -> dict[str, Any]:
        secrets_payload = self.load_setup_secrets(secret=secret)
        telegram = secrets_payload.setdefault("telegram", {})
        telegram["bot_token"] = bot_token.strip()
        self.save_setup_secrets(secrets_payload, secret=secret)
        setup = self.load_setup() or {}
        telegram_meta = setup.setdefault("channels", {}).setdefault("telegram", {})
        telegram_meta.update(
            {
                "connected": True,
                "validated_at": _isoformat(_utcnow()),
                "bot_id": bot_id,
                "bot_username": bot_username,
                "bot_url": f"https://t.me/{bot_username}" if bot_username else "",
            }
        )
        self.save_setup(setup)
        return setup

    def update_whatsapp_status(
        self,
        *,
        status: str,
        jid: str = "",
        phone: str = "",
        chat_url: str = "",
    ) -> dict[str, Any]:
        setup = self.load_setup() or {}
        whatsapp = setup.setdefault("channels", {}).setdefault("whatsapp", {})
        whatsapp["status"] = status
        if status == "connected":
            whatsapp["connected"] = True
            whatsapp["connected_at"] = whatsapp.get("connected_at") or _isoformat(_utcnow())
        else:
            whatsapp["connected"] = False
        if jid:
            whatsapp["jid"] = jid
        if phone:
            whatsapp["phone"] = phone
        if chat_url:
            whatsapp["chat_url"] = chat_url
        self.save_setup(setup)
        return setup

    def store_profile_draft(
        self,
        *,
        submission: dict[str, Any],
        secret: str | None = None,
    ) -> dict[str, Any]:
        phase1 = dict(submission.get("phase1") or {})
        identity = {
            "full_name": phase1.pop("full_name", "").strip(),
            "email": phase1.pop("email", "").strip(),
            "phone": phase1.pop("phone", "").strip(),
        }
        secrets_payload = self.load_setup_secrets(secret=secret)
        secrets_payload["profile_identity"] = identity
        self.save_setup_secrets(secrets_payload, secret=secret)

        setup = self.load_setup() or {}
        setup["profile"] = {
            "submitted_at": _isoformat(_utcnow()),
            "phase1": phase1,
            "phase2": submission.get("phase2") or {},
        }
        self.save_setup(setup)
        return setup

    def load_profile_draft_submission(self, *, secret: str | None = None) -> dict[str, Any] | None:
        setup = self.load_setup() or {}
        profile = setup.get("profile") or {}
        if not profile.get("submitted_at"):
            return None
        secrets_payload = self.load_setup_secrets(secret=secret)
        identity = secrets_payload.get("profile_identity") or {}
        phase1 = dict(profile.get("phase1") or {})
        phase1.update(
            {
                "full_name": identity.get("full_name", ""),
                "email": identity.get("email", ""),
                "phone": identity.get("phone", ""),
            }
        )
        return {
            "phase1": phase1,
            "phase2": dict(profile.get("phase2") or {}),
        }


def is_health_workspace(workspace: Path) -> bool:
    return (workspace / "health" / _PROFILE_NAME).exists()
