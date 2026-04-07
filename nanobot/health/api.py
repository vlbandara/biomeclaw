"""Standalone FastAPI onboarding surface for health mode."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from nanobot.health.bootstrap import persist_health_onboarding
from nanobot.health.hosted import (
    PROVIDER_CHOICES,
    WhatsAppBridgeMonitor,
    build_whatsapp_chat_url,
    get_whatsapp_bridge_token,
    get_whatsapp_bridge_url,
    register_telegram_commands,
    validate_provider_credentials,
    validate_telegram_bot_token,
)
from nanobot.health.storage import HealthWorkspace, get_health_vault_secret


class Phase1Submission(BaseModel):
    full_name: str
    email: str = ""
    phone: str = ""
    timezone: str
    language: str
    preferred_channel: str
    age_range: str
    sex: str
    gender: str
    height_cm: float | None = None
    weight_kg: float | None = None
    known_conditions: list[str] = Field(default_factory=list)
    medications: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    wake_time: str
    sleep_time: str
    consents: list[str] = Field(default_factory=list)


class Phase2Submission(BaseModel):
    mood_interest: int = Field(ge=0, le=3)
    mood_down: int = Field(ge=0, le=3)
    activity_level: str
    nutrition_quality: str
    sleep_quality: str
    stress_level: str
    goals: list[str] = Field(default_factory=list)
    current_concerns: str = ""
    reminder_preferences: list[str] = Field(default_factory=list)
    medication_reminder_windows: list[str] = Field(default_factory=list)
    morning_check_in: bool = True
    weekly_summary: bool = True


class OnboardingSubmission(BaseModel):
    phase1: Phase1Submission
    phase2: Phase2Submission


class ProviderSetupSubmission(BaseModel):
    provider: str = "minimax"
    api_key: str = Field(min_length=1)


class TelegramSetupSubmission(BaseModel):
    bot_token: str = Field(min_length=1)


def _resolve_workspace() -> Path:
    raw = os.environ.get("NANOBOT_WORKSPACE") or "~/.nanobot/workspace"
    return Path(raw).expanduser().resolve()


def _channel_links() -> dict[str, str]:
    links = {
        "telegram": os.environ.get("HEALTH_TELEGRAM_BOT_URL", "").strip(),
        "whatsapp": os.environ.get("HEALTH_WHATSAPP_CHAT_URL", "").strip(),
    }
    return {name: url for name, url in links.items() if url}


def _current_channel_links(
    health: HealthWorkspace,
    *,
    fallback_links: dict[str, str],
    bridge_snapshot: dict[str, Any] | None = None,
) -> dict[str, str]:
    setup = health.load_setup() or {}
    channels = setup.get("channels", {})
    telegram_link = (
        channels.get("telegram", {}).get("bot_url")
        or fallback_links.get("telegram", "")
    )
    whatsapp_link = (
        channels.get("whatsapp", {}).get("chat_url")
        or (bridge_snapshot or {}).get("chat_url", "")
        or fallback_links.get("whatsapp", "")
    )
    links = {
        "telegram": telegram_link,
        "whatsapp": whatsapp_link,
    }
    return {name: url for name, url in links.items() if url}


def _sync_whatsapp_state(
    health: HealthWorkspace,
    snapshot: dict[str, Any] | None,
    *,
    fallback_chat_url: str = "",
) -> None:
    if not snapshot:
        return
    status = snapshot.get("status") or "waiting"
    if status not in {"connected", "disconnected", "qr_ready", "waiting"}:
        return
    health.update_whatsapp_status(
        status="connected" if status == "connected" else "waiting",
        jid=str(snapshot.get("jid") or ""),
        phone=str(snapshot.get("phone") or ""),
        chat_url=build_whatsapp_chat_url(
            str(snapshot.get("phone") or ""),
            str(snapshot.get("chat_url") or fallback_chat_url or ""),
        ),
    )


def _setup_status_payload(
    health: HealthWorkspace,
    *,
    secret: str,
    fallback_links: dict[str, str],
    bridge_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _sync_whatsapp_state(health, bridge_snapshot, fallback_chat_url=fallback_links.get("whatsapp", ""))
    setup = health.load_setup()
    if not setup:
        raise HTTPException(status_code=404, detail="Setup session not found.")

    secrets_payload = health.load_setup_secrets(secret=secret)
    provider = dict(setup.get("provider", {}))
    provider["has_api_key"] = bool(secrets_payload.get("provider", {}).get("api_key"))

    channels = dict(setup.get("channels", {}))
    telegram = dict(channels.get("telegram", {}))
    whatsapp = dict(channels.get("whatsapp", {}))
    telegram["has_token"] = bool(secrets_payload.get("telegram", {}).get("bot_token"))
    whatsapp["chat_url"] = whatsapp.get("chat_url") or ((bridge_snapshot or {}).get("chat_url") or "")
    channels["telegram"] = telegram
    channels["whatsapp"] = whatsapp

    links = _current_channel_links(health, fallback_links=fallback_links, bridge_snapshot=bridge_snapshot)
    return {
        "token": setup.get("token"),
        "state": setup.get("state"),
        "provider": provider,
        "channels": channels,
        "profile": health.load_profile_draft_submission(secret=secret) or {},
        "channelLinks": links,
        "activationReady": bool(
            provider.get("validated_at")
            and any(bool(item.get("connected")) for item in channels.values() if isinstance(item, dict))
            and setup.get("profile", {}).get("submitted_at")
        ),
    }


def create_app() -> FastAPI:
    workspace = _resolve_workspace()
    health = HealthWorkspace(workspace)
    app = FastAPI(title="nanobot health onboarding")
    app.state.workspace = workspace
    app.state.health = health
    app.state.health_secret = get_health_vault_secret()
    app.state.channel_links = _channel_links()

    monitor = WhatsAppBridgeMonitor(
        bridge_url=get_whatsapp_bridge_url(),
        bridge_token=get_whatsapp_bridge_token(),
        fallback_chat_url=app.state.channel_links.get("whatsapp", ""),
        on_status=lambda payload: health.update_whatsapp_status(
            status=str(payload.get("status") or "waiting"),
            jid=str(payload.get("jid") or ""),
            phone=str(payload.get("phone") or ""),
            chat_url=str(payload.get("chat_url") or ""),
        ),
    )
    app.state.whatsapp_monitor = monitor

    template_dir = Path(__file__).with_name("templates")
    static_dir = Path(__file__).with_name("static")
    templates = Jinja2Templates(directory=str(template_dir))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.on_event("startup")
    async def startup() -> None:
        await monitor.start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await monitor.stop()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/setup/{setup_token}", response_class=HTMLResponse)
    async def setup_form(request: Request, setup_token: str) -> HTMLResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "setup_token": setup_token,
                "setup": setup,
                "channel_links": app.state.channel_links,
                "provider_choices": PROVIDER_CHOICES,
            },
        )

    @app.get("/api/setup/{setup_token}/status")
    async def setup_status(setup_token: str) -> JSONResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        payload = _setup_status_payload(
            health,
            secret=app.state.health_secret,
            fallback_links=app.state.channel_links,
            bridge_snapshot=monitor.snapshot,
        )
        return JSONResponse(payload)

    @app.post("/api/setup/{setup_token}/provider")
    async def setup_provider(setup_token: str, submission: ProviderSetupSubmission) -> JSONResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        try:
            provider_meta = await validate_provider_credentials(submission.provider, submission.api_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            provider_label = PROVIDER_CHOICES.get(submission.provider, {}).get("label", "provider")
            raise HTTPException(status_code=502, detail=f"Unable to validate {provider_label} key: {exc}") from exc
        updated = health.store_provider_secret(
            provider_name=provider_meta["provider"],
            model=provider_meta["model"],
            api_key=submission.api_key,
            secret=app.state.health_secret,
        )
        return JSONResponse(
            {
                "status": "ok",
                "provider": provider_meta,
                "setup": updated,
            }
        )

    @app.post("/api/setup/{setup_token}/channels/telegram")
    async def setup_telegram(setup_token: str, submission: TelegramSetupSubmission) -> JSONResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        try:
            telegram_meta = await validate_telegram_bot_token(submission.bot_token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Unable to validate Telegram bot token: {exc}") from exc
        updated = health.store_telegram_secret(
            bot_token=submission.bot_token,
            bot_id=telegram_meta.get("bot_id"),
            bot_username=telegram_meta.get("bot_username", ""),
            secret=app.state.health_secret,
        )
        return JSONResponse(
            {
                "status": "ok",
                "telegram": telegram_meta,
                "setup": updated,
            }
        )

    @app.get("/api/setup/{setup_token}/channels/whatsapp/qr")
    async def setup_whatsapp_qr(setup_token: str) -> JSONResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        _sync_whatsapp_state(
            health,
            monitor.snapshot,
            fallback_chat_url=app.state.channel_links.get("whatsapp", ""),
        )
        snapshot = monitor.snapshot
        return JSONResponse(
            {
                "status": snapshot.get("status", "waiting"),
                "qr": snapshot.get("qr", ""),
                "chatUrl": snapshot.get("chat_url", ""),
            }
        )

    @app.get("/api/setup/{setup_token}/channels/whatsapp/status")
    async def setup_whatsapp_status(setup_token: str) -> JSONResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        _sync_whatsapp_state(
            health,
            monitor.snapshot,
            fallback_chat_url=app.state.channel_links.get("whatsapp", ""),
        )
        return JSONResponse(monitor.snapshot)

    @app.post("/api/setup/{setup_token}/profile")
    async def setup_profile(setup_token: str, submission: OnboardingSubmission) -> JSONResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        updated = health.store_profile_draft(
            submission=submission.model_dump(),
            secret=app.state.health_secret,
        )
        return JSONResponse({"status": "ok", "setup": updated})

    @app.post("/api/setup/{setup_token}/activate")
    async def setup_activate(setup_token: str) -> JSONResponse:
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        payload = _setup_status_payload(
            health,
            secret=app.state.health_secret,
            fallback_links=app.state.channel_links,
            bridge_snapshot=monitor.snapshot,
        )
        provider_ok = bool(payload["provider"].get("validated_at"))
        if not provider_ok and os.environ.get(
            "NANOBOT_SKIP_SETUP_PROVIDER", ""
        ).strip().lower() in {"1", "true", "yes", "on"}:
            provider_ok = True
        if not provider_ok:
            raise HTTPException(status_code=400, detail="Add your AI key first.")
        if not any(
            bool(item.get("connected"))
            for item in payload["channels"].values()
            if isinstance(item, dict)
        ):
            raise HTTPException(status_code=400, detail="Connect Telegram or WhatsApp first.")
        submission = health.load_profile_draft_submission(secret=app.state.health_secret)
        if not submission:
            raise HTTPException(status_code=400, detail="Tell us about you before activation.")

        profile = persist_health_onboarding(
            workspace,
            submission,
            invite=None,
            secret=app.state.health_secret,
        )

        telegram_token = health.load_setup_secrets(secret=app.state.health_secret).get("telegram", {}).get("bot_token")
        if payload["channels"].get("telegram", {}).get("connected") and telegram_token:
            try:
                await register_telegram_commands(telegram_token)
            except Exception:
                pass

        health.mark_setup_active()
        links = _current_channel_links(
            health,
            fallback_links=app.state.channel_links,
            bridge_snapshot=monitor.snapshot,
        )
        return JSONResponse(
            {
                "status": "ok",
                "state": "active",
                "preferredChannel": profile["preferred_channel"],
                "channelLinks": links,
            }
        )

    @app.get("/onboard/{invite}", response_class=HTMLResponse)
    async def onboard_form(request: Request, invite: str) -> HTMLResponse:
        invite_meta = health.validate_invite(invite)
        if not invite_meta:
            raise HTTPException(status_code=404, detail="Invite not found, expired, or already used.")
        return templates.TemplateResponse(
            request,
            "onboard.html",
            {
                "invite": invite,
                "invite_meta": invite_meta,
                "workspace": str(workspace),
                "channel_links": app.state.channel_links,
            },
        )

    @app.post("/api/onboard/{invite}/submit")
    async def onboard_submit(invite: str, submission: OnboardingSubmission) -> JSONResponse:
        invite_meta = health.validate_invite(invite)
        if not invite_meta:
            raise HTTPException(status_code=404, detail="Invite not found, expired, or already used.")
        payload = submission.model_dump()
        profile = persist_health_onboarding(
            workspace,
            payload,
            invite=invite_meta,
            secret=app.state.health_secret,
        )
        health.consume_invite(invite)
        return JSONResponse(
            {
                "status": "ok",
                "userToken": profile["user_token"],
                "preferredChannel": profile["preferred_channel"],
                "channelLinks": app.state.channel_links,
            }
        )

    return app
