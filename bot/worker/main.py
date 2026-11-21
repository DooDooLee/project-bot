"""워커. 대기열에서 일을 꺼내 실행한다. 실행: arq bot.worker.main.WorkerSettings"""
from __future__ import annotations

import logging
import re
import time

from bot import llm, queue, slack_client, slack_ui
from bot.analysis import flow
from bot.config import load_project
from bot.db import jobs, store

log = logging.getLogger("bot.worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")

_MENTION = re.compile(r"<@[A-Z0-9]+>")


def _thread(event: dict) -> str:
    return event.get("thread_ts") or event["ts"]


async def on_mention(ctx, event: dict) -> None:
    """멘션을 받으면 스레드에 접수 메시지와 버튼 두 개를 올린다."""
    channel, thread = event["channel"], _thread(event)
    text = _MENTION.sub("", event.get("text", "")).strip()
    if event.get("thread_ts") and event["thread_ts"] != event["ts"]:
        # 스레드 안에서 멘션했으면 첫 메시지(에러를 붙여 넣은 글)도 함께 본다
        replies = await slack_client.call_get("conversations.replies", {"channel": channel, "ts": thread, "limit": 1})
        parent = (replies.get("messages") or [{}])[0].get("text", "")
        text = (parent + "\n" + text).strip()
    with store.connect() as conn:
        job = jobs.upsert_job(conn, "report", channel, thread, {"report": text, "reported_at_ns": time.time_ns(), "user": event.get("user")})
    job_id = str(job["id"])
    await slack_client.post_message(
        channel,
        "접수했습니다. 무엇을 할까요?",
        thread_ts=thread,
        blocks=[
            slack_client.section("접수했습니다. 무엇을 할까요?"),
            slack_client.buttons(("오류 분석", "analyze_error", job_id), ("질문·조회", "ask_question", job_id)),
        ],
    )


async def on_action(ctx, payload: dict) -> None:
    action = (payload.get("actions") or [{}])[0]
    action_id, value = action.get("action_id", ""), action.get("value", "")
    channel = (payload.get("channel") or {}).get("id")
    message = payload.get("message") or {}
    user = (payload.get("user") or {}).get("id")
    if not value.isdigit():
        return
    job_id = int(value)
    if action_id == "show_evidence":
        await show_evidence(ctx, job_id)
        return
    labels = {
        "analyze_error": "오류 분석",
        "ask_question": "질문·조회",
        "fix_with_ai": "AI로 수정",
        "fix_manually": "직접 수정",
        "mark_resolved": "해결됨",
    }
    # 누른 메시지에서 버튼만 떼고, 누가 무엇을 골랐는지 작은 글씨로 남긴다. 본문은 그대로 둔다
    if channel and message.get("ts"):
        kept = [b for b in message.get("blocks", []) if b.get("type") != "actions"]
        kept.append(slack_ui.context(f"<@{user}> 님이 *{labels.get(action_id, action_id)}* 선택"))
        await slack_client.update_message(channel, message["ts"], message.get("text", ""), blocks=kept)
    handler = {
        "analyze_error": analyze_error,
        "ask_question": ask_question,
        "fix_with_ai": fix_with_ai,
        "fix_manually": fix_manually,
        "mark_resolved": mark_resolved,
    }.get(action_id)
    if handler:
        await handler(ctx, job_id)


async def show_evidence(ctx, job_id: int) -> None:
    """[근거 보기]: 근거, 확인한 코드, 서버 로그, 비슷한 과거 장애를 스레드에 올린다."""
    with store.connect() as conn:
        job = jobs.get_job(conn, job_id)
    text = slack_ui.evidence_text(job["state"])
    await slack_client.post_message(job["slack_channel"], text, thread_ts=job["slack_ts"], blocks=[slack_client.section(text)])


