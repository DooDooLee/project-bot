"""언어 처리기 인터페이스.

프로젝트마다 언어가 달라도 봇의 나머지 코드는 이 인터페이스만 본다.
새 언어를 지원하려면 LanguageAdapter 구현체를 하나 추가하면 된다.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Chunk:
    """검색 단위. 메서드·함수·컴포넌트 하나, 또는 클래스 요약 하나."""

    qualified_name: str  # 서비스 안에서 유일한 이름
    name: str  # 사람이 읽는 짧은 이름 (예: RoomService#findRoom(Long))
    kind: str  # method | constructor | class | function | component
    file_path: str  # 저장소 기준 상대 경로
    start_line: int
    end_line: int
    signature: str
    content: str

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class RawCall:
    """아직 대상이 확정되지 않은 호출. resolve 단계에서 실제 청크로 연결한다."""

    caller: str  # 호출하는 청크의 qualified_name
    name: str  # 호출된 메서드·함수 이름
    receiver: str | None = None  # 호출 대상의 타입 또는 모듈 (모르면 None)
    argc: int | None = None
    kind: str = "call"  # call | render
    line: int = 0


@dataclass
class Route:
    """API 경로. 백엔드는 handler가 그 경로를 처리하고, 프론트는 caller가 그 경로를 호출한다."""

    method: str  # GET, POST, ... (모르면 ANY)
    path: str  # 정규화된 경로 (/api/v1/rooms/{})
    symbol: str  # 청크의 qualified_name
    raw_path: str = ""  # 코드에 적힌 원래 모양
    line: int = 0

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class Frame:
    """스택 트레이스 한 줄."""

    symbol: str  # 클래스.메서드 또는 함수 이름
    file: str
    line: int
    column: int | None = None
    class_name: str | None = None  # 패키지를 포함한 클래스 이름 (Java)
    method: str | None = None


@dataclass
class FileAnalysis:
    file_path: str
    chunks: list[Chunk] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    routes_served: list[Route] = field(default_factory=list)
    routes_called: list[Route] = field(default_factory=list)
    # resolve 단계에서 쓰는 언어별 부가 정보 (import, 상속 관계 등)
    meta: dict = field(default_factory=dict)


@dataclass
class Edge:
    caller: str
    callee: str
    kind: str  # call | render | impl | route | http
    file_path: str = ""


class LanguageAdapter(ABC):
    language: str = ""
    suffixes: tuple[str, ...] = ()

    @abstractmethod
    def extract_chunks(self, source: bytes, file_path: str) -> list[Chunk]:
        """코드를 메서드·함수 단위로 자른다."""

    @abstractmethod
    def extract_calls(self, source: bytes, file_path: str) -> list[RawCall]:
        """청크 안에서 일어나는 호출을 뽑는다."""

    @abstractmethod
    def extract_routes(self, source: bytes, file_path: str) -> tuple[list[Route], list[Route]]:
        """(이 파일이 처리하는 API 경로, 이 파일이 호출하는 API 경로)."""

    @abstractmethod
    def parse_stack_trace(self, text: str, base_packages: list[str] | None = None) -> list[Frame]:
        """스택 트레이스 글에서 우리 코드에 해당하는 줄을 뽑는다."""

    @abstractmethod
    def analyze(self, source: bytes, file_path: str) -> FileAnalysis:
        """위 세 가지 추출을 한 번의 파싱으로 수행한다."""

    @abstractmethod
    def resolve(self, analyses: list[FileAnalysis]) -> list[Edge]:
        """서비스 전체 파일을 보고 호출을 실제 청크에 연결한다."""


_VAR = re.compile(r"\{[^{}]*\}")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+")


def normalize_path(path: str) -> str:
    """API 경로를 서로 비교할 수 있는 모양으로 통일한다.

    - 경로 변수는 이름과 상관없이 {} 로 바꾼다 ({id}, {roomId:\\d+} → {})
    - 주소 앞의 호스트, 쿼리 문자열, 끝의 / 를 뗀다
    """
    p = path.strip()
    p = _SCHEME.sub("", p)
    p = p.split("?", 1)[0].split("#", 1)[0]
    p = _VAR.sub("{}", p)
    # 맨 앞에 붙은 변수(기본 주소 자리)는 경로가 아니다
    while p.startswith("{}"):
        p = p[2:]
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def join_paths(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    if not path:
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")
