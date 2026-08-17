"""
Shared local embedding model — one process-wide SentenceTransformer instance, used by the ADF
tool-search retriever (mcp_servers/adf/tool_search_tool.py) today, and by SOP retrieval once
that's built (deferred — see the "Expose all ADF tools" plan's follow-up notes). A single
shared singleton means the ~90MB model loads exactly once per process, not once per caller.

all-MiniLM-L6-v2 chosen for the same reason claude-desktop's own toolsearch_prototype v7
evaluation used it: small (384-dim), fast on CPU, no GPU dependency — good enough for short
tool-name/description/query text, and the model this org already has local-inference experience
with.

Loaded lazily (on first embed_texts call), not at import time — every test file and script that
imports mcp_servers.adf.tool_search_tool imports this module transitively; an eager
SentenceTransformer(...) at module load would force a ~90MB model load (and a network fetch on
a cold cache) onto every test run and CI invocation, whether or not that run ever actually
embeds anything.
"""

import asyncio
import logging

import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
        logger.info("Loaded local embedding model %s", _MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    """Synchronous, CPU-bound — call via embed_texts_async from an async context (this runs on
    every chat turn via tool_search_tool.py, so blocking the event loop for it is a real cost,
    same reasoning as gateway/vault.py's Key Vault fetch fix)."""
    return _get_model().encode(texts, convert_to_numpy=True, normalize_embeddings=True)


async def embed_texts_async(texts: list[str]) -> np.ndarray:
    return await asyncio.to_thread(embed_texts, texts)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """a, b already L2-normalized (embed_texts uses normalize_embeddings=True), so this is just
    the dot product — no need to divide by norms again."""
    return float(np.dot(a, b))
