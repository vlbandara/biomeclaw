"""Docker-based per-user nanobot instance spawner.

This module is used by the hosted health onboarding surface to create an isolated
nanobot gateway per customer. It is intentionally minimal and favors clarity over
generality (initial target: a handful of users).
"""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.health.bootstrap import persist_health_onboarding
from nanobot.health.storage import get_health_vault_secret


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _docker():
    try:
        import docker  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Docker SDK not installed. Add dependency 'docker' (docker-py) to use the spawner."
        ) from exc
    return docker


def _tar_from_dir(src_dir: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path in sorted(src_dir.rglob("*")):
            rel = path.relative_to(src_dir)
            tf.add(path, arcname=str(rel))
    buf.seek(0)
    return buf.read()


@dataclass(frozen=True)
class SpawnResult:
    container_id: str
    volume_name: str
    workspace_path: str


class HealthInstanceSpawner:
    """Spawn a docker container running `nanobot gateway` with an isolated workspace."""

    def __init__(
        self,
        *,
        docker_base_url: str | None = None,
        image: str | None = None,
        network: str | None = None,
    ):
        self.docker_base_url = docker_base_url or _env("DOCKER_HOST", "")
        self.image = image or _env("NANOBOT_HEALTH_INSTANCE_IMAGE", "nanobot-ai:latest")
        self.network = network or _env("NANOBOT_HEALTH_INSTANCE_NETWORK", "")

    def _client(self):
        docker = _docker()
        if self.docker_base_url:
            return docker.DockerClient(base_url=self.docker_base_url)
        return docker.from_env()

    def _ensure_image(self, image: str) -> None:
        """Ensure docker image exists locally (pull if missing)."""
        client = self._client()
        try:
            client.images.get(image)
            return
        except Exception:
            # Pull latest for that tag if not present.
            client.images.pull(image)

    def _volume_name(self, user_id: str) -> str:
        prefix = _env("NANOBOT_HEALTH_INSTANCE_VOLUME_PREFIX", "nanohealth")
        safe = "".join(ch for ch in user_id if ch.isalnum() or ch in {"-", "_"}).strip("_-")
        return f"{prefix}-{safe}"

    def _container_name(self, user_id: str) -> str:
        prefix = _env("NANOBOT_HEALTH_INSTANCE_CONTAINER_PREFIX", "nanohealth")
        safe = "".join(ch for ch in user_id if ch.isalnum() or ch in {"-", "_"}).strip("_-")
        return f"{prefix}-{safe}"

    def _workspace_in_container(self) -> str:
        return _env("NANOBOT_HEALTH_INSTANCE_WORKSPACE", "/data/workspace")

    def _volume_mountpoint(self) -> str:
        return _env("NANOBOT_HEALTH_INSTANCE_MOUNTPOINT", "/data")

    def _memory_limit(self) -> str:
        return _env("NANOBOT_HEALTH_INSTANCE_MEMORY", "512m")

    def _restart_policy(self) -> dict[str, Any]:
        return {"Name": "unless-stopped"}

    def _create_or_get_volume(self, volume_name: str) -> None:
        client = self._client()
        try:
            client.volumes.get(volume_name)
        except Exception:
            client.volumes.create(name=volume_name)

    def _seed_volume(self, *, volume_name: str, workspace_dir: Path) -> None:
        client = self._client()
        mountpoint = self._volume_mountpoint()
        archive = _tar_from_dir(workspace_dir)
        self._ensure_image("alpine:3.20")
        helper = client.containers.create(
            image="alpine:3.20",
            command=["sh", "-lc", "sleep 30"],
            volumes={volume_name: {"bind": mountpoint, "mode": "rw"}},
        )
        try:
            helper.start()
            # Ensure directories exist before extraction.
            helper.exec_run(["sh", "-lc", f"mkdir -p {mountpoint}/workspace"])
            ok = helper.put_archive(f"{mountpoint}/workspace", archive)
            helper.exec_run(["sh", "-lc", f"chown -R 1000:1000 {mountpoint}"])
            if not ok:  # pragma: no cover
                raise RuntimeError("Failed to copy workspace archive into docker volume.")
        finally:
            try:
                helper.remove(force=True)
            except Exception:
                pass

    def _start_gateway_container(
        self,
        *,
        user_id: str,
        volume_name: str,
        env: dict[str, str],
    ) -> str:
        client = self._client()
        self._ensure_image(self.image)
        name = self._container_name(user_id)
        mountpoint = self._volume_mountpoint()
        workspace_path = self._workspace_in_container()

        # Idempotency for retries: if a container with the same name exists, replace it.
        try:
            existing = client.containers.get(name)
            try:
                existing.remove(force=True)
            except Exception:
                pass
        except Exception:
            pass

        container = client.containers.run(
            self.image,
            name=name,
            command=["gateway", "--config", "/data/workspace/config.json"],
            detach=True,
            restart_policy=self._restart_policy(),
            mem_limit=self._memory_limit(),
            network=self.network or None,
            environment={
                **env,
                "NANOBOT_WORKSPACE": workspace_path,
                # Health mode needs vault key for profile/vault storage + anonymizer.
                "HEALTH_VAULT_KEY": env.get("HEALTH_VAULT_KEY", get_health_vault_secret()),
            },
            volumes={volume_name: {"bind": mountpoint, "mode": "rw"}},
        )
        return str(container.id)

    def spawn_instance(
        self,
        *,
        user_id: str,
        config_json: dict[str, Any],
        onboarding_submission: dict[str, Any],
        telegram_connected: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> SpawnResult:
        """Create/seed a volume and start a gateway container.

        - `config_json` is written as `~/.nanobot/config.json` inside the volume workspace.
        - `onboarding_submission` is passed into `persist_health_onboarding` to render SOUL/USER/etc.
        """
        volume_name = self._volume_name(user_id)
        self._create_or_get_volume(volume_name)

        with tempfile.TemporaryDirectory(prefix="nanohealth-seed-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)

            # Seed config used by nanobot gateway.
            (workspace / "config.json").write_text(
                __import__("json").dumps(config_json, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            # Create health workspace assets (SOUL/USER/HEARTBEAT/memory).
            persist_health_onboarding(
                workspace,
                onboarding_submission,
                invite=None,
                secret=get_health_vault_secret(),
            )

            # Mark telegram connected in setup.json for hosted overrides, if desired.
            if telegram_connected:
                from nanobot.health.storage import HealthWorkspace

                health = HealthWorkspace(workspace)
                health.create_setup_session()
                # The setup secrets (provider key, telegram token) are expected to already
                # be embedded in config_json for the spawned instance; the hosted overrides
                # are primarily used by the single-workspace hosted setup flow.

            self._seed_volume(volume_name=volume_name, workspace_dir=workspace)

        env = dict(extra_env or {})
        container_id = self._start_gateway_container(
            user_id=user_id,
            volume_name=volume_name,
            env=env,
        )
        return SpawnResult(
            container_id=container_id,
            volume_name=volume_name,
            workspace_path=self._workspace_in_container(),
        )

