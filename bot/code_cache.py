"""메서드 코드 캐시 (Redis, cache-aside).

콜 그래프를 따라가며 같은 메서드 코드를 여러 번 읽는다. Redis에 먼저 묻고, 없으면 Postgres에서 읽어 넣어 둔다.
배포 때 바뀐 메서드의 키는 지운다. TTL은 지우기가 빠졌을 때를 대비한 안전망이다.
"""
from __future__ import annotations

import json
import logging

from bot.db import store

log = logging.getLogger("bot.cache")
TTL_SECONDS = 60 * 60 * 24
HIT, MISS = "stats:code_cache:hit", "stats:code_cache:miss"


def key(service: str, qualified_name: str) -> str:
    return f"code:{service}:{qualified_name}"


async def get(redis, conn, qualified_name: str, service: str | None = None) -> dict | None:
    """코드와 호출 대상 목록을 돌려준다. Redis가 죽어 있으면 Postgres로 바로 간다."""
    chunk = None
    if redis is not None and service:
        try:
            cached = await redis.get(key(service, qualified_name))
            if cached:
                await redis.incr(HIT)
                return json.loads(cached)
        except Exception as e:
            log.warning("redis 조회 실패, DB로 진행: %s", e)
            redis = None
    chunk = store.get_chunk(conn, qualified_name, service)
    if chunk is None:
        return None
    value = {
        "service": chunk["service"],
        "qualified_name": chunk["qualified_name"],
        "name": chunk["name"],
        "kind": chunk["kind"],
        "file_path": chunk["file_path"],
        "start_line": chunk["start_line"],
        "end_line": chunk["end_line"],
        "content": chunk["content"],
        "description": chunk["description"],
        "callees": [e["callee"] for e in store.edges_from(conn, qualified_name)],
    }
    if redis is not None:
        try:
            await redis.incr(MISS)
            await redis.set(key(value["service"], qualified_name), json.dumps(value, ensure_ascii=False), ex=TTL_SECONDS)
        except Exception as e:
            log.warning("redis 저장 실패: %s", e)
    return value


async def invalidate(redis, service: str, qualified_names: list[str]) -> int:
    """배포로 바뀌거나 없어진 메서드의 키를 지운다."""
    if redis is None or not qualified_names:
        return 0
    return await redis.delete(*[key(service, qn) for qn in qualified_names])


async def stats(redis) -> dict:
    hit = int(await redis.get(HIT) or 0)
    miss = int(await redis.get(MISS) or 0)
    total = hit + miss
    return {"hit": hit, "miss": miss, "hit_rate": round(hit / total, 3) if total else None}
