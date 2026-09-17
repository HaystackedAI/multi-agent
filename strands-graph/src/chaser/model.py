"""Model factory: Bedrock by default, Anthropic API optionally."""

from __future__ import annotations

import logging
import os

from strands.models import BedrockModel
from strands.models.model import Model

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


def make_model() -> Model:
    """Build the LLM used by every agent.

    - Default: ``BedrockModel`` with ``BEDROCK_MODEL_ID`` / ``AWS_REGION`` and an explicit ``max_tokens``.
    - If ``MODEL_PROVIDER=anthropic`` and ``ANTHROPIC_API_KEY`` is set, use ``AnthropicModel``
      (requires the ``strands-agents[anthropic]`` extra); falls back to Bedrock if the import fails.
    """
    provider = os.getenv("MODEL_PROVIDER", "bedrock").lower()
    if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
        try:
            from strands.models.anthropic import AnthropicModel

            return AnthropicModel(
                client_args={"api_key": os.environ["ANTHROPIC_API_KEY"]},
                model_id=os.getenv("ANTHROPIC_MODEL_ID", "claude-sonnet-4-5"),
                max_tokens=4096,
                params={"temperature": 0.2},
            )
        except ImportError:  # extra not installed
            logger.warning("strands-agents[anthropic] not installed; falling back to Bedrock")

    return BedrockModel(
        model_id=os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        max_tokens=4096,
        temperature=0.2,
    )
