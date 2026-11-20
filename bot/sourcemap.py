"""소스맵으로 압축된 JS의 위치를 원래 파일·줄로 바꾼다.

배포된 JS는 한 줄로 압축돼서 브라우저 에러 위치가 index-a1b2.js:1:48211 처럼 나온다.
빌드할 때 만든 .map 파일에는 "압축된 위치 → 원래 파일의 위치" 표가 Base64 VLQ로 들어 있다.
.map 은 사용자에게 내주지 않고 봇만 보관한다.
"""
from __future__ import annotations

import bisect
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}


def decode_vlq(segment: str) -> list[int]:
    """Base64 VLQ 한 토막을 숫자 목록으로 푼다. 한 글자가 6비트이고, 맨 위 비트가 '다음 글자로 이어짐', 맨 아래 비트가 부호다."""
    out, value, shift = [], 0, 0
    for ch in segment:
        digit = _B64[ch]
        value += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        out.append(-(value >> 1) if value & 1 else value >> 1)
        value, shift = 0, 0
    return out


@dataclass
class Position:
    source: str
    line: int  # 1부터
    column: int  # 0부터
    name: str | None = None


class SourceMap:
    def __init__(self, raw: dict):
        self.sources: list[str] = raw.get("sources", [])
        self.names: list[str] = raw.get("names", [])
        # 생성된 줄마다 (생성된 열, 원본 번호, 원본 줄, 원본 열, 이름 번호) 를 열 순서로 모은다
        self.lines: list[list[tuple]] = []
        src = line = col = name = 0
        for generated_line in raw.get("mappings", "").split(";"):
            entries = []
            gen_col = 0
            for segment in generated_line.split(","):
                if not segment:
                    continue
                f = decode_vlq(segment)
                gen_col += f[0]
                if len(f) >= 4:
                    src, line, col = src + f[1], line + f[2], col + f[3]
                    name_idx = None
                    if len(f) >= 5:
                        name += f[4]
                        name_idx = name
                    entries.append((gen_col, src, line, col, name_idx))
            self.lines.append(entries)

    def lookup(self, line: int, column: int) -> Position | None:
        """압축된 파일의 (줄, 열)에 해당하는 원래 위치. 줄은 1부터, 열은 브라우저가 찍는 값(1부터)을 그대로 받는다."""
        if line < 1 or line > len(self.lines):
            return None
        entries = self.lines[line - 1]
        if not entries:
            return None
        i = bisect.bisect_right([e[0] for e in entries], max(column - 1, 0)) - 1
        if i < 0:
            return None
        _, src, src_line, src_col, name_idx = entries[i]
        if src >= len(self.sources):
            return None
        return Position(self.sources[src], src_line + 1, src_col, self.names[name_idx] if name_idx is not None and name_idx < len(self.names) else None)


def maps_dir() -> Path:
    return Path(os.environ.get("SOURCEMAP_DIR", "/data/sourcemaps"))


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> SourceMap:
    return SourceMap(json.loads(Path(path).read_text(encoding="utf-8")))


def resolve(js_url: str, line: int, column: int) -> Position | None:
    """https://host/assets/index-a1b2.js 의 위치를 원래 소스 위치로 바꾼다. 맵이 없으면 None."""
    name = js_url.split("?")[0].rsplit("/", 1)[-1]
    path = maps_dir() / (name + ".map")
    if not path.exists():
        return None
    pos = _load(str(path), path.stat().st_mtime).lookup(line, column)
    if pos is None:
        return None
    # Vite는 sources 를 "../../src/pages/RoomPage.jsx" 처럼 상대 경로로 적는다
    source = pos.source
    while source.startswith(("../", "./")):
        source = source.split("/", 1)[1]
    return Position(source, pos.line, pos.column, pos.name)
