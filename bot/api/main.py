"""FastAPI 서버. n8n이 전달한 요청을 받아 대기열에 넣고 바로 응답한다. 오래 걸리는 일은 워커가 한다."""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request

from bot import queue

from . import slack

log = logging.getLogger("bot.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.queue = None
    try:
        app.state.queue = await queue.connect()
    except Exception as e:  # Redis가 없어도 서버는 뜬다 (주소 확인 요청에는 답해야 하므로)
        log.error("대기열에 연결하지 못함: %s", e)
    yield
    if app.state.queue is not None:
        await app.state.queue.aclose()


app = FastAPI(title="project-bot", lifespan=lifespan)


async def enqueue(request: Request, job: str, *args) -> None:
    q = getattr(request.app.state, "queue", None)
    if q is None:
        raise HTTPException(status_code=503, detail="queue unavailable")
    await q.enqueue_job(job, *args)


async def verified_body(request: Request) -> bytes:
    body = await request.body()
    ok = slack.verify(
        os.environ.get("SLACK_SIGNING_SECRET", ""),
        request.headers.get("x-slack-request-timestamp"),
        request.headers.get("x-slack-signature"),
        body,
    )
    if not ok:
        log.warning("slack 서명 불일치: path=%s len=%d", request.url.path, len(body))
        raise HTTPException(status_code=401, detail="invalid slack signature")
    return body


def check_internal(request: Request) -> None:
    """Jenkins, GitHub 알림처럼 Slack 서명이 없는 요청은 미리 정한 값으로 확인한다."""
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if not expected or request.headers.get("x-webhook-secret") != expected:
        raise HTTPException(status_code=401, detail="invalid webhook secret")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    # 주소 확인 요청은 받은 값을 그대로 돌려주기만 한다 (앱을 만드는 시점에는 Signing Secret이 아직 없다)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    await verified_body(request)
    if request.headers.get("x-slack-retry-num"):
        return {"ok": True}  # Slack의 재전송은 이미 받은 것이라 무시
    event = payload.get("event", {})
    log.info("slack event type=%s channel=%s ts=%s", event.get("type"), event.get("channel"), event.get("ts"))
    if event.get("type") == "app_mention" and not event.get("bot_id"):
        await enqueue(request, "on_mention", event)
    return {"ok": True}


@app.post("/slack/actions")
async def slack_actions(request: Request) -> dict:
    body = await verified_body(request)
    form = parse_qs(body.decode("utf-8"))
    payload = json.loads(form.get("payload", ["{}"])[0])
    action = (payload.get("actions") or [{}])[0]
    log.info("slack action type=%s id=%s", payload.get("type"), action.get("action_id"))
    await enqueue(request, "on_action", payload)
    return {"ok": True}


@app.post("/hooks/jenkins")
async def jenkins_hook(request: Request) -> dict:
    """배포 시작, 성공, 실패 알림."""
    check_internal(request)
    payload = await request.json()
    log.info("jenkins %s build=%s", payload.get("status"), payload.get("build_number"))
    await enqueue(request, "on_jenkins", payload)
    return {"ok": True}


@app.post("/hooks/github")
async def github_hook(request: Request) -> dict:
    """PR이 merge되면 그 장애의 처리 기록을 해결로 바꾼다."""
    check_internal(request)
    payload = await request.json()
    pr = payload.get("pull_request") or {}
    if payload.get("action") == "closed" and pr.get("merged"):
        await enqueue(request, "on_pr_merged", {"url": pr.get("html_url"), "branch": (pr.get("head") or {}).get("ref")})
    return {"ok": True}
