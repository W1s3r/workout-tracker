"""LLM provider factory."""
from __future__ import annotations

import importlib
from typing import Any, Dict

from hsa.llm.base import LLMClient, LLMError, LLMResponse, ToolCall  # noqa: F401


def make_client(profile: Dict[str, Any]) -> LLMClient:
    ptype = profile["provider_config"].get("type", "openai")
    if ptype == "openai":
        from hsa.llm.openai_compat import OpenAICompatClient
        return OpenAICompatClient(profile)
    if ptype == "mock":
        from hsa.llm.mock import MockClient
        return MockClient(profile)
    if ptype == "custom":
        mod = importlib.import_module(profile["provider_config"]["module"])
        cls = getattr(mod, profile["provider_config"].get("class", "CustomClient"))
        return cls(profile)
    raise LLMError("Unknown provider type: %s" % ptype)
