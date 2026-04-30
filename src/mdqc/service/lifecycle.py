from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from mdqc import __version__ as _agent_version
from mdqc.activity_log import ActivityEntry, ActivityLog
from mdqc.classifier import classify_file
from mdqc.config import load_or_exit, paths
from mdqc.config.defaults import (
    IPC_HEADER,
    SHUTDOWN_GRACE_S,
)
from mdqc.config.schema import Config, InstrumentConfig
from mdqc.crash import install_crash_handlers
from mdqc.diagnostics import run_diagnostics
from mdqc.extractor import Extractor
from mdqc.failed_files import FailedFilesStore
from mdqc.ipc.runtime import RuntimeFile, RuntimeInfo, generate_token
from mdqc.log import configure_logging
from mdqc.service.agent_id import resolve_agent_id
from mdqc.spool import Spool, prune_spool, recover_orphans
from mdqc.types import ExtractionStatus, Vendor
from mdqc.uploader import Uploader, UploaderWorker
from mdqc.watcher.finalizer import Finalizer
from mdqc.watcher.observer import WatchdogObserver
from mdqc.watcher.registry import ProcessedRegistry
from mdqc.webui.auth import SESSION_COOKIE_NAME

log = logging.getLogger(__name__)


EVENT_EXTRACTION_STARTED = "extraction_started"
EVENT_EXTRACTION_COMPLETED = "extraction_completed"
EVENT_EXTRACTION_FAILED = "extraction_failed"
EVENT_UPLOAD_SUCCEEDED = "upload_succeeded"
EVENT_UPLOAD_FAILED = "upload_failed"
EVENT_UPDATE_AVAILABLE = "update_available"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"


@dataclass
class Event:
    type: str
    payload: dict[str, Any]
    id: int = 0

    def to_sse(self) -> dict[str, Any]:
        return {
            "event": self.type,
            "id": str(self.id),
            "data": json.dumps(self.payload),
        }


