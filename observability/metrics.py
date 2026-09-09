from prometheus_client import Counter, Histogram

from observability.context import get_obs_context
from observability.pricing import estimate_cost_usd

_LABELS = ("component", "model", "project", "source")

llm_requests_total = Counter(
    "exocortex_llm_requests_total",
    "LLM-запросы",
    _LABELS + ("status",),
)
llm_tokens_total = Counter(
    "exocortex_llm_tokens_total",
    "Токены LLM",
    _LABELS + ("direction",),
)
llm_cost_usd_total = Counter(
    "exocortex_llm_cost_usd_total",
    "Оценка стоимости LLM в USD",
    _LABELS,
)
llm_latency_seconds = Histogram(
    "exocortex_llm_latency_seconds",
    "Полное время LLM-вызова",
    _LABELS,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 60),
)
llm_ttft_seconds = Histogram(
    "exocortex_llm_ttft_seconds",
    "Time to first token",
    _LABELS,
    buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 6, 12, 24),
)
llm_prefill_seconds = Histogram(
    "exocortex_llm_prefill_seconds",
    "Prefill (приблизительно TTFT)",
    _LABELS,
    buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 6, 12, 24),
)
llm_decode_seconds = Histogram(
    "exocortex_llm_decode_seconds",
    "Decode: от первого токена до конца",
    _LABELS,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32),
)
llm_decode_tokens_per_second = Histogram(
    "exocortex_llm_decode_tokens_per_second",
    "Скорость decode, токенов/с",
    _LABELS,
    buckets=(5, 10, 20, 40, 80, 120, 200, 400, 800),
)

app_events_total = Counter(
    "exocortex_app_events_total",
    "Продуктовые события",
    ("event", "project", "source", "status"),
)


def _labels(component: str, model: str) -> dict[str, str]:
    ctx = get_obs_context()
    return {
        "component": (component or "unknown")[:48],
        "model": (model or "unknown")[:80],
        "project": ctx["project"],
        "source": ctx["source"],
    }


def record_llm_call(
    *,
    component: str,
    model: str,
    duration_s: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    ttft_s: float | None = None,
    cost_usd: float | None = None,
    status: str = "ok",
) -> None:
    labels = _labels(component, model)
    llm_requests_total.labels(**labels, status=status or "ok").inc()
    llm_latency_seconds.labels(**labels).observe(max(duration_s, 0.0))

    if input_tokens:
        llm_tokens_total.labels(**labels, direction="input").inc(input_tokens)
    if output_tokens:
        llm_tokens_total.labels(**labels, direction="output").inc(output_tokens)

    if cost_usd is None:
        cost_usd = estimate_cost_usd(model, input_tokens, output_tokens)
    if cost_usd:
        llm_cost_usd_total.labels(**labels).inc(cost_usd)

    if ttft_s is not None and ttft_s >= 0:
        llm_ttft_seconds.labels(**labels).observe(ttft_s)
        llm_prefill_seconds.labels(**labels).observe(ttft_s)
        decode_s = max(duration_s - ttft_s, 0.0)
        llm_decode_seconds.labels(**labels).observe(decode_s)
        if decode_s > 0 and output_tokens > 0:
            llm_decode_tokens_per_second.labels(**labels).observe(output_tokens / decode_s)


def record_app_event(event: str, status: str = "ok") -> None:
    ctx = get_obs_context()
    app_events_total.labels(
        event=(event or "unknown")[:48],
        project=ctx["project"],
        source=ctx["source"],
        status=status or "ok",
    ).inc()
