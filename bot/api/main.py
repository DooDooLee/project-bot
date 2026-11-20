"""FastAPI 서버. n8n이 전달한 요청을 받아 대기열에 넣고 바로 응답한다. 오래 걸리는 일은 워커가 한다."""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from bot import slack_client

from . import slack

log = logging.getLogger("bot.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")

app = FastAPI(title="project-bot")


def _secret() -> str:
    return os.environ.get("SLACK_SIGNING_SECRET", "")


async def _verified_body(request: Request) -> bytes:
    body = await request.body()
    ok = slack.verify(
        _secret(),
        request.headers.get("x-slack-request-timestamp"),
        request.headers.get("x-slack-signature"),
        body,
    )
    if not ok:
        raise HTTPException(status_code=401, detail="invalid slack signature")
    return body


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    # 주소 확인 요청은 받은 값을 그대로 돌려주기만 한다 (앱을 만드는 시점에는 Signing Secret이 아직 없다)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    await _verified_body(request)
    if request.headers.get("x-slack-retry-num"):
        return {"ok": True}  # Slack의 재전송은 이미 받은 것이라 무시
    event = payload.get("event", {})
    log.info("slack event type=%s channel=%s ts=%s", event.get("type"), event.get("channel"), event.get("ts"))
    if event.get("type") == "app_mention" and not event.get("bot_id"):
        background.add_task(_on_mention, event)
    return {"ok": True}


async def _on_mention(event: dict) -> None:
    """멘션을 받으면 스레드에 접수 메시지와 버튼 두 개를 올린다."""
    thread = event.get("thread_ts") or event["ts"]
    await slack_client.post_message(
        event["channel"],
        "접수했습니다. 무엇을 할까요?",
        thread_ts=thread,
        blocks=[
            slack_client.section("접수했습니다. 무엇을 할까요?"),
            slack_client.buttons(("오류 분석", "analyze_error", thread), ("질문·조회", "ask_question", thread)),
        ],
    )


@app.post("/slack/actions")
async def slack_actions(request: Request) -> dict:
    body = await _verified_body(request)
    form = parse_qs(body.decode("utf-8"))
    payload = json.loads(form.get("payload", ["{}"])[0])
    action = (payload.get("actions") or [{}])[0]
    log.info("slack action id=%s value=%s", action.get("action_id"), action.get("value"))
    await request.app.state.enqueue("handle_slack_action", payload) if hasattr(request.app.state, "enqueue") else None
    return {"ok": True}
