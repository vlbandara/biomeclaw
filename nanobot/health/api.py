"""Standalone FastAPI onboarding surface for health mode."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from nanobot.health import metrics as health_metrics
from nanobot.health.bootstrap import persist_health_onboarding
from nanobot.health.chat_workspace import (
    parse_lifecycle_stage,
    resolve_health_chat_workspace,
    rough_knowledge_score,
)
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
from nanobot.health.registry import HealthRegistry
from nanobot.health.spawner import HealthInstanceSpawner
from nanobot.health.storage import HealthWorkspace, get_health_vault_secret

logger = logging.getLogger("nanobot.health.api")
_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("nanobot_health_request_id", default="")


class SignupSubmission(BaseModel):
    name: str = Field(min_length=1)
    timezone: str = "UTC"


class Phase1Submission(BaseModel):
    # Minimal onboarding: most fields are optional; the service will backfill defaults.
    full_name: str = ""
    location: str = ""
    email: str = ""
    phone: str = ""
    timezone: str = "UTC"
    language: str = "en"
    preferred_channel: str = "telegram"
    age_range: str = "not set"
    sex: str = "not set"
    gender: str = "not set"
    height_cm: float | None = None
    weight_kg: float | None = None
    known_conditions: list[str] = Field(default_factory=list)
    medications: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    wake_time: str = ""
    sleep_time: str = ""
    consents: list[str] = Field(default_factory=list)


class Phase2Submission(BaseModel):
    mood_interest: int = Field(default=0, ge=0, le=3)
    mood_down: int = Field(default=0, ge=0, le=3)
    activity_level: str = "not set"
    nutrition_quality: str = "not set"
    sleep_quality: str = "not set"
    stress_level: str = "not set"
    goals: list[str] = Field(default_factory=list)
    current_concerns: str = ""
    reminder_preferences: list[str] = Field(default_factory=list)
    medication_reminder_windows: list[str] = Field(default_factory=list)
    morning_check_in: bool = True
    weekly_summary: bool = True


class OnboardingSubmission(BaseModel):
    phase1: Phase1Submission
    phase2: Phase2Submission


def _default_onboarding_submission(*, setup_payload: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal onboarding submission so activation can proceed without a long intake."""
    preferred_channel = "telegram"
    timezone = "UTC"
    language = "en"
    try:
        timezone = str((setup_payload.get("profile") or {}).get("phase1", {}).get("timezone") or timezone)
        language = str((setup_payload.get("profile") or {}).get("phase1", {}).get("language") or language)
        preferred_channel = str((setup_payload.get("profile") or {}).get("phase1", {}).get("preferred_channel") or preferred_channel)
    except Exception:
        pass
    return OnboardingSubmission(
        phase1=Phase1Submission(
            preferred_channel=preferred_channel,
            timezone=timezone,
            language=language,
            consents=["privacy", "emergency", "coaching"],
        ),
        phase2=Phase2Submission(
            mood_interest=0,
            mood_down=0,
            goals=[],
            morning_check_in=True,
            weekly_summary=True,
        ),
    ).model_dump()


class ProviderSetupSubmission(BaseModel):
    provider: str = "minimax"
    api_key: str = Field(min_length=1)


class TelegramSetupSubmission(BaseModel):
    bot_token: str = Field(min_length=1)


class WebChatMessage(BaseModel):
    message: str = Field(min_length=1, max_length=32000)


def _request_id_from_request(request: Request | None) -> str:
    if request is not None:
        request_id = str(getattr(getattr(request, "state", None), "request_id", "") or "").strip()
        if request_id:
            return request_id
    return _request_id_ctx.get("")


def _stringify_detail(detail: Any) -> Any:
    if isinstance(detail, (str, int, float, bool)) or detail is None:
        return detail
    if isinstance(detail, list):
        try:
            return json.loads(json.dumps(detail, ensure_ascii=False))
        except Exception:
            return [str(item) for item in detail]
    if isinstance(detail, dict):
        try:
            return json.loads(json.dumps(detail, ensure_ascii=False))
        except Exception:
            return {str(key): str(value) for key, value in detail.items()}
    return str(detail)


