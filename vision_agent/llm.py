"""
LLM factory. Change VISION_BACKEND in .env — zero other code changes.

Local/dev  → ChatAnthropic   (Anthropic API key)
AWS        → ChatBedrockConverse (IAM role, no key needed)

Three tiers:
  get_explorer_llm() — Opus 4.8: exploration reasoning (SUGGEST_EXPLORABLE_ACTIONS, IDENTIFY_RESULT_SCREEN)
  get_llm()          — Sonnet:   screen analysis, element detection, test planning
  get_fast_llm()     — Haiku:    validation only (binary yes/no, no element detection)
"""
from langchain_core.language_models import BaseChatModel
from vision_agent.config import settings


def get_llm() -> BaseChatModel:
    if settings.vision_backend == "bedrock":
        from langchain_aws import ChatBedrockConverse
        return ChatBedrockConverse(
            model=settings.bedrock_model_id,
            region_name=settings.bedrock_region,
        )

    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        max_tokens=8192,
    )


def get_explorer_llm() -> BaseChatModel:
    """Opus 4.8 — used for app exploration reasoning.

    Exploration requires multi-step reasoning about conditional navigation (which buttons
    need preconditions set up, which screens are gated behind state like a non-empty cart).
    Opus significantly outperforms Sonnet on this task.  Used for SUGGEST_EXPLORABLE_ACTIONS
    and IDENTIFY_RESULT_SCREEN — the two calls where exploration quality is determined.
    """
    if settings.vision_backend == "bedrock":
        from langchain_aws import ChatBedrockConverse
        return ChatBedrockConverse(
            model="anthropic.claude-opus-4-8",
            region_name=settings.bedrock_region,
        )

    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=settings.anthropic_explorer_model,
        api_key=settings.anthropic_api_key,
        max_tokens=8192,
    )


def get_fast_llm() -> BaseChatModel:
    """Haiku — used for validate_step only. Faster (~0.8s vs ~2.5s), cheaper."""
    if settings.vision_backend == "bedrock":
        from langchain_aws import ChatBedrockConverse
        return ChatBedrockConverse(
            model="anthropic.claude-haiku-4-5-20251001-v1:0",
            region_name=settings.bedrock_region,
        )

    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=settings.anthropic_fast_model,
        api_key=settings.anthropic_api_key,
        max_tokens=1024,
    )
