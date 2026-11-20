"""프로젝트 설정(project.yml)과 환경 변수(.env)를 읽는다.

프로젝트마다 달라지는 값은 전부 여기를 거친다. 코드에 특정 프로젝트 이름이나 경로를 쓰지 않는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServiceConfig:
    name: str
    path: str  # 저장소 기준 폴더
    language: str
    source_dirs: list[str] = field(default_factory=lambda: ["."])
    base_packages: list[str] = field(default_factory=list)  # 스택 트레이스에서 우리 코드를 고르는 기준
    exclude: list[str] = field(default_factory=list)  # 제외할 경로 패턴 (fnmatch)
    aliases: dict[str, str] = field(default_factory=dict)  # import 별칭 (예: "@" → "src")
    test_command: str | None = None
    log_label: str | None = None  # Loki의 service 라벨 값 (없으면 name)


@dataclass
class ProjectConfig:
    project: str
    repo_url: str
    branch: str
    services: list[ServiceConfig]
    docs: list[str] = field(default_factory=list)

    def service(self, name: str) -> ServiceConfig:
        for s in self.services:
            if s.name == name:
                return s
        raise KeyError(f"설정에 없는 서비스: {name}")


def load_project(path: str | os.PathLike | None = None) -> ProjectConfig:
    path = Path(path or os.environ.get("PROJECT_CONFIG", "project.yml"))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    repo = raw.get("repo") or {}
    services = [ServiceConfig(**s) for s in raw.get("services", [])]
    if not services:
        raise ValueError(f"{path}: services가 비어 있음")
    return ProjectConfig(
        project=raw["project"],
        repo_url=repo.get("url", ""),
        branch=repo.get("branch", "main"),
        services=services,
        docs=raw.get("docs", []),
    )


@dataclass
class Settings:
    """실행 환경 값. 비밀값은 .env에만 두고 저장소에 올리지 않는다."""

    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "postgresql://bot:bot@localhost:5432/bot"))
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    loki_url: str = field(default_factory=lambda: os.environ.get("LOKI_URL", "http://localhost:3100"))
    prometheus_url: str = field(default_factory=lambda: os.environ.get("PROMETHEUS_URL", "http://localhost:9090"))
    repo_dir: str = field(default_factory=lambda: os.environ.get("REPO_DIR", "/data/repo"))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
