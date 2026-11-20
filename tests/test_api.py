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


def test_event_requires_valid_signature(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    sent = []

    async def fake_post(channel, text, thread_ts=None, blocks=None):
        sent.append((channel, thread_ts, blocks))
        return {"ok": True}

    monkeypatch.setattr("bot.slack_client.post_message", fake_post)
    body = json.dumps({"type": "event_callback", "event": {"type": "app_mention", "channel": "C1", "ts": "1.2"}}).encode()
    assert client.post("/slack/events", content=body).status_code == 401
    assert client.post("/slack/events", content=body, headers=_headers(body, secret="wrong")).status_code == 401
    assert sent == []  # 검증에 실패한 요청으로는 아무것도 하지 않는다
    assert client.post("/slack/events", content=body, headers=_headers(body)).status_code == 200
    # 멘션을 받으면 그 스레드에 버튼 두 개를 올린다
    assert len(sent) == 1 and sent[0][0] == "C1" and sent[0][1] == "1.2"
    assert [e["action_id"] for e in sent[0][2][1]["elements"]] == ["analyze_error", "ask_question"]
    # Slack이 같은 이벤트를 다시 보내면 무시한다
    retry = _headers(body) | {"x-slack-retry-num": "1"}
    assert client.post("/slack/events", content=body, headers=retry).status_code == 200
    assert len(sent) == 1


def test_action_parses_form_payload(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    from urllib.parse import urlencode

    payload = {"type": "block_actions", "actions": [{"action_id": "analyze_error", "value": "1"}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    headers = _headers(body) | {"content-type": "application/x-www-form-urlencoded"}
    assert client.post("/slack/actions", content=body, headers=headers).status_code == 200
    assert client.post("/slack/actions", content=body).status_code == 401
