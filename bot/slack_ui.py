"""Slack에 올리는 메시지의 모양. 한눈에 읽히도록 만든다.

- 결과 메시지는 큰 제목 한 줄(결론) → 이름과 값(종류, 신뢰도, 위치) → 짧은 항목 → 버튼 → 작은 글씨 순서다.
- 제목 앞 동그라미 색으로 종류를 알린다 (코드 = 빨강, 수정 완료 = 초록 등).
- 색 띠(attachments)는 한두 줄짜리 알림에만 쓴다. 긴 내용을 넣으면 Slack이 "더 보기"로 접어 버튼이 가려진다.
- 진행 상황은 메시지 하나를 고쳐 쓰고, 결과가 나오면 그 메시지를 지워 결과 하나만 남긴다.
- 근거는 [근거 보기]를 눌렀을 때만 올린다.
"""
from __future__ import annotations

import re

from bot import slack_client

CAUSE_TYPES = {"code": "코드", "config": "서버 설정", "infra": "인프라", "external": "외부 서비스", "unknown": "판단 불가"}
CONFIDENCE = {"high": "높음", "medium": "보통", "low": "낮음"}

# 왼쪽 색 띠와 제목 앞 표시. 색만 보고도 종류를 알 수 있게 한다
COLORS = {
    "code": "#E24B4A", "config": "#E5B93C", "infra": "#E8833A", "external": "#7F77DD", "unknown": "#888780",
    "done": "#2EB67D", "failed": "#E24B4A", "info": "#378ADD", "muted": "#888780",
}
MARKS = {"code": "🔴", "config": "🟡", "infra": "🟠", "external": "🟣", "unknown": "⚪", "done": "🟢", "failed": "🔴", "info": "🔵", "muted": "⚪"}


def clean(text) -> str:
    """LLM 출력에 섞여 나오는 따옴표 앞의 역슬래시를 없애고 앞뒤 공백을 정리한다."""
    return str(text or "").replace('\\"', '"').replace("\\'", "'").strip()


