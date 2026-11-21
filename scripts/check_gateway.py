"""n8n 알림 관문이 제대로 받고, 거르고, 넘기는지 확인한다. 봇 서버의 api 컨테이너 안에서 돌린다.

사용: docker compose exec -T api python - < scripts/check_gateway.py
      (다른 주소를 시험하려면 GATEWAY_URL 환경 변수로 지정. 기본은 서버 안의 n8n)

Slack에 글이 올라가지 않는 요청만 보낸다 (없는 채널, 숫자가 아닌 버튼 값, 틀린 비밀 값).
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("GATEWAY_URL", "http://n8n:5678/webhook")
# Slack 앱에 등록된 주소는 두 개지만 n8n의 Slack 상자는 하나다. 바깥 주소로 시험할 때는 Caddy가 둘을 하나로 모아 준다
OUTSIDE = "GATEWAY_URL" in os.environ
EVENTS = "/slack/events" if OUTSIDE else "/slack"
ACTIONS = "/slack/actions" if OUTSIDE else "/slack"
SIGNING = os.environ.get("SLACK_SIGNING_SECRET", "")
HOOK = os.environ.get("WEBHOOK_SECRET", "")
failed = 0


def send(path: str, body: bytes, headers: dict) -> tuple[int, dict | str]:
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")[:200]


def slack_headers(body: bytes, content_type: str, secret: str | None = None, **extra) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new((secret or SIGNING).encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"content-type": content_type, "x-slack-request-timestamp": ts, "x-slack-signature": sig, **extra}


def check(name: str, got: tuple, status: int, expect: dict) -> None:
    global failed
    ok = got[0] == status and isinstance(got[1], dict) and all(got[1].get(k) == v for k, v in expect.items())
    failed += 0 if ok else 1
    print(("통과" if ok else "실패"), "|", name, "|", got[0], json.dumps(got[1], ensure_ascii=False)[:120])


JSON = "application/json"
FORM = "application/x-www-form-urlencoded"

# ── Slack: 봇을 부름 ──
body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
check("주소 확인 요청에는 n8n이 바로 답한다", send(EVENTS,body, {"content-type": JSON}), 200, {"challenge": "abc123"})

mention = {"type": "event_callback", "event": {"type": "app_mention", "channel": "C0000000000", "ts": "1.0", "user": "U0", "text": "<@U0BOT>"}}
# 한글과 특수 문자가 섞여도 원본 그대로 넘어가야 서명이 맞는다
mention["event"]["text"] = "<@U0BOT>"
body = json.dumps(mention, ensure_ascii=False).encode()
check("봇을 부르면 서명이 유지된 채 봇 API까지 간다", send(EVENTS,body, slack_headers(body, JSON)), 200, {"ok": True, "ignored": None})
check("서명이 틀리면 봇 API가 거절하고 그 답이 그대로 돌아온다", send(EVENTS,body, slack_headers(body, JSON, secret="wrong")), 401, {"detail": "invalid slack signature"})
check("Slack이 다시 보낸 요청은 n8n이 버린다", send(EVENTS,body, slack_headers(body, JSON, **{"x-slack-retry-num": "1"})), 200, {"ignored": True})

own = json.loads(json.dumps(mention))
own["event"]["bot_id"] = "B1"
body = json.dumps(own).encode()
check("봇 자신이 쓴 글은 n8n이 버린다", send(EVENTS,body, slack_headers(body, JSON)), 200, {"ignored": True})

other = {"type": "event_callback", "event": {"type": "reaction_added", "user": "U0"}}
body = json.dumps(other).encode()
check("멘션이 아닌 이벤트는 n8n이 버린다", send(EVENTS,body, slack_headers(body, JSON)), 200, {"ignored": True})

# ── Slack: 버튼을 누름 ── (값이 숫자가 아니면 워커가 아무것도 하지 않는다)
payload = {"type": "block_actions", "user": {"id": "U0"}, "channel": {"id": "C0000000000"},
           "message": {"ts": "1.0", "text": "한글 + 기호 & = % / 포함", "blocks": []},
           "actions": [{"action_id": "show_evidence", "value": "시험", "type": "button"}]}
body = urllib.parse.urlencode({"payload": json.dumps(payload, ensure_ascii=False)}).encode()
check("버튼 누름은 서명이 유지된 채 봇 API까지 간다", send(ACTIONS,body, slack_headers(body, FORM)), 200, {"ok": True, "ignored": None})
# Slack이 실제로 보내는 모양(공백 없는 JSON)도 확인한다
compact = urllib.parse.urlencode({"payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}).encode()
check("공백 없는 JSON으로 온 버튼 누름도 봇 API까지 간다", send(ACTIONS,compact, slack_headers(compact, FORM)), 200, {"ok": True, "ignored": None})
check("버튼 요청도 서명이 틀리면 거절된다", send(ACTIONS,body, slack_headers(body, FORM, secret="wrong")), 401, {"detail": "invalid slack signature"})

view = dict(payload, type="view_submission")
body = urllib.parse.urlencode({"payload": json.dumps(view)}).encode()
check("버튼 누름이 아닌 상호작용은 n8n이 버린다", send(ACTIONS,body, slack_headers(body, FORM)), 200, {"ignored": True})

# ── Jenkins: 배포 알림 ──
body = json.dumps({"status": "PING", "build_number": 0}).encode()
check("다루지 않는 배포 상태는 n8n이 버린다", send("/jenkins", body, {"content-type": JSON, "x-webhook-secret": HOOK}), 200, {"ignored": True})
body = json.dumps({"status": "started", "build_number": 0}).encode()
check("배포 알림은 봇 API까지 가고, 비밀 값이 틀리면 거절된다", send("/jenkins", body, {"content-type": JSON, "x-webhook-secret": "wrong"}), 401, {"detail": "invalid webhook secret"})

# ── GitHub: PR 알림 ──
body = json.dumps({"action": "opened", "pull_request": {"merged": False}}).encode()
check("merge가 아닌 PR 알림은 n8n이 버린다", send("/github?k=" + urllib.parse.quote(HOOK), body, {"content-type": JSON}), 200, {"ignored": True})
body = json.dumps({"action": "closed", "pull_request": {"merged": True, "html_url": "https://example.com/pr/0", "head": {"ref": "x"}}}).encode()
check("merge된 PR 알림은 봇 API까지 가고, 비밀 값이 틀리면 거절된다", send("/github?k=wrong", body, {"content-type": JSON}), 401, {"detail": "invalid webhook secret"})

print("모두 통과" if not failed else f"실패 {failed}건")
sys.exit(1 if failed else 0)
