"""Live trace of a weekly-close run, streamed to the UI as Server-Sent Events.

The deployed AgentCore runtime is request/response and the ApprovalGate never suspends the
run (see ``approval.py``), so a click-to-continue debugger is impossible there. Instead we
*stream* the run: a :class:`TraceEmitter` hook attached to every node turns each event into a
small JSON ``frame`` and pushes it onto a thread-safe queue. A streaming entrypoint drains the
queue and yields the frames; AgentCore serializes a generator entrypoint straight out as SSE.

Frame kinds (all carry ``seq`` and ``ts``)::

    node_enter   {node}                         a specialist starts working
    thinking     {node}                         model call about to run
    said         {node, text}                   assistant narration
    tool_call    {node, tool, input, at}        one tool about to run (``at`` = "file.py:line")
    tool_result  {node, tool, status, output}   that tool's result (or "gated")
    edge         {name, value, detail}          a graph edge condition was evaluated
    done         {ok, report, pending, error}   terminal frame

Nothing here pauses the run; it only observes. The genuine human-in-the-loop interrupt is the
approval decision (a separate ``decide`` call replays the gated tool), surfaced as a
``tool_result`` with ``status="gated"``.
"""

from __future__ import annotations

import inspect
import itertools
import os
import queue
import time
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)

# Sentinel pushed onto the queue to tell a draining consumer the run is over.
_END = object()


def tool_location(selected_tool: Any) -> str:
    """Return "file.py:line" for a Strands tool, or "" if it can't be resolved.

    ``DecoratedFunctionTool`` keeps the undecorated function on ``_tool_func`` (and mirrors it on
    ``__wrapped__``); we read the source file and first line from whichever is present.
    """
    func = getattr(selected_tool, "_tool_func", None) or getattr(selected_tool, "__wrapped__", None)
    if func is None:
        return ""
    try:
        path = inspect.getsourcefile(func) or inspect.getfile(func)
        line = inspect.getsourcelines(func)[1]
    except (OSError, TypeError):
        return ""
    return f"{os.path.basename(path)}:{line}"


def _result_text(result: dict[str, Any] | None) -> str:
    for block in (result or {}).get("content", []) or []:
        if "text" in block:
            return str(block["text"])
        if "json" in block:
            return str(block["json"])
    return ""


class TraceStream:
    """A thread-safe channel of trace frames between the running graph and the SSE generator.

    The graph runs on a worker thread; its hooks call :meth:`emit`. The streaming entrypoint
    calls :meth:`drain` to iterate frames as they arrive until :meth:`close` is called.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[Any] = queue.Queue()
        self._seq = itertools.count(1)

    def emit(self, kind: str, **fields: Any) -> None:
        self._q.put({"seq": next(self._seq), "ts": round(time.time(), 3), "kind": kind, **fields})

    def close(self) -> None:
        self._q.put(_END)

    def drain(self, timeout: float = 0.5) -> Any:
        """Block up to ``timeout`` for the next frame. Returns the frame, ``None`` on timeout,
        or the ``_END`` sentinel when the run is finished (identity-compare with ``is_end``)."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    @staticmethod
    def is_end(item: Any) -> bool:
        return item is _END


class TraceEmitter(HookProvider):
    """Hook attached to every node; converts agent events into trace frames on a TraceStream."""

    def __init__(self, stream: TraceStream) -> None:
        self._stream = stream
        self._entered: set[str] = set()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(MessageAddedEvent, self._message_added)
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    @staticmethod
    def _node(event: Any) -> str:
        return getattr(getattr(event, "agent", None), "name", None) or "agent"

    def _enter(self, node: str) -> None:
        # Emit once per node. bookkeeper and collector run concurrently, so their events interleave;
        # the concurrency stays visible via each tool_call/tool_result's node tag, without re-emitting
        # "enters" every time the interleaving flips back.
        if node not in self._entered:
            self._entered.add(node)
            self._stream.emit("node_enter", node=node)

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        node = self._node(event)
        self._enter(node)
        self._stream.emit("thinking", node=node)

    def _message_added(self, event: MessageAddedEvent) -> None:
        message = event.message or {}
        if message.get("role") != "assistant":
            return
        for block in message.get("content") or []:
            if "text" in block and str(block["text"]).strip():
                self._stream.emit("said", node=self._node(event), text=str(block["text"]))

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use or {}
        name = str(tool_use.get("name") or "tool")
        if name[:1].isupper():
            return  # internal structured-output tool (e.g. WeeklyCloseReport)
        node = self._node(event)
        self._enter(node)
        self._stream.emit(
            "tool_call",
            node=node,
            tool=name,
            input=dict(tool_use.get("input") or {}),
            at=tool_location(event.selected_tool),
        )

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use or {}
        name = str(tool_use.get("name") or "tool")
        if name[:1].isupper():
            return
        text = _result_text(event.result)
        status = str((event.result or {}).get("status", "done"))
        if event.cancel_message or text.startswith("DENIED"):
            status = "gated"
        self._stream.emit("tool_result", node=self._node(event), tool=name, status=status, output=text[:400])
