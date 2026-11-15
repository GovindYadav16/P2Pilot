"""Chat model factory. All real LLM traffic goes through the LiteLLM gateway by alias.

Per-call metadata (tenant, user, skill, run_id) is sent in the request body so LiteLLM's
spend logs can attribute cost per agent/skill.
"""

from __future__ import annotations

from itertools import cycle
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from p2pilot.config import Settings

ModelAlias = Literal["fast", "reasoning"]

FAKE_REPLY = (
    "Hello from P2Pilot. I am running on the offline fake model, "
    "so this reply is deterministic."
)


def make_chat_model(
    settings: Settings,
    *,
    alias: ModelAlias = "fast",
    metadata: dict[str, str] | None = None,
) -> BaseChatModel:
    if settings.llm_fake:
        # Streams FAKE_REPLY word by word, like a real model would stream tokens.
        return GenericFakeChatModel(messages=cycle([AIMessage(content=FAKE_REPLY)]))

    from langchain_openai import ChatOpenAI  # imported lazily: not needed for fake mode

    return ChatOpenAI(
        model=alias,  # LiteLLM resolves the alias to a provider deployment
        base_url=f"{settings.litellm_base_url.rstrip('/')}/v1",
        api_key=settings.litellm_master_key,
        streaming=True,
        stream_usage=True,  # ask for token usage on streamed replies (for evals and spend)
        timeout=settings.llm_timeout_s,
        max_retries=1,  # the gateway owns retries and fallbacks
        extra_body={"metadata": metadata or {}},
    )
