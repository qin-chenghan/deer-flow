"""DeerMem's own LLM construction (no deer-flow ``create_chat_model``).

``build_llm(model_config)`` builds a langchain ``ChatModel`` from DeerMem's
model sub-config (provider/model/api_key/base_url/temperature) via
``langchain.chat_models.init_chat_model``. DeerMem owns the resulting instance
(``self._llm``) and injects it into ``MemoryUpdater`` (dependency injection).

Returns ``None`` when no model is configured (zero-config): DeerMem still
serves non-LLM ops (get/clear/import/get_context); an actual memory update
raises a clear error. Any provider langchain's ``init_chat_model`` supports
works (OpenAI, Anthropic, OpenAI-compatible gateways like DeepSeek, ...).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import DeerMemModelConfig

logger = logging.getLogger(__name__)


def build_llm(model_config: "DeerMemModelConfig | None") -> Any:
    """Build a langchain ChatModel from DeerMem's model config (DI).

    Returns ``None`` if ``model_config`` is None or has no ``model`` set
    (zero-config: no LLM; non-LLM ops still work, an update will raise).
    """
    if model_config is None or not model_config.model:
        return None
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {}
    if model_config.api_key is not None:
        kwargs["api_key"] = model_config.api_key
    if model_config.base_url is not None:
        kwargs["base_url"] = model_config.base_url
    if model_config.temperature is not None:
        kwargs["temperature"] = model_config.temperature
    return init_chat_model(
        model=model_config.model,
        model_provider=model_config.provider or "openai",
        **kwargs,
    )
