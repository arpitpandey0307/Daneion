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


def create_app(ctl: Controller, console_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Daneion controller", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.state.ctl = ctl

    @app.exception_handler(errors.DaneionError)
    async def _daneion_error(_req: Request, exc: errors.DaneionError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"error": {"code": exc.code, "message": exc.message}})

    # ---------------------------------------------------------------- console
    api = APIRouter(prefix="/api/v1")

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

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
    def events(after: int = 0, job_id: str | None = None) -> list[dict[str, Any]]:
        return ctl.events_since(after, job_id)

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

    app.include_router(api)
    app.include_router(wk)

    if console_dir and (console_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=console_dir, html=True), name="console")

    return app
