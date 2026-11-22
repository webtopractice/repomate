from functools import lru_cache

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage

from ...core.config import settings


@lru_cache
def get_llm() -> ChatAnthropic:
    return ChatAnthropic(model=settings.model_name, temperature=0, max_tokens=2048,
                         timeout=45, max_retries=2, api_key=settings.anthropic_api_key or "missing")


def text_of(msg: BaseMessage | str) -> str:
    if isinstance(msg, str):
        return msg
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
