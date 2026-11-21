"""외부 서비스 없이 확인할 수 있는 단위 테스트."""
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.analysis import flow
from bot.indexing import docs
from bot.search import rrf
from bot.tools import jenkins, logs

KST = ZoneInfo("Asia/Seoul")
NS = 1_000_000_000


# ── 신고 글에서 뽑은 값 검증 ────────────────────────────────
def test_verify_drops_values_not_in_report():
    report = "POST https://example.com/api/v1/rooms/17/enter 500 (Internal Server Error)"
    ok = flow._verify(report, {"api_method": "POST", "api_path": "https://example.com/api/v1/rooms/17/enter?x=1", "status": 500})
    assert ok["api_path"] == "/api/v1/rooms/17/enter?x=1" and ok["status"] == 500 and ok["api_method"] == "POST"
    # LLM이 글에 없는 값을 지어내면 버린다
    bad = flow._verify(report, {"api_method": "DELETE", "api_path": "/api/v1/users/3", "status": 404})
    assert bad["api_path"] is None and bad["status"] is None and bad["api_method"] is None


def _ns(h, m, s=0):
    return int(datetime(2026, 11, 21, h, m, s, tzinfo=KST).timestamp() * NS)


def test_time_window():
    now = _ns(14, 30)
    # 시각이 없으면 신고 직전 15분
    start, end = flow._time_window("에러 남", {}, now)
    assert (start, end) == (_ns(14, 15), _ns(14, 31))
    # 글에 시각이 있으면 그 주변 3분
    start, end = flow._time_window("14:03 에 에러", {"time_text": "14:03"}, now)
    assert (start, end) == (_ns(14, 0), _ns(14, 6))
    # "N분 전"
    start, end = flow._time_window("10분 전", {"minutes_ago": 10}, now)
    assert (start, end) == (_ns(14, 15), _ns(14, 25))
    # 지금보다 뒤 시각이면 어제로 본다
    start, _ = flow._time_window("", {"time_text": "23:50"}, now)
    assert datetime.fromtimestamp(start / NS, KST).day == 20


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args=()):
        return self

    def fetchall(self):
        return self.rows


def test_path_candidates_matches_concrete_path():
    conn = FakeConn([
        {"service": "backend", "route": "GET /api/v1/rooms/{}", "handler": "a.RoomController#getRoom(Long)"},
        {"service": "backend", "route": "POST /api/v1/rooms/{}/enter", "handler": "a.RoomController#enter(Long)"},
        {"service": "backend", "route": "ANY /api/v1/audio/{}/{}", "handler": "a.AudioController#get(String,String)"},
    ])
    assert [r["handler"] for r in flow._path_candidates(conn, "POST", "/api/v1/rooms/17/enter")] == ["a.RoomController#enter(Long)"]
    assert [r["handler"] for r in flow._path_candidates(conn, "GET", "https://x.io/api/v1/rooms/17?a=1")] == ["a.RoomController#getRoom(Long)"]
    assert [r["route"] for r in flow._path_candidates(conn, "GET", "/api/v1/audio/3/a.wav")] == ["ANY /api/v1/audio/{}/{}"]
    assert flow._path_candidates(conn, "GET", "/api/v1/unknown") == []


# ── 로그 ───────────────────────────────────────────────────
def test_build_query():
    assert logs.build_query(["backend", "ai-chat"], "error") == '{service=~"backend|ai-chat", level="ERROR"}'
    assert logs.build_query(None, None, 'say "hi"') == '{service=~".+"} |= "say \\"hi\\""'
    # 조회식에 끼어들 수 있는 글자는 걸러 낸다
    assert logs.build_query(['x"} | drop'], None) == '{service=~"xdrop"}'


def test_trim_keeps_head_and_caused_by():
    lines = ["ERROR something failed"] + [f"\tat lib.Frame{i}(F.java:{i})" for i in range(100)]
    lines[70] = "Caused by: java.lang.IllegalStateException: no room"
    out = logs.trim("\n".join(lines))
    assert out.startswith("ERROR something failed")
    assert "Caused by: java.lang.IllegalStateException: no room" in out
    assert "줄 생략" in out and len(out.splitlines()) < 50


