"""Route shared inbound bus messages to per-tenant AgentLoops."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.bus.events import OutboundMessage

if TYPE_CHECKING:
    from nanobot.tenant.manager import TenantManager


class TenantRateLimiter:
    """Simple sliding-window per-tenant rate limit (messages per minute)."""

    def __init__(self, per_minute: int) -> None:
        self._per = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, tenant_id: str) -> bool:
        if self._per <= 0:
            return True
        now = time.monotonic()
        q = self._hits[tenant_id]
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self._per:
            return False
        q.append(now)
        return True


class TenantRouter:
    """Consumes the shared inbound queue and forwards to tenant-private buses."""

    def __init__(
        self,
        shared_bus,
        tenant_manager: TenantManager,
        *,
        rate_limit_per_minute: int = 0,
    ):
        self.shared_bus = shared_bus
        self.tenant_manager = tenant_manager
        self._limiter = TenantRateLimiter(rate_limit_per_minute)
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("Tenant router started")
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self.shared_bus.consume_inbound(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                await self.tenant_manager.evict_idle()
                continue
            except asyncio.CancelledError:
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Tenant router inbound error: {}", e)
                continue

            tenant_id = msg.session_key
            if not self._limiter.allow(tenant_id):
                await self.shared_bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="You're sending messages too quickly. Please wait a minute.",
                        metadata={**dict(msg.metadata or {}), "render_as": "text"},
                    )
                )
                continue

            try:
                tenant = await self.tenant_manager.ensure_tenant(msg)
                await tenant.bus.publish_inbound(msg)
            except Exception:
                logger.exception("Failed to route message for tenant {}", tenant_id)
                await self.shared_bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Sorry, something went wrong starting your session.",
                    )
                )

    def stop(self) -> None:
        self._running = False
