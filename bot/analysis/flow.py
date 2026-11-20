"""오류 분석 흐름 (LangGraph).

순서를 고정한다: 정보 추출 → 증거 수집 → 코드 찾기 → 비슷한 과거 장애 → 원인 분석.
사람이 붙여 넣은 글은 믿을 만한 형식이 아니므로, 스택 트레이스는 항상 Loki에서 다시 가져온다.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

from bot import code_cache, embedding, llm, sourcemap
from bot.adapters import get_adapter
from bot.adapters.base import normalize_path
from bot.config import ProjectConfig
from bot.db import jobs, store
from bot.tools import logs, metrics

log = logging.getLogger("bot.analysis")
KST = ZoneInfo("Asia/Seoul")
NS = 1_000_000_000
CODE_BUDGET = 28_000  # LLM에 넘기는 코드 글자 수 상한
CAUSE_TYPES = {"code": "코드", "config": "서버 설정", "infra": "인프라", "external": "외부 서비스", "unknown": "판단 불가"}

# 확실한 패턴은 LLM보다 코드가 먼저 판단해서 힌트로 넘긴다
_PATTERNS = [
    (r"No space left on device", "infra", "디스크가 가득 찼다는 메시지가 있음"),
    (r"OutOfMemoryError|Cannot allocate memory|Killed process", "infra", "메모리 부족 메시지가 있음"),
    (r"Communications link failure|Connection refused|Unable to connect to Redis|Connection reset", "infra", "DB·Redis 등 연결 실패 메시지가 있음"),
    (r"Too many connections|Connection is not available, request timed out", "infra", "DB 연결 수 한도 관련 메시지가 있음"),
    (r"\b401\b.*(api\.openai\.com|Incorrect API key)|invalid_api_key|insufficient_quota", "external", "외부 API 키·한도 관련 메시지가 있음"),
    (r"\b429\b.*Too Many Requests|rate_limit_exceeded", "external", "외부 API 호출 한도 초과 메시지가 있음"),
    (r"Could not resolve placeholder|Failed to bind properties|BeanCreationException", "config", "설정값 누락·오류로 보이는 메시지가 있음"),
]


class State(TypedDict, total=False):
    report: str  # Slack에 붙여 넣은 원문
    now_ns: int  # 신고 시각
    build_log: str  # 배포 실패일 때 Jenkins 로그
    extracted: dict
    logs: list[dict]
    frames: list[dict]
    code: list[dict]
    incidents: list[dict]
    hints: list[str]
    metrics: list[str]  # 신고 시각 직전의 서버 지표
    result: dict


@dataclass
class Context:
    project: ProjectConfig
    redis: Any
    usage: llm.Usage
    progress: Callable[[str], Awaitable[None]]


EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "증상을 한국어 한 문장으로. 예: 방 입장 요청이 500으로 실패함"},
        "where": {"type": "string", "enum": ["server", "browser", "unknown"], "description": "server: HTTP 상태 코드나 서버 에러가 보임. browser: TypeError 같은 JS 에러만 보임"},
        "api_method": {"type": ["string", "null"], "description": "HTTP 메서드 (GET, POST 등). 글에 없으면 null"},
        "api_path": {"type": ["string", "null"], "description": "요청 경로. 글에 적힌 그대로 (/api/v1/rooms/17). 없으면 null"},
        "status": {"type": ["integer", "null"], "description": "HTTP 상태 코드. 없으면 null"},
        "error_message": {"type": ["string", "null"], "description": "에러 메시지 원문 한 줄. 없으면 null"},
        "time_text": {"type": ["string", "null"], "description": "글에 적힌 시각 그대로 (14:03, 3분 전). 없으면 null"},
        "minutes_ago": {"type": ["number", "null"], "description": "'N분 전'처럼 적혀 있으면 그 분 수. 없으면 null"},
    },
    "required": ["summary", "where", "api_method", "api_path", "status", "error_message", "time_text", "minutes_ago"],
}

EXTRACT_SYSTEM = (
    "너는 개발팀 Slack에 올라온 에러 신고에서 정보를 뽑는다. 신고 글은 브라우저 콘솔, 네트워크 탭, 사람의 설명이 섞여 있다.\n"
    "글에 실제로 적힌 값만 채우고, 적혀 있지 않은 값은 추측하지 말고 null로 둔다."
)

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "cause_type": {"type": "string", "enum": list(CAUSE_TYPES), "description": "code: 저장소 코드의 문제. config: 서버 설정값·환경 변수. infra: 디스크·메모리·DB·네트워크. external: 외부 서비스(OpenAI, 카카오 등). unknown: 근거 부족"},
        "service": {"type": "string", "description": "문제가 난 서비스 이름"},
        "symptom": {"type": "string", "description": "증상 한 문장"},
        "cause": {"type": "string", "description": "원인. 2~4문장. 로그와 코드의 어느 부분이 근거인지 포함"},
        "file_path": {"type": ["string", "null"], "description": "원인 위치의 파일 경로. 코드 문제가 아니면 null"},
        "symbol": {"type": ["string", "null"], "description": "원인 위치의 메서드·함수 이름 (제공된 코드의 이름 그대로)"},
        "line": {"type": ["integer", "null"]},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "근거 2~4개. 로그 줄이나 코드 줄을 짧게 인용"},
        "fix": {"type": ["string", "null"], "description": "cause_type이 code일 때: 어떻게 고치면 되는지 1~3문장"},
        "action": {"type": ["string", "null"], "description": "cause_type이 code가 아닐 때: 사람이 할 조치 방법을 순서대로"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["cause_type", "service", "symptom", "cause", "file_path", "symbol", "line", "evidence", "fix", "action", "confidence"],
}

ANALYZE_SYSTEM = """너는 웹 서비스의 장애 원인을 분석하는 엔지니어다. 신고 내용, 서버 로그, 관련 코드, 비슷한 과거 장애를 받아 원인을 정리한다.

