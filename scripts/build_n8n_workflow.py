"""n8n 워크플로 파일(n8n/workflows/inboundGateway01.json)을 만든다.

n8n은 봇의 문이다. 화면에는 줄이 두 개 있다.

  받는 줄    [Slack] [Jenkins] [GitHub] → [종류별로 나누기] → [봇 API로 넘기기 ×4] → [받았다고 답함]
  보내는 줄  [봇의 결과] → [Slack에 올리기]

- 받는 줄: 바깥에서 오는 알림을 받아, 쓸모없는 것은 버리고, 종류에 맞는 봇 API 주소로 넘긴다.
- 보내는 줄: 봇이 만든 결과(분석, 답변, 수정, 배포 알림)를 받아 Slack에 올린다.
  결과가 n8n을 지나가므로, 나중에 Notion 기록이나 메일 발송을 상자 하나로 붙일 수 있다.

상자와 선을 손으로 JSON에 적으면 틀리기 쉬워서 코드로 만든다.
사용: python scripts/build_n8n_workflow.py
"""
from __future__ import annotations

import json
from pathlib import Path

WF_ID = "inboundGateway01"
OUT = Path(__file__).resolve().parent.parent / "n8n" / "workflows" / f"{WF_ID}.json"

# n8n에 등록해 두는 자격 정보의 이름 (값은 서버에서 넣는다. README 참고)
SLACK_CREDENTIAL = {"id": "slackBot01", "name": "Slack 봇"}
INTERNAL_CREDENTIAL = {"id": "internalHook01", "name": "봇 내부 요청 확인"}

nodes: list[dict] = []
connections: dict[str, dict] = {}


def node(name: str, kind: str, version: float, x: int, y: int, parameters: dict, **extra) -> str:
    nodes.append({
        "id": f"{WF_ID}-{len(nodes) + 1}", "name": name, "type": f"n8n-nodes-base.{kind}", "typeVersion": version,
        "position": [x, y], "parameters": parameters, **extra,
    })
    return name


def link(src: str, dst: str, output: int = 0) -> None:
    """src 의 output 번째 출구를 dst 에 잇는다."""
    outs = connections.setdefault(src, {"main": []})["main"]
    while len(outs) <= output:
        outs.append([])
    outs[output].append({"node": dst, "type": "main", "index": 0})


def note(title: str, text: str, x: int, y: int, width: int, height: int, color: int) -> None:
    """색 메모지. 화면에서 구역을 나눠 보여 주는 용도이고 동작과는 관계없다."""
    node(f"메모: {title}", "stickyNote", 1, x, y, {"content": f"## {title}\n{text}", "width": width, "height": height, "color": color})


