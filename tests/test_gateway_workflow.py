"""n8n 워크플로 파일이 의도한 모양인지, 봇이 결과 글을 n8n으로 보내는지 확인한다.

(n8n이 실제로 받고 넘기는지는 scripts/check_gateway.py 로 서버에서 확인한다)
"""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from bot import slack_client

ROOT = Path(__file__).resolve().parent.parent


def _builder():
    spec = importlib.util.spec_from_file_location("build_n8n_workflow", ROOT / "scripts" / "build_n8n_workflow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nodes(workflow, kind):
    return [n for n in workflow["nodes"] if n["type"] == f"n8n-nodes-base.{kind}"]


def _by_name(workflow):
    return {n["name"]: n for n in workflow["nodes"]}


def _targets(workflow, name):
    return [[c["node"] for c in out] for out in workflow["connections"].get(name, {"main": []})["main"]]


def test_workflow_file_matches_builder():
    """저장소의 워크플로 파일은 만드는 코드의 결과와 같아야 한다 (파일만 손으로 고치지 않도록)."""
    built = _builder().workflow
    saved = json.loads((ROOT / "n8n" / "workflows" / "inboundGateway01.json").read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(built))


def test_receiving_row_has_three_entrances_one_split_four_forwards_one_reply():
    workflow = _builder().workflow
    names = _by_name(workflow)
    inbound = {n["parameters"]["path"]: n for n in _nodes(workflow, "webhook") if not n["parameters"]["path"].startswith("internal/")}
    assert {n["name"] for n in inbound.values()} == {"Slack", "Jenkins", "GitHub"}
    # Slack 서명은 원본 본문으로만 확인할 수 있어서 Slack 쪽만 원본을 보관한다
    assert [p for p, n in inbound.items() if n["parameters"]["options"].get("rawBody")] == ["slack"]
    # 받는 상자는 모두 나누기 상자 하나로 모인다
    assert all(_targets(workflow, n["name"]) == [["종류별로 나누기"]] for n in inbound.values())
    assert len(_nodes(workflow, "switch")) == 1 and not _nodes(workflow, "if")

    split = names["종류별로 나누기"]
    outputs = [r["outputKey"] for r in split["parameters"]["rules"]["values"]]
    assert outputs == ["봇을 부름", "버튼 누름", "배포 알림", "PR 합쳐짐"]
    assert split["parameters"]["options"] == {"fallbackOutput": "extra", "renameFallbackOutput": "버림"}
    # 출구 순서대로 넘기는 상자가 하나씩 붙고, 버림은 바로 답하기로 간다
    assert _targets(workflow, "종류별로 나누기") == [
        ["봇 API로: 봇을 부름"], ["봇 API로: 버튼 누름"], ["봇 API로: 배포 알림"], ["봇 API로: PR 합쳐짐"], ["받았다고 답함"],
    ]
    forwards = [n for n in _nodes(workflow, "httpRequest")]
    assert len(forwards) == 4 and all(_targets(workflow, n["name"]) == [["받았다고 답함"]] for n in forwards)
    assert [n["name"] for n in _nodes(workflow, "respondToWebhook")] == ["받았다고 답함"]


def test_slack_body_is_forwarded_untouched():
    """내용 종류를 '폼'으로 적어 보내면 n8n이 원본을 다시 포장해 서명이 깨진다. 가공하지 않은 데이터로 보내야 한다."""
    workflow = _builder().workflow
    slack = [n for n in _nodes(workflow, "httpRequest") if "/slack/" in n["parameters"]["url"]]
    assert sorted(n["parameters"]["url"].split("}}")[1] for n in slack) == ["/slack/actions", "/slack/events"]
    for n in slack:
        p = n["parameters"]
        assert p["contentType"] == "binaryData" and p["inputDataFieldName"] == "data"
        headers = {h["name"]: h["value"] for h in p["headerParameters"]["parameters"]}
        assert headers["content-type"] == "application/octet-stream"
        assert "x-slack-signature" in headers and "x-slack-request-timestamp" in headers
        assert p["options"]["timeout"] <= 2500  # Slack은 3초 안에 답을 받아야 한다


def test_sending_row_posts_results_with_the_slack_node():
    workflow = _builder().workflow
    names = _by_name(workflow)
    entrance = names["봇의 결과"]
    assert entrance["parameters"]["path"] == "internal/result"
    # 봇이 보낸 요청만 받는다 (미리 정한 값을 헤더로 확인). 답은 Slack 상자의 결과를 그대로 돌려준다
    assert entrance["parameters"]["authentication"] == "headerAuth" and "httpHeaderAuth" in entrance["credentials"]
    assert entrance["parameters"]["responseMode"] == "lastNode"
    assert _targets(workflow, "봇의 결과") == [["Slack에 올리기"]]
    post = names["Slack에 올리기"]
    assert post["type"] == "n8n-nodes-base.slack" and "slackApi" in post["credentials"]
    p = post["parameters"]
    assert (p["resource"], p["operation"], p["messageType"]) == ("message", "post", "block")
    assert "$json.body.blocks" in p["blocksUi"] and "$json.body.channel" in p["channelId"]["value"]
    assert p["otherOptions"]["includeLinkToWorkflow"] is False  # 글 아래에 n8n 홍보 문구를 붙이지 않는다


def test_lines_are_connected_and_notes_are_only_decoration():
    workflow = _builder().workflow
    names = _by_name(workflow)
    outgoing = {src: [d for out in _targets(workflow, src) for d in out] for src in workflow["connections"]}
    assert all(dst in names for dsts in outgoing.values() for dst in dsts)  # 없는 상자로 가는 선이 없다
    ends = {"받았다고 답함", "Slack에 올리기"}
    for n in workflow["nodes"]:
        if n["type"].endswith("stickyNote"):
            assert n["name"] not in outgoing  # 메모지는 선이 없다
        elif n["name"] in ends:
            assert n["name"] not in outgoing
        else:
            assert outgoing.get(n["name"]), n["name"]
    assert len([n for n in workflow["nodes"] if not n["type"].endswith("stickyNote")]) == 11  # 받는 줄 9 + 보내는 줄 2


# ── 봇이 결과 글을 내보내는 길 ─────────────────────────────
class _Recorder:
    def __init__(self):
        self.gateway, self.direct = [], []

    async def publish(self, kind, channel, text, thread_ts, blocks):
        self.gateway.append((kind, channel, text, thread_ts))
        return {"ok": True, "ts": "9.9", "via": "n8n"}

    async def call(self, method, payload):
        self.direct.append((method, payload))
        return {"ok": True, "ts": "1.1"}


@pytest.fixture
def recorder(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(slack_client, "publish", r.publish)
    monkeypatch.setattr(slack_client, "call", r.call)
    return r


def test_result_messages_go_through_n8n(recorder, monkeypatch):
    monkeypatch.setenv("N8N_RESULT_URL", "http://n8n:5678/webhook/internal/result")
    got = asyncio.run(slack_client.post_message("C1", "원인: …", thread_ts="1.0", blocks=[{"type": "divider"}], kind="analysis"))
    assert got["ts"] == "9.9" and recorder.gateway == [("analysis", "C1", "원인: …", "1.0")] and recorder.direct == []


def test_transient_messages_and_color_bars_go_directly(recorder, monkeypatch):
    """곧 고쳐 쓰거나 지울 진행 표시는 n8n을 거치지 않는다."""
    monkeypatch.setenv("N8N_RESULT_URL", "http://n8n:5678/webhook/internal/result")
    asyncio.run(slack_client.post_message("C1", "확인 중…", thread_ts="1.0", transient=True))
    asyncio.run(slack_client.post_message("C1", "분석 중…", thread_ts="1.0", attachments=[{"color": "#378ADD", "blocks": []}]))
    assert recorder.gateway == [] and [m for m, _ in recorder.direct] == ["chat.postMessage", "chat.postMessage"]


def test_without_n8n_or_when_n8n_fails_result_still_reaches_slack(recorder, monkeypatch):
    monkeypatch.delenv("N8N_RESULT_URL", raising=False)
    asyncio.run(slack_client.post_message("C1", "답", thread_ts="1.0"))
    assert recorder.gateway == [] and len(recorder.direct) == 1  # n8n 주소가 없으면 직접 올린다

    async def broken(*args):
        raise RuntimeError("n8n 응답 502")

    monkeypatch.setenv("N8N_RESULT_URL", "http://n8n:5678/webhook/internal/result")
    monkeypatch.setattr(slack_client, "publish", broken)
    got = asyncio.run(slack_client.post_message("C1", "답", thread_ts="1.0", blocks=[{"type": "divider"}]))
    assert got["ts"] == "1.1" and len(recorder.direct) == 2  # n8n이 멈춰 있어도 결과는 올라간다