async def analyze_error(ctx, job_id: int, build_log: str = "") -> None:
    with store.connect() as conn:
        if not jobs.claim(conn, job_id, ("received", "analyzed", "failed"), "analyzing"):
            return
        job = jobs.get_job(conn, job_id)
    channel, thread = job["slack_channel"], job["slack_ts"]
    progress = slack_ui.Progress(channel, thread, "분석 중")
    usage = llm.Usage()
    started = time.time()
    try:
        actx = flow.Context(project=ctx["project"], redis=ctx["redis"], usage=usage, progress=progress)
        state = await flow.run(actx, job["state"].get("report", ""), job["state"].get("reported_at_ns"), build_log or job["state"].get("build_log", ""))
        result = state["result"]
        elapsed = round(time.time() - started, 1)
        vector = None
        from bot import embedding

        vector = await embedding.embed(f"{result['symptom']} {result['cause']}")
        with store.connect() as conn:
            methods = [c["qualified_name"] for c in state.get("code", [])][:10]
            extracted = dict(state.get("extracted") or {})
            if job["kind"] == "deploy_failure":
                extracted["api"] = f"deploy:{job['state'].get('stage', '-')}"
            incident_id = jobs.create_incident(conn, job, result, extracted, methods, vector, job["state"].get("commit") or "")
            jobs.update_job(
                conn, job_id, "analyzed", result=result, extracted=state.get("extracted"), incident_id=incident_id,
                code=[{k: c[k] for k in ("service", "qualified_name", "name", "file_path", "start_line", "end_line")} for c in state.get("code", [])],
                logs=[{**e, "line": e["line"][:4000]} for e in state.get("logs", [])],
                incidents=state.get("incidents", []),
                usage=usage.as_dict(), elapsed_seconds=elapsed,
            )
        await progress.done(f"분석 완료  ·  {elapsed}초  ·  코드 {len(state.get('code', []))}개 확인")
        text, blocks = slack_ui.report_blocks(result, state, job_id)
        await slack_client.post_message(channel, text, thread_ts=thread, blocks=blocks)
    except Exception as e:
        log.exception("analyze_error failed job=%s", job_id)
        with store.connect() as conn:
            jobs.update_job(conn, job_id, "failed", error=str(e)[:500])
        await slack_client.post_message(
            channel, f"분석 중 오류가 났습니다: `{str(e)[:200]}`", thread_ts=thread,
            blocks=[slack_client.section(f"분석 중 오류가 났습니다: `{str(e)[:200]}`"), slack_client.buttons(("다시 시도", "analyze_error", str(job_id)))],
        )


async def ask_question(ctx, job_id: int) -> None:
    from bot.analysis import qa

    await qa.answer(ctx, job_id)


async def fix_with_ai(ctx, job_id: int) -> None:
    from bot.fix import flow as fix_flow

    await fix_flow.run(ctx, job_id)


async def fix_manually(ctx, job_id: int) -> None:
    """로컬 AI 도구에 그대로 넘길 수 있는 분석 요약을 남긴다."""
    with store.connect() as conn:
        job = jobs.get_job(conn, job_id)
    result = job["state"].get("result") or {}
    code = job["state"].get("code") or []
    where = slack_ui.location(result).replace("`", "")
    summary = (
        "*직접 수정용 요약*  아래를 로컬 AI 도구에 붙여 넣으세요.\n```\n"
        f"증상: {slack_ui.clean(result.get('symptom'))}\n원인: {slack_ui.clean(result.get('cause'))}\n"
        f"위치: {where}\n수정 방향: {slack_ui.clean(result.get('fix'))}\n"
        "관련 코드:\n" + "\n".join(f"- {c['name']} ({c['file_path']}:{c['start_line']})" for c in code[:6]) + "\n```"
    )
    await slack_client.post_message(
        job["slack_channel"], summary, thread_ts=job["slack_ts"],
        blocks=[slack_client.section(summary), slack_client.buttons(("해결됨", "mark_resolved", str(job_id)))],
    )


async def mark_resolved(ctx, job_id: int) -> None:
    with store.connect() as conn:
        job = jobs.get_job(conn, job_id)
        incident_id = job["state"].get("incident_id")
        if incident_id:
            jobs.resolve_incident(conn, incident_id)
        jobs.update_job(conn, job_id, "resolved")
    await slack_client.post_message(job["slack_channel"], "해결로 기록했습니다. 다음에 비슷한 장애가 나면 이 기록을 참고합니다.", thread_ts=job["slack_ts"])


async def on_jenkins(ctx, payload: dict) -> None:
    from bot import deploy

    await deploy.handle(ctx, payload)


async def on_pr_merged(ctx, payload: dict) -> None:
    with store.connect() as conn:
        incident = jobs.incident_by_pr(conn, payload.get("url") or "")
        if incident is None:
            return
        jobs.resolve_incident(conn, incident["id"], pr_url=payload["url"])
    await slack_client.post_message(
        incident["slack_channel"], f"PR이 merge되어 해결로 기록했습니다: {payload['url']}", thread_ts=incident["slack_ts"]
    )


async def startup(ctx) -> None:
    ctx["project"] = load_project()
    log.info("worker ready: project=%s services=%s", ctx["project"].project, [s.name for s in ctx["project"].services])


class WorkerSettings:
    functions = [on_mention, on_action, on_jenkins, on_pr_merged]
    on_startup = startup
    redis_settings = queue.redis_settings()
    max_jobs = 10
    job_timeout = 60 * 30  # AI 수정은 Jenkins 테스트를 기다리느라 오래 걸린다
    max_tries = 1
