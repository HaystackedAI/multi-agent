"""FastAPI web app: decision inbox UI + JSON API. Run: uvicorn app.server:app --reload --port 8000."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chaser import service  # noqa: E402
from chaser.backend import Backend, make_backend  # noqa: E402
from chaser.config import configure_logging  # noqa: E402
from chaser.context import get_store  # noqa: E402

configure_logging()
logger = logging.getLogger("chaser.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

_sweep_lock = threading.Lock()  # web-level guard; service.SWEEP_LOCK guards the graph itself
_state: dict[str, Any] = {
    "sweep_running": False,
    "started_at": None,
    "last_result": None,
    "last_error": None,
}
_backend: Backend | None = None


def backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = make_backend()
    return _backend


def _run_sweep_bg() -> None:
    if not _sweep_lock.acquire(blocking=False):
        return
    _state["sweep_running"] = True
    _state["started_at"] = time.time()
    try:
        result = backend().sweep()
        _state["last_result"] = {"ok": result.get("ok"), "cycle_id": result.get("cycle_id")}
        _state["last_error"] = None if result.get("ok") else result.get("error")
    except Exception as exc:  # keep the server alive
        logger.exception("background sweep failed")
        _state["last_error"] = str(exc)
    finally:
        _state["sweep_running"] = False
        _state["started_at"] = None
        _sweep_lock.release()


def _keepalive() -> None:
    """Ping the AgentCore session so its microVM (and the state in it) stays warm.

    The runtime ends an idle session after 15 minutes; the next call then pays a cold start and
    starts from an empty store. A cheap ``status`` call every few minutes avoids both while the
    web app is up. Set KEEPALIVE_SECONDS=0 to disable.
    """
    default = "600" if os.getenv("AGENT_BACKEND", "local").lower() == "agentcore" else "0"
    interval = int(os.getenv("KEEPALIVE_SECONDS", default) or 0)
    if interval <= 0:
        return
    while True:
        time.sleep(interval)
        try:
            backend().status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("keepalive failed: %s", exc)


def _scheduler() -> None:
    interval = int(os.getenv("SWEEP_INTERVAL_SECONDS", "900") or 0)
    if interval <= 0:
        return
    while True:
        time.sleep(interval)
        threading.Thread(target=_run_sweep_bg, name="chaser-sweep", daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.getenv("AGENT_BACKEND", "local").lower() == "local" and not get_store().list_clients():
        logger.info("empty store; seeding demo data")
        service.seed()
    threading.Thread(target=_scheduler, name="chaser-scheduler", daemon=True).start()
    threading.Thread(target=_keepalive, name="chaser-keepalive", daemon=True).start()
    if os.getenv("SWEEP_ON_START", "0") == "1":
        threading.Thread(target=_run_sweep_bg, name="chaser-sweep", daemon=True).start()
    yield


app = FastAPI(title="Chaser", version="0.1.0", lifespan=lifespan)


class DecideBody(BaseModel):
    response: Any = "yes"
    edits: dict[str, Any] = Field(default_factory=dict)


class AskBody(BaseModel):
    prompt: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "backend": os.getenv("AGENT_BACKEND", "local"), "sweep_running": _state["sweep_running"]}


@app.get("/api/state")
def state() -> dict[str, Any]:
    # Ask the backend, not the local store: with AGENT_BACKEND=agentcore the state lives in the runtime.
    try:
        data = backend().state()
    except Exception as exc:  # keep the UI polling
        logger.exception("state failed")
        data = {"ok": False, "error": str(exc), "sweep_running": False}
    data["sweep_running"] = bool(data.get("sweep_running")) or _state["sweep_running"]
    data["running_for_seconds"] = (
        int(time.time() - _state["started_at"]) if _state["sweep_running"] and _state["started_at"] else None
    )
    data["last_error"] = _state["last_error"] or data.get("error")
    return data


@app.post("/api/sweep")
def sweep() -> dict[str, Any]:
    if _state["sweep_running"] or _sweep_lock.locked():
        return {"started": False, "reason": "a sweep is already running"}
    threading.Thread(target=_run_sweep_bg, name="chaser-sweep", daemon=True).start()
    return {"started": True}


@app.post("/api/decisions/{decision_id}")
def decide(decision_id: str, body: DecideBody) -> dict[str, Any]:
    result = backend().decide(decision_id, body.response, body.edits)
    if not result.get("ok") and "unknown decision" in str(result.get("error", "")):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/ask")
def ask(body: AskBody) -> dict[str, Any]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt is required")
    try:
        return backend().ask(prompt)
    except Exception as exc:
        logger.exception("ask failed")
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.post("/api/seed")
def reseed() -> dict[str, Any]:
    return {"ok": True, "counts": service.seed()}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
