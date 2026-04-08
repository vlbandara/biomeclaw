"""Docker-based per-user nanobot instance spawner.

This module is used by the hosted health onboarding surface to create an isolated
nanobot gateway per customer. It is intentionally minimal and favors clarity over
generality (initial target: a handful of users).
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

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

    _LABEL_MANAGED_BY = "nanobot.health.managed"
    _LABEL_USER_ID = "nanobot.health.user_id"

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

    def _spawn_mode(self) -> str:
        """Spawn mode: 'docker' (default), 'swarm', or 'remote'."""
        mode = _env("NANOBOT_HEALTH_SPAWN_MODE", "docker").lower()
        return mode if mode in {"docker", "swarm", "remote"} else "docker"

    def _remote_spawner_url(self) -> str:
        return _env("NANOBOT_HEALTH_REMOTE_SPAWNER_URL", "")

    def stop_instance(self, instance_id: str) -> None:
        """Stop/remove an instance by id (best-effort)."""
        mode = self._spawn_mode()
        if mode == "remote":
            url = self._remote_spawner_url()
            if not url:
                return
            try:
                with httpx.Client(timeout=30.0) as client:
                    client.post(url.rstrip("/") + "/stop", json={"instance_id": instance_id})
            except Exception:
                return
            return

        client = self._client()
        if mode == "swarm":
            try:
                service = client.services.get(instance_id)
                service.remove()
            except Exception:
                pass
            return
        try:
            container = client.containers.get(instance_id)
            try:
                container.stop(timeout=10)
            except Exception:
                pass
        except Exception:
            pass

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

    def _storage_mode(self) -> str:
        mode = _env("NANOBOT_HEALTH_INSTANCE_STORAGE", "volume").lower()
        return mode if mode in {"volume", "bind"} else "volume"

    def _host_workspace_root(self) -> Path:
        raw = _env("NANOBOT_HEALTH_INSTANCE_WORKSPACE_ROOT", "/mnt/nanohealth/workspaces")
        return Path(raw).expanduser().resolve()

    def _host_workspace_dir(self, user_id: str) -> Path:
        safe = "".join(ch for ch in user_id if ch.isalnum() or ch in {"-", "_"}).strip("_-")
        return self._host_workspace_root() / safe

    def _container_name(self, user_id: str) -> str:
        prefix = _env("NANOBOT_HEALTH_INSTANCE_CONTAINER_PREFIX", "nanohealth")
        safe = "".join(ch for ch in user_id if ch.isalnum() or ch in {"-", "_"}).strip("_-")
        return f"{prefix}-{safe}"

    def _workspace_in_container(self) -> str:
        return _env("NANOBOT_HEALTH_INSTANCE_WORKSPACE", "/data/workspace")

    def _volume_mountpoint(self) -> str:
        return _env("NANOBOT_HEALTH_INSTANCE_MOUNTPOINT", "/data")

    def _memory_limit(self, *, tier: str | None = None) -> str:
        t = (tier or "").strip().lower()
        if t:
            override = _env(f"NANOBOT_HEALTH_TIER_{t.upper()}_MEMORY", "")
            if override:
                return override
        return _env("NANOBOT_HEALTH_INSTANCE_MEMORY", "512m")

    def _nano_cpus(self, *, tier: str | None = None) -> int | None:
        t = (tier or "").strip().lower()
        raw = ""
        if t:
            raw = _env(f"NANOBOT_HEALTH_TIER_{t.upper()}_NANO_CPUS", "")
        if not raw:
            raw = _env("NANOBOT_HEALTH_INSTANCE_NANO_CPUS", "")
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("NANOBOT_HEALTH_INSTANCE_NANO_CPUS must be an integer.") from exc
        if value <= 0:
            raise ValueError("NANOBOT_HEALTH_INSTANCE_NANO_CPUS must be > 0.")
        return value

    def _max_instances(self) -> int | None:
        raw = _env("NANOBOT_HEALTH_MAX_INSTANCES", "")
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("NANOBOT_HEALTH_MAX_INSTANCES must be an integer.") from exc
        if value <= 0:
            # Treat 0/negative as "no spawns allowed" (explicit safety switch).
            return 0
        return value

    def _restart_policy(self) -> dict[str, Any]:
        return {"Name": "unless-stopped"}

    def _container_prefix(self) -> str:
        return _env("NANOBOT_HEALTH_INSTANCE_CONTAINER_PREFIX", "nanohealth").strip() or "nanohealth"

    def _service_name(self, user_id: str) -> str:
        # Reuse container naming for services.
        return self._container_name(user_id)

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

    def _seed_bind_workspace(self, *, host_workspace_dir: Path, workspace_dir: Path) -> None:
        host_workspace_dir.parent.mkdir(parents=True, exist_ok=True)
        if host_workspace_dir.exists():
            shutil.rmtree(host_workspace_dir)
        shutil.copytree(workspace_dir, host_workspace_dir)

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
        workspace_path = self._workspace_in_container()
        tier = env.get("NANOBOT_HEALTH_TIER", "").strip() or None
        nano_cpus = self._nano_cpus(tier=tier)
        storage_mode = self._storage_mode()
        mountpoint = self._volume_mountpoint()

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
            mem_limit=self._memory_limit(tier=tier),
            nano_cpus=nano_cpus,
            network=self.network or None,
            labels={
                self._LABEL_MANAGED_BY: "1",
                self._LABEL_USER_ID: user_id,
            },
            environment={
                **env,
                "NANOBOT_WORKSPACE": workspace_path,
                # Health mode needs vault key for profile/vault storage + anonymizer.
                "HEALTH_VAULT_KEY": env.get("HEALTH_VAULT_KEY", get_health_vault_secret()),
            },
            volumes=(
                {volume_name: {"bind": mountpoint, "mode": "rw"}}
                if storage_mode == "volume"
                else {str(self._host_workspace_dir(user_id)): {"bind": workspace_path, "mode": "rw"}}
            ),
        )
        return str(container.id)

    def _start_gateway_service(
        self,
        *,
        user_id: str,
        volume_name: str,
        env: dict[str, str],
    ) -> str:
        """Start a Swarm service for the user gateway."""
        client = self._client()
        self._ensure_image(self.image)
        name = self._service_name(user_id)
        workspace_path = self._workspace_in_container()
        tier = env.get("NANOBOT_HEALTH_TIER", "").strip() or None
        nano_cpus = self._nano_cpus(tier=tier)
        storage_mode = self._storage_mode()
        mountpoint = self._volume_mountpoint()

        docker = _docker()
        if storage_mode == "bind":
            mounts = [
                docker.types.Mount(
                    target=workspace_path,
                    source=str(self._host_workspace_dir(user_id)),
                    type="bind",
                    read_only=False,
                )
            ]
        else:
            mounts = [
                docker.types.Mount(
                    target=mountpoint,
                    source=volume_name,
                    type="volume",
                    read_only=False,
                )
            ]

        # Idempotency for retries: replace service with same name.
        try:
            existing = client.services.get(name)
            try:
                existing.remove()
            except Exception:
                pass
        except Exception:
            pass

        # Resource limits (Swarm expects nanoseconds of CPU via nano_cpus).
        resources = None
        try:
            limits: dict[str, Any] = {}
            if nano_cpus is not None:
                limits["NanoCPUs"] = int(nano_cpus)
            # mem_limit like "512m" -> bytes. Keep it simple: Docker accepts strings in containers,
            # Swarm expects bytes; implement a tiny parser.
            mem_raw = (self._memory_limit(tier=tier) or "").strip().lower()
            if mem_raw:
                mul = 1
                if mem_raw.endswith("g"):
                    mul = 1024**3
                    mem_raw = mem_raw[:-1]
                elif mem_raw.endswith("m"):
                    mul = 1024**2
                    mem_raw = mem_raw[:-1]
                elif mem_raw.endswith("k"):
                    mul = 1024
                    mem_raw = mem_raw[:-1]
                try:
                    limits["MemoryBytes"] = int(float(mem_raw) * mul)
                except Exception:
                    pass
            if limits:
                resources = docker.types.Resources(limits=limits)
        except Exception:
            resources = None

        task_tmpl = docker.types.TaskTemplate(
            container_spec=docker.types.ContainerSpec(
                image=self.image,
                command=["gateway", "--config", "/data/workspace/config.json"],
                env=[
                    *(f"{k}={v}" for k, v in {**env, "NANOBOT_WORKSPACE": workspace_path}.items()),
                    f"HEALTH_VAULT_KEY={env.get('HEALTH_VAULT_KEY', get_health_vault_secret())}",
                ],
                mounts=mounts,
                labels={
                    self._LABEL_MANAGED_BY: "1",
                    self._LABEL_USER_ID: user_id,
                },
            ),
            restart_policy=docker.types.RestartPolicy(condition="any"),
            resources=resources,
        )

        service = client.services.create(
            name=name,
            task_template=task_tmpl,
            networks=[self.network] if self.network else None,
            labels={
                self._LABEL_MANAGED_BY: "1",
                self._LABEL_USER_ID: user_id,
            },
        )
        return str(service.id)

    def count_running_instances(self) -> int:
        """Return number of running managed gateway containers."""
        client = self._client()
        prefix = self._container_prefix()
        if self._spawn_mode() == "swarm":
            services = client.services.list(
                filters={
                    "label": f"{self._LABEL_MANAGED_BY}=1",
                    "name": prefix,
                }
            )
            return len(services)
        if self._spawn_mode() == "remote":
            # Remote scheduler owns capacity; report unknown as 0.
            return 0
        containers = client.containers.list(
            all=False,
            filters={
                "label": f"{self._LABEL_MANAGED_BY}=1",
                # Name filtering is substring-based; keep it as a second guard.
                "name": prefix,
            },
        )
        return len(containers)

    def list_instances(self, *, all: bool = True) -> list[Any]:
        """List managed instances (containers in docker mode; services in swarm mode)."""
        client = self._client()
        prefix = self._container_prefix()
        if self._spawn_mode() == "swarm":
            return client.services.list(
                filters={
                    "label": f"{self._LABEL_MANAGED_BY}=1",
                    "name": prefix,
                }
            )
        if self._spawn_mode() == "remote":
            return []
        return client.containers.list(
            all=all,
            filters={
                "label": f"{self._LABEL_MANAGED_BY}=1",
                "name": prefix,
            },
        )

    def enforce_capacity(self) -> None:
        """Raise if the configured max instance count is reached."""
        limit = self._max_instances()
        if limit is None:
            return
        running = self.count_running_instances()
        if running >= limit:
            raise RuntimeError(
                f"NanoHealth capacity reached: {running} running instances (max {limit})."
            )

    def spawn_instance(
        self,
        *,
        user_id: str,
        config_json: dict[str, Any],
        onboarding_submission: dict[str, Any],
        telegram_connected: bool = True,
        extra_env: dict[str, str] | None = None,
        tier: str | None = None,
    ) -> SpawnResult:
        """Create/seed a volume and start a gateway container.

        - `config_json` is written as `~/.nanobot/config.json` inside the volume workspace.
        - `onboarding_submission` is passed into `persist_health_onboarding` to render SOUL/USER/etc.
        """
        if self._spawn_mode() != "remote":
            self.enforce_capacity()

        storage_mode = self._storage_mode()
        volume_name = self._volume_name(user_id)
        if self._spawn_mode() == "remote":
            # Delegate to an external scheduler (ECS/Cloud Run/Fly/etc).
            url = self._remote_spawner_url()
            if not url:
                raise RuntimeError(
                    "NANOBOT_HEALTH_REMOTE_SPAWNER_URL is required when NANOBOT_HEALTH_SPAWN_MODE=remote."
                )
            payload = {
                "user_id": user_id,
                "config_json": config_json,
                "onboarding_submission": onboarding_submission,
                "telegram_connected": telegram_connected,
                "extra_env": dict(extra_env or {}),
            }
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(url.rstrip("/") + "/spawn", json=payload)
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
            instance_id = str(data.get("instance_id") or data.get("container_id") or "")
            if not instance_id:
                raise RuntimeError("Remote spawner did not return instance_id.")
            return SpawnResult(container_id=instance_id, volume_name="", workspace_path="")

        if storage_mode == "volume":
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

            if storage_mode == "bind":
                self._seed_bind_workspace(
                    host_workspace_dir=self._host_workspace_dir(user_id),
                    workspace_dir=workspace,
                )
            else:
                self._seed_volume(volume_name=volume_name, workspace_dir=workspace)

        env = dict(extra_env or {})
        if tier and "NANOBOT_HEALTH_TIER" not in env:
            env["NANOBOT_HEALTH_TIER"] = tier
        if self._spawn_mode() == "swarm":
            container_id = self._start_gateway_service(
                user_id=user_id,
                volume_name=volume_name,
                env=env,
            )
        else:
            container_id = self._start_gateway_container(
                user_id=user_id,
                volume_name=volume_name,
                env=env,
            )
        return SpawnResult(
            container_id=container_id,
            volume_name=(str(self._host_workspace_dir(user_id)) if storage_mode == "bind" else volume_name),
            workspace_path=self._workspace_in_container(),
        )

