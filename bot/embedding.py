"""임베딩(글 → 벡터). 키가 없으면 None을 돌려주고, 검색은 키워드만으로 동작한다."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("bot.embedding")
MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
_client = None


def enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI()
    return _client


async def embed_many(texts: list[str]) -> list[list[float]] | None:
    if not enabled() or not texts:
        return None
    try:
        out: list[list[float]] = []
        for i in range(0, len(texts), 100):  # 요청을 묶어서 보낸다
            resp = await _get_client().embeddings.create(model=MODEL, input=[t[:8000] for t in texts[i : i + 100]])
            out.extend(d.embedding for d in resp.data)
        return out
    except Exception as e:
        log.warning("임베딩 실패: %s", e)
        return None


async def embed(text: str) -> list[float] | None:
    result = await embed_many([text])
    return result[0] if result else None
