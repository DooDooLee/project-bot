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
