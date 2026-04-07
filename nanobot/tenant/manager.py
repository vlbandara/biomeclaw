"""Per-tenant workspace, AgentLoop, and cron; shared provider and outbound bus."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import ensure_dir, safe_filename

if TYPE_CHECKING:
    from nanobot.config.schema import Config
    from nanobot.providers.base import LLMProvider


def _tenant_workspace_path(base_dir: Path, tenant_id: str) -> Path:
    safe = safe_filename(tenant_id.replace(":", "_"))
    return (base_dir / safe).resolve()


def _init_tenant_workspace(workspace: Path, tenant_id: str) -> str:
    """Create tenant dir, onboarding marker, stable user_token, and tenant_id marker."""
    ensure_dir(workspace)
    (workspace / ".tenant_id").write_text(tenant_id, encoding="utf-8")
    meta_path = workspace / ".tenant_meta.json"
    user_token: str
    if meta_path.is_file():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            user_token = str(data.get("user_token") or "").strip() or _new_user_token()
        except Exception:
            user_token = _new_user_token()
    else:
        user_token = _new_user_token()
    meta_path.write_text(
        json.dumps({"user_token": user_token}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    marker = workspace / ".health_chat_onboarding"
    if not marker.exists():
        marker.write_text("", encoding="utf-8")
    ensure_dir(workspace / "memory")
    return user_token


def _new_user_token() -> str:
    import secrets

    return f"USER-{secrets.token_hex(3).upper()}"


@dataclass
class TenantRuntime:
    tenant_id: str
    workspace: Path
    bus: MessageBus
    loop: AgentLoop
    cron: CronService
    user_token: str
    run_task: asyncio.Task | None = None
    last_active: float = field(default_factory=time.time)


class TenantManager:
    """Maps channel:chat_id tenants to workspaces and running AgentLoops."""

    def __init__(
        self,
        *,
        base_dir: Path,
        config: Config,
        provider: LLMProvider,
        shared_outbound: asyncio.Queue,
        idle_seconds: int = 0,
    ):
        self.base_dir = ensure_dir(base_dir)
        self.config = config
        self.provider = provider
        self.shared_outbound = shared_outbound
        self.idle_seconds = idle_seconds
        self._tenants: dict[str, TenantRuntime] = {}
        self._lock = asyncio.Lock()

    def iter_profile_workspaces(self) -> list[Path]:
        """Tenant dirs that finished health onboarding (have profile.json)."""
        out: list[Path] = []
        if not self.base_dir.is_dir():
            return out
        for p in sorted(self.base_dir.iterdir()):
            if not p.is_dir() or p.name.startswith("_"):
                continue
            if (p / "health" / "profile.json").is_file():
                out.append(p)
        return out

    async def ensure_tenant(self, msg: InboundMessage) -> TenantRuntime:
        tenant_id = msg.session_key
        async with self._lock:
            existing = self._tenants.get(tenant_id)
            if existing is not None:
                existing.last_active = time.time()
                if existing.run_task is None or existing.run_task.done():
                    existing.run_task = asyncio.create_task(
                        existing.loop.run(),
                        name=f"nanobot-tenant-{tenant_id}",
                    )
                return existing

            ws = _tenant_workspace_path(self.base_dir, tenant_id)
            if not ws.exists():
                user_token = _init_tenant_workspace(ws, tenant_id)
            else:
                tid_marker = ws / ".tenant_id"
                if not tid_marker.is_file():
                    tid_marker.write_text(tenant_id, encoding="utf-8")
                user_token = _read_user_token(ws)

            bus = MessageBus(outbound=self.shared_outbound, maxsize=100)
            cron_store = ws / "cron" / "jobs.json"
            cron_store.parent.mkdir(parents=True, exist_ok=True)
            cron = CronService(cron_store)

            loop = self._make_agent_loop(bus, ws, cron)
            cron.on_job = self._make_cron_handler(loop, bus)

            t = TenantRuntime(
                tenant_id=tenant_id,
                workspace=ws,
                bus=bus,
                loop=loop,
                cron=cron,
                user_token=user_token,
            )
            t.run_task = asyncio.create_task(
                loop.run(),
                name=f"nanobot-tenant-{tenant_id}",
            )
            self._tenants[tenant_id] = t
            await cron.start()
            logger.info("Started tenant {} workspace={}", tenant_id, ws)
            return t

    async def ensure_tenant_by_workspace(self, workspace: Path) -> TenantRuntime | None:
        """Load or revive a tenant given its workspace path (heartbeat / dream)."""
        workspace = workspace.resolve()
        map_path = workspace / ".tenant_id"
        if not map_path.is_file():
            return None
        tenant_id = map_path.read_text(encoding="utf-8").strip()
        if ":" not in tenant_id:
            return None
        channel, chat_id = tenant_id.split(":", 1)
        msg = InboundMessage(channel=channel, sender_id=chat_id, chat_id=chat_id, content="")
        return await self.ensure_tenant(msg)

    def _make_agent_loop(self, bus: MessageBus, workspace: Path, cron: CronService) -> AgentLoop:
        cfg = self.config
        loop = AgentLoop(
            bus=bus,
            provider=self.provider,
            workspace=workspace,
            model=cfg.agents.defaults.model,
            max_iterations=cfg.agents.defaults.max_tool_iterations,
            context_window_tokens=cfg.agents.defaults.context_window_tokens,
            web_config=cfg.tools.web,
            context_block_limit=cfg.agents.defaults.context_block_limit,
            max_tool_result_chars=cfg.agents.defaults.max_tool_result_chars,
            provider_retry_mode=cfg.agents.defaults.provider_retry_mode,
            exec_config=cfg.tools.exec,
            cron_service=cron,
            restrict_to_workspace=cfg.tools.restrict_to_workspace,
            session_manager=SessionManager(workspace),
            mcp_servers=cfg.tools.mcp_servers,
            channels_config=cfg.channels,
            timezone=cfg.agents.defaults.timezone,
        )
        dream_cfg = cfg.agents.defaults.dream
        if dream_cfg.model_override:
            loop.dream.model = dream_cfg.model_override
        loop.dream.max_batch_size = dream_cfg.max_batch_size
        loop.dream.max_iterations = dream_cfg.max_iterations
        return loop

    def _make_cron_handler(self, loop: AgentLoop, bus: MessageBus):
        async def on_cron_job(job: CronJob) -> str | None:
            from nanobot.agent.tools.cron import CronTool
            from nanobot.agent.tools.message import MessageTool
            from nanobot.utils.evaluator import evaluate_response

            reminder_note = (
                "[Scheduled Task] Timer finished.\n\n"
                f"Task '{job.name}' has been triggered.\n"
                f"Scheduled instruction: {job.payload.message}"
            )
            cron_tool = loop.tools.get("cron")
            cron_token = None
            if isinstance(cron_tool, CronTool):
                cron_token = cron_tool.set_cron_context(True)
            try:
                resp = await loop.process_direct(
                    reminder_note,
                    session_key=f"cron:{job.id}",
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to or "direct",
                )
            finally:
                if isinstance(cron_tool, CronTool) and cron_token is not None:
                    cron_tool.reset_cron_context(cron_token)

            response = resp.content if resp else ""
            message_tool = loop.tools.get("message")
            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                return response

            if job.payload.deliver and job.payload.to and response:
                should_notify = await evaluate_response(
                    response, job.payload.message, self.provider, loop.model,
                )
                if should_notify:
                    await bus.publish_outbound(OutboundMessage(
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to,
                        content=response,
                    ))
            return response

        return on_cron_job

    async def run_dream_all(self) -> None:
        """Run Dream for every onboarded tenant workspace (loaded or load-on-demand)."""
        paths = self.iter_profile_workspaces()
        for ws in paths:
            tenant = await self.ensure_tenant_by_workspace(ws)
            if tenant is None:
                logger.warning("Dream: skip workspace without tenant mapping {}", ws)
                continue
            try:
                await tenant.loop.dream.run()
            except Exception:
                logger.exception("Dream failed for tenant {}", tenant.tenant_id)

    async def evict_idle(self) -> None:
        if self.idle_seconds <= 0:
            return
        now = time.time()
        async with self._lock:
            victims = [
                tid
                for tid, t in self._tenants.items()
                if now - t.last_active > self.idle_seconds
            ]
        for tid in victims:
            await self._evict_one(tid)

    async def _evict_one(self, tenant_id: str) -> None:
        async with self._lock:
            t = self._tenants.pop(tenant_id, None)
        if not t:
            return
        t.loop.stop()
        if t.run_task and not t.run_task.done():
            t.run_task.cancel()
            try:
                await t.run_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Tenant task shutdown {}", tenant_id)
        t.cron.stop()
        await t.loop.close_mcp()
        logger.info("Evicted idle tenant {}", tenant_id)

    async def shutdown_all(self) -> None:
        async with self._lock:
            ids = list(self._tenants.keys())
        for tid in ids:
            await self._evict_one(tid)


def _read_user_token(ws: Path) -> str:
    p = ws / ".tenant_meta.json"
    if not p.is_file():
        return _new_user_token()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        tok = str(data.get("user_token") or "").strip()
        return tok or _new_user_token()
    except Exception:
        return _new_user_token()