def test_trim_build_log():
    lines = [f"step {i}" for i in range(400)]
    lines[100] = "RoomServiceTest > 없는_방을_조회하면_예외가_난다() FAILED"
    out = jenkins.trim_build_log("\n".join(lines))
    assert "RoomServiceTest > 없는_방을_조회하면_예외가_난다() FAILED" in out
    assert "step 399" in out and "step 99" in out and "step 50" not in out


# ── 검색 순위 합치기 ───────────────────────────────────────
def test_rrf_prefers_items_ranked_high_in_both():
    keyword = ["a", "b", "c"]
    vector = ["b", "d", "a"]
    merged = rrf(keyword, vector)
    assert merged[:2] == ["b", "a"]  # 두 목록 모두에 있는 것이 위로
    assert set(merged) == {"a", "b", "c", "d"}
    assert rrf([], ["x"]) == ["x"]


# ── 문서 ───────────────────────────────────────────────────
def test_mask_secrets():
    text = "\n".join([
        "password: qmz1301",
        "  MYSQL_ROOT_PASSWORD: \"dev_password\"",
        "secret: [OPENVIDU_SECRET]",
        "MM_URL = 'https://meeting.example.com/hooks/3xiyay1rgpygiqgcbh8t9iqnnh'",
        "contact: someone@example.com",
        "key sk-proj-abcdefghijklmnopqrstuvwxyz012345 here",
        "port: 8080",
    ])
    out = docs.mask_secrets(text)
    assert "qmz1301" not in out and "dev_password" not in out
    assert "[OPENVIDU_SECRET]" in out  # 자리 표시는 비밀이 아니므로 그대로
    assert "3xiyay1rgpygiqgcbh8t9iqnnh" not in out and "someone@example.com" not in out
    assert "sk-proj-" not in out
    assert "port: 8080" in out


def test_split_markdown_keeps_heading_trail():
    md = "# 매뉴얼\n소개\n## 3. 인프라\n### 3.2. 포트\n| Jenkins | 8333 |\n```bash\n# 주석은 제목이 아님\n```\n### 3.3. DB\nmysql\n## 4. CI\njenkins"
    chunks = docs.split_markdown(md, "exec/manual.md")
    headings = [c["heading"] for c in chunks]
    assert headings == ["매뉴얼", "매뉴얼 > 3. 인프라 > 3.2. 포트", "매뉴얼 > 3. 인프라 > 3.3. DB", "매뉴얼 > 4. CI"]
    assert "# 주석은 제목이 아님" in chunks[1]["content"]


# ── AI 수정: 제출 검증 ─────────────────────────────────────
import pytest

from bot.config import ProjectConfig, ServiceConfig
from bot.fix import flow as fix_flow

PROJECT = ProjectConfig(
    project="demo", repo_url="https://github.com/o/r", branch="main",
    services=[
        ServiceConfig(name="backend", path="backend", language="java", source_dirs=["src/main/java"]),
        ServiceConfig(name="frontend", path="frontend", language="javascript", source_dirs=["src"]),
    ],
)


def _fix(**over):
    base = {
        "explanation": "x",
        "edits": [{"file_path": "backend/src/main/java/a/Svc.java", "old_string": "return null;", "new_string": "return 1;"}],
        "test_file_path": "backend/src/test/java/a/SvcTest.java",
        "test_content": "class SvcTest {}",
        "test_name": "a.SvcTest",
        "commit_message": "고침",
    }
    return {**base, **over}


@pytest.fixture
def tree(tmp_path):
    f = tmp_path / "backend/src/main/java/a/Svc.java"
    f.parent.mkdir(parents=True)
    f.write_text("class Svc { Object f() { return null; } Object g() { return 0; } }", encoding="utf-8")
    (tmp_path / "Jenkinsfile").write_text("pipeline {}", encoding="utf-8")
    return tmp_path


