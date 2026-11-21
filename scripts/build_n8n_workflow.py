"""n8n 워크플로 파일(n8n/workflows/inboundGateway01.json)을 만든다.

바깥에서 들어오는 알림(Slack, Jenkins, GitHub)을 n8n이 한 곳에서 받아, 쓸모없는 것은 버리고,
종류에 맞는 봇 API 주소로 넘기는 워크플로다. 상자와 선을 손으로 JSON에 적으면 틀리기 쉬워서 코드로 만든다.

사용: python scripts/build_n8n_workflow.py
"""
from __future__ import annotations

import json
from pathlib import Path

WF_ID = "inboundGateway01"
OUT = Path(__file__).resolve().parent.parent / "n8n" / "workflows" / f"{WF_ID}.json"

nodes: list[dict] = []
connections: dict[str, dict] = {}


def node(name: str, kind: str, version: float, x: int, y: int, parameters: dict, **extra) -> str:
    nodes.append({
        "id": f"{WF_ID}-{len(nodes) + 1}", "name": name, "type": f"n8n-nodes-base.{kind}", "typeVersion": version,
        "position": [x, y], "parameters": parameters, **extra,
    })
    return name


def link(src: str, dst: str, output: int = 0) -> None:
    """src 의 output 번째 출구를 dst 에 잇는다. IF 상자는 0 = 참, 1 = 거짓."""
    outs = connections.setdefault(src, {"main": []})["main"]
    while len(outs) <= output:
        outs.append([])
    outs[output].append({"node": dst, "type": "main", "index": 0})


def webhook(name: str, path: str, y: int, raw: bool) -> str:
    # raw=True: 받은 본문을 풀지 않은 원본도 함께 보관한다. Slack 서명은 원본 본문으로만 확인할 수 있다
    return node(name, "webhook", 2, 0, y, {
        "httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {"rawBody": True} if raw else {},
    }, webhookId=f"{WF_ID}-{path.replace('/', '-')}")


def check(name: str, expression: str, x: int, y: int) -> str:
    """조건 상자. expression 이 참이면 위쪽 출구, 거짓이면 아래쪽 출구로 나간다."""
    return node(name, "if", 2.2, x, y, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "conditions": [{
                "id": f"{WF_ID}-cond-{len(nodes) + 1}", "leftValue": "={{ " + expression + " }}", "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {},
    })


RESPONSE = {"response": {"response": {"fullResponse": True, "neverError": True}}}


def forward_slack(name: str, api_path: str, x: int, y: int) -> str:
    """Slack 요청을 원본 그대로 넘긴다. 서명 확인은 봇 API가 한다."""
    return node(name, "httpRequest", 4.2, x, y, {
        "method": "POST", "url": "={{ $env.BOT_API_URL }}" + api_path,
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "x-slack-signature", "value": "={{ $json.headers['x-slack-signature'] }}"},
            {"name": "x-slack-request-timestamp", "value": "={{ $json.headers['x-slack-request-timestamp'] }}"},
            # 내용 종류를 '폼'으로 적어 보내면 HTTP Request 상자가 원본을 폼으로 한 번 더 포장해 글자가 바뀐다
            # (버튼 요청의 서명이 깨지던 원인). '가공하지 않은 데이터'로 적어서 원본 그대로 나가게 한다
            {"name": "content-type", "value": "application/octet-stream"},
        ]},
        "sendBody": True, "contentType": "binaryData", "inputDataFieldName": "data",
        "options": {"timeout": 2500, **RESPONSE},
    }, onError="continueRegularOutput")


def forward_hook(name: str, api_path: str, secret: str, x: int, y: int) -> str:
    """Jenkins, GitHub 알림을 넘긴다. 미리 정한 값(x-webhook-secret)으로 봇 API가 확인한다."""
    return node(name, "httpRequest", 4.2, x, y, {
        "method": "POST", "url": "={{ $env.BOT_API_URL }}" + api_path,
        "sendHeaders": True, "headerParameters": {"parameters": [{"name": "x-webhook-secret", "value": "={{ " + secret + " }}"}]},
        "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.body) }}",
        "options": {"timeout": 5000, **RESPONSE},
    }, onError="continueRegularOutput")


