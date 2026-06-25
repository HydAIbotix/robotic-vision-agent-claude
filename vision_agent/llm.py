"""
LLM factory. Change VISION_BACKEND in .env — zero other code changes.

Local/dev  → ChatAnthropic   (Anthropic API key)
AWS        → ChatBedrockConverse (IAM role, no key needed)
"""
from langchain_core.language_models import BaseChatModel
from vision_agent.config import settings


def get_llm() -> BaseChatModel:
    if settings.vision_backend == "bedrock":
        from langchain_aws import ChatBedrockConverse  # pip install langchain-aws
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