class EventPubSub:
    def __init__(self, *, max_queue: int = 100) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._max_queue = max_queue
        self._lock = asyncio.Lock()
        self._counter = 0

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self._counter += 1
        event = Event(type=event_type, payload=payload, id=self._counter)
        for queue in list(self._subscribers):
            self._enqueue(queue, event)

    def _enqueue(self, queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max_queue)
        async with self._lock:
            self._subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            async with self._lock:
                with contextlib.suppress(ValueError):
                    self._subscribers.remove(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


@dataclass
class AppState:
    cfg: Config
    agent_id: str
    spool: Spool
    failed: FailedFilesStore
    activity: ActivityLog
    processed_registry: ProcessedRegistry
    extractor: Extractor
    uploader: Uploader
    uploader_worker: UploaderWorker
    finalizer: Finalizer
    observer: WatchdogObserver | None
    paused: asyncio.Event
    stop_event: asyncio.Event
    started_at: datetime
    events_pubsub: EventPubSub
    token: str = ""
    port: int = 0
    app: FastAPI | None = None
    config_path: Path = field(default_factory=paths.config_path)


def _extract_token(request: Request) -> str | None:
    header_token = request.headers.get(IPC_HEADER)
    if header_token:
        return header_token
    query_token = request.query_params.get("token")
    if query_token:
        return query_token
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    return None


def _check_token(request: Request, state: AppState) -> None:
    provided = _extract_token(request)
    if not provided or provided != state.token:
        raise HTTPException(status_code=401, detail="invalid or missing token")


def _make_dependency(state: AppState):
    def _dep(request: Request) -> AppState:
        _check_token(request, state)
        return state

    return _dep


def build_api(state: AppState) -> FastAPI:
    app = FastAPI(title="MD QC Agent", version=_agent_version)
    state.app = app

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            return await call_next(request)
        provided = _extract_token(request)
        if not provided or provided != state.token:
            return JSONResponse(
                status_code=401, content={"detail": "invalid or missing token"}
            )
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        uptime = int((datetime.now(UTC) - state.started_at).total_seconds())
        return {
            "service_running": True,
            "uptime_s": uptime,
            "paused": state.paused.is_set(),
            "pending_count": state.spool.pending_count(),
            "uploading_count": _count_dir(state.spool.uploading_dir),
            "failed_count": _count_dir(state.spool.failed_dir),
            "recent_activity": [e.to_dict() for e in state.activity.recent(20)],
            "local_only_mode": state.uploader.is_local_only,
        }

    @app.get("/api/diagnostics")
    async def get_diagnostics() -> dict[str, Any]:
        report = await run_diagnostics(check_cloud=False)
        return report.to_dict()

    @app.post("/api/pause")
    async def pause_endpoint() -> dict[str, bool]:
        state.paused.set()
        state.events_pubsub.publish(EVENT_PAUSED, {})
        return {"ok": True}

    @app.post("/api/resume")
    async def resume_endpoint() -> dict[str, bool]:
        state.paused.clear()
        state.events_pubsub.publish(EVENT_RESUMED, {})
        return {"ok": True}

    @app.post("/api/restart")
    async def restart_endpoint() -> dict[str, bool]:
        loop = asyncio.get_event_loop()
        loop.call_later(0.5, state.stop_event.set)
        return {"ok": True}

    @app.post("/api/reprocess")
    async def reprocess_endpoint(body: dict[str, Any]) -> dict[str, bool]:
        path_raw = body.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            raise HTTPException(status_code=400, detail="missing path")
        target = Path(path_raw)
        vendor = _vendor_for_path(state, target)
        if vendor is None:
            raise HTTPException(
                status_code=400, detail=f"no instrument matches path {target}"
            )
        await state.finalizer.observe(target, vendor)
        return {"ok": True}

    @app.post("/api/failed/retry")
    async def failed_retry_endpoint(body: dict[str, Any]) -> dict[str, int]:
        path_raw = body.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            raise HTTPException(status_code=400, detail="missing path")
        if path_raw == "all":
            entries = state.failed.all()
            count = 0
            for entry in entries:
                vendor = _vendor_for_path(state, Path(entry.path))
                if vendor is None:
                    continue
                await state.finalizer.observe(Path(entry.path), vendor)
                count += 1
            return {"count": count}
        vendor = _vendor_for_path(state, Path(path_raw))
        if vendor is None:
            raise HTTPException(
                status_code=400, detail=f"no instrument matches path {path_raw}"
            )
        await state.finalizer.observe(Path(path_raw), vendor)
        return {"count": 1}

    @app.post("/api/failed/clear")
    async def failed_clear_endpoint() -> dict[str, bool]:
        state.failed.clear()
        return {"ok": True}

    @app.get("/api/config")
    async def get_config_endpoint() -> dict[str, Any]:
        return state.cfg.model_dump(mode="json")

    @app.put("/api/config")
    async def put_config_endpoint(body: dict[str, Any]) -> dict[str, bool]:
        cfg_path = state.config_path
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg_path.with_name(f".{cfg_path.name}.tmp")
        with open(tmp, "wb") as fh:
            tomli_w.dump(body, fh)
        os.replace(tmp, cfg_path)
        return {"ok": True}

    @app.get("/events")
    async def events_endpoint(request: Request) -> EventSourceResponse:
        async def event_generator() -> AsyncIterator[dict[str, Any]]:
            async for event in state.events_pubsub.subscribe():
                if await request.is_disconnected():
                    break
                yield event.to_sse()

        return EventSourceResponse(event_generator())

    return app


def _count_dir(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.iterdir() if p.is_file() and p.suffix == ".json")


def _vendor_for_path(state: AppState, path: Path):
    s = str(path)
    for inst in state.cfg.instruments:
        try:
            inst_root = str(inst.watch_path.resolve())
        except OSError:
            inst_root = str(inst.watch_path)
        if s.startswith(inst_root):
            return inst.vendor
    if state.cfg.instruments:
        return state.cfg.instruments[0].vendor
    return None


def attach_webui(state: AppState) -> None:
    if state.app is None:
        return
    try:
        from mdqc.webui import routes  # type: ignore[import-not-found]
    except ImportError:
        return
    register = getattr(routes, "register", None)
    if callable(register):
        try:
            register(state.app, state)
        except Exception as exc:
            log.warning("webui_attach_failed", extra={"error": str(exc)})


def _instrument_for_path(cfg: Config, path: Path) -> InstrumentConfig | None:
    s = str(path)
    for inst in cfg.instruments:
        try:
            inst_root = str(inst.watch_path.resolve())
        except OSError:
            inst_root = str(inst.watch_path)
        if s.startswith(inst_root):
            return inst
    return None


def _resolve_template_path(instrument: InstrumentConfig | None, template_name: str) -> Path | None:
    if instrument is not None:
        candidate = Path(instrument.template)
        if candidate.is_absolute() and candidate.exists():
            return candidate

    for base in (paths.methods_dir(), paths.templates_dir()):
        candidate = base / template_name
        if candidate.exists():
            return candidate
    return None


async def _build_state(cfg: Config) -> AppState:
    paths.ensure_dirs()

    config_path = paths.config_path()
    agent_id = resolve_agent_id(
        cfg.agent.agent_id, persist_to=config_path if config_path.exists() else None
    )

    spool = Spool(agent_id=agent_id, agent_version=_agent_version)
    failed = FailedFilesStore.load()
    activity = ActivityLog.load()
    processed_registry = ProcessedRegistry()
    extractor = Extractor(cfg.skyline, work_dir=paths.spool_work())
    uploader = Uploader(cfg.cloud, agent_version=_agent_version)
    uploader_worker = UploaderWorker(spool, uploader)

    paused = asyncio.Event()
    stop_event = asyncio.Event()

    state_holder: dict[str, AppState] = {}

    async def _processed_callback(path: Path, vendor: Vendor) -> None:
        state = state_holder["state"]
        instrument = _instrument_for_path(state.cfg, path)
        instrument_id = instrument.id if instrument is not None else None
        finalized = False

        async def _fail(reason: str) -> None:
            nonlocal finalized
            if finalized:
                return
            state.failed.add(str(path), instrument_id, reason)
            state.activity.record(
                ActivityEntry(
                    path=str(path),
                    instrument_id=instrument_id,
                    timestamp=datetime.now(UTC),
                    result=ExtractionStatus.FAILED,
                    error=reason,
                )
            )
            state.events_pubsub.publish(
                EVENT_EXTRACTION_FAILED,
                {"path": str(path), "instrument_id": instrument_id, "error": reason},
            )
            await state.finalizer.mark_failed(path, reason)
            finalized = True

        try:
            state.events_pubsub.publish(
                EVENT_EXTRACTION_STARTED,
                {"path": str(path), "instrument_id": instrument_id},
            )

            classification = classify_file(path, rules=state.cfg.classifier_rules)
            if instrument_id is not None:
                classification.instrument_id = instrument_id

            if not classification.control_type.is_qc():
                state.activity.record(
                    ActivityEntry(
                        path=str(path),
                        instrument_id=instrument_id,
                        timestamp=datetime.now(UTC),
                        result=ExtractionStatus.SKIPPED,
                    )
                )
                state.events_pubsub.publish(
                    EVENT_EXTRACTION_COMPLETED,
                    {
                        "path": str(path),
                        "instrument_id": instrument_id,
                        "skipped": True,
                    },
                )
                await state.finalizer.mark_done(path)
                finalized = True
                return

            template_name = instrument.template if instrument is not None else "QC_Method.sky"
            template_path = _resolve_template_path(instrument, template_name)
            if template_path is None:
                await _fail(f"template not found: {template_name}")
                return

            extraction_result = await state.extractor.extract(
                template_path, path, report_name="MD_QC_Report"
            )

            if extraction_result.status is ExtractionStatus.FAILED:
                reason = extraction_result.error_message or "extraction failed"
                await _fail(reason)
                return

            spool_path = state.spool.enqueue(
                classification, extraction_result, baseline_context=None
            )

            run_metrics = extraction_result.run_metrics
            state.activity.record(
                ActivityEntry(
                    path=str(path),
                    instrument_id=instrument_id,
                    timestamp=datetime.now(UTC),
                    result=ExtractionStatus.SUCCESS,
                    targets_found=run_metrics.targets_found if run_metrics else None,
                    targets_expected=run_metrics.targets_expected if run_metrics else None,
                    extraction_time_ms=extraction_result.extraction_time_ms,
                )
            )
            state.events_pubsub.publish(
                EVENT_EXTRACTION_COMPLETED,
                {
                    "path": str(path),
                    "instrument_id": instrument_id,
                    "run_id": str(extraction_result.run_id),
                    "spool_path": str(spool_path),
                },
            )
            await state.finalizer.mark_done(path)
            finalized = True
        except Exception as exc:
            log.exception("processed_callback_failed", extra={"path": str(path)})
            try:
                await _fail(f"unexpected error: {exc!r}")
            except Exception:
                log.exception("mark_failed_after_callback_failed", extra={"path": str(path)})

    finalizer = Finalizer(
        cfg.watcher,
        registry=processed_registry,
        processed_callback=_processed_callback,
    )

    state = AppState(
        cfg=cfg,
        agent_id=agent_id,
        spool=spool,
        failed=failed,
        activity=activity,
        processed_registry=processed_registry,
        extractor=extractor,
        uploader=uploader,
        uploader_worker=uploader_worker,
        finalizer=finalizer,
        observer=None,
        paused=paused,
        stop_event=stop_event,
        started_at=datetime.now(UTC),
        events_pubsub=EventPubSub(),
        config_path=config_path,
    )
    state_holder["state"] = state
    return state


def _build_observer(
    state: AppState, loop: asyncio.AbstractEventLoop
) -> WatchdogObserver | None:
    if not state.cfg.instruments:
        log.warning("no instruments configured; watcher disabled")
        return None

    specs: list[tuple[Path, Vendor, str]] = [
        (inst.watch_path, inst.vendor, inst.file_pattern) for inst in state.cfg.instruments
    ]

    def _on_detected(path: Path, vendor: Vendor) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                state.finalizer.observe(path, vendor), loop
            )
        except RuntimeError:
            log.warning(
                "observe_dispatch_failed",
                extra={"path": str(path), "vendor": vendor.value},
            )

    return WatchdogObserver(specs, on_detected=_on_detected)