def to_mrkdwn(text) -> str:
    """LLM이 쓴 Markdown을 Slack 표기로 바꾼다. Slack은 굵게를 *한 겹*으로 쓰고 제목(#) 문법이 없다."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", clean(text))
    return re.sub(r"(?m)^#{1,6}\s+(.+)$", r"*\1*", text)


def short(text, limit: int = 140) -> str:
    text = " ".join(clean(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def context(*items: str) -> dict:
    """작은 회색 글씨 한 줄. 걸린 시간처럼 덜 중요한 정보에 쓴다."""
    text = "  ·  ".join(i for i in items if i)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def card(kind: str, blocks: list) -> list:
    """색 띠가 붙은 영역 하나. kind 는 COLORS 의 이름."""
    return [{"color": COLORS.get(kind, COLORS["muted"]), "blocks": blocks}]


def notice(kind: str, text: str) -> list:
    """한두 줄짜리 알림 (배포 시작, 배포 성공 등)."""
    return card(kind, [slack_client.section(text)])


def bullets(items, limit: int = 60, numbered: bool = False) -> str:
    lines = [short(re.sub(r"^\s*(\d+[.)]|[-•])\s*", "", str(i)), limit) for i in items if str(i).strip()]
    return "\n".join(f"{n}. {line}" if numbered else f"• {line}" for n, line in enumerate(lines, 1))


def _sentences(text) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean(text)) if s.strip()]


class Progress:
    """진행 상황을 스레드의 메시지 하나에 고쳐 쓴다.

    inline=True  → 한 줄:   분석 중…  ✓ 정보 추출 · ✓ 로그 조회 · ▸ 코드 찾기
    inline=False → 여러 줄: 단계가 오래 걸리는 작업(Jenkins 테스트 대기)에 쓴다
    """

    def __init__(self, channel: str, thread: str, title: str = "분석 중", inline: bool = True):
        self.channel, self.thread, self.title, self.inline = channel, thread, title, inline
        self.ts: str | None = None
        self.steps: list[str] = []

    def _render(self, finished: str | None = None) -> str:
        if finished is not None:
            return finished
        done, current = self.steps[:-1], self.steps[-1]
        if self.inline:
            parts = [f"✓ {s}" for s in done] + [f"▸ {current}"]
            return f"*{self.title}…*  " + " · ".join(parts)
        return "\n".join([f"*{self.title}*"] + [f"✓ {s}" for s in done] + [f"▸ {current}"])

    async def _show(self, body: str, kind: str = "info") -> None:
        if self.ts is None:
            resp = await slack_client.post_message(self.channel, body, thread_ts=self.thread, attachments=notice(kind, body))
            self.ts = resp.get("ts")
        else:
            await slack_client.update_message(self.channel, self.ts, body, attachments=notice(kind, body))

    async def __call__(self, label: str) -> None:
        """다음 단계로 넘어간다. 앞 단계는 끝난 것으로 표시된다."""
        self.steps.append(label)
        await self._show(self._render())

    async def replace(self, label: str) -> None:
        """지금 단계의 글을 바꾼다 (예: '수정 전 테스트 실행 중' → '수정 전 테스트 실패 확인')."""
        if self.steps:
            self.steps[-1] = label
        else:
            self.steps.append(label)
        await self._show(self._render())

    async def done(self, text: str, kind: str = "done") -> None:
        """끝났을 때 한 줄로 접는다. 여러 줄 모드에서는 거친 단계를 남긴다."""
        if self.inline or not self.steps:
            await self._show(text, kind)
        else:
            await self._show("\n".join([text] + [f"✓ {s}" for s in self.steps]), kind)

    async def remove(self) -> None:
        """결과 메시지를 따로 올릴 때 진행 메시지를 지운다. 스레드에 결과 하나만 남는다."""
        if self.ts:
            await slack_client.delete_message(self.channel, self.ts)
            self.ts = None


def pairs_text(*pairs: tuple[str, str]) -> str:
    """이름: 값 을 한 줄에 하나씩. 이름은 굵게 쓰고 콜론을 붙여, 값과 한 문장처럼 읽히지 않게 한다."""
    return "\n".join(f"*{k}:*  {v}" for k, v in pairs if v)


def location(result: dict) -> str:
    path = result.get("file_path")
    if not path:
        return ""
    name = path.rsplit("/", 1)[-1]
    where = f"`{name}" + (f":{result['line']}" if result.get("line") else "") + "`"
    symbol = clean(result.get("symbol"))
    return where + (f" {symbol}" if symbol else "")


def report_message(result: dict, state: dict, job_id: int, elapsed: float | None = None) -> tuple[str, list]:
    """원인 분석 결과. (알림에 쓰는 한 줄, blocks)"""
    kind = result.get("cause_type") if result.get("cause_type") in CAUSE_TYPES else "unknown"
    title = short(result.get("title") or result.get("symptom"), 80)
    blocks: list = [slack_client.header(f"{MARKS[kind]} {title}")]

    incidents = state.get("incidents") or []
    blocks.append(slack_client.section(pairs_text(
        # 값은 상자 글씨(`…`)로 써서 이름과 눈으로 구분되게 한다
        ("원인 종류", f"`{CAUSE_TYPES[kind]}`"),
        ("신뢰도", f"`{CONFIDENCE.get(result.get('confidence'), '-')}`"),
        ("위치", location(result)),
        ("비슷한 과거 장애", f"`{len(incidents)}건`" if incidents else ""),
    )))

    points = result.get("points") or _sentences(result.get("cause"))[:3]
    if points:
        label = "확인한 것" if kind == "unknown" else "원인"
        blocks.append(slack_client.section(f"*{label}*\n{bullets(points[:3], 70)}"))
    blocks.append(slack_client.divider())

    job = str(job_id)
    if kind == "code":
        if result.get("fix"):
            blocks.append(slack_client.section(f"*수정 방향*\n{short(result['fix'], 200)}"))
        blocks.append(slack_client.buttons(("AI로 수정", "fix_with_ai", job), ("직접 수정", "fix_manually", job), ("근거 보기", "show_evidence", job)))
    else:
        steps = result.get("steps") or [s for s in clean(result.get("action")).splitlines() if s.strip()]
        if steps:
            blocks.append(slack_client.section(f"*다음 할 일*\n{bullets(steps[:3], 80, numbered=True)}"))
        blocks.append(slack_client.buttons(("해결됨", "mark_resolved", job), ("근거 보기", "show_evidence", job)))

    code = state.get("code") or []
    if elapsed or code:
        blocks.append(context(f"{elapsed}초" if elapsed else "", f"코드 {len(code)}개 확인" if code else ""))
    return f"{title} ({CAUSE_TYPES[kind]})", blocks


def evidence_text(state: dict) -> str:
    """[근거 보기]를 눌렀을 때 올리는 내용: 근거, 확인한 코드, 서버 로그의 첫 줄, 비슷한 과거 장애."""
    result = state.get("result") or {}
    parts: list[str] = []
    if result.get("symptom"):
        parts.append(f"*증상*  {short(result['symptom'], 200)}")
    if result.get("cause"):
        parts.append(f"*원인 설명*  {short(result['cause'], 400)}")
    if result.get("evidence"):
        parts.append("*근거*\n" + "\n".join(f"• {short(e, 220)}" for e in result["evidence"][:4]))
    code = state.get("code") or []
    if code:
        lines = [f"• `{c['name']}`  {c['file_path']}:{c['start_line']}" for c in code[:8]]
        more = f"\n외 {len(code) - 8}개" if len(code) > 8 else ""
        parts.append(f"*확인한 코드 {len(code)}개*\n" + "\n".join(lines) + more)
    logs = state.get("logs") or []
    if logs:
        parts.append("*서버 로그*\n" + "\n".join(f"• [{e['service']} {e['time']}] {short(e['line'].splitlines()[0][-200:], 200)}" for e in logs[:3]))
    incidents = state.get("incidents") or []
    if incidents:
        parts.append("*비슷한 과거 장애*\n" + "\n".join(
            f"• {str(i.get('created_at', ''))[:10]} {short(i.get('symptom'), 80)} → {short(i.get('resolution') or i.get('cause') or '-', 120)}" for i in incidents
        ))
    usage = state.get("usage") or {}
    if state.get("elapsed_seconds"):
        parts.append(f"_분석 {state['elapsed_seconds']}초, 입력 토큰 {usage.get('input_tokens', 0):,} / 출력 {usage.get('output_tokens', 0):,}_")
    return "\n\n".join(parts) or "남아 있는 근거가 없습니다."


def fix_result_message(job_id: int, fix: dict, summary: dict) -> tuple[str, list]:
    """AI 수정 결과. summary: elapsed, attempts, before, after, changed, apis, pr_url, branch"""
    blocks: list = [slack_client.header(f"{MARKS['done']} 수정 완료")]
    blocks.append(slack_client.section(pairs_text(
        ("검증", f"수정 전 실패 #{summary['before']} → 수정 후 통과 #{summary['after']}"),
        ("바뀐 파일", ", ".join(f"`{c.rsplit('/', 1)[-1]}`" for c in summary["changed"][:5])),
        ("영향받는 API", ", ".join(f"`{a}`" for a in summary["apis"][:3])),
        ("PR", summary.get("pr_url") or ""),
        ("브랜치", "" if summary.get("pr_url") else f"`{summary['branch']}`"),
    )))
    blocks.append(slack_client.section(f"*바꾼 내용*\n{short(fix.get('explanation'), 300)}"))
    if summary.get("pr_url"):
        blocks.append(context("PR을 확인하고 merge하면 해결로 기록합니다."))
    else:
        blocks.append(slack_client.buttons(("해결됨", "mark_resolved", str(job_id))))
    blocks.append(context(f"{summary['elapsed']}초", f"{summary['attempts']}번째 시도에 통과" if summary["attempts"] > 1 else ""))
    return f"수정 완료 ({summary['elapsed']}초)", blocks


def answer_message(text: str, elapsed: float, tools: str) -> tuple[str, list]:
    """질문·조회의 답. 답은 본문 그대로 보여 주고, 걸린 시간과 쓴 도구는 작은 글씨로 붙인다."""
    shown = to_mrkdwn(text) or "답을 만들지 못했습니다."
    return shown, [slack_client.section(shown), context(f"{elapsed}초", tools)]
