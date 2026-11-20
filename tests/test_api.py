"""API 서버 테스트: Slack 서명 검증과 주소 확인 응답."""
import json
import time

from fastapi.testclient import TestClient

from bot.api import slack
from bot.api.main import app

SECRET = "test-signing-secret"
client = TestClient(app)


def _headers(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict:
    ts = str(ts or int(time.time()))
    return {"x-slack-request-timestamp": ts, "x-slack-signature": slack.sign(secret, ts, body), "content-type": "application/json"}


def test_sign_matches_slack_documented_example():
    # Slack 문서의 예시 값
    secret = "8f742231b10e8888abcd99yyyzzz85a5"
    ts = "1531420618"
    body = b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
    assert slack.sign(secret, ts, body) == "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


def test_verify_rejects_old_wrong_or_missing():
    body = b"{}"
    now = 1_700_000_000
    good = slack.sign(SECRET, str(now), body)
    assert slack.verify(SECRET, str(now), good, body, now=now)
    assert not slack.verify(SECRET, str(now), good, b'{"x":1}', now=now)  # 본문이 바뀜
    assert not slack.verify(SECRET, str(now), good, body, now=now + 301)  # 5분 지남
    assert not slack.verify("other", str(now), good, body, now=now)  # 다른 비밀값
    assert not slack.verify("", str(now), good, body, now=now)  # 비밀값 미설정
    assert not slack.verify(SECRET, None, None, body, now=now)


def test_url_verification_echoes_challenge():
    r = client.post("/slack/events", content=json.dumps({"type": "url_verification", "challenge": "abc123"}))
    assert r.status_code == 200
    assert r.json() == {"challenge": "abc123"}


class FakeQueue:
    """대기열 대신 넣은 일을 기록만 한다."""

    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, args))


def test_event_requires_valid_signature(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    app.state.queue = q = FakeQueue()
    body = json.dumps({"type": "event_callback", "event": {"type": "app_mention", "channel": "C1", "ts": "1.2"}}).encode()
    assert client.post("/slack/events", content=body).status_code == 401
    assert client.post("/slack/events", content=body, headers=_headers(body, secret="wrong")).status_code == 401
    assert q.jobs == []  # 검증에 실패한 요청으로는 아무것도 하지 않는다
    assert client.post("/slack/events", content=body, headers=_headers(body)).status_code == 200
    # 멘션을 받으면 대기열에 넣고 바로 응답한다 (실제 처리는 워커가 한다)
    assert [name for name, _ in q.jobs] == ["on_mention"]
    assert q.jobs[0][1][0]["channel"] == "C1"
    # Slack이 같은 이벤트를 다시 보내면 무시한다
    retry = _headers(body) | {"x-slack-retry-num": "1"}
    assert client.post("/slack/events", content=body, headers=retry).status_code == 200
    assert len(q.jobs) == 1
    # 봇 자신이 쓴 메시지의 멘션은 처리하지 않는다
    own = json.dumps({"type": "event_callback", "event": {"type": "app_mention", "channel": "C1", "ts": "1.3", "bot_id": "B1"}}).encode()
    assert client.post("/slack/events", content=own, headers=_headers(own)).status_code == 200
    assert len(q.jobs) == 1


def test_action_parses_form_payload(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    app.state.queue = q = FakeQueue()
    from urllib.parse import urlencode

    payload = {"type": "block_actions", "actions": [{"action_id": "analyze_error", "value": "1"}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    headers = _headers(body) | {"content-type": "application/x-www-form-urlencoded"}
    assert client.post("/slack/actions", content=body).status_code == 401
    assert client.post("/slack/actions", content=body, headers=headers).status_code == 200
    assert q.jobs == [("on_action", (payload,))]


def test_queue_down_returns_503(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    app.state.queue = None
    body = json.dumps({"type": "event_callback", "event": {"type": "app_mention", "channel": "C1", "ts": "9.9"}}).encode()
    assert client.post("/slack/events", content=body, headers=_headers(body)).status_code == 503


def test_internal_hooks_require_secret(monkeypatch):
    app.state.queue = q = FakeQueue()
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    assert client.post("/hooks/jenkins", json={"status": "FAILURE"}).status_code == 401
    assert client.post("/hooks/jenkins", json={"status": "FAILURE"}, headers={"x-webhook-secret": "wrong"}).status_code == 401
    assert client.post("/hooks/jenkins", json={"status": "FAILURE", "build_number": 7}, headers={"x-webhook-secret": "s3cret"}).status_code == 200
    assert q.jobs == [("on_jenkins", ({"status": "FAILURE", "build_number": 7},))]
    # merge된 PR만 처리한다
    hdr = {"x-webhook-secret": "s3cret"}
    client.post("/hooks/github", json={"action": "closed", "pull_request": {"merged": False, "html_url": "u"}}, headers=hdr)
    client.post("/hooks/github", json={"action": "closed", "pull_request": {"merged": True, "html_url": "u", "head": {"ref": "bot/fix-1"}}}, headers=hdr)
    assert q.jobs[-1] == ("on_pr_merged", ({"url": "u", "branch": "bot/fix-1"},)) and len(q.jobs) == 2
