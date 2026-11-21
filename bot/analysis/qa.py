"""질문·조회. Claude가 읽기 전용 도구 8개 중에서 골라 쓰고 답한다.

도구 정의(name, description, input_schema)를 API 요청에 넣으면 Claude는 어떤 도구를 어떤 값으로 부를지만 정한다.
실행은 우리 코드가 하고, 결과를 다시 Claude에게 돌려준다. 쓰기 작업(PR 생성 등)은 도구 목록에 없다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import code_cache, embedding, llm, search, slack_client, slack_ui
from bot.db import jobs, store
from bot.tools import jenkins, logs, metrics

log = logging.getLogger("bot.qa")
KST = ZoneInfo("Asia/Seoul")
NS = 1_000_000_000
TOOL_LABELS = {
    "query_logs": "로그", "query_metrics": "지표", "get_jenkins_build_log": "빌드 로그", "search_code": "코드 검색",
    "get_call_graph": "호출 관계", "search_docs": "문서", "search_incidents": "장애 기록", "get_git_diff": "변경 내역",
}
OUT_OF_SCOPE = "이 봇은 프로젝트의 코드, 로그, 지표, 배포, 장애 기록에 대한 질문만 답합니다."

TOOLS = [
    {
        "name": "query_logs",
        "description": "서버 로그를 조회한다(Loki). 특정 시간대에 무슨 에러가 났는지, 어떤 로그가 찍혔는지 볼 때 쓴다. "
        "코드 내용이나 CPU·메모리 같은 수치를 볼 때는 쓰지 않는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "서비스 이름. 비우면 전체"},
                "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO"], "description": "로그 레벨. 비우면 전체"},
                "contains": {"type": "string", "description": "이 글자가 들어간 로그만"},
                "minutes": {"type": "integer", "description": "지금부터 몇 분 전까지 볼지 (기본 30, 최대 1440)"},
            },
        },
    },
    {
        "name": "query_metrics",
        "description": "서버와 애플리케이션의 수치를 조회한다(Prometheus). CPU, 메모리, 디스크, 요청 수, 5xx 수, 응답 시간을 볼 때 쓴다. "
        "로그 내용을 볼 때는 쓰지 않는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": list(metrics.TEMPLATES), "description": "; ".join(f"{k}: {v[0]}" for k, v in metrics.TEMPLATES.items())},
                "minutes": {"type": "integer", "description": "지금부터 몇 분 전까지 볼지 (기본 30)"},
            },
            "required": ["metric"],
        },
    },
    {
        "name": "get_jenkins_build_log",
        "description": "Jenkins 빌드 로그를 가져온다. 배포나 빌드가 왜 실패했는지 볼 때 쓴다. 서비스가 실행 중에 낸 에러는 query_logs로 본다.",
        "input_schema": {
            "type": "object",
            "properties": {"build_number": {"type": "integer", "description": "빌드 번호. 비우면 가장 최근 빌드"}, "job": {"type": "string", "description": "Jenkins 작업 이름. 비우면 배포 작업"}},
        },
    },
    {
        "name": "search_code",
        "description": "코드를 찾는다. '방 입장 처리하는 코드 어디야'처럼 기능이나 뜻으로 물을 때, 또는 메서드·클래스 이름 일부만 알 때 쓴다. "
        "정확한 이름을 이미 알고 호출 관계를 보려는 것이면 get_call_graph를 쓴다.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "찾을 기능 설명이나 이름"}, "service": {"type": "string", "description": "서비스 이름. 비우면 전체"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_call_graph",
        "description": "메서드·함수의 호출 관계를 본다. 이 메서드가 무엇을 호출하는지(callees), 누가 이 메서드를 호출하는지(callers) 알 때 쓴다. "
        "프론트 함수에서 시작하면 API 경로를 거쳐 백엔드 메서드까지 이어진다. symbol은 search_code 결과의 qualified_name이나 'GET /api/v1/rooms/{}' 같은 API 경로를 넣는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "direction": {"type": "string", "enum": ["callees", "callers"]},
                "depth": {"type": "integer", "description": "몇 단계까지 따라갈지 (기본 2, 최대 4)"},
            },
            "required": ["symbol", "direction"],
        },
    },
    {
        "name": "search_docs",
        "description": "프로젝트 문서(README, 포팅 매뉴얼)를 찾는다. 서버 구성, 포트, 배포 방법, 환경 설정처럼 문서에 적힌 내용을 물을 때 쓴다. 코드 동작은 search_code로 본다.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "search_incidents",
        "description": "봇이 처리한 장애 기록을 찾는다. '최근 장애 보여줘'처럼 시간 기준이면 mode=latest, '이런 에러 전에 있었어?'처럼 내용 기준이면 mode=similar 와 query를 쓴다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["latest", "similar"]},
                "query": {"type": "string", "description": "mode=similar 일 때 찾을 증상"},
                "service": {"type": "string"},
                "days": {"type": "integer", "description": "최근 며칠 안의 기록만"},
            },
            "required": ["mode"],
        },
    },
    {
        "name": "get_git_diff",
        "description": "최근 배포에서 무엇이 바뀌었는지 본다. '어제 배포에서 뭐 바뀌었어', '이 에러가 최근 변경 때문인가'를 확인할 때 쓴다.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string", "description": "이 경로 아래의 변경만. 비우면 전체"}, "commits": {"type": "integer", "description": "최근 커밋 몇 개를 볼지 (기본 5)"}}},
    },
]

SYSTEM = """너는 개발팀의 프로젝트 봇이다. 팀원이 Slack에서 프로젝트에 대해 묻는다. 도구로 사실을 확인한 뒤 답한다.

