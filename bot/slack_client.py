"""Slack으로 메시지를 보낸다. 받는 쪽은 n8n을 거치고, 보내는 쪽은 Slack API를 직접 호출한다."""
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


async def post_message(channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None,
                       attachments: list | None = None) -> dict:
    """attachments 는 왼쪽에 색 띠가 붙는 영역이다. [{"color": "#E24B4A", "blocks": [...]}]"""
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    if attachments:
        payload["attachments"] = attachments
    return await call("chat.postMessage", payload)


async def update_message(channel: str, ts: str, text: str, blocks: list | None = None, attachments: list | None = None) -> dict:
    payload: dict = {"channel": channel, "ts": ts, "text": text, "blocks": blocks or [], "attachments": attachments or []}
    return await call("chat.update", payload)


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
