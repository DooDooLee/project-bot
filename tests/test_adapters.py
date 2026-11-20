"""언어 처리기 테스트. tests/fixtures 의 작은 예제 코드로 확인한다."""
from pathlib import Path

import pytest

from bot.adapters import JavaAdapter, JavaScriptAdapter
from bot.adapters.base import normalize_path

FIX = Path(__file__).parent / "fixtures"


# ── 경로 정규화 ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/api/v1/rooms/{roomId}", "/api/v1/rooms/{}"),
        ("/api/v1/rooms/{id:\\d+}/leave/", "/api/v1/rooms/{}/leave"),
        ("{}/api/v1/rooms/{}?since=1", "/api/v1/rooms/{}"),
        ("https://example.com/api/v1/rooms", "/api/v1/rooms"),
        ("api//v1", "/api/v1"),
        ("", "/"),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


# ── Java ───────────────────────────────────────────────────
@pytest.fixture(scope="module")
def java():
    adapter = JavaAdapter()
    files = sorted(p for p in FIX.rglob("*.java"))
    analyses = [adapter.analyze(p.read_bytes(), p.relative_to(FIX).as_posix()) for p in files]
    return adapter, analyses


def _names(analyses):
    return {c.qualified_name: c for a in analyses for c in a.chunks}


def test_java_chunks(java):
    _, analyses = java
    chunks = _names(analyses)
    get_room = chunks["com.example.demo.room.RoomController#getRoom(Long)"]
    assert get_room.kind == "method"
    assert get_room.name == "RoomController#getRoom(Long)"
    assert "방 하나를 조회한다" in get_room.content  # 바로 위 주석 포함
    assert "roomService.findRoom(roomId)" in get_room.content
    assert get_room.signature.endswith("getRoom(@PathVariable Long roomId)")
    assert get_room.start_line < get_room.end_line
    # 생성자, 오버로드 구분용 파라미터, 가변 인자, 중첩 클래스
    assert chunks["com.example.demo.room.RoomController#RoomController(RoomService)"].kind == "constructor"
    assert "com.example.demo.room.RoomController#create(RoomRequest,Long)" in chunks
    assert "com.example.demo.room.RoomServiceImpl.Helper#help(List,String...)" in chunks
    # 인터페이스 메서드도 청크가 된다 (호출 대상이 될 수 있도록)
    assert "com.example.demo.room.RoomService#findRoom(Long)" in chunks
    # 클래스 요약 청크
    summary = chunks["com.example.demo.room.RoomController"]
    assert summary.kind == "class"
    assert "private final RoomService roomService;" in summary.content
    assert '@RequestMapping("/api/v1/rooms")' in summary.content


def test_java_chunk_hash_changes_with_content(java):
    adapter, _ = java
    src = (FIX / "Room.java").read_bytes()
    before = {c.qualified_name: c.hash for c in adapter.extract_chunks(src, "Room.java")}
    after = {c.qualified_name: c.hash for c in adapter.extract_chunks(src.replace(b"close() {}", b"close() { name = null; }"), "Room.java")}
    assert before["com.example.demo.room.Room#close()"] != after["com.example.demo.room.Room#close()"]
    assert before["com.example.demo.room.Room#Room(String)"] == after["com.example.demo.room.Room#Room(String)"]


def test_java_routes(java):
    _, analyses = java
    routes = {(r.key, r.symbol.split("#")[1]) for a in analyses for r in a.routes_served}
    assert ("GET /api/v1/rooms/{}", "getRoom(Long)") in routes  # 상수로 적은 경로
    assert ("POST /api/v1/rooms", "create(RoomRequest,Long)") in routes  # 경로 없는 매핑
    assert ("DELETE /api/v1/rooms/{}/leave", "leave(Long)") in routes  # 배열 + method 지정
    assert ("DELETE /api/v1/rooms/{}/exit", "leave(Long)") in routes