규칙
- 도구로 확인한 내용만 답한다. 확인하지 못했으면 모른다고 말한다.
- 쉬운 한국어로 짧게 답한다. 확인 과정이나 확인하지 못한 세부는 길게 늘어놓지 않는다. 꼭 필요하면 마지막에 한 줄로만 쓴다.
- 코드 위치는 파일 이름과 메서드 이름으로 적는다 (예: `OrderService.java`의 `create`). 폴더 경로 전체는 적지 않는다.

모양 (Slack 메시지로 나간다)
- 첫 문단은 결론 한두 문장이다. 꾸미지 않는다.
- 그 아래는 묶음으로 나눈다. 묶음은 2~3개를 넘기지 않고, 묶음 사이에는 빈 줄을 하나 둔다.
- 묶음마다 윗줄에 *라벨*만 따로 쓰고, 아랫줄부터 • 목록을 쓴다. 한 묶음의 목록은 5줄을 넘기지 않는다.
- 굵게(*…*)는 라벨 줄에만 쓴다. 문장 중간의 단어는 굵게 하지 않는다.
- 목록 안에 목록을 넣지 않는다. 한 항목은 한 줄로 쓴다.
- `코드`와 • 목록만 쓰고 표, # 제목, 번호 목록은 쓰지 않는다.
- 지금 시각: {now} (한국 시간)"""


def _fmt_time(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / NS, KST).strftime("%m-%d %H:%M:%S")


def build_handlers(ctx) -> dict:
    project = ctx["project"]
    redis = ctx["redis"]

    async def query_logs(args: dict) -> str:
        minutes = max(1, min(int(args.get("minutes") or 30), 1440))
        end = time.time_ns()
        services = [args["service"]] if args.get("service") else None
        entries = await logs.query_logs(services, end - minutes * 60 * NS, end, args.get("level"), args.get("contains"), limit=40)
        if not entries:
            return f"최근 {minutes}분 동안 조건에 맞는 로그가 없음"
        counts: dict = {}
        for e in entries:
            counts[(e.service, e.level)] = counts.get((e.service, e.level), 0) + 1
        head = ", ".join(f"{s} {lv} {n}건" for (s, lv), n in sorted(counts.items()))
        body = "\n".join(f"[{_fmt_time(e.ts_ns)} {e.service} {e.level}] {logs.trim(e.line, 12, 900)}" for e in entries[:12])
        return f"최근 {minutes}분, {len(entries)}건 ({head}). 최신순 12건:\n{body}"

    async def query_metrics(args: dict) -> str:
        return metrics.format_metric(await metrics.query_metric(args["metric"], int(args.get("minutes") or 30)))

    async def get_jenkins_build_log(args: dict) -> str:
        return await jenkins.build_log_text(args.get("job"), args.get("build_number"))

    async def search_code(args: dict) -> str:
        with store.connect() as conn:
            rows = await search.search_code(conn, args["query"], args.get("service"))
        if not rows:
            return "찾은 코드 없음"
        return "\n\n".join(
            f"qualified_name: {r['qualified_name']}\n서비스: {r['service']} / 위치: {r['file_path']}:{r['start_line']}-{r['end_line']}\n"
            f"설명: {r['description'] or '-'}\n```\n{r['content'][:1800]}\n```" for r in rows
        )

    async def get_call_graph(args: dict) -> str:
        depth = max(1, min(int(args.get("depth") or 2), 4))
        with store.connect() as conn:
            symbol = args["symbol"]
            if store.get_chunk(conn, symbol) is None and " /" not in symbol:
                # 짧은 이름으로 들어오면 이름이 맞는 청크를 찾는다
                row = conn.execute("SELECT qualified_name FROM code_chunk WHERE name = %s OR qualified_name LIKE %s ORDER BY length(qualified_name) LIMIT 1", (symbol, f"%{symbol}%")).fetchone()
                if row:
                    symbol = row["qualified_name"]
            steps = store.neighbors(conn, symbol, args["direction"], depth)
            if not steps:
                return f"{symbol}: 연결된 호출 관계 없음"
            lines = [f"{symbol} 의 {'호출 대상' if args['direction'] == 'callees' else '호출하는 쪽'} ({depth}단계까지)"]
            for s in steps:
                chunk = await code_cache.get(redis, conn, s["node"])
                where = f" ({chunk['service']}, {chunk['file_path']}:{chunk['start_line']})" if chunk else ""
                lines.append(f"{'  ' * (s['depth'] - 1)}- {s['node']}{where}")
        return "\n".join(lines)

    async def search_docs(args: dict) -> str:
        with store.connect() as conn:
            rows = await search.search_docs(conn, args["query"])
        return "\n\n".join(f"[{r['path']} > {r['heading']}]\n{r['content'][:1500]}" for r in rows) or "찾은 문서 없음"

    async def search_incidents(args: dict) -> str:
        vector = await embedding.embed(args["query"]) if args.get("mode") == "similar" and args.get("query") else None
        with store.connect() as conn:
            rows = jobs.search_incidents(conn, args.get("mode", "latest"), args.get("service"), args.get("days"), vector)
        if not rows:
            return "장애 기록 없음"
        return "\n".join(
            f"- #{r['id']} {str(r['created_at'])[:16]} [{r['status']}] {r['service'] or '-'} / 증상: {r['symptom']} / 원인: {r['cause']} / 해결: {r['resolution'] or '-'}"
            + (f" / PR: {r['pr_url']}" if r.get("pr_url") else "") for r in rows
        )

    async def get_git_diff(args: dict) -> str:
        repo = os.environ.get("REPO_DIR", "/data/repo")
        if not os.path.isdir(os.path.join(repo, ".git")):
            return "저장소 사본이 아직 없음"
        n = max(1, min(int(args.get("commits") or 5), 20))
        cmd = ["git", "-C", repo, "log", f"-{n}", "--date=format:%m-%d %H:%M", "--format=%h %ad %an %s", "--stat"]
        if args.get("path"):
            cmd += ["--", args["path"]]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return (out.decode("utf-8", "replace") or err.decode("utf-8", "replace"))[:6000] or "변경 없음"

    _ = project
    return {
        "query_logs": query_logs, "query_metrics": query_metrics, "get_jenkins_build_log": get_jenkins_build_log,
        "search_code": search_code, "get_call_graph": get_call_graph, "search_docs": search_docs,
        "search_incidents": search_incidents, "get_git_diff": get_git_diff,
    }


INTENT_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["error", "question", "out_of_scope"], "description": (
        "error: 무언가 고장 났거나 잘못 동작한다는 신고. 증상, 에러 메시지, 스택 트레이스가 적혀 있고 원인을 찾아야 한다. "
        "question: 정보를 달라는 요청. 코드 위치나 동작 방식 설명, 로그·지표·배포 기록·장애 기록 조회 "
        "('최근 에러 로그 보여 줘'처럼 에러라는 단어가 있어도 보여 달라는 요청이면 question). "
        "out_of_scope: 이 프로젝트의 코드, 로그, 지표, 배포, 장애, 문서와 무관한 것"
    )}},
    "required": ["intent"],
}


async def classify(text: str, usage: llm.Usage | None = None) -> str:
    """메시지가 오류 신고(error)인지, 질문·조회(question)인지, 범위 밖(out_of_scope)인지 고른다."""
    intent = await llm.structured(
        llm.HAIKU, "팀원이 프로젝트 봇에게 보낸 메시지의 종류를 고른다.", text[:3000],
        name="classify", description="메시지 종류를 기록한다", schema=INTENT_SCHEMA, max_tokens=100, usage=usage,
    )
    return intent.get("intent") or "question"


def with_context(question: str, history: str, result: dict | None) -> str:
    """이어서 묻는 질문에는 앞의 대화와 이 스레드의 분석 결과를 참고로 붙인다."""
    parts = []
    if history:
        parts.append("## 이 스레드에서 앞서 주고받은 내용 (참고용)\n" + history)
    if result:
        where = f"{result.get('file_path') or '-'}" + (f":{result['line']}" if result.get("line") else "")
        parts.append(f"## 이 스레드의 장애 분석 결과 (참고용)\n증상: {result.get('symptom')}\n원인: {result.get('cause')}\n위치: {where} {result.get('symbol') or ''}")
    if not parts:
        return question
    parts.append(
        "## 이번 질문\n" + question
        + "\n\n위의 참고 내용은 '그 메서드', '방금 그 에러'처럼 가리키는 말을 이해하는 데만 쓰고, 이번 질문에만 답한다. 앞에서 이미 답한 내용은 되풀이하지 않는다."
    )
    return "\n\n".join(parts)


async def answer(ctx, job_id: int, intent: str | None = None, question: str | None = None, history: str = "") -> None:
    """question 이 없으면 작업에 저장된 첫 글에 답하고, 있으면 스레드에서 이어 묻는 그 질문에 답한다."""
    with store.connect() as conn:
        job = jobs.get_job(conn, job_id)
    channel, thread = job["slack_channel"], job["slack_ts"]
    follow_up = question is not None
    question = (question if follow_up else job["state"].get("report") or "").strip()
    if not question:
        await slack_client.post_message(channel, "질문 내용이 없습니다. 봇을 부를 때 질문을 함께 적어 주세요.", thread_ts=thread)
        return
    usage = llm.Usage()
    started = time.time()
    try:
        if intent is None:
            intent = await classify(question, usage)
        if intent == "out_of_scope":
            await slack_client.post_message(channel, OUT_OF_SCOPE, thread_ts=thread)
            return
        used: list[str] = []
        status = await slack_client.post_message(channel, "*확인 중…*", thread_ts=thread, transient=True)

        async def on_tool(name: str, args: dict) -> None:
            used.append(name)
            if status.get("ts"):
                text = "*확인 중…*  " + " · ".join(TOOL_LABELS.get(u, u) for u in used)
                await slack_client.update_message(channel, status["ts"], text, blocks=[slack_client.section(text)])

        system = SYSTEM.format(now=datetime.now(KST).strftime("%Y-%m-%d %H:%M"))
        text = await llm.tool_loop(
            llm.SONNET, system, [{"role": "user", "content": with_context(question, history, job["state"].get("result") if follow_up else None)}],
            TOOLS, build_handlers(ctx),
            max_turns=8, max_tokens=1500, usage=usage, on_tool=on_tool,
        )
        elapsed = round(time.time() - started, 1)
        with store.connect() as conn:
            # 분석이 끝난 스레드에서 이어 물은 것이면 상태를 바꾸지 않는다 (AI 수정 버튼이 계속 눌려야 한다)
            status_after = "answered" if job["status"] in ("received", "answered") else None
            record = {"answer": text, "tools_used": used, "usage": usage.as_dict(), "elapsed_seconds": elapsed}
            if follow_up:
                # 이어 묻는 질문의 기록은 따로 둔다. 분석 결과의 걸린 시간·토큰 수를 덮어쓰지 않는다
                jobs.update_job(conn, job_id, status_after, last_answer={"question": question, **record})
            else:
                jobs.update_job(conn, job_id, status_after, **record)
        names = " · ".join(TOOL_LABELS.get(u, u) for u in dict.fromkeys(used)) or "도구 없이 답함"
        if status.get("ts"):
            # 진행 메시지는 지우고 답 하나만 남긴다
            await slack_client.delete_message(channel, status["ts"])
        shown, blocks = slack_ui.answer_message(text, elapsed, names)
        await slack_client.post_message(channel, shown, thread_ts=thread, blocks=blocks, kind="answer")
    except Exception as e:
        log.exception("qa failed job=%s", job_id)
        text, blocks = slack_ui.problem_message("답변", e)
        await slack_client.post_message(channel, text, thread_ts=thread, blocks=blocks)