def test_fix_validate_accepts_and_applies(tree):
    fix = _fix()
    assert fix_flow.validate(PROJECT, tree, fix).name == "backend"
    assert fix_flow.apply_edits(tree, fix) == ["backend/src/main/java/a/Svc.java"]
    assert "return 1;" in (tree / "backend/src/main/java/a/Svc.java").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"edits": [{"file_path": "Jenkinsfile", "old_string": "pipeline", "new_string": "x"}]}, "서비스 폴더 밖"),
        ({"edits": [{"file_path": "backend/build.gradle", "old_string": "a", "new_string": "b"}]}, "소스 폴더"),
        ({"edits": [{"file_path": "backend/src/main/java/a/../../../../x.java", "old_string": "", "new_string": "b"}]}, "서비스 폴더 밖"),
        ({"edits": [{"file_path": "backend/src/main/java/a/Svc.java", "old_string": "return", "new_string": "x"}]}, "2번"),
        ({"edits": [{"file_path": "backend/src/main/java/a/Svc.java", "old_string": "없는 글", "new_string": "x"}]}, "0번"),
        ({"edits": [{"file_path": "backend/src/main/java/a/Svc.java", "old_string": "", "new_string": "x"}]}, "이미 있는 파일"),
        ({"edits": []}, "비어"),
        ({"test_file_path": "backend/src/main/java/a/SvcTest.java"}, "src/test"),
        ({"test_file_path": "backend/src/test/java/a/SvcTests.java"}, "Test.java"),
        ({"test_content": "@SpringBootTest class SvcTest {}"}, "SpringBootTest"),
        ({"test_content": "class SvcTest { String q = \"DELETE FROM users\"; }"}, "DELETE"),
        ({"test_content": "jdbc.execute(\"drop table room\")"}, "DROP"),
    ],
)
def test_fix_validate_rejects(tree, over, reason):
    with pytest.raises(fix_flow.Rejected) as e:
        fix_flow.validate(PROJECT, tree, _fix(**over))
    assert reason in str(e.value)


def test_fix_new_file_and_frontend_test_rules(tree):
    fix = _fix(edits=[{"file_path": "backend/src/main/java/a/New.java", "old_string": "", "new_string": "class New {}"}])
    fix_flow.validate(PROJECT, tree, fix)
    fix_flow.apply_edits(tree, fix)
    assert (tree / "backend/src/main/java/a/New.java").read_text(encoding="utf-8") == "class New {}"
    page = tree / "frontend/src/pages/P.jsx"
    page.parent.mkdir(parents=True)
    page.write_text("export default function P() { return null; }", encoding="utf-8")
    js = _fix(edits=[{"file_path": "frontend/src/pages/P.jsx", "old_string": "return null;", "new_string": "return 1;"}],
              test_file_path="frontend/src/pages/P.test.jsx", test_name="src/pages/P.test.jsx")
    assert fix_flow.validate(PROJECT, tree, js).name == "frontend"
    with pytest.raises(fix_flow.Rejected):
        fix_flow.validate(PROJECT, tree, {**js, "test_file_path": "frontend/src/pages/P.spec.ts"})


def test_test_outcome_distinguishes_failure_from_build_error():
    assert fix_flow.test_outcome({"result": "SUCCESS", "log": "BUILD SUCCESSFUL"}) == "passed"
    failed = "SvcTest > 없는_아이템이면_404() FAILED\n    org.opentest4j.AssertionFailedError\n> Task :test FAILED\nThere were failing tests."
    assert fix_flow.test_outcome({"result": "FAILURE", "log": failed}) == "test_failed"
    broken = "> Task :compileTestJava FAILED\nSvcTest.java:12: error: cannot find symbol"
    assert fix_flow.test_outcome({"result": "FAILURE", "log": broken}) == "build_error"
    assert fix_flow.test_outcome({"result": "FAILURE", "log": "docker: no such image"}) == "build_error"


