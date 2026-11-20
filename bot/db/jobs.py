"""작업(Slack 스레드 하나)과 처리 기록(incident) 저장."""
from __future__ import annotations

import json

from psycopg.types.json import Jsonb


def upsert_job(conn, kind: str, channel: str, thread_ts: str, state: dict) -> dict:
    row = conn.execute(
        """
        INSERT INTO job (kind, slack_channel, slack_ts, state) VALUES (%s,%s,%s,%s)
        ON CONFLICT (slack_channel, slack_ts) DO UPDATE SET state = job.state || EXCLUDED.state, updated_at = now()
        RETURNING *
        """,
        (kind, channel, thread_ts, Jsonb(state)),
    ).fetchone()
    conn.commit()
    return row


def get_job(conn, job_id: int) -> dict | None:
    return conn.execute("SELECT * FROM job WHERE id = %s", (job_id,)).fetchone()


def update_job(conn, job_id: int, status: str | None = None, **state) -> dict:
    row = conn.execute(
        "UPDATE job SET status = coalesce(%s, status), state = state || %s, updated_at = now() WHERE id = %s RETURNING *",
        (status, Jsonb(json.loads(json.dumps(state, default=str))), job_id),
    ).fetchone()
    conn.commit()
    return row


def claim(conn, job_id: int, from_status: tuple[str, ...], to_status: str) -> bool:
    """같은 버튼을 두 번 눌러도 한 번만 실행되게 한다."""
    row = conn.execute(
        "UPDATE job SET status = %s, updated_at = now() WHERE id = %s AND status = ANY(%s) RETURNING id",
        (to_status, job_id, list(from_status)),
    ).fetchone()
    conn.commit()
    return row is not None


def _vector(embedding: list[float] | None) -> str | None:
    return None if embedding is None else "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"


def create_incident(conn, job: dict, result: dict, extracted: dict, methods: list[str], embedding: list[float] | None, commit_sha: str = "") -> int:
    row = conn.execute(
        """
        INSERT INTO incident (service, api, symptom, cause, cause_type, resolution, methods, commit_sha, slack_channel, slack_ts, embedding)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) RETURNING id
        """,
        (
            result.get("service"),
            extracted.get("api"),
            result.get("symptom") or "",
            result.get("cause"),
            result.get("cause_type"),
            result.get("fix") or result.get("action"),
            methods,
            commit_sha,
            job["slack_channel"],
            job["slack_ts"],
            _vector(embedding),
        ),
    ).fetchone()
    conn.commit()
    return row["id"]


def resolve_incident(conn, incident_id: int, resolution: str | None = None, pr_url: str | None = None) -> None:
    conn.execute(
        "UPDATE incident SET status = 'resolved', resolution = coalesce(%s, resolution), pr_url = coalesce(%s, pr_url) WHERE id = %s",
        (resolution, pr_url, incident_id),
    )
    conn.commit()


def set_incident_pr(conn, incident_id: int, pr_url: str) -> None:
    conn.execute("UPDATE incident SET pr_url = %s WHERE id = %s", (pr_url, incident_id))
    conn.commit()


def incident_by_pr(conn, pr_url: str) -> dict | None:
    return conn.execute("SELECT * FROM incident WHERE pr_url = %s", (pr_url,)).fetchone()


def similar_incidents(conn, embedding: list[float] | None, limit: int = 3, max_distance: float = 0.6) -> list[dict]:
    """해결된 처리 기록 가운데 증상·원인이 비슷한 것을 찾는다. <=> 는 코사인 거리라 작을수록 가깝다."""
    if embedding is None:
        return []
    return conn.execute(
        """
        SELECT id, created_at, service, api, symptom, cause, cause_type, resolution, pr_url, embedding <=> %s::vector AS distance
        FROM incident
        WHERE status = 'resolved' AND embedding IS NOT NULL AND embedding <=> %s::vector < %s
        ORDER BY distance LIMIT %s
        """,
        (_vector(embedding), _vector(embedding), max_distance, limit),
    ).fetchall()


def search_incidents(conn, mode: str = "latest", service: str | None = None, days: int | None = None,
                     embedding: list[float] | None = None, limit: int = 5) -> list[dict]:
    """처리 기록 조회. mode=latest 는 최신순, mode=similar 는 뜻이 가까운 순."""
    where, args = ["TRUE"], []
    if service:
        where.append("service = %s")
        args.append(service)
    if days:
        where.append("created_at > now() - make_interval(days => %s)")
        args.append(days)
    cols = "id, created_at, service, api, symptom, cause, cause_type, resolution, pr_url, status"
    if mode == "similar" and embedding is not None:
        where.append("embedding IS NOT NULL")
        sql = f"SELECT {cols}, embedding <=> %s::vector AS distance FROM incident WHERE {' AND '.join(where)} ORDER BY distance LIMIT %s"
        return conn.execute(sql, [_vector(embedding), *args, limit]).fetchall()
    sql = f"SELECT {cols} FROM incident WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT %s"
    return conn.execute(sql, [*args, limit]).fetchall()
