import time
from typing import Any, Sequence

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from observability.metrics import record_llm_call


def _usage_from_response(response: Any) -> tuple[int, int, float | None]:
    input_tokens = 0
    output_tokens = 0
    cost = None

    meta = getattr(response, "usage_metadata", None) or {}
    if isinstance(meta, dict):
        input_tokens = int(meta.get("input_tokens") or 0)
        output_tokens = int(meta.get("output_tokens") or 0)

    resp_meta = getattr(response, "response_metadata", None) or {}
    if isinstance(resp_meta, dict):
        usage = resp_meta.get("token_usage") or resp_meta.get("usage") or {}
        if isinstance(usage, dict):
            input_tokens = input_tokens or int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = output_tokens or int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            if usage.get("total_cost") is not None:
                cost = float(usage["total_cost"])
        if resp_meta.get("cost") is not None and cost is None:
            cost = float(resp_meta["cost"])
    return input_tokens, output_tokens, cost


class LLMMetricsCallback(BaseCallbackHandler):
    """Снимает latency и usage с обычных invoke-вызовов (atomizer/linker)."""

    def __init__(self, component: str, model: str):
        self.component = component
        self.model = model
        self._t0 = 0.0
        self._ttft: float | None = None

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._t0 = time.perf_counter()
        self._ttft = None

    def on_llm_new_token(self, token: str, **kwargs):
        if self._ttft is None and self._t0:
            self._ttft = time.perf_counter() - self._t0

    def on_llm_end(self, response, **kwargs):
        duration = time.perf_counter() - self._t0 if self._t0 else 0.0
        input_tokens = 0
        output_tokens = 0
        cost = None
        llm_output = getattr(response, "llm_output", None) or {}
        if isinstance(llm_output, dict):
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            if isinstance(usage, dict):
                input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                if usage.get("total_cost") is not None:
                    cost = float(usage["total_cost"])
        gens = getattr(response, "generations", None) or []
        if gens and gens[0]:
            msg = getattr(gens[0][0], "message", None)
            if msg is not None:
                i2, o2, c2 = _usage_from_response(msg)
                input_tokens = input_tokens or i2
                output_tokens = output_tokens or o2
                cost = cost if cost is not None else c2
        record_llm_call(
            component=self.component,
            model=self.model,
            duration_s=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ttft_s=self._ttft,
            cost_usd=cost,
            status="ok",
        )

    def on_llm_error(self, error, **kwargs):
        duration = time.perf_counter() - self._t0 if self._t0 else 0.0
        record_llm_call(
            component=self.component,
            model=self.model,
            duration_s=duration,
            status="error",
        )


def make_chat_openai(
    *,
    component: str,
    model_name: str,
    temperature: float,
    streaming: bool = False,
    instrument: bool = True,
) -> ChatOpenAI:
    import os

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": os.getenv("LLM_API_KEY"),
        "base_url": os.getenv("LLM_BASE_URL"),
        "temperature": temperature,
    }
    if instrument:
        kwargs["callbacks"] = [LLMMetricsCallback(component, model_name)]
    if streaming:
        kwargs["streaming"] = True
        try:
            return ChatOpenAI(**kwargs, stream_usage=True)
        except TypeError:
            return ChatOpenAI(**kwargs)
    return ChatOpenAI(**kwargs)


def invoke_chat_stream(
    llm: ChatOpenAI,
    messages: Sequence[BaseMessage],
    *,
    component: str,
    model_name: str,
) -> str:
    """Стрим с TTFT/prefill/decode. Callback на модели лучше отключить, чтобы не двойнить метрики."""
    t0 = time.perf_counter()
    ttft = None
    parts: list[str] = []
    last = None
    status = "ok"
    try:
        for chunk in llm.stream(messages):
            if ttft is None:
                ttft = time.perf_counter() - t0
            last = chunk
            parts.append(getattr(chunk, "content", None) or "")
    except Exception:
        status = "error"
        record_llm_call(
            component=component,
            model=model_name,
            duration_s=time.perf_counter() - t0,
            ttft_s=ttft,
            status=status,
        )
        raise
    duration = time.perf_counter() - t0
    input_tokens, output_tokens, cost = _usage_from_response(last) if last is not None else (0, 0, None)
    record_llm_call(
        component=component,
        model=model_name,
        duration_s=duration,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        ttft_s=ttft,
        cost_usd=cost,
        status=status,
    )
    return "".join(parts)