def test_build_log_masks_secrets():
    text = "\n".join([
        '+ curl -s -H "x-webhook-secret: 2d962a217a113272cf00fb9ffa0d2e2c" https://bot/webhook/jenkins',
        "Authorization: Bearer abc.def.ghi",
        "DB_ROOT_PASSWORD=supersecretvalue123",
        "using key sk-proj-abcdefghijklmnopqrstuvwxyz0123",
        "BUILD FAILED in 3s",
    ])
    out = jenkins.trim_build_log(text)
    for secret in ("2d962a217a113272cf00fb9ffa0d2e2c", "abc.def.ghi", "supersecretvalue123", "sk-proj-abcdef"):
        assert secret not in out
    assert "BUILD FAILED in 3s" in out and "x-webhook-secret: ***" in out


# ── 소스맵 ─────────────────────────────────────────────────
from bot import sourcemap


def test_decode_vlq():
    assert sourcemap.decode_vlq("AAAA") == [0, 0, 0, 0]
    assert sourcemap.decode_vlq("AACA") == [0, 0, 1, 0]
    assert sourcemap.decode_vlq("D") == [-1]
    assert sourcemap.decode_vlq("gB") == [16]  # 두 글자로 이어지는 값
    assert sourcemap.decode_vlq("6rk2B") == [886973]  # 소스맵 명세의 예


def test_sourcemap_lookup():
    # 생성된 1번째 줄: 열 0 → a.js 1줄, 열 10 → a.js 3줄(이름 load), 열 25 → b.jsx 57줄
    raw = {"version": 3, "sources": ["../../src/a.js", "../../src/pages/b.jsx"], "names": ["load"],
           "mappings": "AAAA,UAEAA,eCsDA"}
    sm = sourcemap.SourceMap(raw)
    assert (sm.lookup(1, 1).source, sm.lookup(1, 1).line) == ("../../src/a.js", 1)
    hit = sm.lookup(1, 15)  # 열 10~24 구간
    assert (hit.source, hit.line, hit.name) == ("../../src/a.js", 3, "load")
    far = sm.lookup(1, 48211)  # 마지막 구간 뒤쪽은 마지막 항목으로
    assert (far.source, far.line) == ("../../src/pages/b.jsx", 57)
    assert sm.lookup(2, 1) is None and sm.lookup(0, 1) is None


