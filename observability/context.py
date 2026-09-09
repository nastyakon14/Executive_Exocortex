from contextvars import ContextVar, Token
from typing import Any

_project: ContextVar[str] = ContextVar("obs_project", default="unknown")
_source: ContextVar[str] = ContextVar("obs_source", default="unknown")


def _clean(value: str | None, fallback: str = "unknown") -> str:
    text = (value or "").strip() or fallback
    return text[:64]


def get_obs_context() -> dict[str, str]:
    return {"project": _project.get(), "source": _source.get()}


def set_obs_context(project: str | None = None, source: str | None = None) -> dict[str, Token[Any]]:
    tokens: dict[str, Token[Any]] = {}
    if project is not None:
        tokens["project"] = _project.set(_clean(project))
    if source is not None:
        tokens["source"] = _source.set(_clean(source))
    return tokens


def reset_obs_context(tokens: dict[str, Token[Any]]) -> None:
    if "project" in tokens:
        _project.reset(tokens["project"])
    if "source" in tokens:
        _source.reset(tokens["source"])
