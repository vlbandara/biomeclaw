"""SQLite-backed registry for NanoHealth hosted instances.

This is intentionally small and pragmatic (3 users target) while keeping a clear
path to Postgres later.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_registry_path() -> Path:
    raw = os.environ.get("NANOBOT_HEALTH_REGISTRY_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path("~/.nanobot/health-registry.sqlite3").expanduser().resolve()


def _new_token(prefix: str) -> str:
    tok = secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24]
    return f"{prefix}_{tok}"


@dataclass(frozen=True)
class UserRecord:
    id: str
    name: str
    timezone: str
    setup_token: str
    status: str
    created_at: str
    last_active: str | None
    telegram_bot_username: str
    container_id: str
    workspace_volume: str


class HealthRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or _resolve_registry_path()

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA foreign_keys=ON;")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    setup_token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_active TEXT,
                    telegram_bot_token TEXT,
                    telegram_bot_username TEXT NOT NULL DEFAULT '',
                    container_id TEXT NOT NULL DEFAULT '',
                    workspace_volume TEXT NOT NULL DEFAULT ''
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_setup_token ON users(setup_token);"
            )
            await db.commit()

    async def create_user(self, *, name: str, timezone: str) -> UserRecord:
        await self.init()
        user_id = _new_token("usr")
        setup_token = _new_token("setup")
        created_at = _utcnow_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users (
                    id, name, timezone, setup_token, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, name.strip(), timezone.strip(), setup_token, "setup", created_at),
            )
            await db.commit()
        return UserRecord(
            id=user_id,
            name=name.strip(),
            timezone=timezone.strip(),
            setup_token=setup_token,
            status="setup",
            created_at=created_at,
            last_active=None,
            telegram_bot_username="",
            container_id="",
            workspace_volume="",
        )

    async def get_by_setup_token(self, setup_token: str) -> UserRecord | None:
        await self.init()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute_fetchone(
                "SELECT * FROM users WHERE setup_token = ?",
                (setup_token,),
            )
            if not row:
                return None
            return UserRecord(
                id=str(row["id"]),
                name=str(row["name"]),
                timezone=str(row["timezone"]),
                setup_token=str(row["setup_token"]),
                status=str(row["status"]),
                created_at=str(row["created_at"]),
                last_active=str(row["last_active"]) if row["last_active"] else None,
                telegram_bot_username=str(row["telegram_bot_username"] or ""),
                container_id=str(row["container_id"] or ""),
                workspace_volume=str(row["workspace_volume"] or ""),
            )

    async def update_last_active(self, user_id: str) -> None:
        await self.init()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET last_active = ? WHERE id = ?",
                (_utcnow_iso(), user_id),
            )
            await db.commit()

    async def set_telegram(
        self,
        *,
        user_id: str,
        bot_token: str,
        bot_username: str,
    ) -> None:
        await self.init()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET telegram_bot_token = ?, telegram_bot_username = ?
                WHERE id = ?
                """,
                (bot_token.strip(), bot_username.strip(), user_id),
            )
            await db.commit()

    async def set_container(
        self,
        *,
        user_id: str,
        container_id: str,
        workspace_volume: str,
        status: str,
    ) -> None:
        await self.init()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET container_id = ?, workspace_volume = ?, status = ?
                WHERE id = ?
                """,
                (container_id, workspace_volume, status, user_id),
            )
            await db.commit()

    async def list_users(self) -> list[dict[str, Any]]:
        await self.init()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT
                    id, name, timezone, setup_token, status,
                    created_at, last_active,
                    telegram_bot_username, container_id, workspace_volume
                FROM users
                ORDER BY created_at DESC
                """
            )
            return [dict(row) for row in rows]

