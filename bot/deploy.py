"""Jenkins 배포 알림 처리.

시작: 채널에 "배포 시작" 메시지를 올린다. 이후 알림은 이 메시지의 스레드에 단다.
성공: 바뀐 코드만 다시 읽어 검색용 DB를 갱신한다.
실패: 사람이 버튼을 누르지 않아도 바로 원인 분석을 시작한다.
"""
from __future__ import annotations

import logging
import os
import time

from bot import code_cache, repo, slack_client
from bot.db import jobs, store
from bot.indexing import docs, enrich
from bot.indexing.indexer import index_project
from bot.tools import jenkins

log = logging.getLogger("bot.deploy")


def _channel() -> str:
    return os.environ.get("SLACK_CHANNEL", "")


def _key(payload: dict) -> str:
    return f"deploy:{payload.get('job', 'deploy')}:{payload.get('build_number')}"


async def reindex(ctx) -> dict:
    """저장소를 최신으로 맞추고, 해시가 달라진 청크만 갱신한다."""
    project = ctx["project"]
    before, after = await repo.sync(project)
    indexes = index_project(repo.repo_dir(), project)
    summary = {"commit": after[:8], "inserted": 0, "updated": 0, "deleted": 0, "edges": 0}
    with store.connect() as conn:
        for idx in indexes:
            r = store.sync_service(conn, idx, after)
            for k in ("inserted", "updated", "deleted", "edges"):
                summary[k] += r[k]
            # 바뀌거나 없어진 메서드의 캐시 키를 지운다 (이벤트 기반 무효화)
            await code_cache.invalidate(ctx["redis"], idx.service, r["changed"] + r["removed"])
        enriched = await enrich.enrich(conn)
        await docs.index_docs(conn, repo.repo_dir(), project.docs)
    summary["described"] = enriched["described"]
    summary["changed_files"] = len(await repo.changed_files(before, after))
    return summary


async def handle(ctx, payload: dict) -> None:
    channel = _channel()
    if not channel:
        log.warning("SLACK_CHANNEL 이 없어 배포 알림을 올리지 못함")
        return
    status = (payload.get("status") or "").upper()
    number = payload.get("build_number")
    commit = (payload.get("commit") or "")[:8]
    redis = ctx["redis"]
    if status not in ("STARTED", "SUCCESS", "FAILURE", "UNSTABLE", "ABORTED"):
        return

    if status == "STARTED":
        text = f"*배포 시작 #{number}*  브랜치 `{payload.get('branch', '-')}`  커밋 `{commit or '-'}`"
        resp = await slack_client.post_message(channel, text, blocks=[slack_client.section(text)])
        if resp.get("ts"):
            await redis.set(_key(payload), resp["ts"], ex=60 * 60 * 6)
        return

    thread = await redis.get(_key(payload))
    thread = thread.decode() if isinstance(thread, bytes) else thread
    if thread is None:  # 시작 알림을 못 받았으면 새 메시지로 시작한다
        resp = await slack_client.post_message(channel, f"*배포 #{number}*  커밋 `{commit or '-'}`")
        thread = resp.get("ts")

    if status == "SUCCESS":
        await slack_client.post_message(channel, f"배포 성공 #{number}. 바뀐 코드를 다시 읽는 중…", thread_ts=thread)
        try:
            s = await reindex(ctx)
            await slack_client.post_message(
                channel,
                f"코드 갱신 완료 (커밋 `{s['commit']}`): 추가 {s['inserted']} / 변경 {s['updated']} / 삭제 {s['deleted']}, 설명 다시 생성 {s['described']}개",
                thread_ts=thread,
            )
        except Exception as e:
            log.exception("reindex failed")
            await slack_client.post_message(channel, f"코드 갱신 실패: `{str(e)[:200]}`", thread_ts=thread)
        return

    if status in ("FAILURE", "UNSTABLE", "ABORTED"):
        stage = payload.get("stage") or "-"
        await slack_client.post_message(channel, f"배포 실패 #{number} (단계: {stage}). 원인 분석을 시작합니다.", thread_ts=thread)
        try:
            build_log = jenkins.trim_build_log(await jenkins.console_text(payload.get("job"), number))
        except Exception as e:
            build_log = f"(빌드 로그를 가져오지 못함: {e})"
        with store.connect() as conn:
            # 같은 커밋이 같은 단계에서 또 실패했으면 이전 분석을 다시 쓴다
            previous = None
            if payload.get("commit"):
                previous = conn.execute(
                    "SELECT * FROM incident WHERE commit_sha = %s AND api = %s ORDER BY created_at DESC LIMIT 1",
                    (payload["commit"], f"deploy:{stage}"),
                ).fetchone()
            job = jobs.upsert_job(
                conn, "deploy_failure", channel, thread,
                {"report": f"Jenkins 배포 #{number} 실패 (단계: {stage}, 커밋 {commit})", "build_log": build_log,
                 "reported_at_ns": time.time_ns(), "commit": payload.get("commit"), "stage": stage},
            )
        if previous:
            text = (f"같은 커밋(`{commit}`)이 같은 단계에서 이미 실패한 적이 있어 이전 분석을 다시 보여 드립니다.\n"
                    f"*원인*  {previous['cause']}\n*조치*  {previous['resolution'] or '-'}")
            await slack_client.post_message(channel, text, thread_ts=thread, blocks=[slack_client.section(text)])
            return
        from bot.worker.main import analyze_error

        await analyze_error(ctx, job["id"], build_log)
