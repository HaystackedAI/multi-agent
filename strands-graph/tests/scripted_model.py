"""A deterministic Strands ``Model`` that replays scripted turns (no network).

Each turn is either ``{"text": "..."}`` (an end_turn assistant message) or
``{"tool": "name", "input": {...}}`` (a single tool_use). A turn may also be a list of
several tool dicts to emit multiple tool uses in one assistant message. Event shapes
mirror ``strands.models.bedrock.BedrockModel._convert_non_streaming_to_streaming``.

Structured output requested through ``agent(prompt, structured_output_model=M)`` arrives as
a normal ``stream()`` call whose tool specs include a tool named after ``M``; script it as
``{"tool": "M", "input": {...}}``. ``structured_output()`` (legacy path) is also supported.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.streaming import StreamEvent

T = TypeVar("T", bound=BaseModel)

Turn = dict[str, Any] | list[dict[str, Any]]


class ScriptedModel(Model):
    """Replays scripted turns; after the script is exhausted it answers with ``final_text``."""

    def __init__(self, turns: list[Turn] | None = None, final_text: str = "Done.") -> None:
        self.turns: list[Turn] = list(turns or [])
        self.final_text = final_text
        self.calls: list[dict[str, Any]] = []  # captured (messages, tool_specs, system_prompt) per call
        self._config: dict[str, Any] = {"model_id": "scripted"}

    # -- Model interface -----------------------------------------------------------------
    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self._config

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        self.calls.append({"messages": messages, "tool_specs": tool_specs, "system_prompt": system_prompt})
        turn: Turn = self.turns.pop(0) if self.turns else {"text": self.final_text}
        blocks = turn if isinstance(turn, list) else [turn]

        yield {"messageStart": {"role": "assistant"}}
        stop_reason = "end_turn"
        for block in blocks:
            if "tool" in block:
                stop_reason = "tool_use"
                tool_use_id = f"tooluse_{uuid.uuid4().hex[:12]}"
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": block["tool"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(block.get("input", {}))}}}}
                yield {"contentBlockStop": {}}
            else:
                yield {"contentBlockDelta": {"delta": {"text": block.get("text", "")}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop_reason, "additionalModelResponseFields": None}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "metrics": {"latencyMs": 1},
            }
        }

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[dict[str, Any]],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        turn = self.turns.pop(0) if self.turns else {}
        data = turn.get("input", turn) if isinstance(turn, dict) else {}
        yield {"output": output_model(**data)}


def text(t: str) -> dict[str, Any]:
    return {"text": t}


def call(tool: str, **kwargs: Any) -> dict[str, Any]:
    return {"tool": tool, "input": kwargs}
