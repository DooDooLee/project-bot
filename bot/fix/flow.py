"""AI로 수정.

1. Sonnet이 수정 코드와 그 버그를 재현하는 테스트를 쓴다.
2. 테스트만 먼저 올려 Jenkins에서 돌린다. 여기서 실패해야 버그를 재현한 것이다.
3. 수정을 올려 다시 돌린다. 여기서 통과해야 고친 것이다.
4. 실패하면 실패 로그를 주고 다시 고치게 한다 (최대 3번). 재시도 때 테스트는 바꾸지 않는다.
5. 통과하면 PR을 만든다. merge는 사람이 한다.

AI는 코드를 쓰기만 하고, 실행과 판정은 Jenkins가 한다.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from bot import github, llm, repo, slack_client, slack_ui
from bot.config import ProjectConfig, ServiceConfig
from bot.db import jobs, store
from bot.tools import jenkins

log = logging.getLogger("bot.fix")
MAX_ATTEMPTS = 3
_DANGEROUS_SQL = re.compile(r"(?i)\b(drop\s+(table|database|schema)|truncate\s+table|delete\s+from)\b")

SYSTEM = """너는 장애 원인이 분석된 버그를 고치는 엔지니어다. submit_fix 도구로 수정 코드와 재현 테스트를 제출한다.

수정 규칙
- 원인을 고치는 데 필요한 만큼만 바꾼다. 관련 없는 정리, 이름 변경, 포맷 변경을 하지 않는다.
- edits의 old_string은 파일에 정확히 한 번 나오는 글이어야 한다. 공백과 줄바꿈까지 그대로 복사한다.
- 새 파일을 만들 때는 old_string을 빈 글로 두고 new_string에 파일 전체를 쓴다.
- 서비스의 소스 폴더 안의 파일만 고친다. 빌드 설정, 배포 설정, 환경 설정 파일은 고치지 않는다.
- 프로젝트에 이미 있는 라이브러리만 쓴다.

테스트 규칙
- 이 버그를 재현하는 테스트를 하나 쓴다. 수정 전 코드에서는 실패하고 수정 후 코드에서는 통과해야 한다.
- Java: JUnit 5와 Mockito로 쓰는 단위 테스트. Spring 컨텍스트나 DB를 띄우지 않는다 (@SpringBootTest 금지).
  클래스 이름은 Test로 끝나고, 패키지는 테스트 대상과 같게 한다. 수정으로 새로 만드는 클래스를 테스트에서 직접 써도 된다.
- JavaScript: Vitest와 @testing-library/react. 파일 이름은 .test.jsx 또는 .test.js, 서버 응답은 vi.mock으로 대체한다.
- 테스트에서 DROP, TRUNCATE, DELETE 같은 SQL을 쓰지 않는다.