def test_sourcemap_resolve_reads_map_file(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("SOURCEMAP_DIR", str(tmp_path))
    (tmp_path / "index-a1b2.js.map").write_text(json.dumps(
        {"version": 3, "sources": ["../../src/pages/RoomPage.jsx"], "names": [], "mappings": "AAwDA"}), encoding="utf-8")
    pos = sourcemap.resolve("https://example.com/assets/index-a1b2.js?v=3", 1, 48211)
    assert (pos.source, pos.line) == ("src/pages/RoomPage.jsx", 57)
    assert sourcemap.resolve("https://example.com/assets/other.js", 1, 1) is None


def test_excerpt_keeps_error_line_in_long_code():
    lines = [f"line{i}" for i in range(1, 401)]  # 100번째 줄에서 시작하는 400줄짜리 컴포넌트
    content = "\n".join(lines) + "\n" + "x" * 6000
    out = flow._excerpt(content, start_line=100, focus_line=350)
    assert "line251   // ← 350번째 줄 (에러 위치)" in out  # 350 - 100 = 251번째 줄(0부터 250)
    assert out.startswith("line1\n") and "줄 생략" in out
    assert "line206" in out and "line296" in out and "line150" not in out
    # 짧은 코드는 그대로, 줄 번호를 모르면 앞부분만
    assert flow._excerpt("short", 1, 1) == "short"
    assert flow._excerpt(content, 100, None) == content[:5000]


def test_outcome_rejects_runs_that_found_no_tests():
    """테스트를 찾지 못해 실패한 실행을 '수정 전 실패'로 인정하면 안 된다."""
    vitest = "RUN  v5.1.1 /src\n\nNo test files found, exiting with code 1\n\nfilter: frontend/src/App.test.jsx"
    assert fix_flow.test_outcome({"result": "FAILURE", "log": vitest}) == "no_tests"
    gradle = "> Task :test FAILED\nNo tests found for given includes: [a.SvcTest](--tests filter)"
    assert fix_flow.test_outcome({"result": "FAILURE", "log": gradle}) == "no_tests"
    real = " FAIL  src/App.test.jsx > App 라우팅 > 없는 주소\nTestingLibraryElementError: Unable to find an element\n Test Files  1 failed (1)"
    assert fix_flow.test_outcome({"result": "FAILURE", "log": real}) == "test_failed"


def test_missing_new_symbol_requires_name_in_log():
    fix = _fix(edits=[
        {"file_path": "backend/src/main/java/a/PurchaseException.java", "old_string": "", "new_string": "class PurchaseException {}"},
        {"file_path": "backend/src/main/java/a/Svc.java", "old_string": "return null;", "new_string": "return 1;"},
    ])
    assert fix_flow.missing_new_symbol(fix, "SvcTest.java:5: error: cannot find symbol\n  symbol: class PurchaseException")
    # 다른 이유로 빌드가 깨진 것은 인정하지 않는다
    assert not fix_flow.missing_new_symbol(fix, "SvcTest.java:9: error: ';' expected")
    # 새 파일이 없는 수정이면 빌드 실패는 언제나 테스트 쪽 문제다
    assert not fix_flow.missing_new_symbol(_fix(), "error: cannot find symbol Whatever")


def test_build_log_strips_color_codes():
    out = jenkins.trim_build_log(" \x1b[32m✓\x1b[39m src/App.test.jsx \x1b[2m(1 test)\x1b[22m\n \x1b[1m\x1b[32m2 passed\x1b[39m\x1b[22m")
    assert "\x1b" not in out and "✓ src/App.test.jsx (1 test)" in out and "2 passed" in out


# ── Slack 메시지 모양 ──────────────────────────────────────
from bot import slack_ui


def test_progress_renders_one_line_and_list():
    p = slack_ui.Progress("C1", "1.0", "분석 중")
    p.steps = ["정보 추출", "로그 조회", "코드 찾기"]
    assert p._render() == "*분석 중…*  ✓ 정보 추출 · ✓ 로그 조회 · ▸ 코드 찾기"
    q = slack_ui.Progress("C1", "1.0", "AI 수정 중", inline=False)
    q.steps = ["수정안과 재현 테스트 작성", "수정 전 실패 확인 (Jenkins #5)", "수정 후 코드로 테스트 실행 중"]
    assert q._render().splitlines() == ["*AI 수정 중*", "✓ 수정안과 재현 테스트 작성", "✓ 수정 전 실패 확인 (Jenkins #5)", "▸ 수정 후 코드로 테스트 실행 중"]


def test_report_blocks_is_short_and_keeps_evidence_behind_button():
    result = {
        "cause_type": "code", "title": "라우터에 없는 주소를 받는 경로가 없음", "symptom": "없는 주소에서 빈 화면",
        "cause": 'AppContent의 <Routes>에 path=\\"*\\" 라우트가 없어 아무것도 그려지지 않는다.',
        "file_path": "frontend/src/App.jsx", "symbol": "AppContent", "line": 86, "confidence": "high",
        "evidence": ["<Routes> 안에 정해진 경로만 있음", "Loki ERROR 로그 없음"], "fix": "맨 끝에 catch-all 라우트를 추가한다.", "action": None,
    }
    text, blocks = slack_ui.report_blocks(result, {"incidents": [{"symptom": "x"}]}, 7)
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "context", "section", "actions"]
    head = blocks[0]["text"]["text"]
    assert head.startswith("*라우터에 없는 주소를 받는 경로가 없음*") and 'path="*"' in head and "\\" not in head
    meta = blocks[1]["elements"][0]["text"]
    assert "원인 종류: *코드*" in meta and "확신: 높음" in meta and "`frontend/src/App.jsx:86` AppContent" in meta and "비슷한 과거 장애 1건" in meta
    assert [e["action_id"] for e in blocks[3]["elements"]] == ["fix_with_ai", "fix_manually", "show_evidence"]
    # 근거는 본문에 없고 [근거 보기]를 눌렀을 때 나온다
    assert "Loki ERROR 로그 없음" not in "".join(str(b) for b in blocks)
    shown = slack_ui.evidence_text({"result": result, "code": [{"name": "App.jsx#AppContent", "file_path": "frontend/src/App.jsx", "start_line": 40}], "elapsed_seconds": 14.5})
    assert "Loki ERROR 로그 없음" in shown and "App.jsx#AppContent" in shown and "14.5초" in shown


