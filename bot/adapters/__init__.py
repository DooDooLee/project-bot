"""언어 처리기 등록소. project.yml의 language 값으로 처리기를 고른다."""
from __future__ import annotations

from .base import Chunk, Edge, FileAnalysis, Frame, LanguageAdapter, RawCall, Route
from .java import JavaAdapter
from .javascript import JavaScriptAdapter

_ADAPTERS: dict[str, type[LanguageAdapter]] = {
    "java": JavaAdapter,
    "javascript": JavaScriptAdapter,
}


def get_adapter(language: str) -> LanguageAdapter:
    try:
        return _ADAPTERS[language.lower()]()
    except KeyError:
        raise ValueError(f"지원하지 않는 언어: {language} (지원: {', '.join(_ADAPTERS)})") from None


__all__ = [
    "Chunk", "Edge", "FileAnalysis", "Frame", "LanguageAdapter", "RawCall", "Route",
    "JavaAdapter", "JavaScriptAdapter", "get_adapter",
]
