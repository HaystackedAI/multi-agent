"""Amazon Bedrock AgentCore Runtime entrypoint for Chaser.

Payload contract (JSON):
    {"action": "sweep"}
    {"action": "sweep_stream"}  # like sweep, but streams a live trace as SSE (generator entrypoint)
    {"action": "decide", "decision_id": "...", "response": "yes"|"no"|"<text>", "edits": {...}}
    {"action": "ask", "prompt": "..."}
    {"action": "status"}
    {"action": "seed"}          # project-specific: reload the demo dataset
    {"action": "state"}         # project-specific: everything the UI shows
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from chaser import service  # noqa: E402
from chaser.config import configure_logging  # noqa: E402
from chaser.context import get_store  # noqa: E402

configure_logging()
logger = logging.getLogger("chaser.agentcore")

app = BedrockAgentCoreApp()


def _ensure_seeded() -> None:
    """AgentCore's filesystem starts empty: seed the SQLite store on first use."""
    if not get_store().list_clients():
        service.seed()


def _normalize(payload: dict | None) -> dict:
    """Accept the `agentcore invoke` shape too: it wraps whatever you pass as {"prompt": "<text>"}.

    A prompt that is a JSON object becomes the payload; any other bare prompt is an "ask".
    """
    payload = dict(payload or {})
    if "action" in payload or "prompt" not in payload:
        return payload
    prompt = payload["prompt"]
    if isinstance(prompt, str) and prompt.lstrip().startswith("{"):
        try:
            inner = json.loads(prompt)
        except ValueError:
            inner = None
        if isinstance(inner, dict):
            return {**payload, **inner}
    return {**payload, "action": "ask"}


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Route a payload to the service layer. Never raises."""
    try:
        payload = _normalize(payload)
        action = payload.get("action")
        _ensure_seeded()
        if action == "sweep":
            result = service.run_sweep()
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error", "sweep failed")}
            return {
                "ok": True,
                "report": result["report"],
                "pending_decisions": result["pending_decisions"],
                "actions_taken": result["actions_taken"],
            }
        if action == "decide":
            if not payload.get("decision_id"):
                return {"ok": False, "error": "decision_id is required"}
            return service.decide(payload["decision_id"], payload.get("response", "no"), payload.get("edits") or {})
        if action == "ask":
            prompt = (payload.get("prompt") or "").strip()
            if not prompt:
                return {"ok": False, "error": "prompt is required"}
            return {"ok": True, "answer": service.ask(prompt)}
        if action == "status":
            return {"ok": True, **service.status()}
        if action == "seed":
            return {"ok": True, "counts": service.seed()}
        if action == "state":
            return {"ok": True, **service.ui_state()}
        return {"ok": False, "error": f"unknown action: {action!r}"}
    except Exception as exc:  # never raise out of the entrypoint
        logger.exception("invoke failed")
        return {"ok": False, "error": str(exc)}


@app.entrypoint
def invoke(payload: dict, context: Any = None) -> Any:
    # A generator return value is streamed out by AgentCore as text/event-stream (SSE).
    normalized = _normalize(dict(payload or {}))
    if normalized.get("action") == "sweep_stream":
        try:
            _ensure_seeded()
        except Exception:
            logger.exception("seed before trace stream failed")
        return service.run_sweep_stream()
    return dispatch(payload)


if __name__ == "__main__":
    app.run()  # serves POST /invocations and GET /ping on 0.0.0.0:8080
