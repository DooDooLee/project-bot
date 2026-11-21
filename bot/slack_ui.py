"""Slack에 올리는 메시지의 모양. 한눈에 읽히도록 짧게 만든다.

- 진행 상황은 메시지 하나를 고쳐 쓴다 (단계마다 새 메시지를 올리지 않는다).
- 결과는 결론 → 한 줄 정보 → 다음 행동 순서로 보여 주고, 근거는 [근거 보기]를 눌렀을 때만 올린다.
"""
from __future__ import annotations

import re

from bot import slack_client

CAUSE_TYPES = {"code": "코드", "config": "서버 설정", "infra": "인프라", "external": "외부 서비스", "unknown": "판단 불가"}
CONFIDENCE = {"high": "높음", "medium": "보통", "low": "낮음"}


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
    """작은 회색 글씨 한 줄. 종류, 확신, 위치 같은 부가 정보에 쓴다."""
    text = "  ·  ".join(i for i in items if i)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


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

    async def _show(self, body: str) -> None:
        if self.ts is None:
            resp = await slack_client.post_message(self.channel, body, thread_ts=self.thread, blocks=[slack_client.section(body)])
            self.ts = resp.get("ts")
        else:
            await slack_client.update_message(self.channel, self.ts, body, blocks=[slack_client.section(body)])

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

    async def done(self, text: str) -> None:
        """끝났을 때 한 줄로 접는다. 여러 줄 모드에서는 거친 단계를 남긴다."""
        if self.inline or not self.steps:
            await self._show(text)
        else:
            await self._show("\n".join([text] + [f"✓ {s}" for s in self.steps]))


def location(result: dict) -> str:
    path = result.get("file_path")
    if not path:
        return ""
    where = f"`{path}" + (f":{result['line']}" if result.get("line") else "") + "`"
    symbol = clean(result.get("symbol"))
    return where + (f" {symbol}" if symbol else "")


def report_blocks(result: dict, state: dict, job_id: int) -> tuple[str, list]:
    """원인 분석 결과. 결론과 다음 행동만 보여 주고 근거는 버튼 뒤에 둔다."""
    kind = CAUSE_TYPES.get(result.get("cause_type"), "판단 불가")
    title = short(result.get("title") or result.get("symptom"), 80)
    head = f"*{title}*\n{short(result.get('cause'), 400)}"
    blocks: list = [slack_client.section(head)]
    incidents = state.get("incidents") or []
    blocks.append(context(
        f"원인 종류: *{kind}*",
        f"확신: {CONFIDENCE.get(result.get('confidence'), '-')}",
        location(result),
        f"비슷한 과거 장애 {len(incidents)}건" if incidents else "",
    ))
    if result.get("cause_type") == "code":
        if result.get("fix"):
            blocks.append(slack_client.section(f"*수정 방향*  {short(result['fix'], 300)}"))
        blocks.append(slack_client.buttons(
            ("AI로 수정", "fix_with_ai", str(job_id)), ("직접 수정", "fix_manually", str(job_id)), ("근거 보기", "show_evidence", str(job_id)),
        ))
    else:
        if result.get("action"):
            blocks.append(slack_client.section(f"*조치 방법*\n{clean(result['action'])[:900]}"))
        blocks.append(slack_client.buttons(("해결됨", "mark_resolved", str(job_id)), ("근거 보기", "show_evidence", str(job_id)), style_first=True))
    return f"{title} — {short(result.get('cause'), 200)}", blocks


def evidence_text(state: dict) -> str:
    """[근거 보기]를 눌렀을 때 올리는 내용: 근거, 확인한 코드, 서버 로그의 첫 줄, 비슷한 과거 장애."""
    result = state.get("result") or {}
    parts: list[str] = []
    if result.get("symptom"):
        parts.append(f"*증상*  {short(result['symptom'], 200)}")
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


def fix_result_blocks(job_id: int, fix: dict, summary: dict) -> tuple[str, list]:
    """AI 수정 결과. summary: elapsed, attempts, before, after, changed, apis, pr_url, branch"""
    head = f"*수정 완료*  ·  {summary['elapsed']}초" + (f"  ·  {summary['attempts']}번째 시도에 통과" if summary["attempts"] > 1 else "")
    blocks: list = [slack_client.section(f"{head}\n{short(fix.get('explanation'), 400)}")]
    files = ", ".join(f"`{c.rsplit('/', 1)[-1]}`" for c in summary["changed"][:5])
    blocks.append(context(
        f"검증: 수정 전 실패 (#{summary['before']}) → 수정 후 통과 (#{summary['after']})",
        f"바뀐 파일: {files}",
        ("영향받는 API: " + ", ".join(f"`{a}`" for a in summary["apis"][:3])) if summary["apis"] else "",
    ))
    if summary.get("pr_url"):
        blocks.append(slack_client.section(f"*PR*  {summary['pr_url']}\n확인하고 merge하면 해결로 기록합니다."))
    else:
        blocks.append(slack_client.section(f"*브랜치*  `{summary['branch']}`\nGitHub 토큰이 없어 PR은 만들지 않았습니다."))
        blocks.append(slack_client.buttons(("해결됨", "mark_resolved", str(job_id))))
    return f"수정 완료 ({summary['elapsed']}초)", blocks
