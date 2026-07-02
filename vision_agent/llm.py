"""
LLM factory. Change VISION_BACKEND in .env — zero other code changes.

Local/dev  → ChatAnthropic   (Anthropic API key)
AWS        → ChatBedrockConverse (IAM role, no key needed)

Every Claude call in the system uses Opus 4.8 — the three functions differ only by
output-token cap, kept separate so exploration / short-response tiers can be tuned later.
  get_explorer_llm() — Opus 4.8: exploration reasoning (SUGGEST_EXPLORABLE_ACTIONS, IDENTIFY_RESULT_SCREEN)
  get_llm()          — Opus 4.8: screen analysis, element detection, planning, Tier-3
  get_fast_llm()     — Opus 4.8: short JSON responses (validate_pipeline verify, conclusive verdict)
"""
import json
from langchain_core.language_models import BaseChatModel
from vision_agent.config import settings


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if "```" in text:
        # Take the content between the first pair of fences, drop a leading "json" tag.
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
            text = text.strip()
    return text


def invoke_json(llm: BaseChatModel, messages: list, *, retries: int = 2, default=None, label: str = "llm"):
    """Invoke an LLM and parse its response as JSON, resiliently.

    Real models occasionally return an empty string or a non-JSON preamble — and a bare
    json.loads() on that crashes the whole graph with "Expecting value: line 1 column 1".
    This helper retries a few times and, if every attempt fails, returns `default` instead
    of raising, so one flaky response degrades a single step rather than killing the run.
    """
    last_err = None
    for attempt in range(1, retries + 2):   # e.g. retries=2 → 3 total attempts
        try:
            content = llm.invoke(messages).content
            # ChatAnthropic returns a str for plain text, or a list of blocks when the
            # response is multi-part; concatenate any text blocks in the latter case.
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                )
            raw = _strip_fences(content)
            if not raw:
                raise ValueError("empty response")
            return json.loads(raw)
        except Exception as exc:   # JSONDecodeError, ValueError, API hiccup, etc.
            last_err = exc
            if attempt <= retries:
                print(f"  [LLM/{label}] unparseable response (attempt {attempt}/{retries + 1}): {exc} — retrying")
    print(f"  [LLM/{label}] giving up after {retries + 1} attempts ({last_err}) — using fallback")
    return default


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
    """Short-response tier — Opus 4.8, same as every other call for consistency.

    Used for validate_pipeline (Tier-1/2 verify) and the conclusive verdict: short JSON
    responses, so the only difference from get_llm() is a lower max_tokens cap.
    """
    if settings.vision_backend == "bedrock":
        from langchain_aws import ChatBedrockConverse
        return ChatBedrockConverse(
            model="anthropic.claude-opus-4-8",
            region_name=settings.bedrock_region,
        )

    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=settings.anthropic_fast_model,
        api_key=settings.anthropic_api_key,
        max_tokens=2048,
    )