def respond(name: str, body: str, x: int, y: int, code: str | None = None) -> str:
    options = {"responseCode": "={{ " + code + " }}"} if code else {}
    return node(name, "respondToWebhook", 1.1, x, y, {"respondWith": "json", "responseBody": "={{ " + body + " }}", "options": options})


# ── 응답 상자 (여러 줄기가 함께 쓴다) ─────────────────────────
ANSWER = respond("응답: 봇 API의 답 그대로", "JSON.stringify($json.body || { ok: false })", 1080, 300, "$json.statusCode || 502")
IGNORED = respond("응답: 받았지만 넘기지 않음", "JSON.stringify({ ok: true, ignored: true })", 1080, 620)

# ── 1. Slack: 봇을 부름 ──────────────────────────────────
w = webhook("Slack: 봇을 부름", "slack/events", 0, raw=True)
is_check = check("주소 확인 요청인가", "$json.body.type === 'url_verification'", 260, 0)
challenge = respond("응답: 주소 확인", "JSON.stringify({ challenge: $json.body.challenge || '' })", 520, -120)
# Slack이 다시 보낸 요청, 봇 자신이 쓴 글, 멘션이 아닌 이벤트는 넘기지 않는다
is_mention = check(
    "처리할 부름인가",
    "!$json.headers['x-slack-retry-num'] && ($json.body.event || {}).type === 'app_mention' && !($json.body.event || {}).bot_id",
    520, 60,
)
to_events = forward_slack("봇 API로: 부름", "/slack/events", 800, 0)
link(w, is_check)
link(is_check, challenge, 0)
link(is_check, is_mention, 1)
link(is_mention, to_events, 0)
link(is_mention, IGNORED, 1)
link(to_events, ANSWER)

# ── 2. Slack: 버튼을 누름 ─────────────────────────────────
w = webhook("Slack: 버튼을 누름", "slack/actions", 220, raw=True)
# 본문의 payload 는 JSON 글자다. 공백이 있든 없든 맞도록 공백을 빼고 종류를 본다
is_click = check("버튼 누름인가", "String($json.body.payload || '').replace(/\\s/g, '').includes('\"type\":\"block_actions\"')", 260, 220)
to_actions = forward_slack("봇 API로: 버튼", "/slack/actions", 800, 220)
link(w, is_click)
link(is_click, to_actions, 0)
link(is_click, IGNORED, 1)
link(to_actions, ANSWER)

# ── 3. Jenkins: 배포 알림 ────────────────────────────────
w = webhook("Jenkins: 배포 알림", "jenkins", 440, raw=False)
is_deploy = check(
    "다루는 배포 상태인가",
    "['STARTED', 'SUCCESS', 'FAILURE', 'UNSTABLE', 'ABORTED'].includes(String($json.body.status || '').toUpperCase())",
    260, 440,
)
to_jenkins = forward_hook("봇 API로: 배포", "/hooks/jenkins", "$json.headers['x-webhook-secret']", 800, 440)
link(w, is_deploy)
link(is_deploy, to_jenkins, 0)
link(is_deploy, IGNORED, 1)
link(to_jenkins, ANSWER)

# ── 4. GitHub: PR 알림 ──────────────────────────────────
w = webhook("GitHub: PR 알림", "github", 660, raw=False)
is_merged = check("merge된 PR인가", "$json.body.action === 'closed' && ($json.body.pull_request || {}).merged === true", 260, 660)
to_github = forward_hook("봇 API로: PR merge", "/hooks/github", "$json.query.k", 800, 660)
link(w, is_merged)
link(is_merged, to_github, 0)
link(is_merged, IGNORED, 1)
link(to_github, ANSWER)

workflow = {
    "id": WF_ID,
    "name": "알림 관문 (Slack, Jenkins, GitHub → 봇 API)",
    "active": False,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "nodes": nodes,
    "connections": connections,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{OUT.name}: 상자 {len(nodes)}개")
