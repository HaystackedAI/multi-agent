"""Thin client to the recongraph agent. The web app never runs the graph itself.

By default it calls the deployed AgentCore Runtime (``AGENT_RUNTIME_ARN``) over boto3. Set
``AGENT_LOCAL_URL`` (e.g. ``http://localhost:8080``) to instead POST to a recongraph server
running locally (``python main.py``) — same ``/invocations`` contract, no AWS — for integration
testing. Either way the agent runs in its own process; this module only speaks the payload contract.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

# A sweep is one synchronous call that can run for minutes. Defaults (60 s read timeout, automatic
# retries) would time out and then re-run the sweep, so we wait up to 15 minutes and never retry.
INVOKE_READ_TIMEOUT_SECONDS = 900
COLD_START_RETRIES = 3
COLD_START_RETRY_SECONDS = 4


def _is_cold_start_error(exc: Exception) -> bool:
    """AgentCore surfaces a runtime that is still booting as RuntimeClientError with a 502."""
    text = str(exc)
    return "RuntimeClientError" in text and "502" in text


class AgentClient:
    """Speaks the recongraph payload contract: ``{"action": ...}`` -> JSON result."""

    def __init__(self, runtime_arn: str | None = None, region: str | None = None) -> None:
        self.local_url = os.getenv("AGENT_LOCAL_URL", "").rstrip("/") or None
        self.session_id = (
            os.getenv("AGENTCORE_SESSION_ID") or f"chaser-web-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}"
        )
        if self.local_url:
            self.client = None
            self.runtime_arn = None
            return

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

    @property
    def target(self) -> str:
        return self.local_url or "runtime"

    def _invoke_local(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.local_url}/invocations",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=INVOKE_READ_TIMEOUT_SECONDS) as resp:
            body = resp.read()
        return json.loads(body) if body else {"ok": False, "error": "empty response"}

    def _invoke_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A brand-new session pays a cold start (a microVM boots and imports the code); the first
        # request can bounce with a gateway 502 before our app ever saw it, so it is safe to retry.
        # Anything else is raised as-is, and botocore itself never retries.
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

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._invoke_local(payload) if self.local_url else self._invoke_runtime(payload)

    def sweep(self) -> dict[str, Any]:
        return self._invoke({"action": "sweep"})

    def _stream_local(self, payload: dict[str, Any]) -> Iterator[str]:
        req = urllib.request.Request(
            f"{self.local_url}/invocations",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=INVOKE_READ_TIMEOUT_SECONDS) as resp:
            for raw in resp:
                yield raw.decode("utf-8").rstrip("\n")

    def _stream_runtime(self, payload: dict[str, Any]) -> Iterator[str]:
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
        for raw in response["response"].iter_lines():
            yield raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)

    def sweep_stream(self) -> Iterator[dict[str, Any]]:
        """Yield the agent's live trace frames (SSE ``data:`` lines parsed back into dicts)."""
        payload = {"action": "sweep_stream"}
        lines = self._stream_local(payload) if self.local_url else self._stream_runtime(payload)
        for line in lines:
            data = line[len("data:"):].strip() if line.startswith("data:") else line.strip()
            if not data:
                continue
            try:
                yield json.loads(data)
            except ValueError:
                continue

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

    def seed(self) -> dict[str, Any]:
        return self._invoke({"action": "seed"})
