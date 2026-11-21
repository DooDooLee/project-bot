"""사람 없이 Slack 흐름을 시험한다. Slack이 보내는 것과 같은 모양의 요청에 서명을 붙여 봇에게 보낸다.

사용:
  python scripts/simulate_slack.py mention "에러 내용..."        → 채널에 글을 올리고 봇을 멘션한 것처럼 처리. 스레드 ts를 출력
  python scripts/simulate_slack.py reply <스레드 ts> "이어 묻는 말"  → 그 스레드 안에서 봇을 다시 부른 것처럼 처리
  python scripts/simulate_slack.py click <스레드 ts> <action_id>  → 그 스레드의 버튼을 누른 것처럼 처리
  python scripts/simulate_slack.py show <스레드 ts>               → 스레드의 메시지를 출력

.env 에서 SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, BOT_HOST, SLACK_CHANNEL, SLACK_TEST_USER 를 읽는다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def load_env() -> dict:
    env = dict(os.environ)
    path = Path(__file__).resolve().parent.parent / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


ENV = load_env()
CHANNEL = ENV.get("SLACK_CHANNEL", "")
USER = ENV.get("SLACK_TEST_USER", "U000TEST")
BASE = f"https://{ENV.get('BOT_HOST', '')}/webhook"


def slack(method: str, payload: dict | None = None, query: dict | None = None) -> dict:
    url = f"https://slack.com/api/{method}" + ("?" + urllib.parse.urlencode(query) if query else "")
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Authorization": "Bearer " + ENV["SLACK_BOT_TOKEN"], "Content-Type": "application/json; charset=utf-8"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def send_signed(path: str, body: bytes, content_type: str) -> str:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(ENV["SLACK_SIGNING_SECRET"].encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(BASE + path, data=body, headers={"content-type": content_type, "x-slack-request-timestamp": ts, "x-slack-signature": sig, "user-agent": "slack-sim/1"})
    return urllib.request.urlopen(req, timeout=20).read().decode()


def mention(text: str) -> None:
    posted = slack("chat.postMessage", {"channel": CHANNEL, "text": f"[시험] {text}"})
    ts = posted["ts"]
    event = {"type": "event_callback", "event": {"type": "app_mention", "channel": CHANNEL, "ts": ts, "user": USER, "text": f"<@BOT> {text}"}}
    print("webhook:", send_signed("/slack/events", json.dumps(event).encode(), "application/json"))
    print("thread_ts:", ts)


def reply(thread_ts: str, text: str) -> None:
    """스레드 안에서 봇을 다시 부른 것처럼 처리한다 (이어서 묻기)."""
    posted = slack("chat.postMessage", {"channel": CHANNEL, "thread_ts": thread_ts, "text": f"[시험] {text}"})
    event = {"type": "event_callback", "event": {"type": "app_mention", "channel": CHANNEL, "ts": posted["ts"], "thread_ts": thread_ts,
                                                 "user": USER, "text": f"<@BOT> {text}"}}
    print("webhook:", send_signed("/slack/events", json.dumps(event).encode(), "application/json"))


def replies(thread_ts: str) -> list[dict]:
    return slack("conversations.replies", query={"channel": CHANNEL, "ts": thread_ts, "limit": 50}).get("messages", [])


def _blocks(msg: dict) -> list:
    """메시지의 블록 전부. 색 띠(attachments) 안의 블록도 포함한다."""
    inner = [b for a in msg.get("attachments") or [] for b in a.get("blocks") or []]
    return list(msg.get("blocks") or []) + inner


def click(thread_ts: str, action_id: str) -> None:
    for msg in reversed(replies(thread_ts)):
        for block in _blocks(msg):
            if block.get("type") != "actions":
                continue
            for el in block["elements"]:
                if el.get("action_id") == action_id:
                    payload = {"type": "block_actions", "user": {"id": USER}, "channel": {"id": CHANNEL},
                               "message": {"ts": msg["ts"], "text": msg.get("text", ""), "blocks": msg.get("blocks") or [],
                                           "attachments": msg.get("attachments") or []},
                               "actions": [{"action_id": action_id, "value": el["value"], "type": "button"}]}
                    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
                    print("webhook:", send_signed("/slack/actions", body, "application/x-www-form-urlencoded"))
                    return
    print(f"버튼을 찾지 못함: {action_id}")


def show(thread_ts: str) -> None:
    for msg in replies(thread_ts):
        print("─" * 60)
        blocks = _blocks(msg)
        if not blocks:
            print(msg.get("text", ""))
        for b in blocks:
            kind = b.get("type")
            if kind == "header":
                print("[큰 제목]", b["text"]["text"])
            elif kind == "section" and b.get("fields"):
                print("[표]", " | ".join(f["text"].replace(chr(10), " = ", 1).replace(chr(10), " ") for f in b["fields"]))
            elif kind == "section":
                print(b["text"]["text"])
            elif kind == "divider":
                print("- - -")
            elif kind == "context":
                print("[작은 글씨]", " ".join(e.get("text", "") for e in b["elements"]))
            elif kind == "actions":
                print("[버튼]", " | ".join(e["text"]["text"] for e in b["elements"]))
        colors = [a.get("color") for a in msg.get("attachments") or [] if a.get("color")]
        if colors:
            print("[띠 색]", ", ".join(colors))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "mention":
        mention(sys.argv[2])
    elif cmd == "reply":
        reply(sys.argv[2], sys.argv[3])
    elif cmd == "click":
        click(sys.argv[2], sys.argv[3])
    elif cmd == "show":
        show(sys.argv[2])
    else:
        print(__doc__)
