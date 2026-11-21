"""Slack으로 메시지를 보낸다.

결과 글(분석, 답변, 수정, 배포 알림)은 n8n의 '봇의 결과' 문을 거쳐 올린다. n8n을 지나가야 나중에
Notion 기록이나 메일 발송을 상자 하나로 붙일 수 있다.
'분석 중…'처럼 몇 초마다 고쳐 쓰다가 지우는 글, 글 고치기, 글 지우기는 Slack API를 직접 부른다.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("bot.slack")
API = "https://slack.com/api"


def _token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "")


async def call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API}/{method}", json=payload, headers={"Authorization": f"Bearer {_token()}"})
    data = r.json()
    if not data.get("ok"):
        log.error("slack %s failed: %s", method, data.get("error"))
    return data


async def call_get(method: str, params: dict) -> dict:
    """조회용 API (conversations.replies 등)는 쿼리 문자열로 값을 받는다."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API}/{method}", params=params, headers={"Authorization": f"Bearer {_token()}"})
    data = r.json()
    if not data.get("ok"):
        log.error("slack %s failed: %s", method, data.get("error"))
    return data


def result_gateway() -> str:
    """n8n의 '봇의 결과' 문 주소. 비어 있으면 n8n 없이 Slack에 직접 올린다."""
    return os.environ.get("N8N_RESULT_URL", "")


async def publish(kind: str, channel: str, text: str, thread_ts: str | None, blocks: list | None) -> dict:
    """결과 글을 n8n으로 보낸다. n8n의 Slack 상자가 올리고, 올라간 글의 ts 를 돌려준다."""
    body = {"kind": kind, "channel": channel, "thread_ts": thread_ts or "", "text": text, "blocks": blocks or [section(text)]}
    headers = {"x-webhook-secret": os.environ.get("WEBHOOK_SECRET", "")}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(result_gateway(), json=body, headers=headers)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    ts = data.get("ts") or (data.get("message") or {}).get("ts") or data.get("message_timestamp")
    if r.status_code != 200 or not ts:
        raise RuntimeError(f"n8n 응답 {r.status_code}: {str(data or r.text)[:200]}")
    return {"ok": True, "ts": ts, "channel": data.get("channel") or channel, "via": "n8n"}


async def post_message(channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None,
                       attachments: list | None = None, kind: str = "notice", transient: bool = False) -> dict:
    """새 글을 올린다.

    kind       글의 종류 (analysis, answer, fix, deploy, notice …). n8n에서 종류별로 다르게 다룰 때 쓴다.
    transient  곧 고쳐 쓰거나 지울 글(진행 표시)이면 True. n8n을 거치지 않고 직접 올린다.
    attachments 왼쪽에 색 띠가 붙는 영역. [{"color": "#E24B4A", "blocks": [...]}]
    """
    if not transient and not attachments and result_gateway():
        try:
            return await publish(kind, channel, text, thread_ts, blocks)
        except Exception as e:  # n8n이 멈춰 있어도 결과는 사람에게 닿아야 한다
            log.warning("n8n으로 올리지 못해 Slack에 직접 올림: %s", e)
    text, attachments = _only_once(text, blocks, attachments)
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    if attachments:
        payload["attachments"] = attachments
    return await call("chat.postMessage", payload)


async def update_message(channel: str, ts: str, text: str, blocks: list | None = None, attachments: list | None = None) -> dict:
    text, attachments = _only_once(text, blocks, attachments)
    payload: dict = {"channel": channel, "ts": ts, "text": text, "blocks": blocks or [], "attachments": attachments or []}
    return await call("chat.update", payload)


def _only_once(text: str, blocks: list | None, attachments: list | None) -> tuple[str, list | None]:
    """색 띠만 있는 메시지는 글이 두 번 보이지 않게 한다.

    Slack은 blocks 가 없으면 text 를 화면에 그리고, 그 아래 attachments 도 그린다.
    그래서 text 는 비우고, 알림에 쓰는 글은 화면에 그려지지 않는 fallback 자리에 넣는다.
    """
    if not attachments or blocks:
        return text, attachments
    first = {**attachments[0], "fallback": text[:300]}
    return "", [first, *attachments[1:]]


async def delete_message(channel: str, ts: str) -> dict:
    return await call("chat.delete", {"channel": channel, "ts": ts})


def buttons(*items: tuple[str, str, str], style_first: bool = True) -> dict:
    """items: (버튼 글자, action_id, value)"""
    elements = []
    for i, (label, action_id, value) in enumerate(items):
        el = {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action_id, "value": value}
        if i == 0 and style_first:
            el["style"] = "primary"
        elements.append(el)
    return {"type": "actions", "elements": elements}


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def header(text: str) -> dict:
    """큰 글씨 한 줄. 메시지에서 가장 먼저 읽히는 결론에 쓴다."""
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def fields(*pairs: tuple[str, str]) -> dict:
    """이름과 값을 두 칸씩 나란히 보여 준다. pairs: (이름, 값)"""
    return {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"[:1900]} for k, v in pairs[:10]]}


def divider() -> dict:
    return {"type": "divider"}
