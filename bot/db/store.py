"""Postgres 저장과 조회. 코드 청크, 콜 그래프."""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from bot.adapters import Frame
from bot.indexing.indexer import ServiceIndex


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://bot:bot@localhost:5432/bot")


@contextmanager
def connect(url: str | None = None):
    with psycopg.connect(url or database_url(), row_factory=dict_row) as conn:
        yield conn


def sync_service(conn: psycopg.Connection, index: ServiceIndex, commit_sha: str = "") -> dict:
    """인덱싱 결과를 DB에 맞춘다. 해시가 같은 청크는 건드리지 않는다 (설명·벡터를 다시 만들지 않기 위해)."""
    service = index.service
    rows = conn.execute("SELECT qualified_name, hash FROM code_chunk WHERE service = %s", (service,))
    existing = {r["qualified_name"]: r["hash"] for r in rows}
    current = {c.qualified_name: c for c in index.chunks}
    inserted = updated = 0
    with conn.cursor() as cur:
        for qn, c in current.items():
            old = existing.get(qn)
            if old == c.hash:
                # 내용은 같고 위치만 바뀌었을 수 있다 (위쪽 코드가 늘어난 경우)
                cur.execute(
                    "UPDATE code_chunk SET file_path=%s, start_line=%s, end_line=%s "
                    "WHERE service=%s AND qualified_name=%s AND (file_path<>%s OR start_line<>%s OR end_line<>%s)",
                    (c.file_path, c.start_line, c.end_line, service, qn, c.file_path, c.start_line, c.end_line),
                )
                continue
            cur.execute(
                """
                INSERT INTO code_chunk (service, qualified_name, name, kind, file_path, start_line, end_line, signature, content, hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (service, qualified_name) DO UPDATE SET
                    name=EXCLUDED.name, kind=EXCLUDED.kind, file_path=EXCLUDED.file_path,
                    start_line=EXCLUDED.start_line, end_line=EXCLUDED.end_line, signature=EXCLUDED.signature,
                    content=EXCLUDED.content, hash=EXCLUDED.hash,
                    description=NULL, embedding=NULL, updated_at=now()
                """,
                (service, qn, c.name, c.kind, c.file_path, c.start_line, c.end_line, c.signature, c.content, c.hash),
            )
            if old is None:
                inserted += 1
            else:
                updated += 1
        gone = [qn for qn in existing if qn not in current]
        if gone:
            cur.execute("DELETE FROM code_chunk WHERE service=%s AND qualified_name = ANY(%s)", (service, gone))
        # 엣지는 서비스 전체를 보고 다시 계산한 결과라 통째로 바꾼다
        cur.execute("DELETE FROM call_edge WHERE service=%s", (service,))
        cur.executemany(
            "INSERT INTO call_edge (service, caller, callee, kind, file_path) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            [(service, e.caller, e.callee, e.kind, e.file_path) for e in index.edges],
        )
        if commit_sha:
            cur.execute(
                "INSERT INTO index_state (service, commit_sha) VALUES (%s,%s) "
                "ON CONFLICT (service) DO UPDATE SET commit_sha=EXCLUDED.commit_sha, indexed_at=now()",
                (service, commit_sha),
            )
    conn.commit()
    return {
        "service": service,
        "inserted": inserted,
        "updated": updated,
        "deleted": len(gone),
        "unchanged": len(current) - inserted - updated,
        "edges": len(index.edges),
        "changed": [qn for qn, c in current.items() if existing.get(qn) != c.hash],
        "removed": gone,
    }


def get_chunk(conn, qualified_name: str, service: str | None = None) -> dict | None:
    sql = "SELECT * FROM code_chunk WHERE qualified_name = %s"
    args: list = [qualified_name]
    if service:
        sql += " AND service = %s"
        args.append(service)
    return conn.execute(sql + " LIMIT 1", args).fetchone()


def chunk_for_frame(conn, frame: Frame, service: str | None = None) -> dict | None:
    """스택 트레이스 한 줄에 해당하는 메서드 청크를 찾는다. 줄 번호가 들어 있는 메서드를 우선한다."""
    if not frame.class_name or not frame.method:
        return None
    prefix = f"{frame.class_name}#{frame.method}("
    sql = "SELECT * FROM code_chunk WHERE kind <> %s AND left(qualified_name, %s) = %s"
    args: list = ["class", len(prefix), prefix]
    if service:
        sql += " AND service = %s"
        args.append(service)
    sql += " ORDER BY (start_line <= %s AND end_line >= %s) DESC, start_line LIMIT 1"
    args += [frame.line, frame.line]
    return conn.execute(sql, args).fetchone()


def chunk_at(conn, file_suffix: str, line: int, service: str | None = None) -> dict | None:
    """파일 경로 끝부분과 줄 번호로 청크를 찾는다 (프론트 소스맵 결과용)."""
    sql = "SELECT * FROM code_chunk WHERE kind <> %s AND file_path LIKE %s AND start_line <= %s AND end_line >= %s"
    args: list = ["class", "%" + file_suffix.lstrip("./"), line, line]
    if service:
        sql += " AND service = %s"
        args.append(service)
    return conn.execute(sql + " ORDER BY (end_line - start_line) LIMIT 1", args).fetchone()


def neighbors(conn, start: str, direction: str = "callees", depth: int = 2, limit: int = 60) -> list[dict]:
    """콜 그래프를 따라간다. callees: start가 호출하는 쪽, callers: start를 호출하는 쪽.

    API 경로 노드(GET /api/...)를 거치므로 프론트 함수에서 백엔드 메서드까지 이어진다.
    """
    src, dst = ("caller", "callee") if direction == "callees" else ("callee", "caller")
    sql = f"""
        WITH RECURSIVE walk(node, depth, path) AS (
            SELECT %s::text, 0, ARRAY[%s::text]
            UNION ALL
            SELECT e.{dst}, w.depth + 1, w.path || e.{dst}
            FROM walk w JOIN call_edge e ON e.{src} = w.node
            WHERE w.depth < %s AND NOT e.{dst} = ANY(w.path)
        )
        SELECT node, min(depth) AS depth FROM walk WHERE depth > 0 GROUP BY node ORDER BY depth, node LIMIT %s
    """
    return conn.execute(sql, (start, start, depth, limit)).fetchall()


def edges_from(conn, caller: str) -> list[dict]:
    return conn.execute("SELECT service, callee, kind FROM call_edge WHERE caller = %s ORDER BY kind, callee", (caller,)).fetchall()


def edges_to(conn, callee: str) -> list[dict]:
    return conn.execute("SELECT service, caller, kind FROM call_edge WHERE callee = %s ORDER BY kind, caller", (callee,)).fetchall()


def routes_like(conn, method: str | None, path: str) -> list[dict]:
    """API 경로를 처리하는 메서드를 찾는다. path는 정규화된 모양 (/api/v1/rooms/{})."""
    if method:
        return conn.execute(
            "SELECT service, caller AS route, callee AS handler FROM call_edge WHERE kind = %s AND caller IN (%s, %s)",
            ("route", f"{method} {path}", f"ANY {path}"),
        ).fetchall()
    return conn.execute(
        "SELECT service, caller AS route, callee AS handler FROM call_edge WHERE kind = %s AND split_part(caller, %s, 2) = %s",
        ("route", " ", path),
    ).fetchall()


def all_routes(conn) -> list[dict]:
    return conn.execute(
        "SELECT service, caller AS route, callee AS handler FROM call_edge WHERE kind = %s ORDER BY caller", ("route",)
    ).fetchall()
