"""Claude 호출. 단순한 일은 Haiku, 원인 분석과 코드 수정은 Sonnet이 맡는다."""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic, BadRequestError

log = logging.getLogger("bot.llm")

HAIKU = os.environ.get("MODEL_FAST", "claude-haiku-5-5")
SONNET = os.environ.get("MODEL_SMART", "claude-sonnet-5-5")

_client: AsyncAnthropic | None = None
_NO_FORCED_TOOL: set[str] = set()  # 도구 강제 지정을 지원하지 않는 모델


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic()  # ANTHROPIC_API_KEY 를 환경 변수에서 읽는다
    return _client


@dataclass
class Usage:
    """작업 한 건이 쓴 토큰. 프롬프트 캐싱 효과를 재는 데 쓴다."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, usage: Any) -> None:
        self.calls += 1
        m = self.by_model.setdefault(model, {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        m["calls"] += 1
        for attr, key in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_read_input_tokens", "cache_read"),
            ("cache_creation_input_tokens", "cache_write"),
        ):
            value = getattr(usage, attr, 0) or 0
            setattr(self, attr, getattr(self, attr) + value)
            m[key] += value

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "by_model": self.by_model,
        }


def _with_cache_mark(messages: list[dict]) -> list[dict]:
    """마지막 메시지에 cache_control 을 붙인다.

    여기까지의 내용이 캐시에 올라가고, 다음 호출에서 같은 앞부분은 싸게 읽힌다.
    호출을 여러 번 이어 가는 흐름(도구 반복, 수정 재시도)에서만 쓴다.
    """
    if not messages:
        return messages
    out = messages[:-1] + [copy.copy(messages[-1])]
    content = out[-1]["content"]
    if isinstance(content, str):
        out[-1]["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        return out
    blocks = list(content)
    last = blocks[-1]
    last = dict(last) if isinstance(last, dict) else last.model_dump(exclude_none=True)
    last["cache_control"] = {"type": "ephemeral"}
    blocks[-1] = last
    out[-1]["content"] = blocks
    return out


async def call(
    model: str,
    system: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    max_tokens: int = 2000,
    usage: Usage | None = None,
    cache: bool = False,
):
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": _with_cache_mark(messages) if cache else messages,
    }
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    resp = await client().messages.create(**kwargs)
    if usage is not None:
        usage.add(model, resp.usage)
    return resp


def text_of(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def structured(
    model: str,
    system: str,
    user: str | list,
    *,
    name: str,
    description: str,
    schema: dict,
    max_tokens: int = 2000,
    usage: Usage | None = None,
) -> dict:
    """정해진 칸을 채운 JSON으로 답을 받는다.

    도구 하나를 정의하고 그 도구를 쓰게 한다. 도구 강제 지정(tool_choice)을 지원하지 않는 모델에는
    자동 선택으로 보내고, 도구를 부르지 않으면 한 번 더 요청한다.
    """
    tool = {"name": name, "description": description, "input_schema": schema}
    messages: list[dict] = [{"role": "user", "content": user}]
    nudge = f" 답은 반드시 {name} 도구를 호출해서 제출한다. 글로만 답하지 않는다."
    for _ in range(3):
        forced = model not in _NO_FORCED_TOOL
        try:
            resp = await call(
                model,
                system if forced else system + nudge,
                messages,
                tools=[tool],
                tool_choice={"type": "tool", "name": name} if forced else {"type": "auto"},
                max_tokens=max_tokens,
                usage=usage,
            )
        except BadRequestError as e:
            if forced and "tool_choice" in str(e):
                _NO_FORCED_TOOL.add(model)
                continue
            raise
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        messages = messages + [{"role": "assistant", "content": resp.content}, {"role": "user", "content": f"{name} 도구를 호출해서 답을 제출해 주세요."}]
    raise RuntimeError(f"{name}: 구조화된 응답이 없음")


ToolHandler = Callable[[dict], Awaitable[str]]


async def tool_loop(
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
    handlers: dict[str, ToolHandler],
    *,
    max_turns: int = 8,
    max_tokens: int = 2000,
    usage: Usage | None = None,
    on_tool: Callable[[str, dict], Awaitable[None]] | None = None,
) -> str:
    """Claude가 도구를 골라 쓰게 하고, 도구 실행은 우리 코드가 한다. 더 부를 도구가 없으면 답을 돌려준다."""
    messages = list(messages)
    for _ in range(max_turns):
        resp = await call(model, system, messages, tools=tools, max_tokens=max_tokens, usage=usage, cache=True)
        if resp.stop_reason != "tool_use":
            return text_of(resp)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if on_tool is not None:
                await on_tool(block.name, dict(block.input))
            handler = handlers.get(block.name)
            try:
                output = await handler(dict(block.input)) if handler else f"알 수 없는 도구: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output[:12000]})
            except Exception as e:
                log.exception("tool %s failed", block.name)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"도구 실행 실패: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})
    # 횟수를 다 쓰면 도구 없이 지금까지 모은 것으로 답하게 한다
    messages.append({"role": "user", "content": "도구 호출 횟수를 다 썼습니다. 지금까지 확인한 내용만으로 답해 주세요."})
    resp = await call(model, system, messages, max_tokens=max_tokens, usage=usage)
    return text_of(resp)
