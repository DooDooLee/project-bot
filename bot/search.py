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


_SUFFIXES = ("어디야", "에서", "으로", "하는", "하고", "이야", "인지", "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만")
_STOP = {"코드", "어디", "어디야", "부분", "로직", "처리", "함수", "기능", "API", "api", "보여", "알려", "the"}


def _stem(term: str) -> str:
    """한국어 단어 끝의 조사를 한 번 뗀다 (닉네임을 → 닉네임). 설명 글에서 부분 일치로 찾기 위해서다."""
    if term.isascii():
        return term
    for suffix in _SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= 2:
            return term[: -len(suffix)]
    return term


async def search_code(conn, query: str, service: str | None = None, limit: int = 5, *, use_keyword: bool = True,
                      use_vector: bool = True, qvec: list[float] | None = None) -> list[dict]:
    terms = [t for t in dict.fromkeys(_stem(t) for t in _terms(query)) if t not in _STOP] if use_keyword else []
    svc_sql, svc_args = ("AND service = %s", [service]) if service else ("", [])
    keyword_ids: list[int] = []
    if terms:
        # 이름에 있으면 3점, 설명에 있으면 2점, 시그니처에 있으면 1점. 여러 단어가 맞을수록 위로 온다
        score_sql = " + ".join(
            ["(CASE WHEN name ILIKE %s THEN 3 ELSE 0 END + CASE WHEN description ILIKE %s THEN 2 ELSE 0 END + CASE WHEN signature ILIKE %s THEN 1 ELSE 0 END)"] * len(terms)
        )
        args: list = []
        for t in terms:
            args += [f"%{t}%"] * 3
        sql = f"""
            SELECT id FROM (SELECT id, kind, ({score_sql}) AS score FROM code_chunk WHERE TRUE {svc_sql}) t
            WHERE score > 0 ORDER BY score DESC, (kind = 'class'), id LIMIT 20
        """
        keyword_ids = [r["id"] for r in conn.execute(sql, [*args, *svc_args]).fetchall()]
    vector_ids: list[int] = []
    if use_vector and qvec is None:
        qvec = await embedding.embed(query)
    if use_vector and qvec is not None:
        sql = f"SELECT id FROM code_chunk WHERE embedding IS NOT NULL {svc_sql} ORDER BY embedding <=> %s::vector LIMIT 20"
        vector_ids = [r["id"] for r in conn.execute(sql, [*svc_args, _vector(qvec)]).fetchall()]
    ids = rrf(keyword_ids, vector_ids)
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id, service, qualified_name, name, kind, file_path, start_line, end_line, signature, description, content FROM code_chunk WHERE id = ANY(%s)",
        (ids,),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    ordered = [by_id[i] for i in ids if i in by_id]
    # 클래스 요약 청크는 메서드 청크 뒤로 보낸다. 질문은 대부분 "어떤 메서드가 하는가"를 묻기 때문이다
    ordered.sort(key=lambda r: r["kind"] == "class")
    return ordered[:limit]


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
