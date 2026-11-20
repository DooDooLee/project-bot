"""저장소 사본 관리. 원본은 Git이고, 봇은 읽기용 사본 하나와 수정 작업용 폴더를 따로 둔다."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from bot.config import ProjectConfig


def repo_dir() -> Path:
    return Path(os.environ.get("REPO_DIR", "/data/repo"))


def work_root() -> Path:
    return Path(os.environ.get("WORK_DIR", "/data/work"))


def fetch_url(project: ProjectConfig) -> str:
    """코드를 받아 오고 수정 브랜치를 올리는 저장소. 기본은 프로젝트의 GitHub 저장소이고,
    REPO_FETCH_URL 을 주면 그 주소(예: 사내 미러)를 쓴다."""
    return os.environ.get("REPO_FETCH_URL") or project.repo_url


def _auth_url(url: str) -> str:
    """push가 필요할 때만 토큰을 넣은 주소를 쓴다. 토큰은 환경 변수에서만 읽는다."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{token}@")
    return url


def _redact(text: str) -> str:
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", text)


async def git(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd) if cwd else None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(_redact(f"git {' '.join(args[:2])} 실패: {err.decode('utf-8', 'replace')[:400]}"))
    return out.decode("utf-8", "replace").strip()


async def sync(project: ProjectConfig) -> tuple[str, str]:
    """읽기용 사본을 배포 브랜치의 최신 커밋에 맞춘다. (이전 커밋, 지금 커밋)을 돌려준다."""
    path = repo_dir()
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        await git("clone", "--branch", project.branch, fetch_url(project), str(path))
        return "", await git("rev-parse", "HEAD", cwd=path)
    before = await git("rev-parse", "HEAD", cwd=path)
    await git("remote", "set-url", "origin", fetch_url(project), cwd=path)
    await git("fetch", "--prune", "origin", cwd=path)
    await git("checkout", "-q", project.branch, cwd=path)
    await git("reset", "-q", "--hard", f"origin/{project.branch}", cwd=path)
    return before, await git("rev-parse", "HEAD", cwd=path)


async def changed_files(before: str, after: str) -> list[str]:
    if not before or before == after:
        return []
    out = await git("diff", "--name-only", before, after, cwd=repo_dir())
    return [line for line in out.splitlines() if line]


async def new_worktree(project: ProjectConfig, branch: str) -> Path:
    """수정 작업용 폴더. 배포 브랜치의 최신 커밋에서 새 브랜치를 만든다. 운영 중인 사본은 건드리지 않는다."""
    await sync(project)
    path = work_root() / branch.replace("/", "-")
    if path.exists():
        await git("worktree", "remove", "--force", str(path), cwd=repo_dir(), check=False)
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    await git("worktree", "add", "-q", "-B", branch, str(path), f"origin/{project.branch}", cwd=repo_dir())
    await git("config", "user.name", os.environ.get("BOT_GIT_NAME", "project-bot"), cwd=path)
    await git("config", "user.email", os.environ.get("BOT_GIT_EMAIL", "project-bot@users.noreply.github.com"), cwd=path)
    return path


async def commit_all(path: Path, message: str) -> str:
    await git("add", "-A", cwd=path)
    await git("commit", "-q", "-m", message, cwd=path)
    return await git("rev-parse", "HEAD", cwd=path)


async def push(project: ProjectConfig, path: Path, branch: str) -> None:
    await push_to(fetch_url(project), path, branch)


async def push_to(url: str, path: Path, branch: str) -> None:
    await git("push", "-q", "--force", _auth_url(url), f"HEAD:refs/heads/{branch}", cwd=path)


async def remove_worktree(path: Path) -> None:
    await git("worktree", "remove", "--force", str(path), cwd=repo_dir(), check=False)


def owner_repo(url: str) -> tuple[str, str]:
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
    if not m:
        raise ValueError(f"GitHub 저장소 주소가 아님: {url}")
    return m.group(1), m.group(2)
