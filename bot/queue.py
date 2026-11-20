"""대기열(Redis + arq). API 서버는 일을 넣기만 하고, 워커가 꺼내서 실행한다."""
from __future__ import annotations

import os

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


async def connect() -> ArqRedis:
    return await create_pool(redis_settings())
