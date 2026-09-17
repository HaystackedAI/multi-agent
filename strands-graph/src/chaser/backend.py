"""Backends for the web app: run the agent in-process or call an AgentCore Runtime."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Protocol

from . import service

# A sweep is one synchronous InvokeAgentRuntime call that can run for minutes. boto3's defaults
# (60 s read timeout, automatic retries) would time out and then re-run the sweep, so the client
# waits up to 15 minutes and never retries.
INVOKE_READ_TIMEOUT_SECONDS = 900
COLD_START_RETRIES = 3
COLD_START_RETRY_SECONDS = 4


def _is_cold_start_error(exc: Exception) -> bool:
    """AgentCore surfaces a runtime that is still booting as RuntimeClientError with a 502."""
    text = str(exc)
    return "RuntimeClientError" in text and "502" in text


class Backend(Protocol):
    def sweep(self) -> dict[str, Any]: ...
    def decide(self, decision_id: str, response: Any, edits: dict[str, Any] | None) -> dict[str, Any]: ...
    def ask(self, prompt: str) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...
    def state(self) -> dict[str, Any]: ...


class LocalBackend:
    """Runs the Strands graph and agents inside the web server process."""

    def sweep(self) -> dict[str, Any]:
        return service.run_sweep()

    def decide(self, decision_id: str, response: Any, edits: dict[str, Any] | None) -> dict[str, Any]:
        return service.decide(decision_id, response, edits)

    def ask(self, prompt: str) -> dict[str, Any]:
        return {"ok": True, "answer": service.ask(prompt)}

    def status(self) -> dict[str, Any]:
        return {"ok": True, **service.status()}

    def state(self) -> dict[str, Any]:
        return {"ok": True, **service.ui_state()}


class AgentCoreBackend:
    """Invokes the deployed AgentCore Runtime (``main.py``) with the shared payload contract."""

    def __init__(self, runtime_arn: str | None = None, region: str | None = None) -> None:
        import boto3
        from botocore.config import Config

        self.runtime_arn = runtime_arn or os.environ["AGENT_RUNTIME_ARN"]
        self.client = boto3.client(
            "bedrock-agentcore",
            region_name=region or os.getenv("AWS_REGION", "us-east-1"),
            config=Config(
                read_timeout=INVOKE_READ_TIMEOUT_SECONDS,
                connect_timeout=10,
                retries={"total_max_attempts": 1},
            ),
        )
        # AgentCore requires a stable session id of at least 33 characters. Set AGENTCORE_SESSION_ID
        # for a hosted UI so every visitor shares one runtime session (and its state).
        self.session_id = os.getenv("AGENTCORE_SESSION_ID") or f"chaser-web-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}"

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A brand-new session pays a cold start (a microVM boots and imports the code); the first
        # request in that window can bounce with a gateway 502 before our app ever saw it, so it
        # is safe to retry. Anything else is raised as-is, and botocore itself never retries.
        for attempt in range(COLD_START_RETRIES + 1):
            try:
                response = self.client.invoke_agent_runtime(
                    agentRuntimeArn=self.runtime_arn,
                    runtimeSessionId=self.session_id,
                    payload=json.dumps(payload).encode("utf-8"),
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt >= COLD_START_RETRIES or not _is_cold_start_error(exc):
                    raise
                time.sleep(COLD_START_RETRY_SECONDS)
        body = response["response"].read()
        return json.loads(body) if body else {"ok": False, "error": "empty response"}

    def sweep(self) -> dict[str, Any]:
        return self._invoke({"action": "sweep"})

    def decide(self, decision_id: str, response: Any, edits: dict[str, Any] | None) -> dict[str, Any]:
        return self._invoke(
            {"action": "decide", "decision_id": decision_id, "response": response, "edits": edits or {}}
        )

    def ask(self, prompt: str) -> dict[str, Any]:
        return self._invoke({"action": "ask", "prompt": prompt})

    def status(self) -> dict[str, Any]:
        return self._invoke({"action": "status"})

    def state(self) -> dict[str, Any]:
        return self._invoke({"action": "state"})


def make_backend() -> Backend:
    """Select the backend from ``AGENT_BACKEND`` (``local`` default, or ``agentcore``)."""
    if os.getenv("AGENT_BACKEND", "local").lower() == "agentcore":
        return AgentCoreBackend()
    return LocalBackend()