def test_java_call_resolution(java):
    adapter, analyses = java
    edges = {(e.caller.split("room.")[-1], e.callee.split("room.")[-1], e.kind) for e in adapter.resolve(analyses)}
    # 필드 타입으로 대상 찾기 + 인터페이스 호출은 구현 클래스까지
    assert ("RoomController#getRoom(Long)", "RoomService#findRoom(Long)", "call") in edges
    assert ("RoomController#getRoom(Long)", "RoomServiceImpl#findRoom(Long)", "call") in edges
    # 정적 호출 (import 한 다른 패키지의 클래스)
    assert ("RoomController#getRoom(Long)", "dto.RoomResponse#from(Room)", "call") in edges
    # this.필드.메서드()
    assert ("RoomController#create(RoomRequest,Long)", "RoomService#createRoom(String,Long)", "call") in edges
    # 같은 클래스 안의 호출, 지역 변수 타입
    assert ("RoomController#leave(Long)", "RoomController#log()", "call") in edges
    assert ("RoomServiceImpl#leave(Long)", "RoomServiceImpl#findRoom(Long)", "call") in edges
    assert ("RoomServiceImpl#leave(Long)", "Room#close()", "call") in edges
    assert ("RoomServiceImpl#createRoom(String,Long)", "RoomRepository#save(Room)", "call") in edges
    # API 경로 → 처리 메서드
    assert ("GET /api/v1/rooms/{}", "RoomController#getRoom(Long)", "route") in edges
    # 체이닝 뒤쪽(orElseThrow)처럼 대상을 모르는 호출은 잇지 않는다
    assert not any("orElseThrow" in callee for _, callee, _ in edges)


def test_java_stack_trace():
    text = """
java.lang.IllegalStateException: no room
	at com.example.demo.room.RoomServiceImpl.lambda$findRoom$0(RoomServiceImpl.java:17)
	at java.base/java.util.Optional.orElseThrow(Optional.java:403)
	at com.example.demo.room.RoomServiceImpl.findRoom(RoomServiceImpl.java:17)
	at com.example.demo.room.RoomServiceImpl$$SpringCGLIB$$0.findRoom(<generated>)
	at com.example.demo.room.RoomController.getRoom(RoomController.java:19)
	at org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:885)
Caused by: java.lang.RuntimeException: x
	at com.example.demo.room.RoomServiceImpl$Helper.help(RoomServiceImpl.java:36)
"""
    frames = JavaAdapter().parse_stack_trace(text, ["com.example.demo"])
    assert [(f.class_name.rsplit(".", 1)[-1], f.method, f.line) for f in frames] == [
        ("RoomServiceImpl", "findRoom", 17),  # 람다는 원래 메서드 이름으로
        ("RoomServiceImpl", "findRoom", 17),
        ("RoomController", "getRoom", 19),
        ("Helper", "help", 36),  # 중첩 클래스
    ]
    assert frames[3].class_name == "com.example.demo.room.RoomServiceImpl.Helper"
    # 패키지 조건이 없으면 라이브러리 줄도 나온다
    assert len(JavaAdapter().parse_stack_trace(text)) == 6


# ── JavaScript ─────────────────────────────────────────────
@pytest.fixture(scope="module")
def js():
    adapter = JavaScriptAdapter()
    root = FIX / "web"
    files = sorted(p for p in root.rglob("*.js*"))
    analyses = [adapter.analyze(p.read_bytes(), p.relative_to(root).as_posix()) for p in files]
    return adapter, analyses


def test_js_chunks(js):
    _, analyses = js
    chunks = _names(analyses)
    assert chunks["src/pages/RoomPage.jsx#RoomPage"].kind == "component"
    assert chunks["src/pages/RoomPage.jsx#RoomPage"].name == "RoomPage.jsx#RoomPage"
    assert chunks["src/pages/RoomPage.jsx#formatName"].kind == "function"
    assert chunks["src/api/room.js#getRoom"].kind == "function"
    assert "src/api/room.js#createRoom" in chunks  # const 화살표 함수
    assert "src/api/room.js#roomApi.leave" in chunks  # 객체 안의 함수
    assert "export default function RoomCard" in chunks["src/components/RoomCard.jsx#RoomCard"].content
    # 컴포넌트 안의 함수(loadRoom)는 따로 자르지 않고 컴포넌트에 포함
    assert "src/pages/RoomPage.jsx#loadRoom" not in chunks
    assert "const loadRoom" in chunks["src/pages/RoomPage.jsx#RoomPage"].content