규칙
- 로그와 코드에 근거가 있는 것만 말한다. 근거가 부족하면 cause_type을 unknown으로 하고 무엇이 더 필요한지 cause에 적는다.
- 스택 트레이스의 맨 위 줄이 항상 원인은 아니다. 값이 어디서 잘못 들어왔는지 호출한 쪽 코드까지 확인한다.
- 브라우저 에러라도 서버가 준 응답 모양이 원인일 수 있다. 프론트 코드가 쓰는 필드와 백엔드 응답 클래스의 필드를 비교한다.
- 원인이 저장소의 코드가 아니면(설정값, 디스크·메모리, 외부 서비스) code로 분류하지 않는다. 그런 문제는 코드를 고쳐서 해결되지 않는다.
- 쉬운 한국어로 짧게 쓴다. 메서드 이름, 파일 경로는 제공된 그대로 쓴다."""


def _time_window(report: str, extracted: dict, now_ns: int) -> tuple[int, int]:
    """로그를 찾을 시간 구간. 글에 시각이 있으면 그 주변, 없으면 신고 시각 직전 15분."""
    now = datetime.fromtimestamp(now_ns / NS, KST)
    minutes_ago = extracted.get("minutes_ago")
    if minutes_ago:
        center = now - timedelta(minutes=float(minutes_ago))
        return int((center - timedelta(minutes=5)).timestamp() * NS), int((center + timedelta(minutes=5)).timestamp() * NS)
    m = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)", extracted.get("time_text") or "")
    if m:
        center = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0), microsecond=0)
        if center > now + timedelta(minutes=2):
            center -= timedelta(days=1)
        if now - center < timedelta(hours=24):
            return int((center - timedelta(minutes=3)).timestamp() * NS), int((center + timedelta(minutes=3)).timestamp() * NS)
    return int((now - timedelta(minutes=15)).timestamp() * NS), int((now + timedelta(minutes=1)).timestamp() * NS)


def _verify(report: str, extracted: dict) -> dict:
    """LLM이 뽑은 값이 원문에 실제로 있는지 코드로 확인한다. 없으면 버린다."""
    out = dict(extracted)
    path = out.get("api_path")
    if path:
        path = re.sub(r"^[a-zA-Z]+://[^/]+", "", path.strip())
        if path.split("?")[0] not in report:
            path = None
        out["api_path"] = path
    status = out.get("status")
    if status is not None and str(status) not in report:
        out["status"] = None
    method = out.get("api_method")
    if method and method.upper() not in report.upper():
        out["api_method"] = None
    return out


def _excerpt(content: str, start_line: int, focus_line: int | None, limit: int = 5000, around: int = 45) -> str:
    """LLM에 넘길 코드. 짧으면 전부, 길면 에러가 난 줄 주변을 넘긴다 (앞부분만 자르면 정작 에러 줄이 빠질 수 있다)."""
    if len(content) <= limit:
        return content
    lines = content.split("\n")
    if focus_line is None or not (start_line <= focus_line < start_line + len(lines)):
        return content[:limit]
    at = focus_line - start_line
    lo, hi = max(0, at - around), min(len(lines), at + around + 1)
    out = []
    if lo > 0:
        out += lines[:6] + [f"    // ... ({lo - 6}줄 생략)"] if lo > 6 else lines[:lo]
    for i in range(lo, hi):
        out.append(lines[i] + (f"   // ← {focus_line}번째 줄 (에러 위치)" if i == at else ""))
    if hi < len(lines):
        out.append(f"    // ... (아래 {len(lines) - hi}줄 생략)")
    return "\n".join(out)[: limit + 2000]


def _path_candidates(conn, method: str | None, path: str) -> list[dict]:
    """/api/v1/rooms/17 처럼 값이 들어간 경로를 등록된 경로(/api/v1/rooms/{})와 맞춘다."""
    target = normalize_path(path).split("/")
    found = []
    for r in store.all_routes(conn):
        r_method, _, r_path = r["route"].partition(" ")
        if method and r_method not in (method.upper(), "ANY"):
            continue
        parts = r_path.split("/")
        if len(parts) == len(target) and all(p == "{}" or p == t for p, t in zip(parts, target)):
            found.append(r)
    return found


def build_graph(ctx: Context):
    java_services = [s for s in ctx.project.services if s.language == "java"]
    js_services = [s for s in ctx.project.services if s.language == "javascript"]

    async def extract(state: State) -> dict:
        await ctx.progress("신고 내용에서 API, 상태 코드, 시각을 찾는 중")
        report = state["report"]
        if state.get("build_log"):
            return {"extracted": {"summary": "배포(빌드) 실패", "where": "deploy"}, "hints": []}
        raw = await llm.structured(
            llm.HAIKU, EXTRACT_SYSTEM, report[:6000], name="record_report", description="신고 글에서 뽑은 정보를 기록한다",
            schema=EXTRACT_SCHEMA, max_tokens=600, usage=ctx.usage,
        )
        return {"extracted": _verify(report, raw)}

    async def evidence(state: State) -> dict:
        ex = state["extracted"]
        report = state["report"]
        text_for_hints = report + "\n" + state.get("build_log", "")
        found: list[dict] = []
        if not state.get("build_log"):
            await ctx.progress("Loki에서 그 시각의 서버 에러 로그를 찾는 중")
            start, end = _time_window(report, ex, state["now_ns"])
            names = [s.log_label or s.name for s in java_services]
            entries = []
            for widen in (0, 45):  # 못 찾으면 구간을 넓혀 한 번 더
                try:
                    entries = await logs.query_logs(names, start - widen * 60 * NS, end, level="ERROR", limit=60)
                except Exception as e:
                    log.warning("loki 조회 실패: %s", e)
                    entries = []
                if entries:
                    break
            # 브라우저에서 난 JS 에러는 서버 로그에 남지 않는다. 같은 시각의 관계없는 서버 에러가 섞이지 않게 한다
            if ex.get("where") == "browser" and not ex.get("api_path"):
                message = (ex.get("error_message") or "")[:40]
                entries = [e for e in entries if message and message in e.line]
            # 신고된 API를 처리하는 메서드가 스택 트레이스에 있으면 그 로그가 우선
            handlers = []
            if ex.get("api_path"):
                with store.connect() as conn:
                    handlers = [r["handler"].split("#")[0].rsplit(".", 1)[-1] for r in _path_candidates(conn, ex.get("api_method"), ex["api_path"])]

            def score(e: logs.LogEntry) -> tuple:
                has_handler = any(h and h in e.line for h in handlers)
                has_ours = any(p in e.line for s in java_services for p in s.base_packages)
                has_msg = bool(ex.get("error_message")) and ex["error_message"][:40] in e.line
                return (has_handler, has_msg, has_ours, e.ts_ns)

            # 신고된 API의 처리 메서드가 찍힌 로그가 있으면 그것만 쓴다 (같은 시각의 다른 에러가 섞이지 않게)
            matched = [e for e in entries if any(h and h in e.line for h in handlers)]
            if matched:
                entries = matched
            seen = set()
            for e in sorted(entries, key=score, reverse=True):
                head = e.line.split("\n", 1)[0][-160:]
                if head in seen:
                    continue
                seen.add(head)
                found.append({"service": e.service, "time": datetime.fromtimestamp(e.ts_ns / NS, KST).strftime("%H:%M:%S"), "line": e.line})
                if len(found) == 3:
                    break
            text_for_hints += "\n" + "\n".join(f["line"] for f in found)
        hints = [f"{note} (자동 감지, 유형 후보: {CAUSE_TYPES[kind]})" for pattern, kind, note in _PATTERNS if re.search(pattern, text_for_hints)]
        # 에러 시각의 서버 상태도 본다. 코드가 아니라 서버 자원이 원인인 경우를 구분하기 위해서다
        snapshot: list[str] = []
        checks = (
            ("cpu_steal", 20, "CPU steal이 높음. 클라우드가 CPU를 제한하고 있어 모든 처리가 느려짐"),
            ("memory", 90, "메모리 사용률이 높음"),
            ("disk", 90, "디스크 사용률이 높음"),
            ("cpu", 101, ""),
        )
        for name, limit, note in checks:
            try:
                m = await metrics.query_metric(name, 15, state["now_ns"] / NS)
            except Exception as e:
                log.warning("지표 조회 실패(%s): %s", name, e)
                continue
            if not m["series"]:
                continue
            top = m["series"][0]
            snapshot.append(f"{m['title']}: 평균 {top['avg']}{m['unit']}, 최대 {top['max']}{m['unit']}")
            if note and top["max"] >= limit:
                hints.append(f"{note} (최대 {top['max']}{m['unit']}, 자동 감지, 유형 후보: 인프라)")
        return {"logs": found, "hints": hints, "metrics": snapshot}

    async def find_code(state: State) -> dict:
        await ctx.progress("스택 트레이스의 메서드와 콜 그래프로 관련 코드를 찾는 중")
        ex = state["extracted"]
        picked: dict[str, dict] = {}
        frames_out: list[dict] = []
        budget = CODE_BUDGET

        with store.connect() as conn:

            async def add(qualified_name: str, service: str | None, why: str, focus_line: int | None = None) -> dict | None:
                nonlocal budget
                if qualified_name in picked or budget <= 0:
                    return picked.get(qualified_name)
                chunk = await code_cache.get(ctx.redis, conn, qualified_name, service)
                if chunk is None or chunk["kind"] == "class" and why.startswith("호출"):
                    return None
                chunk = {**chunk, "why": why, "content": _excerpt(chunk["content"], chunk["start_line"], focus_line)}
                picked[qualified_name] = chunk
                budget -= len(chunk["content"])
                return chunk

            # 1) 스택 트레이스 (서버 로그 + 신고 글 + 빌드 로그)
            trace_text = "\n".join(e["line"] for e in state.get("logs", [])) + "\n" + state["report"] + "\n" + state.get("build_log", "")
            for svc in java_services:
                adapter = get_adapter(svc.language)
                for frame in adapter.parse_stack_trace(trace_text, svc.base_packages)[:12]:
                    row = store.chunk_for_frame(conn, frame, svc.name)
                    if row is None:
                        continue
                    frames_out.append({"service": svc.name, "symbol": row["name"], "file": row["file_path"], "line": frame.line})
                    await add(row["qualified_name"], svc.name, f"스택 트레이스 {frame.file}:{frame.line}", frame.line)
            # 2) 신고된 API 경로 → 처리 메서드
            if ex.get("api_path"):
                for r in _path_candidates(conn, ex.get("api_method"), ex["api_path"])[:3]:
                    await add(r["handler"], r["service"], f"{r['route']} 를 처리하는 메서드")
                    # 그 API를 부르는 프론트 함수
                    for caller in store.edges_to(conn, r["route"])[:2]:
                        await add(caller["caller"], caller["service"], f"{r['route']} 를 호출하는 프론트 코드")
            # 2-0) 배포 후 브라우저 확인(E2E)이 실패했으면 화면 경로를 정하는 라우터 코드를 함께 본다
            if "playwright" in state.get("build_log", "").lower():
                for svc in js_services:
                    rows = conn.execute(
                        "SELECT qualified_name FROM code_chunk WHERE service = %s AND content LIKE %s ORDER BY length(content) DESC LIMIT 2",
                        (svc.name, "%<Route %"),
                    ).fetchall()
                    for row in rows:
                        await add(row["qualified_name"], svc.name, "E2E 실패라 화면 경로를 정하는 라우터 코드를 확인")
            # 2-1) 401·403 은 인증 설정이 원인일 수 있어 보안 설정 클래스를 함께 본다
            if ex.get("status") in (401, 403):
                rows = conn.execute(
                    "SELECT qualified_name, service FROM code_chunk WHERE kind <> 'class' AND content ILIKE %s ORDER BY length(content) DESC LIMIT 2",
                    ("%SecurityFilterChain%",),
                ).fetchall()
                for row in rows:
                    await add(row["qualified_name"], row["service"], "상태 코드가 401·403 이라 인증 설정을 확인")
            # 3) 브라우저 에러: 신고 글의 함수 이름으로 프론트 코드 찾기
            if ex.get("where") == "browser":
                for svc in js_services:
                    for frame in get_adapter(svc.language).parse_stack_trace(state["report"])[:6]:
                        # 압축된 위치(index-a1b2.js:1:48211)를 소스맵으로 원래 파일·줄로 바꾼다
                        pos = sourcemap.resolve(frame.file, frame.line, frame.column or 1)
                        if pos is not None:
                            row = store.chunk_at(conn, pos.source, pos.line, svc.name)
                            if row is not None:
                                frames_out.append({"service": svc.name, "symbol": row["name"], "file": row["file_path"], "line": pos.line})
                                await add(row["qualified_name"], svc.name, f"소스맵: {frame.file.rsplit('/', 1)[-1]}:{frame.line}:{frame.column} → {pos.source}:{pos.line}", pos.line)
                                continue
                        if not frame.method:
                            continue
                        rows = conn.execute(
                            "SELECT qualified_name FROM code_chunk WHERE service = %s AND (qualified_name LIKE %s OR qualified_name LIKE %s) LIMIT 2",
                            (svc.name, f"%#{frame.method}", f"%.{frame.method}"),
                        ).fetchall()
                        for row in rows:
                            await add(row["qualified_name"], svc.name, f"브라우저 스택의 함수 {frame.method}")
            # 4) 지금까지 찾은 코드가 호출하는 쪽 (콜 그래프를 따라 한두 단계)
            for qn in list(picked)[:6]:
                for step in store.neighbors(conn, qn, "callees", depth=2, limit=12):
                    node = step["node"]
                    if " /" in node:  # API 경로 노드는 건너뛰고 그 뒤의 메서드로 이어진다
                        continue
                    await add(node, None, f"{picked[qn]['name']} 에서 호출 (콜 그래프 {step['depth']}단계)")
        return {"frames": frames_out, "code": list(picked.values())}

    async def similar(state: State) -> dict:
        ex = state["extracted"]
        query = " ".join(filter(None, [ex.get("summary"), ex.get("error_message"), (state.get("logs") or [{}])[0].get("line", "")[:300]]))
        vector = await embedding.embed(query)
        with store.connect() as conn:
            rows = jobs.similar_incidents(conn, vector)
        if rows:
            await ctx.progress(f"비슷한 과거 장애 {len(rows)}건을 찾음")
        return {"incidents": [{k: (str(v) if k == "created_at" else v) for k, v in r.items()} for r in rows]}

    async def analyze(state: State) -> dict:
        await ctx.progress("원인을 분석하는 중")
        parts = [f"## 신고 내용\n{state['report'][:6000]}"]
        if state.get("build_log"):
            parts.append(f"## Jenkins 빌드 로그 (실패 주변)\n{state['build_log'][:9000]}")
        if state.get("hints"):
            parts.append("## 자동 감지된 패턴\n" + "\n".join(f"- {h}" for h in state["hints"]))
        if state.get("metrics"):
            parts.append("## 서버 지표 (신고 직전 15분, Prometheus)\n" + "\n".join(f"- {m}" for m in state["metrics"]))
        if state.get("logs"):
            parts.append("## 서버 에러 로그 (Loki)\n" + "\n\n".join(f"[{e['service']} {e['time']}]\n{logs.trim(e['line'])}" for e in state["logs"]))
        else:
            parts.append("## 서버 에러 로그 (Loki)\n해당 시각에 ERROR 로그를 찾지 못함")
        if state.get("code"):
            parts.append("## 관련 코드\n" + "\n\n".join(
                f"### {c['name']} ({c['service']}, {c['file_path']}:{c['start_line']}-{c['end_line']})\n찾은 경로: {c['why']}\n```\n{c['content']}\n```" for c in state["code"]
            ))
        else:
            parts.append("## 관련 코드\n관련 코드를 찾지 못함")
        if state.get("incidents"):
            parts.append("## 비슷한 과거 장애 (해결됨)\n" + "\n".join(
                f"- {i['created_at'][:10]} 증상: {i['symptom']} / 원인: {i['cause']} / 해결: {i['resolution']}" for i in state["incidents"]
            ))
        result = await llm.structured(
            llm.SONNET, ANALYZE_SYSTEM, "\n\n".join(parts), name="report_cause", description="분석한 원인을 보고한다",
            schema=REPORT_SCHEMA, max_tokens=1800, usage=ctx.usage,
        )
        return {"result": result}

    g = StateGraph(State)
    g.add_node("extract", extract)
    g.add_node("evidence", evidence)
    g.add_node("find_code", find_code)
    g.add_node("similar", similar)
    g.add_node("analyze", analyze)
    g.add_edge(START, "extract")
    g.add_edge("extract", "evidence")
    g.add_edge("evidence", "find_code")
    g.add_edge("find_code", "similar")
    g.add_edge("similar", "analyze")
    g.add_edge("analyze", END)
    return g.compile()


async def run(ctx: Context, report: str, now_ns: int | None = None, build_log: str = "") -> State:
    graph = build_graph(ctx)
    state: State = {"report": report, "now_ns": now_ns or time.time_ns()}
    if build_log:
        state["build_log"] = build_log
    return await graph.ainvoke(state)
