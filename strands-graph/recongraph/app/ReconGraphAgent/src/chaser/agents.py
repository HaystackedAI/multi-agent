"""Agent factories, the tool-ownership registry, and the weekly-close Graph.

Topology (Strands ``GraphBuilder``)::

    reconciler --always--> bookkeeper --\\
        \\--if overdue > 0--> collector ----> reporter

Each node is an ``Agent`` with its own tool subset, the shared ``AuditHook`` and (for the
collector) the ``ApprovalGate`` intervention. Agents carry no session manager: the graph is
rebuilt and re-run fresh every cycle and all durable state lives in the ``Store``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.model import Model
from strands.multiagent import GraphBuilder
from strands.multiagent.graph import Graph, GraphState
from strands.session.session_manager import SessionManager

from . import config, prompts, tools
from .approval import ApprovalGate
from .hooks import AuditHook, ProgressHook
from .model import make_model
from .store import Store

NODE_NAMES: tuple[str, ...] = ("reconciler", "collector", "bookkeeper", "reporter")

ModelFactory = Callable[[str], Model]
"""Given a node name, return the model that node should use (lets tests script each node)."""


def default_model_factory(_node: str) -> Model:
    return make_model()


NODE_TOOLS: dict[str, list[Any]] = {
    "reconciler": tools.RECONCILER_TOOLS,
    "collector": tools.COLLECTOR_TOOLS,
    "bookkeeper": tools.BOOKKEEPER_TOOLS,
    "reporter": tools.REPORTER_TOOLS,
}

NODE_DESCRIPTIONS: dict[str, str] = {
    "reconciler": "Matches bank deposits to invoices, records partial payments, flags unknown deposits, "
    "and captures client email context.",
    "collector": "Chooses a follow-up tier for each overdue invoice and drafts client emails; every "
    "client-facing action is gated for owner approval.",
    "bookkeeper": "Categorizes expenses, attaches receipts and creates missing-receipt to-dos.",
    "reporter": "Summarizes the week into the WeeklyCloseReport.",
}

NODE_PROMPTS: dict[str, str] = {
    "reconciler": prompts.RECONCILER_PROMPT,
    "collector": prompts.COLLECTOR_PROMPT,
    "bookkeeper": prompts.BOOKKEEPER_PROMPT,
    "reporter": prompts.REPORTER_PROMPT,
}

TOOL_OWNERS: dict[str, str] = {t.tool_name: node for node, node_tools in NODE_TOOLS.items() for t in node_tools}
"""tool_name -> node that owns it (used to execute approved proposals through the right agent)."""


def make_node_agent(
    node: str,
    model: Model,
    *,
    gate: ApprovalGate | None = None,
    record_direct_tool_call: bool = True,
    hooks: list[Any] | None = None,
) -> Agent:
    """Build one specialist agent. ``gate`` is attached only when given (collector in the graph)."""
    if node not in NODE_TOOLS:
        raise KeyError(f"unknown node {node}")
    return Agent(
        model=model,
        name=node,
        agent_id=f"chaser-{node}",
        description=NODE_DESCRIPTIONS[node],
        system_prompt=prompts.render(NODE_PROMPTS[node], config.today().isoformat()),
        tools=NODE_TOOLS[node],
        interventions=[gate] if gate else None,
        hooks=hooks if hooks is not None else [AuditHook(), ProgressHook()],
        conversation_manager=SlidingWindowConversationManager(window_size=40),
        callback_handler=None,
        record_direct_tool_call=record_direct_tool_call,
    )


def make_execution_agent(tool_name: str, model: Model | None = None) -> Agent:
    """Agent used to execute an approved proposal directly via ``agent.tool.<tool_name>``.

    Built WITHOUT the ApprovalGate (the owner already approved) and without recording the
    direct call in message history. The AuditHook still records the execution.
    """
    node = TOOL_OWNERS.get(tool_name)
    if node is None:
        raise KeyError(f"no agent owns tool {tool_name}")
    return make_node_agent(node, model or make_model(), gate=None, record_direct_tool_call=False)


def build_graph(
    store: Store,
    model_factory: ModelFactory = default_model_factory,
    *,
    session_manager: SessionManager | None = None,
) -> tuple[Graph, dict[str, Agent], ApprovalGate]:
    """Build the weekly-close graph. Returns (graph, node agents by name, the approval gate)."""
    gate = ApprovalGate()
    agents = {
        "reconciler": make_node_agent("reconciler", model_factory("reconciler")),
        "collector": make_node_agent("collector", model_factory("collector"), gate=gate),
        "bookkeeper": make_node_agent("bookkeeper", model_factory("bookkeeper")),
        "reporter": make_node_agent("reporter", model_factory("reporter")),
    }

    def has_overdue(_state: GraphState) -> bool:
        # Evaluated when the reconciler finishes, so it reflects deposits matched this cycle.
        return store.count_overdue_open_invoices(config.today()) > 0

    b = GraphBuilder()
    for name, agent in agents.items():
        b.add_node(agent, name)
    b.add_edge("reconciler", "bookkeeper")
    b.add_edge("reconciler", "collector", condition=has_overdue)
    b.add_edge("collector", "reporter")
    b.add_edge("bookkeeper", "reporter")
    b.set_entry_point("reconciler")
    b.set_max_node_executions(8)
    b.set_execution_timeout(600)
    b.set_node_timeout(240)
    b.set_graph_id("chaser-weekly-close")
    if session_manager is not None:
        b.set_session_manager(session_manager)
    return b.build(), agents, gate


def make_ask_agent(model: Model | None = None, session_manager: SessionManager | None = None) -> Agent:
    """Conversational agent with read-only tools for `ask`; keeps history via the session manager."""
    return Agent(
        model=model or make_model(),
        name="chaser",
        agent_id="chaser-ask",
        description="Answers the owner's questions about receivables, approvals and books.",
        system_prompt=prompts.render(prompts.ASK_PROMPT, config.today().isoformat()),
        tools=tools.ASK_TOOLS,
        hooks=[AuditHook()],
        session_manager=session_manager,
        conversation_manager=SlidingWindowConversationManager(window_size=40),
        callback_handler=None,
    )