def test_js_http_calls(js):
    _, analyses = js
    called = {(r.key, r.symbol.split("#")[1]) for a in analyses for r in a.routes_called}
    assert ("GET /api/v1/rooms/{}", "getRoom") in called  # 템플릿 + 상수
    assert ("POST /api/v1/rooms", "createRoom") in called  # 상수만
    assert ("DELETE /api/v1/rooms/{}/leave", "roomApi.leave") in called  # 문자열 더하기
    assert ("POST /api/v1/rooms/{}/events", "streamRoom") in called  # fetch + 기본 주소 + 쿼리 제거


def test_js_call_resolution(js):
    adapter, analyses = js
    edges = {(e.caller, e.callee, e.kind) for e in adapter.resolve(analyses, {"@": "src"})}
    page = "src/pages/RoomPage.jsx#RoomPage"
    assert (page, "src/api/room.js#getRoom", "call") in edges  # 이름으로 import
    assert (page, "src/pages/RoomPage.jsx#formatName", "call") in edges  # 같은 파일
    assert (page, "src/api/room.js#roomApi.leave", "call") in edges  # 객체.메서드
    assert (page, "src/api/room.js#createRoom", "call") in edges  # import * as
    assert (page, "src/components/RoomCard.jsx#RoomCard", "render") in edges  # 별칭 + JSX
    assert ("src/api/room.js#getRoom", "GET /api/v1/rooms/{}", "http") in edges
    # 외부 패키지(react) 호출은 잇지 않는다
    assert not any("useState" in callee for _, callee, _ in edges)


def test_js_stack_trace():
    text = """
TypeError: Cannot read properties of undefined (reading 'map')
    at RoomPage (https://example.com/assets/index-a1b2.js:1:48211)
    at async loadRoom (https://example.com/assets/index-a1b2.js:1:50001)
    at https://example.com/assets/index-a1b2.js:2:10
    at renderWithHooks (https://example.com/node_modules/react-dom/x.js:5:5)
"""
    frames = JavaScriptAdapter().parse_stack_trace(text)
    assert [(f.symbol, f.line, f.column) for f in frames] == [
        ("RoomPage", 1, 48211),
        ("loadRoom", 1, 50001),
        ("<anonymous>", 2, 10),
    ]
    assert frames[0].file == "https://example.com/assets/index-a1b2.js"


def test_frontend_links_to_backend(java, js):
    """프론트의 HTTP 호출과 백엔드의 API 경로가 같은 글자로 이어진다."""
    jadapter, janalyses = java
    sadapter, sanalyses = js
    served = {e.caller: e.callee for e in jadapter.resolve(janalyses) if e.kind == "route"}
    called = {e.callee: e.caller for e in sadapter.resolve(sanalyses, {"@": "src"}) if e.kind == "http"}
    assert served["GET /api/v1/rooms/{}"].endswith("RoomController#getRoom(Long)")
    assert called["GET /api/v1/rooms/{}"] == "src/api/room.js#getRoom"
    assert set(called) & set(served) == {"GET /api/v1/rooms/{}", "POST /api/v1/rooms", "DELETE /api/v1/rooms/{}/leave"}


def test_java_stack_trace_with_log_prefix():
    """Jenkins 빌드 로그처럼 줄마다 시각이 붙어 있어도 스택 트레이스를 읽는다."""
    text = (
        "[2026-11-20T20:49:25.290Z] Caused by: java.lang.IllegalStateException: No compatible attachment provider is available\n"
        "[2026-11-20T20:49:25.290Z] \tat reactor.tools.agent.ReactorDebugAgent.init(ReactorDebugAgent.java:56)\n"
        "[2026-11-20T20:49:25.290Z] \tat com.sudaduck.aichat.AiChatApplication.main(AiChatApplication.java:15)\n"
    )
    frames = JavaAdapter().parse_stack_trace(text, ["com.sudaduck.aichat"])
    assert [(f.class_name, f.method, f.line) for f in frames] == [("com.sudaduck.aichat.AiChatApplication", "main", 15)]