async def _finalizer_loop(state: AppState) -> None:
    try:
        while not state.stop_event.is_set():
            try:
                await state.finalizer.tick()
            except Exception:
                log.exception("finalizer_tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.stop_event.wait(), timeout=5.0)
    except asyncio.CancelledError:
        return


async def _prune_loop(state: AppState) -> None:
    try:
        while not state.stop_event.is_set():
            try:
                prune_spool()
            except Exception:
                log.exception("prune_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(state.stop_event.wait(), timeout=3600.0)
    except asyncio.CancelledError:
        return


def _install_signal_handlers(stop_event: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    def _handle() -> None:
        loop.call_soon_threadsafe(stop_event.set)

    if sys.platform == "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_args: _handle())
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle)
            except (NotImplementedError, RuntimeError):
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, lambda *_args: _handle())


async def main_async(*, service_mode: bool) -> int:
    cfg = load_or_exit()
    configure_logging(level=cfg.agent.log_level)

    state = await _build_state(cfg)

    state.spool.recover_uploading_to_pending()
    recover_orphans()

    state.token = generate_token()
    app = build_api(state)
    attach_webui(state)

    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level=cfg.agent.log_level if cfg.agent.log_level != "trace" else "debug",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.05)
        if server_task.done():
            break

    actual_port = _resolve_port(server)
    state.port = actual_port

    runtime_file = RuntimeFile()
    runtime_file.write(
        RuntimeInfo(
            port=actual_port,
            token=state.token,
            pid=os.getpid(),
            started_at=state.started_at,
        )
    )

    loop = asyncio.get_running_loop()
    _install_signal_handlers(state.stop_event, loop)

    state.observer = _build_observer(state, loop)
    if state.observer is not None:
        state.observer.start()

    finalizer_task = asyncio.create_task(_finalizer_loop(state))
    uploader_task = asyncio.create_task(state.uploader_worker.run(state.stop_event))
    prune_task = asyncio.create_task(_prune_loop(state))

    log.info(
        "service_started",
        extra={"port": actual_port, "service_mode": service_mode, "agent_id": state.agent_id},
    )

    if not service_mode:
        import sys
        print(
            f"\n  Web UI: http://127.0.0.1:{actual_port}/?token={state.token}\n",
            file=sys.stderr,
            flush=True,
        )

    exit_code = 0
    try:
        await state.stop_event.wait()
    except Exception:
        log.exception("service_loop_failed")
        exit_code = 1
    finally:
        log.info("service_stopping")
        if state.observer is not None:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(state.observer.stop),
                    timeout=SHUTDOWN_GRACE_S,
                )
            except TimeoutError:
                log.warning("observer_stop_timed_out")
            except Exception:
                log.exception("observer_stop_failed")
        background_tasks: list[asyncio.Task[Any]] = [
            finalizer_task,
            uploader_task,
            prune_task,
        ]
        for task in background_tasks:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*background_tasks, return_exceptions=True),
                timeout=SHUTDOWN_GRACE_S,
            )
        except TimeoutError:
            log.warning("shutdown_grace_exceeded")

        with contextlib.suppress(Exception):
            await state.uploader.aclose()

        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(server_task, timeout=SHUTDOWN_GRACE_S)

        runtime_file.clear()
        log.info("service_stopped")

    return exit_code


def _resolve_port(server: Any) -> int:
    servers = getattr(server, "servers", None)
    if not servers:
        return 0
    for srv in servers:
        sockets = getattr(srv, "sockets", None) or []
        for sock in sockets:
            try:
                addr = sock.getsockname()
            except OSError:
                continue
            if isinstance(addr, tuple) and len(addr) >= 2:
                return int(addr[1])
    return 0


def main_blocking(*, service_mode: bool) -> None:
    install_crash_handlers()
    try:
        code = asyncio.run(main_async(service_mode=service_mode))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


__all__ = [
    "EVENT_EXTRACTION_COMPLETED",
    "EVENT_EXTRACTION_FAILED",
    "EVENT_EXTRACTION_STARTED",
    "EVENT_PAUSED",
    "EVENT_RESUMED",
    "EVENT_UPDATE_AVAILABLE",
    "EVENT_UPLOAD_FAILED",
    "EVENT_UPLOAD_SUCCEEDED",
    "AppState",
    "Event",
    "EventPubSub",
    "attach_webui",
    "build_api",
    "main_async",
    "main_blocking",
]
