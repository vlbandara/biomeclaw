"""Hosted health onboarding integrations."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import websockets
from openai import AsyncOpenAI

from nanobot.channels.telegram import TelegramChannel

PROVIDER_CHOICES: dict[str, dict[str, str]] = {
    "minimax": {
        "label": "MiniMax",
        "model": "MiniMax-M2.7",
        "api_base": "https://api.minimax.io/v1",
    },
    "openrouter": {
        "label": "OpenRouter",
        "model": "openai/gpt-4o-mini",
        "api_base": "https://openrouter.ai/api/v1",
    },
}


def get_whatsapp_bridge_url() -> str:
    return (
        os.environ.get("HEALTH_WHATSAPP_BRIDGE_URL")
        or os.environ.get("NANOBOT_WHATSAPP_BRIDGE_URL")
        or "ws://whatsapp-bridge:3001"
    ).strip()


def get_whatsapp_bridge_token() -> str:
    return (
        os.environ.get("WHATSAPP_BRIDGE_TOKEN")
        or os.environ.get("BRIDGE_TOKEN")
        or ""
    ).strip()


def _extract_phone(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits


def build_whatsapp_chat_url(phone: str = "", fallback_url: str = "") -> str:
    if fallback_url:
        return fallback_url
    if phone:
        return f"https://wa.me/{phone}"
    return ""


def get_provider_choice(provider_name: str) -> dict[str, str]:
    normalized = (provider_name or "").strip().lower()
    if normalized not in PROVIDER_CHOICES:
        raise ValueError("Choose MiniMax or OpenRouter.")
    return PROVIDER_CHOICES[normalized]


async def validate_provider_credentials(provider_name: str, api_key: str) -> dict[str, Any]:
    choice = get_provider_choice(provider_name)
    client = AsyncOpenAI(
        api_key=api_key.strip(),
        base_url=choice["api_base"],
    )
    await client.chat.completions.create(
        model=choice["model"],
        messages=[{"role": "user", "content": "Reply with OK."}],
        max_tokens=8,
        temperature=0,
    )
    return {
        "provider": provider_name.strip().lower(),
        "label": choice["label"],
        "model": choice["model"],
        "api_base": choice["api_base"],
    }


async def validate_telegram_bot_token(bot_token: str) -> dict[str, Any]:
    token = bot_token.strip()
    url = f"https://api.telegram.org/bot{token}/getMe"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok") or not isinstance(payload.get("result"), dict):
        raise ValueError("Telegram did not accept that bot token.")
    result = payload["result"]
    return {
        "bot_id": result.get("id"),
        "bot_username": result.get("username", ""),
        "bot_name": result.get("first_name", ""),
        "bot_url": f"https://t.me/{result.get('username')}" if result.get("username") else "",
    }


async def register_telegram_commands(bot_token: str) -> None:
    commands = [
        {"command": cmd.command, "description": cmd.description}
        for cmd in TelegramChannel.BOT_COMMANDS
    ]
    token = bot_token.strip()
    url = f"https://api.telegram.org/bot{token}/setMyCommands"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json={"commands": commands})
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise ValueError("Telegram rejected command registration.")


class WhatsAppBridgeMonitor:
    """Background listener that caches QR and status updates from the bridge."""

    def __init__(
        self,
        *,
        bridge_url: str,
        bridge_token: str,
        fallback_chat_url: str = "",
        on_status: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.bridge_url = bridge_url
        self.bridge_token = bridge_token
        self.fallback_chat_url = fallback_chat_url
        self.on_status = on_status
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._snapshot: dict[str, Any] = {
            "status": "waiting",
            "qr": "",
            "jid": "",
            "phone": "",
            "chat_url": fallback_chat_url,
            "last_error": "",
        }

    @property
    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)

    async def start(self) -> None:
        if not self.bridge_token or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.bridge_url) as websocket:
                    await websocket.send(json.dumps({"type": "auth", "token": self.bridge_token}))
                    async for raw in websocket:
                        self._handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._snapshot["last_error"] = str(exc)
                await asyncio.sleep(2)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type")
        if msg_type == "qr":
            self._snapshot["qr"] = data.get("qr", "")
            self._snapshot["status"] = "qr_ready"
        elif msg_type == "status":
            status = data.get("status")
            if isinstance(status, dict):
                normalized = dict(status)
            else:
                normalized = {"status": status}
            state = str(normalized.get("status") or "waiting")
            jid = str(normalized.get("jid") or "")
            phone = str(normalized.get("phone") or _extract_phone(jid))
            chat_url = build_whatsapp_chat_url(phone, self.fallback_chat_url)
            self._snapshot.update(
                {
                    "status": state,
                    "jid": jid,
                    "phone": phone,
                    "chat_url": chat_url,
                    "last_error": "",
                }
            )
            if state == "connected":
                self._snapshot["qr"] = ""
            if self.on_status:
                result = self.on_status(
                    {
                        "status": state,
                        "jid": jid,
                        "phone": phone,
                        "chat_url": chat_url,
                    }
                )
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