def test_report_blocks_for_non_code_cause():
    result = {"cause_type": "infra", "title": "CPU가 제한돼 기동이 느림", "cause": "CPU steal이 70%까지 올랐다.", "confidence": "medium",
              "file_path": None, "symbol": None, "line": None, "action": "1. 크레딧 방식 확인\n2. 사양 올리기", "fix": None}
    _, blocks = slack_ui.report_blocks(result, {}, 3)
    assert "*조치 방법*" in blocks[2]["text"]["text"]
    assert [e["action_id"] for e in blocks[3]["elements"]] == ["mark_resolved", "show_evidence"]


def test_fix_result_blocks():
    summary = {"elapsed": 77, "attempts": 1, "before": 5, "after": 6, "changed": ["frontend/src/pages/NotFoundPage.jsx", "frontend/src/App.jsx"],
               "apis": [], "pr_url": "", "branch": "bot/fix-14"}
    text, blocks = slack_ui.fix_result_blocks(14, {"explanation": "NotFoundPage를 만들고 catch-all 라우트로 연결했습니다."}, summary)
    assert blocks[0]["text"]["text"].startswith("*수정 완료*  ·  77초\nNotFoundPage")
    meta = blocks[1]["elements"][0]["text"]
    assert "수정 전 실패 (#5) → 수정 후 통과 (#6)" in meta and "`NotFoundPage.jsx`, `App.jsx`" in meta
    assert "`bot/fix-14`" in blocks[2]["text"]["text"] and blocks[3]["type"] == "actions"
    # PR이 있으면 주소를 보여 주고, 해결 버튼은 두지 않는다 (merge되면 자동으로 해결 처리)
    _, with_pr = slack_ui.fix_result_blocks(14, {"explanation": "x"}, {**summary, "pr_url": "https://github.com/o/r/pull/1", "attempts": 2})
    assert "2번째 시도에 통과" in with_pr[0]["text"]["text"] and "pull/1" in with_pr[2]["text"]["text"] and len(with_pr) == 3


def test_empty_mention_is_not_accepted(monkeypatch):
    """내용 없이 봇만 부르면 접수하지 않고 쓰는 법을 안내한다 (빈 질문이 AI로 넘어가 400이 나던 문제)."""
    import asyncio

    from bot.worker import main as worker

    posted = []

    async def fake_post(channel, text, **kwargs):
        posted.append((channel, text, kwargs))
        return {"ts": "1.0"}

    def no_db():
        raise AssertionError("빈 멘션으로는 작업을 만들지 않는다")

    monkeypatch.setattr(worker.slack_client, "post_message", fake_post)
    monkeypatch.setattr(worker.store, "connect", no_db)
    asyncio.run(worker.on_mention({}, {"channel": "C1", "ts": "1.2", "text": "<@U0BOT>  "}))
    assert len(posted) == 1 and posted[0][1] == worker.EMPTY_MENTION
    assert "actions" not in [b["type"] for b in posted[0][2]["blocks"]]
