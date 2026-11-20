"""GitHub PR 생성. 봇이 GitHub에 하는 쓰기 작업은 수정 브랜치 push와 PR 생성뿐이다. merge는 사람이 한다."""
from __future__ import annotations

import os

import httpx

API = "https://api.github.com"


def enabled() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN"))


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def create_pr(owner: str, repo: str, head: str, base: str, title: str, body: str) -> str:
    """PR을 만들고 주소를 돌려준다. 같은 브랜치의 PR이 이미 열려 있으면 그 주소를 돌려준다."""
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        r = await client.post(f"{API}/repos/{owner}/{repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})
        if r.status_code == 201:
            return r.json()["html_url"]
        if r.status_code == 422:
            existing = await client.get(f"{API}/repos/{owner}/{repo}/pulls", params={"head": f"{owner}:{head}", "state": "open"})
            if existing.status_code == 200 and existing.json():
                return existing.json()[0]["html_url"]
        raise RuntimeError(f"PR 생성 실패 ({r.status_code}): {r.text[:300]}")
