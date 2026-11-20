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
