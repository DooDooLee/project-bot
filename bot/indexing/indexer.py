"""저장소의 코드를 읽어 청크와 호출 관계를 만든다. (DB 저장은 bot.db.store)"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from bot.adapters import Chunk, Edge, FileAnalysis, get_adapter
from bot.config import ProjectConfig, ServiceConfig

_SKIP_DIRS = {"node_modules", "build", "dist", "out", "target", ".gradle", ".git", ".idea", "__pycache__", "coverage"}


@dataclass
class ServiceIndex:
    service: str
    chunks: list[Chunk] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def iter_source_files(repo_root: Path, svc: ServiceConfig, suffixes: tuple[str, ...]):
    """서비스 폴더에서 분석할 파일을 저장소 기준 상대 경로(/ 구분)로 돌려준다."""
    for src_dir in svc.source_dirs:
        base = (repo_root / svc.path / src_dir).resolve()
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fname in sorted(filenames):
                if not fname.endswith(suffixes):
                    continue
                rel = Path(dirpath, fname).resolve().relative_to(repo_root.resolve()).as_posix()
                if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(fname, pat) for pat in svc.exclude):
                    continue
                yield rel


def analyze_files(repo_root: Path, svc: ServiceConfig, files: list[str]) -> list[FileAnalysis]:
    adapter = get_adapter(svc.language)
    out = []
    for rel in files:
        source = (repo_root / rel).read_bytes()
        out.append(adapter.analyze(source, rel))
    return out


def index_service(repo_root: str | Path, svc: ServiceConfig) -> ServiceIndex:
    repo_root = Path(repo_root)
    adapter = get_adapter(svc.language)
    files = list(iter_source_files(repo_root, svc, adapter.suffixes))
    analyses = analyze_files(repo_root, svc, files)
    if svc.language == "javascript":
        aliases = {k: (Path(svc.path) / v).as_posix() for k, v in svc.aliases.items()}
        edges = adapter.resolve(analyses, aliases)  # type: ignore[call-arg]
    else:
        edges = adapter.resolve(analyses)
    chunks = [c for a in analyses for c in a.chunks]
    raw_calls = sum(len(a.calls) for a in analyses)
    kinds: dict[str, int] = {}
    for e in edges:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    return ServiceIndex(
        service=svc.name,
        chunks=chunks,
        edges=edges,
        files=files,
        stats={
            "files": len(files),
            "chunks": len(chunks),
            "raw_calls": raw_calls,
            "edges": kinds,
            "routes_served": sum(len(a.routes_served) for a in analyses),
            "routes_called": sum(len(a.routes_called) for a in analyses),
        },
    )


def index_project(repo_root: str | Path, cfg: ProjectConfig) -> list[ServiceIndex]:
    return [index_service(repo_root, svc) for svc in cfg.services]


def link_report(indexes: list[ServiceIndex]) -> dict:
    """프론트가 호출하는 API 경로가 백엔드의 처리 메서드와 이어지는지 집계한다."""
    served: dict[str, list[tuple[str, str]]] = {}
    for idx in indexes:
        for e in idx.edges:
            if e.kind == "route":
                served.setdefault(e.caller, []).append((idx.service, e.callee))
    called = [(idx.service, e.caller, e.callee) for idx in indexes for e in idx.edges if e.kind == "http"]

    def match(key: str) -> list[tuple[str, str]]:
        if key in served:
            return served[key]
        method, _, path = key.partition(" ")
        if method == "ANY":
            return [x for k, v in served.items() if k.partition(" ")[2] == path for x in v]
        return served.get(f"ANY {path}", [])

    linked = [(c, match(c[2])) for c in called]
    return {
        "called": len(called),
        "linked": sum(1 for _, m in linked if m),
        "unlinked": sorted({c[2] for c, m in linked if not m}),
    }
