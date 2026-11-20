"""Jenkins 연동. 빌드 로그 읽기와, 봇이 만든 수정 브랜치의 테스트 실행.

빌드·테스트는 봇이 직접 돌리지 않고 Jenkins에 맡긴다. AI는 코드와 테스트를 쓰기만 하고, 통과 여부는 Jenkins 결과로 판단한다.
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx

_SIGNAL = re.compile(r"ERROR|FAILED|FAILURE|BUILD FAILED|Exception|error:|✗|AssertionError|Tests run:.*Failures: [1-9]| FAILED$")


def _cfg() -> tuple[str, tuple[str, str] | None]:
    url = os.environ.get("JENKINS_URL", "").rstrip("/")
    user, token = os.environ.get("JENKINS_USER", ""), os.environ.get("JENKINS_TOKEN", "")
    return url, ((user, token) if user and token else None)


def configured() -> bool:
    return bool(_cfg()[0])


def deploy_job() -> str:
    return os.environ.get("JENKINS_DEPLOY_JOB", "deploy")


def test_job() -> str:
    return os.environ.get("JENKINS_TEST_JOB", "test-branch")


def trim_build_log(text: str, tail: int = 60, context: int = 3, max_chars: int = 9000) -> str:
    """빌드 로그는 수천 줄이 될 수 있다. 끝부분과 실패 신호 주변 줄만 남긴다."""
    lines = text.splitlines()
    keep: set[int] = set(range(max(0, len(lines) - tail), len(lines)))
    for i, line in enumerate(lines):
        if _SIGNAL.search(line):
            keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    out, prev = [], -1
    for i in sorted(keep):
        if prev != -1 and i != prev + 1:
            out.append(f"... ({i - prev - 1}줄 생략)")
        out.append(lines[i])
        prev = i
    result = "\n".join(out)
    return result[-max_chars:] if len(result) > max_chars else result


async def console_text(job: str | None = None, build_number: int | None = None) -> str:
    url, auth = _cfg()
    if not url:
        raise RuntimeError("Jenkins가 연결되어 있지 않음 (JENKINS_URL)")
    target = f"{url}/job/{job or deploy_job()}/{build_number or 'lastBuild'}/consoleText"
    async with httpx.AsyncClient(timeout=30, auth=auth) as client:
        r = await client.get(target)
    r.raise_for_status()
    return r.text


async def build_log_text(job: str | None = None, build_number: int | None = None) -> str:
    try:
        return trim_build_log(await console_text(job, build_number))
    except Exception as e:
        return f"빌드 로그를 가져오지 못함: {e}"


async def run_and_wait(job: str, params: dict, timeout: int = 60 * 25, poll: float = 5.0) -> dict:
    """작업을 실행하고 끝날 때까지 기다린다. {'result': 'SUCCESS'|'FAILURE'|..., 'number': n, 'url': ..., 'log': ...}"""
    url, auth = _cfg()
    if not url:
        raise RuntimeError("Jenkins가 연결되어 있지 않음 (JENKINS_URL)")
    async with httpx.AsyncClient(timeout=30, auth=auth) as client:
        # 비밀번호로 인증할 때는 CSRF 토큰(crumb)을 먼저 받아 함께 보내야 한다
        headers = {}
        crumb = await client.get(f"{url}/crumbIssuer/api/json")
        if crumb.status_code == 200:
            headers[crumb.json()["crumbRequestField"]] = crumb.json()["crumb"]
        r = await client.post(f"{url}/job/{job}/buildWithParameters", params=params, headers=headers)
        r.raise_for_status()
        queue_url = r.headers.get("location", "").rstrip("/")
        # 내부 주소로 바꿔서 대기열 항목을 따라간다
        queue_path = re.sub(r"^https?://[^/]+(/jenkins)?", "", queue_url)
        base = re.sub(r"/jenkins$", "", url)
        prefix = "/jenkins" if url.endswith("/jenkins") else ""
        number = None
        waited = 0.0
        while waited < timeout and number is None:
            q = await client.get(f"{base}{prefix}{queue_path}/api/json")
            if q.status_code == 200:
                number = (q.json().get("executable") or {}).get("number")
            if number is None:
                await asyncio.sleep(2)
                waited += 2
        if number is None:
            raise TimeoutError("Jenkins 대기열에서 빌드가 시작되지 않음")
        while waited < timeout:
            b = await client.get(f"{url}/job/{job}/{number}/api/json", params={"tree": "result,building,url"})
            data = b.json() if b.status_code == 200 else {}
            if data and not data.get("building") and data.get("result"):
                log_text = (await client.get(f"{url}/job/{job}/{number}/consoleText")).text
                return {"result": data["result"], "number": number, "url": data.get("url"), "log": trim_build_log(log_text)}
            await asyncio.sleep(poll)
            waited += poll
    raise TimeoutError(f"Jenkins 빌드 #{number} 가 {timeout}초 안에 끝나지 않음")
