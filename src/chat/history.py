"""
Reconstructs a chat thread's message history into the shape the OpenAI Agents SDK's
Runner.run(agent, input=...) expects for a multi-turn conversation.

Deliberately does NOT replay tool-call items in the SDK's internal function-call
representation (versioned, call-ID-bearing, fragile to hand-reconstruct from a DB row and
easy to silently drift from as `openai-agents` updates). The model only needs continuity of
what was SAID, not what was LOOKED UP — each turn re-derives evidence via its own tool calls
if it needs to. This is a deliberate simplification, not an oversight.
"""
from db.models import ChatMessage


def to_input_list(messages: list[ChatMessage]) -> list[dict]:
    """`messages` must already be ordered oldest-first (by id/created_at)."""
    return [
        {"role": m.role, "content": m.content}
        for m in messages
        if m.role in ("user", "assistant")
    ]
