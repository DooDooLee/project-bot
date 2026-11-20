"""청크마다 한국어 한 줄 설명을 만들고, 그 설명으로 벡터를 만든다.

코드 전체를 벡터로 만들면 코드 내용에 묻혀서 한국어 질문과의 유사도가 낮게 나온다.
그래서 벡터는 "설명 + 시그니처"로 만들고, 코드 원문은 content 칸에 따로 둔다. 설명은 DB에만 저장하고 소스에는 넣지 않는다.
"""
from __future__ import annotations

import asyncio
import logging

from bot import embedding, llm

log = logging.getLogger("bot.enrich")
BATCH = 12

SYSTEM = (
    "코드 조각마다 무엇을 하는 코드인지 한국어 한 문장으로 설명한다. 팀원이 '방 입장 처리하는 코드'처럼 검색했을 때 찾을 수 있게 "
    "기능 중심으로 쓴다. 메서드 이름을 그대로 옮기지 말고, 코드가 실제로 하는 일을 쓴다. 40자 안팎."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {"id": {"type": "integer"}, "description": {"type": "string"}}, "required": ["id", "description"]},
        }
    },
    "required": ["items"],
}


async def _describe(rows: list[dict], usage: llm.Usage) -> dict[int, str]:
    body = "\n\n".join(f"### id={r['id']} ({r['service']}, {r['kind']}) {r['name']}\n```\n{r['content'][:1500]}\n```" for r in rows)
    try:
        result = await llm.structured(llm.HAIKU, SYSTEM, body, name="describe", description="코드 조각별 설명을 기록한다", schema=SCHEMA, max_tokens=1500, usage=usage)
        return {int(i["id"]): i["description"].strip() for i in result.get("items", []) if i.get("description")}
    except Exception as e:
        log.warning("설명 생성 실패 (%d개): %s", len(rows), e)
        return {}


async def enrich(conn, limit: int | None = None, concurrency: int = 4) -> dict:
    """설명이나 벡터가 없는 청크만 채운다. 배포 때는 바뀐 청크만 해당된다."""
    usage = llm.Usage()
    rows = conn.execute(
        "SELECT id, service, kind, name, signature, content FROM code_chunk WHERE description IS NULL ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    batches = [rows[i : i + BATCH] for i in range(0, len(rows), BATCH)]
    described = 0
    sem = asyncio.Semaphore(concurrency)

    async def work(batch):
        async with sem:
            return await _describe(batch, usage)

    for group in [batches[i : i + 20] for i in range(0, len(batches), 20)]:
        for result in await asyncio.gather(*(work(b) for b in group)):
            for chunk_id, desc in result.items():
                conn.execute("UPDATE code_chunk SET description = %s WHERE id = %s", (desc, chunk_id))
                described += 1
        conn.commit()

    embedded = 0
    if embedding.enabled():
        todo = conn.execute("SELECT id, name, signature, description FROM code_chunk WHERE embedding IS NULL AND description IS NOT NULL ORDER BY id").fetchall()
        for i in range(0, len(todo), 100):
            part = todo[i : i + 100]
            vectors = await embedding.embed_many([f"{r['description']}\n{r['signature'][:300]}" for r in part])
            if vectors is None:
                break
            for r, v in zip(part, vectors):
                conn.execute("UPDATE code_chunk SET embedding = %s::vector WHERE id = %s", ("[" + ",".join(f"{x:.6f}" for x in v) + "]", r["id"]))
                embedded += 1
            conn.commit()
    return {"described": described, "embedded": embedded, "usage": usage.as_dict()}