def webhook(name: str, path: str, x: int, y: int, raw: bool = False, **more) -> str:
    # raw=True: 받은 본문을 풀지 않은 원본도 함께 보관한다. Slack 서명은 원본 본문으로만 확인할 수 있다
    parameters = {"httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {"rawBody": True} if raw else {}}
    parameters.update(more.pop("parameters", {}))
    return node(name, "webhook", 2, x, y, parameters, webhookId=f"{WF_ID}-{path.replace('/', '-')}", **more)


def rule(label: str, expression: str) -> dict:
    """나누기 상자의 출구 하나. expression 이 참이면 이 출구로 나간다."""
    return {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
            "conditions": [{
                "id": f"{WF_ID}-rule-{label}", "leftValue": "={{ " + expression + " }}", "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "renameOutput": True,
        "outputKey": label,
    }


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


# ════════════════════════ 받는 줄 ════════════════════════
X_IN, X_SPLIT, X_OUT, X_REPLY = 0, 340, 700, 1060

note("① 받기", "바깥에서 오는 알림을 받는다.", X_IN - 70, -90, 260, 640, 4)
note("② 나누기", "무엇인지 보고 갈 곳을 정한다.\n쓸모없는 것은 '버림'으로 보낸다.", X_SPLIT - 70, -90, 260, 640, 5)
note("③ 봇 API로 넘기기", "종류에 맞는 주소로 넘긴다.", X_OUT - 70, -90, 260, 640, 6)
note("④ 답하기", "보낸 쪽에 받았다고 답한다.", X_REPLY - 70, -90, 260, 640, 7)

slack = webhook("Slack", "slack", X_IN, 60, raw=True)
jenkins = webhook("Jenkins", "jenkins", X_IN, 230)
github = webhook("GitHub", "github", X_IN, 400)

# 출구 순서: 0 봇을 부름, 1 버튼 누름, 2 배포 알림, 3 PR 합쳐짐, 4 버림(위 넷에 해당하지 않는 것 전부)
split = node("종류별로 나누기", "switch", 3.2, X_SPLIT, 230, {
    "mode": "rules",
    "rules": {"values": [
        # Slack이 다시 보낸 요청, 봇 자신이 쓴 글, 멘션이 아닌 이벤트는 여기에 해당하지 않아 버려진다
        rule("봇을 부름", "$('Slack').isExecuted && !$json.headers['x-slack-retry-num'] && ($json.body.event || {}).type === 'app_mention' && !($json.body.event || {}).bot_id"),
        # 본문의 payload 는 JSON 글자다. 공백이 있든 없든 맞도록 공백을 빼고 종류를 본다
        rule("버튼 누름", "$('Slack').isExecuted && String($json.body.payload || '').replace(/\\s/g, '').includes('\"type\":\"block_actions\"')"),
        rule("배포 알림", "$('Jenkins').isExecuted && ['STARTED', 'SUCCESS', 'FAILURE', 'UNSTABLE', 'ABORTED'].includes(String($json.body.status || '').toUpperCase())"),
        rule("PR 합쳐짐", "$('GitHub').isExecuted && $json.body.action === 'closed' && ($json.body.pull_request || {}).merged === true"),
    ]},
    "options": {"fallbackOutput": "extra", "renameFallbackOutput": "버림"},
})
for entrance in (slack, jenkins, github):
    link(entrance, split)

to_mention = forward_slack("봇 API로: 봇을 부름", "/slack/events", X_OUT, -20)
to_click = forward_slack("봇 API로: 버튼 누름", "/slack/actions", X_OUT, 150)
to_deploy = forward_hook("봇 API로: 배포 알림", "/hooks/jenkins", "$json.headers['x-webhook-secret']", X_OUT, 320)
to_merged = forward_hook("봇 API로: PR 합쳐짐", "/hooks/github", "$json.query.k", X_OUT, 490)

# 넘긴 것은 봇 API의 답과 상태 코드를 그대로 돌려준다. 버린 것은 '받았지만 넘기지 않음'으로 답한다.
# Slack 앱을 등록할 때 오는 주소 확인 요청에는 받은 값을 그대로 돌려준다
reply = node("받았다고 답함", "respondToWebhook", 1.1, X_REPLY, 230, {
    "respondWith": "json",
    "responseBody": "={{ JSON.stringify($json.statusCode ? ($json.body || {}) : ($json.error ? { ok: false } : "
                    "((($json.body || {}).type === 'url_verification') ? { challenge: $json.body.challenge || '' } : { ok: true, ignored: true }))) }}",
    "options": {"responseCode": "={{ $json.statusCode || ($json.error ? 502 : 200) }}"},
})
for i, target in enumerate((to_mention, to_click, to_deploy, to_merged)):
    link(split, target, i)
    link(target, reply)
link(split, reply, 4)

# ════════════════════════ 보내는 줄 ════════════════════════
Y_SEND = 760
note("⑤ 봇의 결과 받기", "봇이 만든 결과(분석, 답변, 수정, 배포 알림)가 들어온다.\n서버 안에서만 부를 수 있다.", X_IN - 70, Y_SEND - 110, 260, 300, 4)
note("⑥ 내보내기", "결과를 Slack에 올린다.\nNotion 기록이나 메일 발송은 여기에 상자를 하나 더 달면 된다.", X_SPLIT - 70, Y_SEND - 110, 620, 300, 3)

# 응답은 마지막 상자(Slack)의 결과를 그대로 돌려준다. 봇은 거기서 올라간 글의 ts 를 받는다
result = webhook("봇의 결과", "internal/result", X_IN, Y_SEND, parameters={
    "responseMode": "lastNode", "authentication": "headerAuth",
}, credentials={"httpHeaderAuth": INTERNAL_CREDENTIAL})

post = node("Slack에 올리기", "slack", 2.3, X_SPLIT, Y_SEND, {
    "authentication": "accessToken", "resource": "message", "operation": "post", "select": "channel",
    "channelId": {"__rl": True, "mode": "id", "value": "={{ $json.body.channel }}"},
    "messageType": "block",
    "blocksUi": "={{ JSON.stringify({ blocks: $json.body.blocks }) }}",
    "text": "={{ $json.body.text }}",
    "otherOptions": {
        "includeLinkToWorkflow": False,
        "thread_ts": {"replyValues": {"thread_ts": "={{ $json.body.thread_ts || undefined }}", "reply_broadcast": False}},
    },
}, credentials={"slackApi": SLACK_CREDENTIAL})
link(result, post)

workflow = {
    "id": WF_ID,
    "name": "봇의 문 (받기: Slack·Jenkins·GitHub → 봇 API / 보내기: 봇의 결과 → Slack)",
    "active": False,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "nodes": nodes,
    "connections": connections,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    working = [n for n in nodes if not n["type"].endswith("stickyNote")]
    print(f"{OUT.name}: 상자 {len(working)}개, 메모지 {len(nodes) - len(working)}개")
