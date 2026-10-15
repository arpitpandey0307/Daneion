"""HTTP API: console endpoints under /api/v1, worker endpoints under /api/v1/worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from daneion import __version__
from daneion.controller import errors
from daneion.controller.db import Worker
from daneion.controller.service import Controller
from daneion.controller.states import WorkerState


class JobIn(BaseModel):
    handler: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    mode: str = "auto"


class HelloIn(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    os: str = Field(default="", max_length=64)
    slots: int = Field(default=1, ge=1, le=256)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class PollIn(BaseModel):
    free_slots: int = Field(ge=0, le=256)
    wait_s: float | None = Field(default=None, ge=0, le=60)


class FenceIn(BaseModel):
    fence: int = Field(ge=1)


class FailIn(BaseModel):
    fence: int = Field(ge=1)
    error_class: str = Field(max_length=64)
    message: str = Field(default="", max_length=4000)


class PairIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    name: str = Field(default="", max_length=64)
    os: str = Field(default="", max_length=64)
    slots: int = Field(default=1, ge=1, le=256)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class AttemptRef(BaseModel):
    attempt_id: str
    fence: int


class HeartbeatIn(BaseModel):
    attempts: list[AttemptRef] = Field(default_factory=list, max_length=256)


def current_worker(request: Request,
                   authorization: Annotated[str | None, Header()] = None) -> Worker:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return request.app.state.ctl.authenticate(token)


WorkerDep = Annotated[Worker, Depends(current_worker)]


LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_only(value: str) -> str:
    value = value.split("://", 1)[-1].split("/", 1)[0]
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def create_app(ctl: Controller, console_dir: Path | None = None, *, role: str = "console",
               lan_info: dict[str, Any] | None = None) -> FastAPI:
    """role="console": loopback listener with everything (dashboard, CLI, local worker).
    role="lan": the network-facing TLS listener, which only serves pairing and worker routes."""
    if role not in ("console", "lan"):
        raise ValueError(role)
    console = role == "console"
    app = FastAPI(title=f"Daneion controller ({role})", version=__version__,
                  docs_url="/api/docs" if console else None,
                  openapi_url="/api/openapi.json" if console else None, redoc_url=None)
    app.state.ctl = ctl

    @app.exception_handler(errors.DaneionError)
    async def _daneion_error(_req: Request, exc: errors.DaneionError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"error": {"code": exc.code, "message": exc.message}})

    if console:
        @app.middleware("http")
        async def _loopback_only(request: Request, call_next):  # type: ignore[no-untyped-def]
            # DNS-rebinding and cross-site protection: the console answers only to localhost
            # names, and browser requests must come from a localhost origin.
            host = _host_only(request.headers.get("host", ""))
            origin = request.headers.get("origin")
            if host not in LOCAL_HOSTS or (origin and _host_only(origin) not in LOCAL_HOSTS):
                return JSONResponse(status_code=403, content={"error": {
                    "code": "FORBIDDEN_ORIGIN", "message": "console is localhost-only"}})
            return await call_next(request)

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "role": role}

    # ---------------------------------------------------------------- console
    api = APIRouter(prefix="/api/v1")

    @api.post("/pairing")
    def open_pairing() -> dict[str, Any]:
        info = lan_info or {}
        out = ctl.open_pairing()
        out.update({"fingerprint": info.get("fingerprint", ""), "lan_port": info.get("port"),
                    "addresses": info.get("addresses", [])})
        host = out["addresses"][0] if out["addresses"] else "<controller-ip>"
        port = "" if info.get("port") in (None, 8751) else f" --port {info.get('port')}"
        out["command"] = f"daneion join {host} {out['code']} --fp {out['fingerprint']}{port}"
        return out

    @api.get("/pairing")
    def pairing_status() -> dict[str, Any]:
        return {**ctl.pairing_status(), "lan_enabled": bool(lan_info)}

    @api.get("/cluster")
    def cluster() -> dict[str, Any]:
        workers = ctl.list_workers()
        return {"version": __version__, "max_nodes": ctl.cfg.max_nodes,
                "workers": workers,
                "online": sum(1 for w in workers if w["state"] == WorkerState.ONLINE)}

    @api.get("/workloads")
    def workloads() -> list[dict[str, Any]]:
        from daneion.workloads import REGISTRY

        return [{"name": w.name, "title": w.title,
                 "presets": {k: w.validate(v) for k, v in w.presets.items()}}
                for w in REGISTRY.values()]

    @api.get("/workers")
    def workers() -> list[dict[str, Any]]:
        return ctl.list_workers()

    @api.post("/workers/{worker_id}/{action}")
    def worker_action(worker_id: str, action: str) -> dict[str, Any]:
        states = {"pause": WorkerState.PAUSED, "resume": WorkerState.ONLINE,
                  "revoke": WorkerState.REVOKED}
        if action not in states:
            raise errors.NotFound("action")
        ctl.set_worker_state(worker_id, states[action])
        return {"ok": True}

    @api.post("/jobs", status_code=201)
    def submit(job: JobIn) -> dict[str, Any]:
        return ctl.submit_job(job.handler, job.params, job.mode)

    @api.get("/jobs")
    def jobs(limit: int = 50) -> list[dict[str, Any]]:
        return ctl.list_jobs(min(max(limit, 1), 200))

    @api.get("/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        return ctl.get_job(job_id)

    @api.get("/jobs/{job_id}/tasks")
    def job_tasks(job_id: str) -> list[dict[str, Any]]:
        return ctl.job_tasks(job_id)

    @api.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, Any]:
        return ctl.cancel_job(job_id)

    @api.get("/jobs/{job_id}/result")
    def result(job_id: str) -> FileResponse:
        path = ctl.job_result(job_id)
        return FileResponse(path, filename=path.name)

    @api.get("/events")
    def events(after: int = 0, job_id: str | None = None,
               limit: int = 500) -> list[dict[str, Any]]:
        return ctl.events_since(after, job_id, min(max(limit, 1), 5000))

    @api.get("/events/stream")
    async def event_stream(request: Request, after: int = 0) -> StreamingResponse:
        async def gen():  # type: ignore[no-untyped-def]
            last = after
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                batch = await run_in_threadpool(ctl.events_since, last)
                for ev in batch:
                    last = ev["id"]
                    yield f"id: {ev['id']}\nevent: daneion\ndata: {json.dumps(ev)}\n\n"
                if not batch:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- worker
    wk = APIRouter(prefix="/api/v1/worker")

    @wk.post("/hello")
    def hello(body: HelloIn, worker: WorkerDep) -> dict[str, Any]:
        ctl.update_worker(worker.id, capabilities=body.capabilities, slots=body.slots,
                          os_name=body.os)
        return {"worker_id": worker.id, "name": worker.name,
                "heartbeat_interval_s": ctl.cfg.heartbeat_interval_s,
                "lease_ttl_s": ctl.cfg.lease_ttl_s}

    @wk.post("/poll")
    def poll(body: PollIn, worker: WorkerDep) -> dict[str, Any]:
        return {"leases": ctl.poll(worker, body.free_slots, body.wait_s)}

    @wk.post("/attempts/{attempt_id}/accept")
    def accept(attempt_id: str, body: FenceIn, worker: WorkerDep) -> dict[str, Any]:
        ctl.accept(worker, attempt_id, body.fence)
        return {"ok": True}

    @wk.post("/attempts/{attempt_id}/start")
    def start(attempt_id: str, body: FenceIn, worker: WorkerDep) -> dict[str, Any]:
        ctl.start(worker, attempt_id, body.fence)
        return {"ok": True}

    @wk.post("/heartbeat")
    def heartbeat(body: HeartbeatIn, worker: WorkerDep) -> dict[str, Any]:
        return ctl.heartbeat(worker, [a.model_dump() for a in body.attempts])

    @wk.put("/attempts/{attempt_id}/result")
    async def put_result(
        attempt_id: str,
        request: Request,
        worker: WorkerDep,
        x_daneion_fence: Annotated[int, Header()],
        x_daneion_sha256: Annotated[str, Header(min_length=64, max_length=64)],
        x_daneion_duration: Annotated[float | None, Header()] = None,
    ) -> dict[str, Any]:
        declared = int(request.headers.get("content-length") or 0)
        if declared > ctl.cfg.max_result_bytes:
            raise errors.InvalidRequest("result too large", code="RESULT_TOO_LARGE")
        data = await request.body()
        return await run_in_threadpool(ctl.complete, worker, attempt_id, x_daneion_fence, data,
                                       x_daneion_sha256, x_daneion_duration)

    @wk.post("/attempts/{attempt_id}/fail")
    def fail(attempt_id: str, body: FailIn, worker: WorkerDep) -> dict[str, Any]:
        ctl.fail(worker, attempt_id, body.fence, body.error_class, body.message)
        return {"ok": True}

    @wk.post("/pair")
    def pair(body: PairIn, request: Request) -> dict[str, Any]:
        ip = request.client.host if request.client else "unknown"
        worker_id, token = ctl.pair(body.code, body.name or ip, client_ip=ip, os_name=body.os,
                                    capabilities=body.capabilities, slots=body.slots)
        return {"worker_id": worker_id, "token": token,
                "heartbeat_interval_s": ctl.cfg.heartbeat_interval_s}

    if console:
        app.include_router(api)
    app.include_router(wk)

    if console and console_dir and (console_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=console_dir, html=True), name="console")

    return app
