"""n8n 알림 관문 워크플로 파일이 의도한 모양인지 확인한다. (실제로 받고 넘기는지는 scripts/check_gateway.py 로 서버에서 확인한다)"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _builder():
    spec = importlib.util.spec_from_file_location("build_n8n_workflow", ROOT / "scripts" / "build_n8n_workflow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nodes(workflow, kind):
    return [n for n in workflow["nodes"] if n["type"] == f"n8n-nodes-base.{kind}"]


def test_workflow_file_matches_builder():
    """저장소의 워크플로 파일은 만드는 코드의 결과와 같아야 한다 (파일만 손으로 고치지 않도록)."""
    built = _builder().workflow
    saved = json.loads((ROOT / "n8n" / "workflows" / "inboundGateway01.json").read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(built))


def test_all_inbound_notifications_enter_through_one_workflow():
    workflow = _builder().workflow
    hooks = {n["parameters"]["path"]: n for n in _nodes(workflow, "webhook")}
    assert set(hooks) == {"slack/events", "slack/actions", "jenkins", "github"}
    # Slack 서명은 원본 본문으로만 확인할 수 있어서 Slack 쪽만 원본을 보관한다
    assert {p for p, n in hooks.items() if n["parameters"]["options"].get("rawBody")} == {"slack/events", "slack/actions"}
    # 받은 요청은 응답 상자로 답한다 (봇 API의 답이나 '넘기지 않음'을 돌려주려고)
    assert all(n["parameters"]["responseMode"] == "responseNode" for n in hooks.values())


def test_slack_body_is_forwarded_untouched():
    """내용 종류를 '폼'으로 적어 보내면 n8n이 원본을 다시 포장해 서명이 깨진다. 가공하지 않은 데이터로 보내야 한다."""
    workflow = _builder().workflow
    slack = [n for n in _nodes(workflow, "httpRequest") if "/slack/" in n["parameters"]["url"]]
    assert len(slack) == 2
    for n in slack:
        p = n["parameters"]
        assert p["contentType"] == "binaryData" and p["inputDataFieldName"] == "data"
        headers = {h["name"]: h["value"] for h in p["headerParameters"]["parameters"]}
        assert headers["content-type"] == "application/octet-stream"
        assert "x-slack-signature" in headers and "x-slack-request-timestamp" in headers
        assert p["options"]["timeout"] <= 2500  # Slack은 3초 안에 답을 받아야 한다


def test_every_path_ends_with_a_response():
    """모든 줄기는 응답 상자에서 끝난다. 답하지 않는 줄기가 있으면 보낸 쪽이 시간 초과로 다시 보낸다."""
    workflow = _builder().workflow
    names = {n["name"]: n for n in workflow["nodes"]}
    outgoing = {src: [c["node"] for out in v["main"] for c in out] for src, v in workflow["connections"].items()}
    assert all(dst in names for dsts in outgoing.values() for dst in dsts)  # 없는 상자로 가는 선이 없다
    for n in workflow["nodes"]:
        kind = n["type"].rsplit(".", 1)[-1]
        if kind == "respondToWebhook":
            assert n["name"] not in outgoing  # 응답 상자가 끝이다
        else:
            assert outgoing.get(n["name"]), n["name"]  # 나머지는 모두 다음 상자가 있다
        if kind == "if":
            assert len(workflow["connections"][n["name"]]["main"]) == 2  # 참과 거짓 양쪽이 모두 이어져 있다
