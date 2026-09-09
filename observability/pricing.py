"""USD за 1M токенов. Подправьте под фактический прайс LiteLLM/провайдера."""

# model -> (input_usd_per_1m, output_usd_per_1m)
MODEL_PRICES_USD_PER_1M: dict[str, tuple[float, float]] = {
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.5-pro": (1.25, 10.00),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4.1": (2.00, 8.00),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
}

DEFAULT_PRICE_USD_PER_1M = (0.50, 1.50)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = MODEL_PRICES_USD_PER_1M.get(model, DEFAULT_PRICE_USD_PER_1M)
    return (input_tokens * inp + output_tokens * out) / 1_000_000.0