def _runtime_metrics_state() -> dict[str, Any]:
    return {
        "requests": {
            "total": 0,
            "success": 0,
            "clientErrors": 0,
            "serverErrors": 0,
        },
        "lastError": {
            "time": "",
            "requestId": "",
            "errorId": "",
            "path": "",
            "method": "",
            "statusCode": 0,
            "detail": "",
        },
        "release": {
            "version": os.environ.get("APP_RELEASE", "").strip(),
            "deployedAt": os.environ.get("APP_DEPLOYED_AT", "").strip(),
        },
    }


def _record_runtime_error(
    app: FastAPI,
    *,
    request: Request,
    status_code: int,
    detail: Any,
) -> None:
    runtime = getattr(app.state, "runtime_metrics", None)
    if not isinstance(runtime, dict):
        return
    request_id = _request_id_from_request(request)
    runtime["lastError"] = {
        "time": datetime.now(UTC).isoformat(),
        "requestId": request_id,
        "errorId": request_id,
        "path": request.url.path,
        "method": request.method,
        "statusCode": int(status_code),
        "detail": _stringify_detail(detail),
    }


def _log_request_error(
    request: Request,
    *,
    status_code: int,
    detail: Any,
    event: str,
    exc: Exception | None = None,
) -> None:
    request_id = _request_id_from_request(request)
    extra = {
        "request_id": request_id,
        "error_id": request_id,
        "path": request.url.path,
        "method": request.method,
        "status_code": int(status_code),
        "detail": _stringify_detail(detail),
        "event": event,
    }
    if exc is None:
        logger.error(event, extra=extra)
    else:
        logger.exception(event, extra=extra)


def _json_error_response(
    *,
    status_code: int,
    detail: Any,
    request_id: str,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "detail": _stringify_detail(detail),
        "requestId": request_id,
    }
    if status_code >= 500:
        payload["errorId"] = request_id
    return JSONResponse(payload, status_code=status_code, headers={"X-Request-ID": request_id})


def _web_chat_nanobot(app: FastAPI, workspace: Path) -> Any:
    from nanobot.nanobot import Nanobot

    cache = getattr(app.state, "web_chat_nanobots", None)
    if cache is None:
        app.state.web_chat_nanobots = {}
        cache = app.state.web_chat_nanobots
    key = str(workspace.resolve())
    if key not in cache:
        cache[key] = Nanobot.from_config(workspace=workspace)
    return cache[key]


def _web_chat_lock(app: FastAPI, key: str) -> asyncio.Lock:
    locks = getattr(app.state, "web_chat_locks", None)
    if locks is None:
        app.state.web_chat_locks = {}
        locks = app.state.web_chat_locks
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]


def _resolve_workspace() -> Path:
    raw = os.environ.get("NANOBOT_WORKSPACE") or "~/.nanobot/workspace"
    return Path(raw).expanduser().resolve()


def _load_health_instance_config_template() -> dict[str, Any]:
    path = Path(__file__).with_name("config_template.json")
    return json.loads(path.read_text(encoding="utf-8"))


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
                # Minimal onboarding: profile is optional; activation only requires a connected channel.
        ),
    }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
        }
        for field in ("request_id", "error_id", "path", "method", "status_code", "detail", "event"):
            value = getattr(record, field, None)
            if value not in (None, ""):
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return f'{{"level":"{record.levelname}","message":"{record.getMessage()}"}}'


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            request_id = _request_id_ctx.get("")
            if request_id:
                record.request_id = request_id
        return True


def _configure_logging() -> None:
    if os.environ.get("NANOBOT_JSON_LOGS", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    root = logging.getLogger()
    if any(isinstance(h.formatter, _JsonFormatter) for h in root.handlers if getattr(h, "formatter", None)):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RequestContextFilter())
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


async def _readiness_snapshot(app: FastAPI) -> tuple[bool, dict[str, Any]]:
    checks: dict[str, Any] = {}
    ok = True

    try:
        await app.state.registry.list_users()
        checks["registry"] = {"status": "ok"}
    except Exception as exc:
        ok = False
        checks["registry"] = {"status": "error", "detail": str(exc)}

    monitor_task = getattr(app.state, "instance_monitor_task", None)
    if monitor_task is not None and not monitor_task.done():
        checks["instanceMonitor"] = {"status": "ok"}
    else:
        ok = False
        checks["instanceMonitor"] = {"status": "error", "detail": "Instance monitor task is not running."}

    if os.environ.get("NANOBOT_HEALTH_ASYNC_SPAWN", "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            redis = await create_pool(
                RedisSettings.from_dsn(os.environ.get("ARQ_REDIS_URL", "redis://redis:6379/0"))
            )
            await redis.ping()
            await redis.close()
            checks["asyncSpawnQueue"] = {"status": "ok"}
        except Exception as exc:
            ok = False
            checks["asyncSpawnQueue"] = {"status": "error", "detail": str(exc)}

    return ok, checks