Jenkins가 실패를 돌려주면 로그를 읽고 수정만 다시 제출한다. 테스트는 처음 통과한 것을 그대로 둔다."""

FIX_TOOL = {
    "name": "submit_fix",
    "description": "수정 코드와 재현 테스트를 제출한다",
    "input_schema": {
        "type": "object",
        "properties": {
            "explanation": {"type": "string", "description": "무엇을 어떻게 바꿨는지 두 문장 이내. Slack에 그대로 나간다"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "저장소 기준 경로"},
                        "old_string": {"type": "string", "description": "바꿀 부분. 파일에 정확히 한 번 나와야 한다. 새 파일이면 빈 글"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            },
            "test_file_path": {"type": "string", "description": "테스트 파일의 저장소 기준 경로"},
            "test_content": {"type": "string", "description": "테스트 파일 전체 내용"},
            "test_name": {"type": "string", "description": "Java: 패키지를 포함한 테스트 클래스 이름. JavaScript: 테스트 파일 경로(서비스 폴더 기준)"},
            "commit_message": {"type": "string", "description": "한국어 한 줄. 무엇을 고쳤는지"},
        },
        "required": ["explanation", "edits", "test_file_path", "test_content", "test_name", "commit_message"],
    },
}


class Rejected(Exception):
    """제출한 수정이 규칙에 맞지 않음. 이유를 모델에게 돌려준다."""


def service_of(project: ProjectConfig, path: str) -> ServiceConfig | None:
    for svc in project.services:
        if path.startswith(svc.path.rstrip("/") + "/"):
            return svc
    return None


def validate(project: ProjectConfig, root: Path, fix: dict, check_test: bool = True) -> ServiceConfig:
    if not fix.get("edits"):
        raise Rejected("edits가 비어 있음")
    services = set()
    for e in fix["edits"]:
        path = e["file_path"].lstrip("/")
        svc = service_of(project, path)
        if svc is None or ".." in path:
            raise Rejected(f"{path}: 서비스 폴더 밖의 파일은 고칠 수 없음")
        allowed = [f"{svc.path}/{d}".rstrip("/.") + "/" for d in svc.source_dirs]
        if not any(path.startswith(a) for a in allowed):
            raise Rejected(f"{path}: 소스 폴더({', '.join(allowed)}) 안의 파일만 고칠 수 있음")
        services.add(svc.name)
        file = root / path
        if e["old_string"] == "":
            if file.exists():
                raise Rejected(f"{path}: 이미 있는 파일. old_string에 바꿀 부분을 적어야 함")
            continue
        if not file.exists():
            raise Rejected(f"{path}: 파일이 없음")
        count = file.read_text(encoding="utf-8").count(e["old_string"])
        if count != 1:
            raise Rejected(f"{path}: old_string이 파일에 {count}번 나옴. 정확히 한 번 나오도록 앞뒤 줄을 더 포함해야 함")
    if len(services) != 1:
        raise Rejected("한 번에 한 서비스만 고칠 수 있음")
    svc = project.service(services.pop())
    if check_test:
        tpath = fix["test_file_path"].lstrip("/")
        if svc.language == "java":
            if not tpath.startswith(f"{svc.path}/src/test/") or not tpath.endswith("Test.java"):
                raise Rejected(f"테스트 파일은 {svc.path}/src/test/ 아래에 있고 이름이 Test.java로 끝나야 함")
            if "@SpringBootTest" in fix["test_content"]:
                raise Rejected("@SpringBootTest는 쓸 수 없음. JUnit 5와 Mockito 단위 테스트로 작성")
        else:
            if not tpath.startswith(f"{svc.path}/") or not re.search(r"\.test\.jsx?$", tpath):
                raise Rejected(f"테스트 파일은 {svc.path}/ 아래에 있고 이름이 .test.jsx 또는 .test.js로 끝나야 함")
        if _DANGEROUS_SQL.search(fix["test_content"]):
            raise Rejected("테스트에 DROP, TRUNCATE, DELETE 문을 쓸 수 없음")
    return svc


def apply_edits(root: Path, fix: dict) -> list[str]:
    changed = []
    for e in fix["edits"]:
        file = root / e["file_path"].lstrip("/")
        if e["old_string"] == "":
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(e["new_string"], encoding="utf-8")
        else:
            file.write_text(file.read_text(encoding="utf-8").replace(e["old_string"], e["new_string"], 1), encoding="utf-8")
        path = e["file_path"].lstrip("/")
        if path not in changed:  # 한 파일을 여러 군데 고쳐도 한 번만 적는다
            changed.append(path)
    return changed


def test_outcome(result: dict) -> str:
    """passed | test_failed | build_error | no_tests.

    테스트가 실패한 것, 빌드가 깨진 것, 테스트를 아예 찾지 못한 것을 구분한다.
    셋 다 Jenkins에서는 실패로 끝나지만, '버그를 재현했다'고 볼 수 있는 것은 test_failed 뿐이다.
    """
    if result["result"] == "SUCCESS":
        return "passed"
    text = result["log"]
    if re.search(r"No test files found|No tests found for given includes|no tests? (?:were )?(?:found|ran)", text, re.I):
        return "no_tests"
    if re.search(r"compile\w*Java FAILED|compile\w*Kotlin FAILED|error: |Could not resolve|SyntaxError|Failed to resolve import|Transform failed", text):
        return "build_error"
    if re.search(r"\bFAILED\b|\bFAIL\b|There were failing tests|\d+ failed|AssertionError|AssertionFailedError|TestingLibraryElementError|✗|×", text):
        return "test_failed"
    return "build_error"


def missing_new_symbol(fix: dict, log_text: str) -> bool:
    """수정 전 빌드 실패가 '수정으로 새로 만드는 파일이 아직 없어서'인지 로그로 확인한다.

    새 파일의 이름(예: PurchaseException, NotFoundPage)이 실패 로그에 나와야 한다.
    다른 이유로 빌드가 깨진 것을 재현 성공으로 잘못 인정하지 않기 위해서다.
    """
    names = [Path(e["file_path"]).stem for e in fix["edits"] if e["old_string"] == ""]
    return any(name and name in log_text for name in names)


def _read(root: Path, path: str, limit: int = 14000) -> str:
    file = root / path
    return file.read_text(encoding="utf-8", errors="replace")[:limit] if file.exists() else ""


def build_context(project: ProjectConfig, root: Path, job: dict) -> str:
    result = job["state"].get("result") or {}
    code = job["state"].get("code") or []
    logs = job["state"].get("logs") or []
    parts = [
        "## 분석된 원인",
        f"증상: {result.get('symptom')}\n원인: {result.get('cause')}\n위치: {result.get('symbol')} ({result.get('file_path')}:{result.get('line')})\n수정 방향: {result.get('fix')}",
    ]
    if logs:
        parts.append("## 서버 에러 로그\n" + logs[0]["line"][:3000])
    files: list[str] = []
    for p in [result.get("file_path")] + [c["file_path"] for c in code]:
        if p and p not in files:
            files.append(p)
    budget = 60000
    parts.append("## 관련 파일 (전체 내용)")
    for p in files[:8]:
        text = _read(root, p)
        if not text or budget <= 0:
            continue
        budget -= len(text)
        parts.append(f"### {p}\n```\n{text}\n```")
    svc = service_of(project, result.get("file_path") or (files[0] if files else ""))
    if svc is not None:
        if svc.language == "java":
            parts.append(f"## 빌드 설정 ({svc.path}/build.gradle)\n```\n{_read(root, svc.path + '/build.gradle', 4000)}\n```")
            tests = sorted((root / svc.path / "src/test").rglob("*Test.java")) if (root / svc.path / "src/test").exists() else []
        else:
            parts.append(f"## 패키지 설정 ({svc.path}/package.json)\n```\n{_read(root, svc.path + '/package.json', 3000)}\n```")
            tests = sorted((root / svc.path).rglob("*.test.js*"))
            tests = [t for t in tests if "node_modules" not in t.parts]
        if tests:
            example = tests[0]
            parts.append(f"## 기존 테스트 예시 ({example.relative_to(root).as_posix()})\n```\n{example.read_text(encoding='utf-8', errors='replace')[:3000]}\n```")
        else:
            parts.append("## 기존 테스트\n이 서비스에는 아직 테스트가 없다. 표준 위치에 새로 만든다.")
    return "\n\n".join(parts)


async def ask(messages: list[dict], usage: llm.Usage) -> tuple[dict, str]:
    """수정을 받는다. 대화를 이어 가므로 앞부분은 캐시에서 읽힌다."""
    for _ in range(2):
        resp = await llm.call(llm.SONNET, SYSTEM + " 답은 반드시 submit_fix 도구를 호출해서 제출한다.", messages, tools=[FIX_TOOL], max_tokens=8000, usage=usage, cache=True)
        messages.append({"role": "assistant", "content": resp.content})
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input), block.id
        messages.append({"role": "user", "content": "submit_fix 도구를 호출해서 제출해 주세요."})
    raise RuntimeError("모델이 수정을 제출하지 않음")


def feedback(messages: list[dict], tool_use_id: str, text: str) -> None:
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text[:10000], "is_error": True}]})


async def impact(conn, symbols: list[str]) -> list[str]:
    """고친 메서드를 호출하는 쪽을 콜 그래프에서 거꾸로 따라가 영향받는 API를 찾는다."""
    apis: list[str] = []
    for qn in symbols:
        for step in store.neighbors(conn, qn, "callers", depth=4, limit=80):
            node = step["node"]
            if " /" in node and node not in apis:
                apis.append(node)
    return apis[:15]


async def run(ctx, job_id: int) -> None:
    project: ProjectConfig = ctx["project"]
    with store.connect() as conn:
        if not jobs.claim(conn, job_id, ("analyzed", "fix_failed"), "fixing"):
            return
        job = jobs.get_job(conn, job_id)
    channel, thread = job["slack_channel"], job["slack_ts"]

    # 진행 상황은 메시지 하나를 고쳐 쓴다. Jenkins 테스트를 기다리는 시간이 길어 단계를 여러 줄로 보여 준다
    progress = slack_ui.Progress(channel, thread, f"AI 수정 중  ·  브랜치 `bot/fix-{job_id}`", inline=False)

    def tag(attempt: int) -> str:
        """두 번째 시도부터만 몇 번째 시도인지 붙인다."""
        return f"({attempt}번째 시도) " if attempt > 1 else ""

    usage = llm.Usage()
    started = time.time()
    branch = f"bot/fix-{job_id}"
    tests_repo = os.environ.get("JENKINS_TEST_REPO_URL") or project.repo_url
    history: list[dict] = []
    try:
        if not jenkins.configured():
            raise RuntimeError("Jenkins가 연결되어 있지 않아 테스트를 돌릴 수 없음")
        await progress("수정안과 재현 테스트 작성")
        root = await repo.new_worktree(project, branch)
        messages: list[dict] = [{"role": "user", "content": build_context(project, root, job)}]
        test_ok = False
        test_commit = ""
        before_number = 0
        fix: dict = {}
        svc: ServiceConfig | None = None
        tool_id = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                await progress(f"{tag(attempt)}수정안 다시 작성")
            fix_new, tool_id = await ask(messages, usage)
            if test_ok:  # 재시도 때 테스트는 처음 통과한 것을 그대로 쓴다
                fix_new["test_file_path"], fix_new["test_content"], fix_new["test_name"] = fix["test_file_path"], fix["test_content"], fix["test_name"]
            fix = fix_new
            try:
                if test_ok:
                    await repo.git("reset", "-q", "--hard", test_commit, cwd=root)
                svc = validate(project, root, fix, check_test=not test_ok)
            except Rejected as e:
                history.append({"attempt": attempt, "stage": "validate", "outcome": str(e)})
                await progress.replace(f"{tag(attempt)}제출이 규칙에 맞지 않아 거부: {slack_ui.short(e, 90)}")
                feedback(messages, tool_id, f"제출이 거부됨: {e}")
                continue

            if svc.language == "java":
                test_filter = fix["test_name"]
            else:
                # Vitest는 서비스 폴더 안에서 돌기 때문에, 저장소 기준 경로에서 서비스 폴더를 뗀 경로를 넘긴다
                test_filter = fix["test_file_path"].lstrip("/").removeprefix(svc.path.rstrip("/") + "/")
            if not test_ok:
                # 1단계: 테스트만 올려서 수정 전 코드에서 실패하는지 본다
                tfile = root / fix["test_file_path"].lstrip("/")
                tfile.parent.mkdir(parents=True, exist_ok=True)
                tfile.write_text(fix["test_content"], encoding="utf-8")
                # 수정으로 새로 만드는 클래스를 테스트가 쓰면, 수정 전에는 컴파일이 안 된다. 이 경우도 '수정 전 실패'로 본다
                test_commit = await repo.commit_all(root, f"재현 테스트 추가: {Path(fix['test_file_path']).name}")
                await repo.push(project, root, branch)
                await progress(f"{tag(attempt)}수정 전 코드로 재현 테스트 실행 중")
                before = await jenkins.run_and_wait(jenkins.test_job(), {"REPO_URL": tests_repo, "BRANCH": branch, "SERVICE": svc.name, "TESTS": test_filter})
                outcome = test_outcome(before)
                history.append({"attempt": attempt, "stage": "before_fix", "build": before["number"], "outcome": outcome})
                if outcome == "passed":
                    await progress.replace(f"{tag(attempt)}수정 전에도 테스트가 통과함 → 재현하지 못해 거부 (Jenkins #{before['number']})")
                    await repo.git("reset", "-q", "--hard", "HEAD~1", cwd=root)
                    feedback(messages, tool_id, "테스트가 수정 전 코드에서도 통과함. 버그를 재현하지 못함. 수정 전에는 실패하는 테스트로 다시 제출.")
                    continue
                if outcome == "no_tests":
                    # 테스트가 실행되지도 않았다. 실패로 끝났어도 버그를 재현한 것이 아니다
                    await progress.replace(f"{tag(attempt)}Jenkins가 테스트를 찾지 못함 → 재현으로 인정하지 않음 (Jenkins #{before['number']})")
                    await repo.git("reset", "-q", "--hard", "HEAD~1", cwd=root)
                    feedback(messages, tool_id, f"테스트 실행기가 테스트를 찾지 못함. 테스트 파일 위치와 이름, test_name을 확인해 다시 제출.\n{before['log'][-3000:]}")
                    continue
                if outcome == "build_error" and not missing_new_symbol(fix, before["log"]):
                    await progress.replace(f"{tag(attempt)}테스트가 빌드되지 않아 거부 (Jenkins #{before['number']})")
                    await repo.git("reset", "-q", "--hard", "HEAD~1", cwd=root)
                    feedback(messages, tool_id, f"테스트가 수정 전 코드에서 컴파일되지 않음. 테스트를 고쳐 다시 제출.\n{before['log'][-5000:]}")
                    continue
                test_ok = True
                if outcome == "build_error":
                    await progress.replace(f"수정 전 실패 확인: 새로 만드는 파일이 없어 빌드되지 않음 (Jenkins #{before['number']})")
                else:
                    await progress.replace(f"수정 전 실패 확인 (Jenkins #{before['number']})")
                before_number = before["number"]

            # 2단계: 수정을 올려서 통과하는지 본다
            changed = apply_edits(root, fix)
            await repo.commit_all(root, fix["commit_message"].splitlines()[0][:100])
            await repo.push(project, root, branch)
            await progress(f"{tag(attempt)}수정 후 코드로 테스트 실행 중")
            after_filter = "*Test" if svc.language == "java" else ""
            after = await jenkins.run_and_wait(jenkins.test_job(), {"REPO_URL": tests_repo, "BRANCH": branch, "SERVICE": svc.name, "TESTS": after_filter})
            outcome = test_outcome(after)
            history.append({"attempt": attempt, "stage": "after_fix", "build": after["number"], "outcome": outcome})
            if outcome == "passed":
                await progress.replace(f"수정 후 통과 확인 (Jenkins #{after['number']})")
                await finish(ctx, job, project, root, branch, fix, changed, history, usage, started, before_number, after["number"], progress)
                return
            await progress.replace(f"{tag(attempt)}수정 후에도 테스트 실패 (Jenkins #{after['number']})")
            feedback(messages, tool_id, f"Jenkins 테스트 실패 (#{after['number']}). 로그를 보고 수정만 다시 제출. 테스트는 그대로 둔다.\n{after['log'][-7000:]}")

        with store.connect() as conn:
            jobs.update_job(conn, job_id, "fix_failed", fix={"branch": branch, "history": history, "usage": usage.as_dict()})
        await progress.done("*AI 수정 실패*", "failed")
        text, blocks = slack_ui.fix_failed_message(job_id, MAX_ATTEMPTS, branch)
        await slack_client.post_message(channel, text, thread_ts=thread, blocks=blocks)
    except Exception as e:
        log.exception("fix failed job=%s", job_id)
        with store.connect() as conn:
            jobs.update_job(conn, job_id, "fix_failed", fix={"branch": branch, "history": history, "error": str(e)[:500], "usage": usage.as_dict()})
        text, blocks = slack_ui.problem_message("수정", e, ("다시 시도", "fix_with_ai", str(job_id)))
        await slack_client.post_message(channel, text, thread_ts=thread, blocks=blocks)


async def finish(ctx, job, project, root, branch, fix, changed, history, usage, started, before_number, build_number, progress) -> None:
    job_id = job["id"]
    result = job["state"].get("result") or {}
    elapsed = round(time.time() - started)
    with store.connect() as conn:
        symbols = [c["qualified_name"] for c in (job["state"].get("code") or []) if c["file_path"] in changed]
        apis = await impact(conn, symbols)
    diff = await repo.git("diff", "--stat", f"origin/{project.branch}...HEAD", cwd=root, check=False)
    body = "\n".join([
        "## 증상", str(result.get("symptom")), "",
        "## 원인", str(result.get("cause")), "",
        "## 수정 내용", fix["explanation"], "", "```", diff, "```", "",
        "## 검증",
        f"- 재현 테스트 `{fix['test_name']}`: 수정 전 실패, 수정 후 통과 (Jenkins `{jenkins.test_job()}` #{build_number})",
        f"- 시도 횟수: {max(h['attempt'] for h in history)}", "",
        "## 영향받는 API (콜 그래프 기준)", *([f"- `{a}`" for a in apis] or ["- 찾지 못함"]), "",
        "_프로젝트 봇이 만든 PR입니다. 내용을 확인한 뒤 merge해 주세요._",
    ])
    pr_url = ""
    if github.enabled():
        owner, name = repo.owner_repo(project.repo_url)
        await repo.push_to(project.repo_url, root, branch)
        pr_url = await github.create_pr(owner, name, branch, project.branch, fix["commit_message"].splitlines()[0][:100], body)
    with store.connect() as conn:
        jobs.update_job(conn, job_id, "fixed", fix={"branch": branch, "pr_url": pr_url, "history": history, "changed": changed,
                                                   "test": fix["test_name"], "usage": usage.as_dict(), "elapsed_seconds": elapsed, "pr_body": body})
        if pr_url and job["state"].get("incident_id"):
            jobs.set_incident_pr(conn, job["state"]["incident_id"], pr_url)
    attempts = max(h["attempt"] for h in history)
    await progress.done(f"*AI 수정 완료*  ·  {elapsed}초")
    text, blocks = slack_ui.fix_result_message(job_id, fix, {
        "elapsed": elapsed, "attempts": attempts, "before": before_number, "after": build_number,
        "changed": changed, "apis": apis, "pr_url": pr_url, "branch": branch,
    })
    await slack_client.post_message(job["slack_channel"], text, thread_ts=job["slack_ts"], blocks=blocks)
