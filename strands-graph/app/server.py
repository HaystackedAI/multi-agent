"""FastAPI web app: decision inbox UI + JSON API. Run: uvicorn app.server:app --reload --port 8000.

The web app is a thin client. It never runs the agent graph in-process — every action is a call to
the recongraph agent (deployed AgentCore Runtime, or a local server via AGENT_LOCAL_URL). See app/agent.py.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import AgentClient

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


def _scheduler() -> None:
    interval = int(os.getenv("SWEEP_INTERVAL_SECONDS", "900") or 0)
    if interval <= 0:
        return
    while True:
        time.sleep(interval)
        threading.Thread(target=_run_sweep_bg, name="chaser-sweep", daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
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
