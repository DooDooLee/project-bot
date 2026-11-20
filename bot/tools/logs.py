"""Loki 로그 조회. LLM이 LogQL을 직접 쓰지 않고, 정해진 틀에 값만 채운다."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx


@dataclass
class LogEntry:
    ts_ns: int
    line: str
    service: str
    level: str


def loki_url() -> str:
    return os.environ.get("LOKI_URL", "http://localhost:3100")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(services: list[str] | None = None, level: str | None = None, contains: str | None = None) -> str:
    """{service=~"backend|ai-chat", level="ERROR"} |= "글자" 모양의 조회식을 만든다."""
    selectors = []
    if services:
        safe = [re.sub(r"[^\w.-]", "", s) for s in services]
        selectors.append(f'service=~"{"|".join(safe)}"')
    else:
        selectors.append('service=~".+"')
    if level:
        selectors.append(f'level="{re.sub(r"[^A-Z]", "", level.upper())}"')
    query = "{" + ", ".join(selectors) + "}"
    if contains:
        query += " |= " + _quote(contains)
    return query


async def query_range(query: str, start_ns: int, end_ns: int, limit: int = 100, direction: str = "backward") -> list[LogEntry]:
    params = {"query": query, "start": str(start_ns), "end": str(end_ns), "limit": str(limit), "direction": direction}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{loki_url()}/loki/api/v1/query_range", params=params)
    r.raise_for_status()
    entries: list[LogEntry] = []
    for stream in r.json().get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            entries.append(LogEntry(int(ts), line, labels.get("service", ""), labels.get("level", "")))
    entries.sort(key=lambda e: e.ts_ns, reverse=(direction == "backward"))
    return entries


async def query_logs(
    services: list[str] | None,
    start_ns: int,
    end_ns: int,
    level: str | None = None,
    contains: str | None = None,
    limit: int = 100,
) -> list[LogEntry]:
    return await query_range(build_query(services, level, contains), start_ns, end_ns, limit)


def trim(line: str, max_lines: int = 60, max_chars: int = 6000) -> str:
    """스택 트레이스가 긴 로그는 앞부분과 Caused by 주변만 남긴다 (LLM 입력을 줄이기 위해)."""
    lines = line.splitlines()
    if len(lines) > max_lines:
        keep = set(range(0, 25))
        for i, text in enumerate(lines):
            if text.lstrip().startswith("Caused by"):
                keep.update(range(i, min(i + 12, len(lines))))
        out, prev = [], -1
        for i in sorted(keep):
            if i >= len(lines):
                break
            if prev != -1 and i != prev + 1:
                out.append(f"    ... ({i - prev - 1}줄 생략)")
            out.append(lines[i])
            prev = i
        if prev < len(lines) - 1:
            out.append(f"    ... ({len(lines) - 1 - prev}줄 생략)")
        lines = out
    return "\n".join(lines)[:max_chars]
