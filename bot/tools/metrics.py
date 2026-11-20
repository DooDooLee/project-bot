"""Prometheus 지표 조회. LLM은 지표 이름만 고르고, PromQL은 미리 만든 틀을 쓴다. 집계도 코드가 한다."""
from __future__ import annotations

import os
import time

import httpx

# 이름 → (설명, PromQL, 단위)
TEMPLATES: dict[str, tuple[str, str, str]] = {
    "cpu": ("서버 CPU 사용률", '100 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100', "%"),
    "cpu_steal": ("CPU steal (클라우드가 이 서버의 CPU를 제한한 비율)", 'avg(rate(node_cpu_seconds_total{mode="steal"}[2m])) * 100', "%"),
    "memory": ("서버 메모리 사용률", "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100", "%"),
    "disk": ("디스크 사용률 (/)", '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100', "%"),
    "http_requests": ("서비스별 초당 요청 수", "sum by (service) (rate(http_server_requests_seconds_count[2m]))", "req/s"),
    "http_errors": ("5xx 응답 수 (경로별, 2분 단위)", 'sum by (service, uri, status) (increase(http_server_requests_seconds_count{status=~"5.."}[2m]))', "건"),
    "http_latency": ("경로별 평균 응답 시간", "sum by (service, uri) (rate(http_server_requests_seconds_sum[2m])) / sum by (service, uri) (rate(http_server_requests_seconds_count[2m]))", "초"),
    "jvm_memory": ("JVM 힙 사용량", 'sum by (service) (jvm_memory_used_bytes{area="heap"}) / 1024 / 1024', "MB"),
    "container_memory": ("컨테이너별 메모리 사용량", 'sum by (name) (container_memory_usage_bytes{name!=""}) / 1024 / 1024', "MB"),
}
MAX_MINUTES = 24 * 60


def prometheus_url() -> str:
    return os.environ.get("PROMETHEUS_URL", "http://localhost:9090")


async def query_metric(metric: str, minutes: int = 10, end_ts: float | None = None) -> dict:
    if metric not in TEMPLATES:
        raise ValueError(f"지원하지 않는 지표: {metric} (가능: {', '.join(TEMPLATES)})")
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    title, promql, unit = TEMPLATES[metric]
    end = end_ts or time.time()
    start = end - minutes * 60
    step = max(15, minutes * 60 // 60)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{prometheus_url()}/api/v1/query_range", params={"query": promql, "start": start, "end": end, "step": step})
    r.raise_for_status()
    series = []
    for s in r.json().get("data", {}).get("result", []):
        values = [float(v) for _, v in s.get("values", []) if v not in ("NaN", "+Inf", "-Inf")]
        if not values:
            continue
        labels = {k: v for k, v in s.get("metric", {}).items() if k not in ("__name__", "instance", "job")}
        series.append({
            "labels": labels,
            "min": round(min(values), 2), "avg": round(sum(values) / len(values), 2),
            "max": round(max(values), 2), "last": round(values[-1], 2),
        })
    series.sort(key=lambda x: x["max"], reverse=True)
    return {"metric": metric, "title": title, "unit": unit, "minutes": minutes, "series": series[:15]}


def format_metric(result: dict) -> str:
    if not result["series"]:
        return f"{result['title']}: 최근 {result['minutes']}분 동안 값이 없음"
    lines = [f"{result['title']} (최근 {result['minutes']}분, 단위 {result['unit']})"]
    for s in result["series"]:
        label = ", ".join(f"{k}={v}" for k, v in s["labels"].items()) or "전체"
        lines.append(f"- {label}: 최소 {s['min']} / 평균 {s['avg']} / 최대 {s['max']} / 지금 {s['last']}")
    return "\n".join(lines)