async def _container_health_monitor(app: FastAPI) -> None:
    """Best-effort monitor that restarts exited/dead managed containers."""
    interval_s = max(2, _env_int("NANOBOT_HEALTH_MONITOR_INTERVAL_SECONDS", 10))
    app.state.instance_monitor = {
        "started_at": time.time(),
        "interval_seconds": interval_s,
        "restart_attempts": 0,
        "restart_success": 0,
        "restart_failures": 0,
        "last_run_at": None,
        "last_error": "",
        "last_snapshot": {},
    }
    while True:
        try:
            now = time.time()
            restarted: list[dict[str, Any]] = []
            unhealthy: list[dict[str, Any]] = []
            containers = app.state.spawner.list_instances(all=True)
            health_metrics.instance_total.set(len(containers))
            for c in containers:
                try:
                    if not hasattr(c, "restart"):
                        # In Swarm mode list_instances returns Service objects; Swarm handles restarts.
                        continue
                    c.reload()
                    state = (getattr(c, "attrs", {}) or {}).get("State", {}) or {}
                    status = str(state.get("Status") or getattr(c, "status", "") or "")
                    name = ""
                    try:
                        name = str((getattr(c, "name", "") or "")).lstrip("/")
                    except Exception:
                        name = ""
                    container_id = str(getattr(c, "id", "") or "")
                    if status not in {"running"}:
                        unhealthy.append(
                            {"id": container_id, "name": name, "status": status}
                        )
                        app.state.instance_monitor["restart_attempts"] += 1
                        try:
                            c.restart()
                            app.state.instance_monitor["restart_success"] += 1
                            restarted.append(
                                {"id": container_id, "name": name, "from": status, "to": "running"}
                            )
                        except Exception as exc:
                            app.state.instance_monitor["restart_failures"] += 1
                            unhealthy[-1]["restart_error"] = str(exc)
                except Exception:
                    # Don't let a single container break the loop.
                    continue
            app.state.instance_monitor["last_run_at"] = now
            app.state.instance_monitor["last_error"] = ""
            app.state.instance_monitor["last_snapshot"] = {
                "running": app.state.spawner.count_running_instances(),
                "total": len(containers),
                "unhealthy": unhealthy,
                "restarted": restarted,
            }
            health_metrics.instance_running.set(int(app.state.instance_monitor["last_snapshot"]["running"]))

            # Idle shutdown (best-effort) based on registry last_active timestamps.
            idle_s = _env_int("NANOBOT_HEALTH_IDLE_SHUTDOWN_SECONDS", 0)
            if idle_s > 0:
                try:
                    users = await app.state.registry.list_users()
                    cutoff = datetime.now(UTC) - timedelta(seconds=idle_s)
                    stopped: list[dict[str, Any]] = []
                    for u in users:
                        if (u.get("status") or "") != "active":
                            continue
                        instance_id = str(u.get("container_id") or "")
                        if not instance_id:
                            continue
                        last_active_raw = u.get("last_active") or ""
                        try:
                            last_active = datetime.fromisoformat(str(last_active_raw)).astimezone(UTC)
                        except Exception:
                            continue
                        if last_active < cutoff:
                            app.state.spawner.stop_instance(instance_id)
                            stopped.append({"userId": u.get("id"), "instanceId": instance_id})
                    if stopped:
                        app.state.instance_monitor["last_snapshot"]["stoppedIdle"] = stopped
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.instance_monitor["last_error"] = str(exc)
        await asyncio.sleep(interval_s)


def _staging_root(workspace_root: Path) -> Path:
    return workspace_root / "health-staging"


