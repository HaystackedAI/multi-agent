"""FastAPI web app: decision inbox UI + JSON API. Run: uvicorn app.server:app --reload --port 8000.

The web app is a thin client. It never runs the agent graph in-process — every action is a call to
the recongraph agent (deployed AgentCore Runtime, or a local server via AGENT_LOCAL_URL). See app/agent.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parent / ".env")  # load app/.env regardless of CWD

from app.agent import AgentClient  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("chaser.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

_sweep_lock = threading.Lock()
_state: dict[str, Any] = {
    "sweep_running": False,
    "started_at": None,
    "last_result": None,
    "last_error": None,
}
_agent: AgentClient | None = None


def agent() -> AgentClient:
    global _agent
    if _agent is None:
        _agent = AgentClient()
    return _agent


def _run_sweep_bg() -> None:
    if not _sweep_lock.acquire(blocking=False):
        return
    _state["sweep_running"] = True
    _state["started_at"] = time.time()
    try:
        result = agent().sweep()
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
    """Ping the agent session so its microVM (and the state in it) stays warm.

    The runtime ends an idle session after 15 minutes; the next call then pays a cold start and
    starts from an empty store. A cheap ``status`` call every few minutes avoids both while the
    web app is up. Set KEEPALIVE_SECONDS=0 to disable (default 0 when using a local agent).
    """
    default = "0" if os.getenv("AGENT_LOCAL_URL") else "600"
    interval = int(os.getenv("KEEPALIVE_SECONDS", default) or 0)
    if interval <= 0:
        return
    while True:
        time.sleep(interval)
        try:
            agent().status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("keepalive failed: %s", exc)


def _last_close_epoch() -> float:
    """Epoch seconds of the last *completed* close, read from the agent's persisted state.

    Lives in the agent's store (``last_sweep_at``), so it is independent of this web process and
    survives its restarts. Returns 0.0 if unknown/unreachable.
    """
    try:
        iso = agent().status().get("last_sweep_at")
    except Exception:  # never let the scheduler die on a transient agent error
        return 0.0
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _scheduler() -> None:
    """Auto-close only once a full interval has elapsed since the last close.

    The next-due time is anchored to the last *completed* close (persisted in the agent) and to
    this process's start — never to process boot alone. This makes the schedule restart-proof: a
    worker recycle cannot cause an early close, and a manual run (button or trace) counts as a
    close and pushes the next auto-close out by a full interval. On a fresh deploy the first
    auto-close still waits a full interval after startup. Set SWEEP_INTERVAL_SECONDS=0 to disable.
    """
    interval = int(os.getenv("SWEEP_INTERVAL_SECONDS", "7200") or 0)  # default: every 2 hours
    if interval <= 0:
        return
    baseline = time.time()  # a fresh deploy waits a full interval before its first close
    check_every = min(interval, 60)
    while True:
        time.sleep(check_every)
        due_after = max(_last_close_epoch(), baseline) + interval
        if time.time() >= due_after and not (_state["sweep_running"] or _sweep_lock.locked()):
            threading.Thread(target=_run_sweep_bg, name="chaser-sweep", daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # No close on startup: lifespan runs on every (re)start, so a boot-fire would trigger a close
    # on every worker recycle. The scheduler decides when a close is due; a manual run is the only
    # other way to start one. (SWEEP_ON_START is intentionally no longer honored here.)
    threading.Thread(target=_scheduler, name="chaser-scheduler", daemon=True).start()
    threading.Thread(target=_keepalive, name="chaser-keepalive", daemon=True).start()
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
    return {"ok": True, "target": agent().target, "sweep_running": _state["sweep_running"]}


@app.get("/api/state")
def state() -> dict[str, Any]:
    # State lives in the agent process, not here; ask it.
    try:
        data = agent().state()
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


@app.get("/api/sweep/stream")
def sweep_stream() -> StreamingResponse:
    """Relay the agent's live trace to the browser as Server-Sent Events."""

    def gen() -> Any:
        try:
            for frame in agent().sweep_stream():
                yield f"data: {json.dumps(frame, default=str)}\n\n"
        except Exception as exc:  # surface a terminal frame instead of a dead stream
            logger.exception("trace stream failed")
            yield f"data: {json.dumps({'kind': 'done', 'ok': False, 'error': str(exc)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/decisions/{decision_id}")
def decide(decision_id: str, body: DecideBody) -> dict[str, Any]:
    result = agent().decide(decision_id, body.response, body.edits)
    if not result.get("ok") and "unknown decision" in str(result.get("error", "")):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/ask")
def ask(body: AskBody) -> dict[str, Any]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt is required")
    try:
        return agent().ask(prompt)
    except Exception as exc:
        logger.exception("ask failed")
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})


@app.post("/api/seed")
def reseed() -> dict[str, Any]:
    return agent().seed()


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
