from pathlib import Path
from typing import Literal
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.prompts import (
    graphrag_actions_empty_response,
    graphrag_actions_query_template,
    graphrag_entity_empty_response_template,
    graphrag_entity_query_template,
    graphrag_no_context_response,
    graphrag_risks_empty_response,
    graphrag_risks_query_template,
    graphrag_system_prompt,
    graphrag_user_prompt_template,
    linker_system_prompt,
    linker_user_prompt_template,
    zettel_atomizer_system_prompt,
    zettel_atomizer_user_prompt_template,
)


class Settings(BaseSettings):
    """
    Центральная конфигурация приложения.

    Промпты и LLM-параметры вынесены сюда для A/B-тестов и мониторинга (Langfuse).
    Значения можно переопределять через .env — имена переменных в UPPER_SNAKE_CASE.
    """

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # embedding
    embedding_model_name: str = "intfloat/multilingual-e5-base"

    # neo4j — основное хранилище графа
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = os.getenv('NEO4J_PASSWORD')  # задайте в .env: NEO4J_PASSWORD
    neo4j_database: str = "neo4j"

    # atomizer llm
    zettel_atomizer_model_name: str = Field(
        # default="openai/gpt-4o",
        default="google/gemini-2.5-flash",
        description="LLM для разбиения заметок на атомарные мысли",
    )
    zettel_atomizer_temperature: float = Field(
        default=0.0,
        description="Temperature для atomizer",
    )
    zettel_atomizer_system_prompt: str = Field(
        default=zettel_atomizer_system_prompt,
        description="System prompt для atomizer",
    )
    zettel_atomizer_user_prompt_template: str = Field(
        default=zettel_atomizer_user_prompt_template,
        description="User prompt template для atomizer ({text})",
    )

    # linker llm
    linker_model_name: str = Field(
        # default="openai/gpt-4o",
        default="google/gemini-2.5-flash",
        description="LLM для решения о встраивании мысли в граф",
    )
    linker_temperature: float = Field(
        default=0.0,
        description="Temperature для linker",
    )
    linker_system_prompt: str = Field(
        default=linker_system_prompt,
        description="System prompt для linker",
    )
    linker_user_prompt_template: str = Field(
        default=linker_user_prompt_template,
        description="User prompt template для linker",
    )
    linker_similarity_threshold: float = 0.5
    linker_max_candidates: int = 5

    # graphrag llm
    graphrag_model_name: str = Field(
        default="google/gemini-2.5-flash",
        # default="openai/gpt-4o",
        # default="anthropic/claude-haiku-4.5",
        description="LLM для генерации ответов GraphRAG",
    )
    graphrag_temperature: float = Field(
        default=0.3,
        description="Temperature для GraphRAG generator",
    )
    graphrag_system_prompt: str = Field(
        default=graphrag_system_prompt,
        description="System prompt для GraphRAG generator",
    )
    graphrag_user_prompt_template: str = Field(
        default=graphrag_user_prompt_template,
        description="User prompt template для GraphRAG ({context}, {query})",
    )
    graphrag_no_context_response: str = Field(
        default=graphrag_no_context_response,
        description="Ответ, когда контекст для GraphRAG пуст",
    )
    graphrag_entity_query_template: str = Field(
        default=graphrag_entity_query_template,
        description="Шаблон user query для поиска по сущности ({entity_name})",
    )
    graphrag_actions_query_template: str = Field(
        default=graphrag_actions_query_template,
        description="User query для списка задач",
    )
    graphrag_risks_query_template: str = Field(
        default=graphrag_risks_query_template,
        description="User query для списка рисков",
    )
    graphrag_entity_empty_response_template: str = Field(
        default=graphrag_entity_empty_response_template,
        description="Ответ, когда сущность не найдена ({entity_name})",
    )
    graphrag_actions_empty_response: str = Field(
        default=graphrag_actions_empty_response,
        description="Ответ, когда задач нет",
    )
    graphrag_risks_empty_response: str = Field(
        default=graphrag_risks_empty_response,
        description="Ответ, когда рисков нет",
    )

    # graphrag retrieval
    graphrag_search_limit: int = 5
    graphrag_context_hops: int = 1
    graphrag_similarity_threshold: float = 0.3

    # chromadb (legacy)
    chroma_mode: Literal["http", "persistent", "memory"] = "http"
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    chroma_persist_dir: str = "./storage/chromadb/data"
    chroma_collection_name: str = "zettelkasten_nodes"

    @property
    def zettel_atomizer_prompt(self) -> str:
        """Алиас для обратной совместимости."""
        return self.zettel_atomizer_system_prompt


settings = Settings()
