"""
Token-budget-triggered context summarization — same technique this Claude Code session's own
context compaction uses (per its own system prompt: summarize the old portion, keep the
recent tail raw, hand both to the next turn). Runs synchronously, but only at the start of
the turn that actually crosses the budget — not on every turn, not as a background job racing
the conversation.
"""

import logging

import tiktoken
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from db.models import ChatMessage, ChatThread

logger = logging.getLogger(__name__)

_ENCODING = tiktoken.get_encoding("cl100k_base")
# gpt-4o's context window is 128k tokens — 6k (the original placeholder value) triggered
# summarization after just a handful of turns (a single tool-result payload like a pipeline
# definition can easily be 1-3k tokens on its own), paying for an extra LLM call far more
# often than the model's real capacity warrants. 24k leaves generous headroom under 128k for
# the system prompt/tool schemas/response while letting genuinely long conversations run many
# turns before the first summarization pass.
_TOKEN_BUDGET = 10_000

_client = AsyncOpenAI(
    base_url=settings.azure_openai_v1_base_url, api_key=settings.azure_openai_api_key
)


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


async def maybe_summarize(db: AsyncSession, thread: ChatThread) -> None:
    """Called at the end of post_message, after the reply is already persisted — never blocks
    the reply itself. If this turn crossed the budget, the NEXT turn's history-building sees
    the freshly-compressed context_summary + advanced cursor."""
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.thread_id == thread.thread_id,
            ChatMessage.created_at
            > (thread.summarized_through_timestamp or thread.created_at),
        )
        .order_by(ChatMessage.created_at)
    )
    unsummarized = list(result.scalars().all())
    if not unsummarized:
        return

    combined_text = "\n".join(f"{m.role}: {m.content}" for m in unsummarized)
    if count_tokens(combined_text) <= _TOKEN_BUDGET:
        return

    prompt = (
        "Summarize this conversation concisely, preserving specific facts (pipeline names, "
        "error codes, decisions made, fixes tried) that a continuation of the conversation "
        "would need. Prior summary (may be empty):\n\n"
        f"{thread.context_summary or '(none)'}\n\n"
        f"New messages to fold in:\n{combined_text}"
    )
    try:
        response = await _client.chat.completions.create(
            model=settings.azure_openai_deployment,
            messages=[{"role": "user", "content": prompt}],
        )
        new_summary = response.choices[0].message.content
    except Exception:
        logger.exception(
            "Summarization pass failed for thread_id=%s — leaving context_summary unchanged",
            thread.thread_id,
        )
        return

    thread.context_summary = new_summary
    thread.summarized_through_timestamp = unsummarized[-1].created_at
    await db.commit()
