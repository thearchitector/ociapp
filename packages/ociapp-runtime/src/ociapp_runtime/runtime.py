import asyncio
import shutil
import stat
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING
from uuid import uuid4

from ociapp import DEFAULT_SOCKET_PATH

from .client import open_worker_session
from .engine import DockerAdapter
from .errors import (
    ArtifactLoadError,
    InstanceStartupError,
    OCIAppRuntimeError,
    ResponseProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .client import WorkerSession
    from .engine import EngineAdapter


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
    startup_future: asyncio.Future[None]
    teardown: AsyncExitStack
    active_request_count: int = 0
    session: "WorkerSession | None" = None
    shutdown_error: BaseException | None = None
    retired: bool = False


@dataclass(slots=True)
class ImagePool:
    """Tracks warm worker instances for one OCIApp artifact."""

    artifact_path: Path
    image_reference: str | None = None
    instances: list[WorkerInstance] = field(default_factory=list)


class Runtime:
    """Executes OCIApp artifacts through a warm pool of Docker workers."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        runtime_root: Path | str | None = None,
        engine: "EngineAdapter | None" = None,
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
        self._engine = engine or DockerAdapter()
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._idle_timeout = idle_timeout
        self._reaper_interval = reaper_interval
        self._clock = clock or time.monotonic
        self._pools: dict[str, ImagePool] = {}
        self._lock = asyncio.Lock()
        self._exit_stack: AsyncExitStack | None = None
        self._task_group: asyncio.TaskGroup | None = None
        self._reaper_stop: asyncio.Event | None = None
        self._accepting_requests = False
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

        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            self._runtime_root.mkdir(parents=True, exist_ok=True)
            stack.callback(shutil.rmtree, self._runtime_root, ignore_errors=True)

            task_group = await stack.enter_async_context(asyncio.TaskGroup())
            reaper_stop = asyncio.Event()
            task_group.create_task(
                self._run_reaper(reaper_stop), name="ociapp-runtime-reaper"
            )
        except Exception:
            await stack.aclose()
            raise

        self._exit_stack = stack
        self._task_group = task_group
        self._reaper_stop = reaper_stop
        self._accepting_requests = True
        self._started = True

    async def close(self) -> None:
        """Stops all workers and the background reaper."""

        if not self._started:
            return

        closing_error = OCIAppRuntimeError("runtime is closing")
        async with self._lock:
            self._accepting_requests = False
            for pool in self._pools.values():
                for instance in pool.instances:
                    instance.state = InstanceState.STOPPING
                    instance.shutdown_error = closing_error
            self._pools.clear()

            stack = self._exit_stack
            reaper_stop = self._reaper_stop
            self._exit_stack = None
            self._task_group = None
            self._reaper_stop = None
            self._started = False

        if reaper_stop is not None:
            reaper_stop.set()
        if stack is not None:
            await stack.aclose()

    async def execute(
        self, image_path: Path | str, request: dict[str, object]
    ) -> dict[str, object]:
        """Executes one OCIApp request against an artifact worker."""

        if not self._started:
            raise OCIAppRuntimeError(
                "runtime must be started before executing requests"
            )
        if not self._accepting_requests:
            raise OCIAppRuntimeError("runtime is closing")

        artifact_path = _resolve_artifact_path(image_path)
        instance = await self._acquire_instance(artifact_path)
        try:
            session = instance.session
            if session is None:
                raise OCIAppRuntimeError("worker session did not start successfully")

            return await session.execute(request, request_timeout=self._request_timeout)
        finally:
            await self._release_instance(instance)

    async def _acquire_instance(self, artifact_path: Path) -> WorkerInstance:
        image_key = str(artifact_path)

        while True:
            should_start = False
            async with self._lock:
                self._ensure_accepting_requests_locked()
                pool = self._pools.get(image_key)
                if pool is None:
                    pool = ImagePool(artifact_path=artifact_path)
                    self._pools[image_key] = pool

                instance = _find_dispatchable_instance(pool.instances)
                if instance is None:
                    instance = self._create_instance(pool)
                    should_start = True
                elif _instance_can_accept_requests(instance):
                    instance.active_request_count += 1
                    instance.last_used_at = self._clock()
                    instance.state = _state_for_active_request_count(
                        instance.active_request_count
                    )
                    return instance

                startup_future = instance.startup_future

            if should_start:
                await self._start_instance(instance)
            else:
                await startup_future

            async with self._lock:
                self._ensure_accepting_requests_locked()
                if _instance_can_accept_requests(instance):
                    instance.active_request_count += 1
                    instance.last_used_at = self._clock()
                    instance.state = _state_for_active_request_count(
                        instance.active_request_count
                    )
                    return instance

    async def _release_instance(self, instance: WorkerInstance) -> None:
        async with self._lock:
            instance.active_request_count = max(0, instance.active_request_count - 1)
            instance.last_used_at = self._clock()
            if instance.state == InstanceState.STOPPING:
                return

            instance.state = _state_for_active_request_count(
                instance.active_request_count
            )

    def _create_instance(self, pool: ImagePool) -> WorkerInstance:
        image_reference = pool.image_reference
        if image_reference is None:
            image_reference = self._engine.load_archive(pool.artifact_path)
            pool.image_reference = image_reference

        container_name = (
            f"{self._engine.build_container_name(pool.artifact_path)}-{uuid4().hex[:8]}"
        )
        mount_dir = self._runtime_root / pool.artifact_path.stem / uuid4().hex
        socket_path = mount_dir / Path(DEFAULT_SOCKET_PATH).name
        container_id = self._engine.run_container(
            image_reference, mount_dir, container_name
        )

        instance = WorkerInstance(
            image_key=str(pool.artifact_path),
            container_id=container_id,
            container_name=container_name,
            mount_dir=mount_dir,
            socket_path=socket_path,
            state=InstanceState.STARTING,
            last_used_at=self._clock(),
            startup_future=asyncio.get_running_loop().create_future(),
            teardown=AsyncExitStack(),
        )
        self._register_instance_teardown(instance)
        pool.instances.append(instance)
        return instance

    def _register_instance_teardown(self, instance: WorkerInstance) -> None:
        exit_stack = self._exit_stack
        if exit_stack is None:
            raise OCIAppRuntimeError("runtime must be started before creating workers")

        instance.teardown.callback(
            shutil.rmtree, instance.mount_dir, ignore_errors=True
        )
        instance.teardown.callback(
            self._engine.stop_container, instance.container_id, self._shutdown_timeout
        )
        instance.teardown.push_async_callback(self._close_instance_session, instance)
        exit_stack.push_async_callback(instance.teardown.aclose)

    async def _start_instance(self, instance: WorkerInstance) -> None:
        try:
            await self._wait_for_socket(instance)
            session = await open_worker_session(instance.socket_path)
        except Exception as exc:
            await self._retire_instance(instance, error=exc)
            raise

        try:
            task_group = self._require_task_group()
            async with self._lock:
                self._ensure_accepting_requests_locked()
                if instance.state == InstanceState.STOPPING:
                    raise instance.shutdown_error or OCIAppRuntimeError(
                        "runtime is closing"
                    )

                instance.session = session
                instance.state = InstanceState.READY
                instance.last_used_at = self._clock()
                if not instance.startup_future.done():
                    instance.startup_future.set_result(None)

            task_group.create_task(
                self._run_response_reader(instance),
                name=f"ociapp-runtime-reader:{instance.container_name}",
            )
        except Exception as exc:
            await self._retire_instance(instance, error=exc)
            raise

    async def _run_response_reader(self, instance: WorkerInstance) -> None:
        session = instance.session
        if session is None:
            return

        try:
            await session.read_responses()
        except ResponseProtocolError as exc:
            await self._retire_instance(instance, error=exc)

    async def _retire_instance(
        self, instance: WorkerInstance, *, error: BaseException | None = None
    ) -> None:
        async with self._lock:
            if error is not None and instance.shutdown_error is None:
                instance.shutdown_error = error
            if instance.retired:
                return

            instance.retired = True
            instance.state = InstanceState.STOPPING
            _remove_instance_from_pool(self._pools, instance)

        await instance.teardown.aclose()

    async def _close_instance_session(self, instance: WorkerInstance) -> None:
        close_error = instance.shutdown_error
        if not instance.startup_future.done():
            instance.startup_future.set_exception(
                close_error
                or OCIAppRuntimeError(
                    "worker instance was retired before startup completed"
                )
            )

        if instance.session is not None:
            await instance.session.close(close_error)

    async def _wait_for_socket(self, instance: WorkerInstance) -> None:
        deadline = self._clock() + self._startup_timeout
        while self._clock() < deadline:
            if _is_socket(instance.socket_path):
                return
            if instance.state == InstanceState.STOPPING or not self._accepting_requests:
                raise OCIAppRuntimeError("runtime is closing")
            await asyncio.sleep(0.05)

        raise InstanceStartupError(
            f"worker socket did not appear within {self._startup_timeout}s: {instance.socket_path}"
        )

    async def _run_reaper(self, stop_event: asyncio.Event) -> None:
        while True:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._reaper_interval)
                return
            except TimeoutError:
                pass

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
                await self._retire_instance(instance)

    def _ensure_accepting_requests_locked(self) -> None:
        if not self._accepting_requests:
            raise OCIAppRuntimeError("runtime is closing")

    def _require_task_group(self) -> asyncio.TaskGroup:
        task_group = self._task_group
        if task_group is None:
            raise OCIAppRuntimeError("runtime must be started before creating workers")
        return task_group


def _find_dispatchable_instance(
    instances: list[WorkerInstance],
) -> WorkerInstance | None:
    for state in (InstanceState.READY, InstanceState.BUSY, InstanceState.STARTING):
        for instance in instances:
            if instance.state == state:
                return instance

    return None


def _instance_can_accept_requests(instance: WorkerInstance) -> bool:
    session = instance.session
    return (
        instance.state in {InstanceState.READY, InstanceState.BUSY}
        and session is not None
        and session.is_open
        and not instance.retired
    )


def _state_for_active_request_count(active_request_count: int) -> InstanceState:
    if active_request_count > 0:
        return InstanceState.BUSY

    return InstanceState.READY


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


def _resolve_artifact_path(image_path: Path | str) -> Path:
    artifact_path = Path(image_path).resolve()
    if not artifact_path.exists():
        raise ArtifactLoadError(f"OCIApp artifact does not exist: {artifact_path}")

    return artifact_path
