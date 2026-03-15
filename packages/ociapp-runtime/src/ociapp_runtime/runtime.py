import asyncio
import contextlib
import shutil
import stat
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from ociapp import DEFAULT_SOCKET_PATH

from .client import execute_request
from .engine import PodmanAdapter
from .errors import (
    ArtifactLoadError,
    InstanceStartupError,
    OCIAppRuntimeError,
    RequestTimeoutError,
    ResponseProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class RuntimeEngine(Protocol):
    """Defines the engine operations needed by the runtime."""

    def load_archive(self, artifact_path: Path) -> str:
        """Loads an OCI archive and returns an image reference."""

    def run_container(
        self, image_reference: str, mount_dir: Path, container_name: str
    ) -> str:
        """Starts a worker container and returns its container id."""

    def stop_container(self, container_id: str, timeout_seconds: float) -> None:
        """Stops a running worker container."""

    @staticmethod
    def build_container_name(artifact_path: Path) -> str:
        """Builds a stable worker container name prefix."""


class InstanceState(StrEnum):
    """Represents the lifecycle state of a worker instance."""

    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"


@dataclass(slots=True)
class WorkerInstance:
    """Tracks runtime metadata for a single container worker."""

    image_key: str
    container_id: str
    container_name: str
    mount_dir: Path
    socket_path: Path
    state: InstanceState
    last_used_at: float
    active_request_count: int = 0


@dataclass(slots=True)
class ImagePool:
    """Tracks warm worker instances for one OCIApp artifact."""

    artifact_path: Path
    image_reference: str | None = None
    instances: list[WorkerInstance] = field(default_factory=list)


class Runtime:
    """Executes OCIApp artifacts through a warm pool of Podman workers."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        runtime_root: Path | str | None = None,
        engine: RuntimeEngine | None = None,
        startup_timeout: float = 10.0,
        request_timeout: float = 30.0,
        shutdown_timeout: float = 10.0,
        idle_timeout: float = 60.0,
        reaper_interval: float = 1.0,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self._runtime_root = (
            Path(gettempdir()) / "ociapp-runtime"
            if runtime_root is None
            else Path(runtime_root)
        ).resolve()
        self._engine = engine or PodmanAdapter()
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._idle_timeout = idle_timeout
        self._reaper_interval = reaper_interval
        self._clock = clock or time.monotonic
        self._pools: dict[str, ImagePool] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._started = False

    async def __aenter__(self) -> "Runtime":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Starts the runtime background tasks."""

        if self._started:
            return

        self._runtime_root.mkdir(parents=True, exist_ok=True)
        self._reaper_task = asyncio.create_task(
            self._run_reaper(), name="ociapp-runtime-reaper"
        )
        self._started = True

    async def close(self) -> None:
        """Stops all workers and the background reaper."""

        if not self._started:
            return

        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None

        async with self._lock:
            instances = [
                instance for pool in self._pools.values() for instance in pool.instances
            ]
            for instance in instances:
                instance.state = InstanceState.STOPPING
            self._pools.clear()

        for instance in instances:
            await self._stop_instance(instance)

        _prune_runtime_root(self._runtime_root)
        self._started = False

    async def execute(
        self, image_path: Path | str, request: dict[str, object]
    ) -> dict[str, object]:
        """Executes one OCIApp request against an artifact worker."""

        if not self._started:
            raise OCIAppRuntimeError(
                "runtime must be started before executing requests"
            )

        artifact_path = Path(image_path).resolve()
        if not artifact_path.exists():
            raise ArtifactLoadError(f"OCIApp artifact does not exist: {artifact_path}")

        instance = await self._acquire_instance(artifact_path)
        retire_instance = False
        try:
            return await asyncio.wait_for(
                execute_request(instance.socket_path, request),
                timeout=self._request_timeout,
            )
        except asyncio.TimeoutError as exc:
            retire_instance = True
            raise RequestTimeoutError(
                f"request timed out after {self._request_timeout}s"
            ) from exc
        except ResponseProtocolError:
            retire_instance = True
            raise
        finally:
            await self._release_instance(instance, retire=retire_instance)

    async def _acquire_instance(self, artifact_path: Path) -> WorkerInstance:
        image_key = str(artifact_path)
        async with self._lock:
            pool = self._pools.get(image_key)
            if pool is None:
                pool = ImagePool(artifact_path=artifact_path)
                self._pools[image_key] = pool

            ready_instance = _find_ready_instance(pool.instances)
            if ready_instance is not None:
                ready_instance.state = InstanceState.BUSY
                ready_instance.active_request_count += 1
                ready_instance.last_used_at = self._clock()
                return ready_instance

            if pool.image_reference is None:
                pool.image_reference = self._engine.load_archive(artifact_path)

            image_reference = pool.image_reference
            assert image_reference is not None
            container_name = (
                f"{self._engine.build_container_name(artifact_path)}-{uuid4().hex[:8]}"
            )
            mount_dir = self._runtime_root / artifact_path.stem / uuid4().hex
            socket_path = mount_dir / Path(DEFAULT_SOCKET_PATH).name
            container_id = self._engine.run_container(
                image_reference, mount_dir, container_name
            )
            instance = WorkerInstance(
                image_key=image_key,
                container_id=container_id,
                container_name=container_name,
                mount_dir=mount_dir,
                socket_path=socket_path,
                state=InstanceState.STARTING,
                last_used_at=self._clock(),
            )
            pool.instances.append(instance)

        try:
            await self._wait_for_socket(instance.socket_path)
        except Exception:
            await self._remove_instance(instance)
            await self._stop_instance(instance)
            raise

        async with self._lock:
            instance.state = InstanceState.BUSY
            instance.active_request_count += 1
            instance.last_used_at = self._clock()

        return instance

    async def _release_instance(self, instance: WorkerInstance, retire: bool) -> None:
        should_stop = False
        async with self._lock:
            instance.active_request_count = max(0, instance.active_request_count - 1)
            instance.last_used_at = self._clock()
            if retire:
                instance.state = InstanceState.STOPPING
                _remove_instance_from_pool(self._pools, instance)
                should_stop = True
            elif instance.active_request_count == 0:
                instance.state = InstanceState.READY
            else:
                instance.state = InstanceState.BUSY

        if should_stop:
            await self._stop_instance(instance)

    async def _remove_instance(self, instance: WorkerInstance) -> None:
        async with self._lock:
            _remove_instance_from_pool(self._pools, instance)

    async def _stop_instance(self, instance: WorkerInstance) -> None:
        self._engine.stop_container(instance.container_id, self._shutdown_timeout)
        _remove_directory_tree(instance.mount_dir)

    async def _wait_for_socket(self, socket_path: Path) -> None:
        deadline = self._clock() + self._startup_timeout
        while self._clock() < deadline:
            if _is_socket(socket_path):
                return
            await asyncio.sleep(0.05)

        raise InstanceStartupError(
            f"worker socket did not appear within {self._startup_timeout}s: {socket_path}"
        )

    async def _run_reaper(self) -> None:
        while True:
            await asyncio.sleep(self._reaper_interval)
            candidates: list[WorkerInstance] = []
            async with self._lock:
                now = self._clock()
                for pool in self._pools.values():
                    for instance in list(pool.instances):
                        if (
                            instance.state == InstanceState.READY
                            and instance.active_request_count == 0
                            and now - instance.last_used_at >= self._idle_timeout
                        ):
                            instance.state = InstanceState.STOPPING
                            candidates.append(instance)
                for instance in candidates:
                    _remove_instance_from_pool(self._pools, instance)

            for instance in candidates:
                await self._stop_instance(instance)


def _find_ready_instance(instances: list[WorkerInstance]) -> WorkerInstance | None:
    for instance in instances:
        if instance.state == InstanceState.READY and instance.active_request_count == 0:
            return instance

    return None


def _remove_instance_from_pool(
    pools: dict[str, ImagePool], instance: WorkerInstance
) -> None:
    pool = pools.get(instance.image_key)
    if pool is None:
        return

    pool.instances = [item for item in pool.instances if item is not instance]


def _is_socket(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False

    return stat.S_ISSOCK(mode)


def _prune_runtime_root(runtime_root: Path) -> None:
    shutil.rmtree(runtime_root, ignore_errors=True)


def _remove_directory_tree(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