def _health_for_setup_token(app: FastAPI, setup_token: str) -> HealthWorkspace:
    root = _staging_root(app.state.workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    session_dir = root / setup_token
    session_dir.mkdir(parents=True, exist_ok=True)
    return HealthWorkspace(session_dir)


def _global_health(app: FastAPI) -> HealthWorkspace:
    # Shared workspace state (invites, shared onboarding, etc.)
    return HealthWorkspace(app.state.workspace_root)


def create_app() -> FastAPI:
    _configure_logging()
    workspace_root = _resolve_workspace()
    app = FastAPI(title="nanobot health onboarding")
    app.state.workspace_root = workspace_root
    app.state.health_secret = get_health_vault_secret()
    app.state.channel_links = _channel_links()
    app.state.registry = HealthRegistry()
    app.state.spawner = HealthInstanceSpawner()
    app.state.runtime_metrics = _runtime_metrics_state()

    monitor = WhatsAppBridgeMonitor(
        bridge_url=get_whatsapp_bridge_url(),
        bridge_token=get_whatsapp_bridge_token(),
        fallback_chat_url=app.state.channel_links.get("whatsapp", ""),
        on_status=lambda _payload: None,
    )
    app.state.whatsapp_monitor = monitor

    template_dir = Path(__file__).with_name("templates")
    static_dir = Path(__file__).with_name("static")
    templates = Jinja2Templates(directory=str(template_dir))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = (request.headers.get("X-Request-ID") or "").strip() or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        request.state.error_logged = False
        token = _request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        except Exception as exc:
            if not request.state.error_logged:
                _log_request_error(
                    request,
                    status_code=500,
                    detail="Internal server error.",
                    event="request.unhandled_exception",
                    exc=exc,
                )
                request.state.error_logged = True
            _record_runtime_error(
                request.app,
                request=request,
                status_code=500,
                detail="Internal server error.",
            )
            response = _json_error_response(
                status_code=500,
                detail="Internal server error.",
                request_id=request_id,
            )

        response.headers["X-Request-ID"] = request_id
        runtime = request.app.state.runtime_metrics["requests"]
        runtime["total"] += 1
        if response.status_code >= 500:
            runtime["serverErrors"] += 1
        elif response.status_code >= 400:
            runtime["clientErrors"] += 1
        else:
            runtime["success"] += 1
        _request_id_ctx.reset(token)
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = _request_id_from_request(request)
        if exc.status_code >= 500 and not request.state.error_logged:
            _log_request_error(
                request,
                status_code=exc.status_code,
                detail=exc.detail,
                event="request.http_exception",
            )
            request.state.error_logged = True
            _record_runtime_error(
                request.app,
                request=request,
                status_code=exc.status_code,
                detail=exc.detail,
            )
        return _json_error_response(
            status_code=exc.status_code,
            detail=exc.detail,
            request_id=request_id,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _json_error_response(
            status_code=422,
            detail=exc.errors(),
            request_id=_request_id_from_request(request),
        )

    @app.on_event("startup")
    async def startup() -> None:
        await monitor.start()
        app.state.instance_monitor_task = asyncio.create_task(_container_health_monitor(app))

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await monitor.stop()
        task = getattr(app.state, "instance_monitor_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except Exception:
                pass

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ok, checks = await _readiness_snapshot(app)
        return JSONResponse(
            {
                "status": "ok" if ok else "degraded",
                "checks": checks,
            },
            status_code=200 if ok else 503,
        )

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        monitor_state = getattr(app.state, "instance_monitor", {}) or {}
        snap = monitor_state.get("last_snapshot") or {}
        ready_ok, readiness_checks = await _readiness_snapshot(app)
        runtime = app.state.runtime_metrics
        # Prometheus-style text is nice, but JSON is friendlier for early stages.
        return JSONResponse(
            {
                "status": "ok",
                "release": runtime["release"],
                "readiness": {
                    "status": "ok" if ready_ok else "degraded",
                    "checks": readiness_checks,
                },
                "runtime": {
                    "requests": runtime["requests"],
                    "lastError": runtime["lastError"],
                },
                "spawns": {
                    "attempts": health_metrics.spawn_attempts.value,
                    "success": health_metrics.spawn_success.value,
                    "failures": health_metrics.spawn_failures.value,
                },
                "instances": {
                    "running": health_metrics.instance_running.value,
                    "total": health_metrics.instance_total.value,
                    "unhealthy": len(snap.get("unhealthy") or []),
                    "restartAttempts": monitor_state.get("restart_attempts", 0),
                    "restartSuccess": monitor_state.get("restart_success", 0),
                    "restartFailures": monitor_state.get("restart_failures", 0),
                },
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "landing.html", {})

    @app.post("/api/signup")
    async def signup(request: Request, submission: SignupSubmission) -> JSONResponse:
        try:
            user = await app.state.registry.create_user(
                name=submission.name,
                timezone=submission.timezone or "UTC",
            )
        except Exception as exc:
            request.state.error_logged = True
            _log_request_error(
                request,
                status_code=502,
                detail="Unable to create setup session right now.",
                event="signup.failed",
                exc=exc,
            )
            _record_runtime_error(
                request.app,
                request=request,
                status_code=502,
                detail="Unable to create setup session right now.",
            )
            raise HTTPException(status_code=502, detail="Unable to create setup session right now.") from exc
        # Create a per-user staging workspace keyed by the registry setup token.
        health = _health_for_setup_token(app, user.setup_token)
        health.create_setup_session_with_token(user.setup_token)
        return JSONResponse({"status": "ok", "setupToken": user.setup_token, "userId": user.id})

    @app.get("/setup/{setup_token}", response_class=HTMLResponse)
    async def setup_form(request: Request, setup_token: str) -> HTMLResponse:
        health = _health_for_setup_token(app, setup_token)
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        display_name = ""
        try:
            record = await app.state.registry.get_by_setup_token(setup_token)
            if record:
                display_name = (record.name or "").strip()
        except Exception:
            display_name = ""
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "setup_token": setup_token,
                "display_name": display_name,
                "setup": setup,
                "channel_links": app.state.channel_links,
                "provider_choices": PROVIDER_CHOICES,
            },
        )

    @app.get("/api/setup/{setup_token}/status")
    async def setup_status(setup_token: str) -> JSONResponse:
        health = _health_for_setup_token(app, setup_token)
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
        health = _health_for_setup_token(app, setup_token)
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
        health = _health_for_setup_token(app, setup_token)
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
        health = _health_for_setup_token(app, setup_token)
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
        health = _health_for_setup_token(app, setup_token)
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
        health = _health_for_setup_token(app, setup_token)
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        updated = health.store_profile_draft(
            submission=submission.model_dump(),
            secret=app.state.health_secret,
        )
        return JSONResponse({"status": "ok", "setup": updated})

    @app.post("/api/setup/{setup_token}/activate")
    async def setup_activate(request: Request, setup_token: str) -> JSONResponse:
        health = _health_for_setup_token(app, setup_token)
        setup = health.validate_setup_token(setup_token)
        if not setup:
            raise HTTPException(status_code=404, detail="Setup session not found or expired.")
        payload = _setup_status_payload(
            health,
            secret=app.state.health_secret,
            fallback_links=app.state.channel_links,
            bridge_snapshot=monitor.snapshot,
        )
        if not any(
            bool(item.get("connected"))
            for item in payload["channels"].values()
            if isinstance(item, dict)
        ):
            raise HTTPException(status_code=400, detail="Connect Telegram first.")
        submission = health.load_profile_draft_submission(secret=app.state.health_secret)
        if not submission:
            submission = _default_onboarding_submission(setup_payload=payload)

        telegram_token = (
            health.load_setup_secrets(secret=app.state.health_secret)
            .get("telegram", {})
            .get("bot_token")
        )
        if not telegram_token:
            raise HTTPException(status_code=400, detail="Paste your Telegram bot token first.")

        if os.environ.get("NANOBOT_HEALTH_ASYNC_SPAWN", "").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                from arq import create_pool
                from arq.connections import RedisSettings

                redis = await create_pool(
                    RedisSettings.from_dsn(os.environ.get("ARQ_REDIS_URL", "redis://redis:6379/0"))
                )
                await redis.enqueue_job("spawn_instance_job", setup_token)
            except Exception as exc:
                request.state.error_logged = True
                _log_request_error(
                    request,
                    status_code=502,
                    detail=f"Unable to queue provisioning: {exc}",
                    event="activate.queue_failed",
                    exc=exc,
                )
                _record_runtime_error(
                    request.app,
                    request=request,
                    status_code=502,
                    detail=f"Unable to queue provisioning: {exc}",
                )
                raise HTTPException(status_code=502, detail=f"Unable to queue provisioning: {exc}") from exc
            return JSONResponse({"status": "ok", "state": "provisioning"})

        profile = persist_health_onboarding(
            app.state.workspace_root,
            submission,
            invite=None,
            secret=app.state.health_secret,
        )

        if payload["channels"].get("telegram", {}).get("connected") and telegram_token:
            try:
                await register_telegram_commands(telegram_token)
            except Exception:
                pass

        # Spawn the per-user coach container.
        config_json = _load_health_instance_config_template()
        try:
            config_json.setdefault("agents", {}).setdefault("defaults", {})["timezone"] = profile.get(
                "timezone", "UTC"
            )
        except Exception:
            pass
        minimax_api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        spawn = None
        warnings: list[str] = []
        if not minimax_api_key:
            warnings.append("MINIMAX_API_KEY is not set; the coach may not be able to reply.")
        try:
            health_metrics.spawn_attempts.inc()
            record = await app.state.registry.get_by_setup_token(setup_token)
            tier = (record.tier if record else "standard") if record else "standard"
            spawn = app.state.spawner.spawn_instance(
                user_id=setup_token,
                config_json=config_json,
                onboarding_submission=submission,
                tier=tier,
                extra_env={
                    "MINIMAX_API_KEY": minimax_api_key,
                    "TELEGRAM_BOT_TOKEN": telegram_token,
                    "HEALTH_VAULT_KEY": os.environ.get("HEALTH_VAULT_KEY", "").strip()
                    or app.state.health_secret,
                },
            )
            health_metrics.spawn_success.inc()
        except Exception as exc:
            health_metrics.spawn_failures.inc()
            request.state.error_logged = True
            _log_request_error(
                request,
                status_code=502,
                detail=f"Unable to spawn coach container: {exc}",
                event="activate.spawn_failed",
                exc=exc,
            )
            _record_runtime_error(
                request.app,
                request=request,
                status_code=502,
                detail=f"Unable to spawn coach container: {exc}",
            )
            warnings.append(f"Unable to spawn coach container: {exc}")
        if not spawn:
            raise HTTPException(
                status_code=502,
                detail="Unable to start your coach: " + "; ".join(warnings) if warnings else "Container spawn failed.",
            )

        try:
            registry_user_id = record.id if record else setup_token
            await app.state.registry.set_container(
                user_id=registry_user_id,
                container_id=spawn.container_id,
                workspace_volume=spawn.volume_name,
                status="active",
            )
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
                "containerId": spawn.container_id,
                "warnings": warnings,
            }
        )

    @app.get("/api/admin/status")
    async def admin_status() -> JSONResponse:
        users = await app.state.registry.list_users()
        monitor_state = getattr(app.state, "instance_monitor", {}) or {}
        return JSONResponse(
            {
                "status": "ok",
                "users": users,
                "instances": monitor_state.get("last_snapshot") or {},
                "instanceMonitor": {
                    "startedAt": monitor_state.get("started_at"),
                    "intervalSeconds": monitor_state.get("interval_seconds"),
                    "lastRunAt": monitor_state.get("last_run_at"),
                    "restartAttempts": monitor_state.get("restart_attempts"),
                    "restartSuccess": monitor_state.get("restart_success"),
                    "restartFailures": monitor_state.get("restart_failures"),
                    "lastError": monitor_state.get("last_error", ""),
                },
                "runtime": app.state.runtime_metrics,
            }
        )

    @app.post("/api/instance/{setup_token}/ping")
    async def instance_ping(setup_token: str) -> JSONResponse:
        record = await app.state.registry.get_by_setup_token(setup_token)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown setup token.")
        try:
            await app.state.registry.update_last_active(record.id)
        except Exception:
            pass
        return JSONResponse({"status": "ok"})

    @app.get("/onboard/{invite}", response_class=HTMLResponse)
    async def onboard_form(request: Request, invite: str) -> HTMLResponse:
        health = _global_health(app)
        invite_meta = health.validate_invite(invite)
        if not invite_meta:
            raise HTTPException(status_code=404, detail="Invite not found, expired, or already used.")
        return templates.TemplateResponse(
            request,
            "onboard.html",
            {
                "invite": invite,
                "invite_meta": invite_meta,
                "workspace": str(workspace_root),
                "channel_links": app.state.channel_links,
            },
        )

    @app.post("/api/onboard/{invite}/submit")
    async def onboard_submit(invite: str, submission: OnboardingSubmission) -> JSONResponse:
        health = _global_health(app)
        invite_meta = health.validate_invite(invite)
        if not invite_meta:
            raise HTTPException(status_code=404, detail="Invite not found, expired, or already used.")
        payload = submission.model_dump()
        profile = persist_health_onboarding(
            app.state.workspace_root,
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

    @app.get("/chat/{token}", response_class=HTMLResponse)
    async def chat_page(request: Request, token: str) -> HTMLResponse:
        if resolve_health_chat_workspace(token=token, workspace_root=app.state.workspace_root) is None:
            raise HTTPException(
                status_code=404,
                detail="Companion workspace not found. Finish setup, use your chat link after activation, or open Telegram.",
            )
        return templates.TemplateResponse(
            request,
            "chat.html",
            {"chat_token": token},
        )

    @app.get("/api/chat/{token}/companion-status")
    async def chat_companion_status(token: str) -> JSONResponse:
        ws = resolve_health_chat_workspace(token=token, workspace_root=app.state.workspace_root)
        if ws is None:
            raise HTTPException(status_code=404, detail="Unknown chat token.")
        memory_md = ws / "memory" / "MEMORY.md"
        stage = parse_lifecycle_stage(memory_md)
        topics: list[str] = []
        try:
            raw = memory_md.read_text(encoding="utf-8")
            for label, needle in (
                ("Symptom trends", "## Symptom Trends"),
                ("Adherence patterns", "## Adherence Patterns"),
                ("Goals and supports", "## Goals And Supports"),
            ):
                idx = raw.find(needle)
                if idx == -1:
                    continue
                chunk = raw[idx : idx + 280]
                if "No durable" not in chunk and "not recorded yet" not in chunk:
                    topics.append(label)
        except OSError:
            pass
        hour = datetime.now(UTC).hour
        companion_state = "sleepy" if hour < 5 or hour >= 23 else "idle"
        return JSONResponse(
            {
                "stage": stage,
                "knowledgeScore": rough_knowledge_score(memory_md),
                "topicsLearned": topics[:8],
                "companionState": companion_state,
            }
        )

    @app.post("/api/chat/{token}/message")
    async def chat_message_stream(token: str, body: WebChatMessage) -> StreamingResponse:
        ws = resolve_health_chat_workspace(token=token, workspace_root=app.state.workspace_root)
        if ws is None:
            raise HTTPException(status_code=404, detail="Unknown chat token.")

        lock = _web_chat_lock(app, str(ws.resolve()))
        session_key = f"health_web:{token}"

        async def stream_with_lock():
            async with lock:
                q: asyncio.Queue = asyncio.Queue()
                final_holder: dict[str, str] = {"text": ""}

                async def on_stream(delta: str) -> None:
                    await q.put(("delta", delta))

                async def on_stream_end(*, resuming: bool = False) -> None:
                    await q.put(("end", resuming))

                async def run_turn() -> None:
                    try:
                        bot = _web_chat_nanobot(app, ws)
                        resp = await bot._loop.process_direct(
                            body.message,
                            session_key=session_key,
                            channel="web",
                            chat_id=token,
                            on_stream=on_stream,
                            on_stream_end=on_stream_end,
                        )
                        final_holder["text"] = (getattr(resp, "content", None) or "") if resp else ""
                        await q.put(("done", None))
                    except Exception as exc:
                        await q.put(("error", str(exc)))

                task = asyncio.create_task(run_turn())
                try:
                    while True:
                        kind, data = await asyncio.wait_for(q.get(), timeout=300.0)
                        if kind == "delta":
                            yield f"data: {json.dumps({'type': 'token', 'text': data})}\n\n"
                        elif kind == "end":
                            continue
                        elif kind == "done":
                            yield f"data: {json.dumps({'type': 'complete', 'text': final_holder['text']})}\n\n"
                            break
                        elif kind == "error":
                            yield f"data: {json.dumps({'type': 'error', 'message': data})}\n\n"
                            break
                finally:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except Exception:
                            pass

        return StreamingResponse(
            stream_with_lock(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
