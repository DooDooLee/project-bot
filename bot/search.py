"""코드·문서 검색. 키워드 검색(Postgres 전문 검색)과 벡터 검색(pgvector)을 따로 돌린 뒤 순위를 합친다.

이름이나 에러 코드처럼 정확한 단어는 키워드 검색이, "방 입장 처리"처럼 뜻으로 묻는 질문은 벡터 검색이 잡는다.
합칠 때는 RRF(Reciprocal Rank Fusion)를 쓴다: 각 검색에서의 순위 r 에 대해 1/(60+r) 을 더한 값으로 다시 줄을 세운다.
"""
from __future__ import annotations

import re

from bot import embedding

RRF_K = 60
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|[가-힣]{2,}")


def _terms(query: str) -> list[str]:
    seen, out = set(), []
    for t in _WORD.findall(query):
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:8]


def _vector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def rrf(*rankings: list) -> list:
    """여러 순위 목록을 하나로 합친다. 여러 목록에서 위쪽에 있을수록 점수가 높다."""
    scores: dict = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda k: scores[k], reverse=True)


async def search_code(conn, query: str, service: str | None = None, limit: int = 5) -> list[dict]:
    terms = _terms(query)
    svc_sql, svc_args = ("AND service = %s", [service]) if service else ("", [])
    keyword_ids: list[int] = []
    if terms:
        tsquery = " | ".join(re.sub(r"[^\w가-힣]", "", t) for t in terms)
        ascii_terms = [t for t in terms if t.isascii()]
        like = " OR ".join(["name ILIKE %s"] * len(ascii_terms))
        sql = f"""
            SELECT id FROM code_chunk
            WHERE (search @@ to_tsquery('simple', %s) {('OR ' + like) if like else ''}) {svc_sql}
            ORDER BY ts_rank(search, to_tsquery('simple', %s)) DESC, (kind = 'class') LIMIT 20
        """
        args = [tsquery, *[f"%{t}%" for t in ascii_terms], *svc_args, tsquery]
        keyword_ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    vector_ids: list[int] = []
    qvec = await embedding.embed(query)
    if qvec is not None:
        sql = f"SELECT id FROM code_chunk WHERE embedding IS NOT NULL {svc_sql} ORDER BY embedding <=> %s::vector LIMIT 20"
        vector_ids = [r["id"] for r in conn.execute(sql, [*svc_args, _vector(qvec)]).fetchall()]
    ids = rrf(keyword_ids, vector_ids)[:limit]
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id, service, qualified_name, name, kind, file_path, start_line, end_line, signature, description, content FROM code_chunk WHERE id = ANY(%s)",
        (ids,),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def search_docs(conn, query: str, limit: int = 4) -> list[dict]:
    terms = _terms(query)
    keyword_ids: list[int] = []
    if terms:
        tsquery = " | ".join(re.sub(r"[^\w가-힣]", "", t) for t in terms)
        keyword_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM doc_chunk WHERE search @@ to_tsquery('simple', %s) ORDER BY ts_rank(search, to_tsquery('simple', %s)) DESC LIMIT 15",
                (tsquery, tsquery),
            ).fetchall()
        ]
    vector_ids: list[int] = []
    qvec = await embedding.embed(query)
    if qvec is not None:
        vector_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM doc_chunk WHERE embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT 15", (_vector(qvec),)
            ).fetchall()
        ]
    ids = rrf(keyword_ids, vector_ids)[:limit]
    if not ids:
        return []
    rows = conn.execute("SELECT id, path, heading, content FROM doc_chunk WHERE id = ANY(%s)", (ids,)).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]
