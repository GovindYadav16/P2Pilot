"""Sprint 1 agent: a one-node LangGraph that answers using the conversation history.

Sprint 2 replaces this with a supervisor that routes to skill subgraphs
(invoice-processing, policy-qa, ...). The streaming adapter in runner.py stays the same.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

SYSTEM_PROMPT = (
    "You are P2Pilot, an assistant for accounts-payable and procurement teams at a "
    "manufacturing and distribution company. Be concise. If you are not sure, say so. "
    "Never invent invoice, PO or vendor data."
)


class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_chat_graph(llm: BaseChatModel, checkpointer: BaseCheckpointSaver | None = None):
    async def respond(state: ChatState, config: RunnableConfig) -> dict:
        # Passing `config` through is what lets token streaming reach graph.astream().
        reply = await llm.ainvoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]], config)
        return {"messages": [reply]}

    graph = StateGraph(ChatState)
    graph.add_node("respond", respond)
    graph.add_edge(START, "respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)
